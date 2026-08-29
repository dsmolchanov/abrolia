"""The control-plane half of Art. 7(3) withdrawal.

Before this existed nothing ever set `consent_receipts.revoked_at`: retention
deleted already-revoked rows and export read them, but no code withdrew
anything. The Art 9(2)(a) copy nonetheless promises the family withdrawal "at
any time in one step".

Withdrawal has three effects and needs all three. Marking the receipt closes
every future boundary; revoking the standing revisions makes the durable state
agree; and only the push reaches an instance that is already serving.
"""

from __future__ import annotations

import json

import pytest

from control_plane.models import StepKind
from control_plane.privacy.consent import (
    CONSENT_TEXTS,
    CONTENT_RESOURCE_TYPES,
    CONTENT_RESTRICTION_PURPOSE,
    HOUSEHOLD_CONTENT_PURPOSE,
    WITHDRAWAL_SCOPES,
)
from control_plane.privacy.withdraw import (
    REVOKE_CONSENT_OPERATION,
    ConsentNotHeld,
    ConsentWithdrawalService,
)
from control_plane.provisioning.contracts import (
    InspectResult,
    InspectState,
    ProviderRegistry,
    ProviderWaiting,
)
from control_plane.provisioning.fakes import (
    DeterministicFakeProvisioner,
    synthetic_provider_registry,
)
from control_plane.provisioning.planner import DesiredSpecPlanner
from control_plane.provisioning.worker import COMPENSATED_STEP_KINDS

from .test_art9_household_consent import (
    WHATSAPP_SELECTION,
    complete_onboarding,
    real_email_selection,
)

BASE_TIME = 1_800_000_000.0
# Must satisfy control_plane.privacy.runtime.RUNTIME_REF; a ref that does
# not is rejected before the push, which would make these tests vacuous.
RUNTIME_REF = "abrolia-hh-" + "abcdefghijklmnopqrstuvwxyz"[:26]


class FakeRuntime:
    """Stands in for the private-network POST to a live runtime."""

    def __init__(self, *, status_code: int = 200, error: Exception | None = None):
        self.status_code = status_code
        self.error = error
        self.calls: list[dict[str, object]] = []

    def post(self, url, *, headers=None, timeout=None, content=None):
        # `content` mirrors httpx: the stop now carries the withdrawn receipt
        # ids so the runtime can tell it from one left over by an earlier
        # consent cycle at the same stable reference.
        self.calls.append({
            "url": url,
            "headers": dict(headers or {}),
            "body": json.loads(content.decode("utf-8")) if content else None,
        })
        if self.error is not None:
            raise self.error
        return _Response(self.status_code)


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def withdrawal(cp_stack) -> ConsentWithdrawalService:
    return ConsentWithdrawalService(cp_stack.database, jobs=cp_stack.jobs)


def set_runtime_ref(cp_stack, runtime_ref: str = RUNTIME_REF) -> None:
    with cp_stack.database.write() as connection:
        connection.execute(
            "UPDATE households SET runtime_ref = ? WHERE id = ?",
            (runtime_ref, cp_stack.household.id),
        )


def drain(cp_stack, *, now: float = BASE_TIME + 50) -> None:
    """Run the queue empty, so a later `run_once` reaches the job under test."""
    for _ in range(10):
        result = cp_stack.make_worker(now=now).run_once()
        if result is None:
            return
    raise AssertionError("provisioning queue did not drain")


def receipt_row(cp_stack, purpose: str):
    return cp_stack.database.query_one(
        "SELECT revoked_at FROM consent_receipts WHERE household_id = ? AND purpose = ?",
        (cp_stack.household.id, purpose),
    )


def test_withdrawal_marks_the_receipt(cp_stack) -> None:
    complete_onboarding(cp_stack)
    assert receipt_row(cp_stack, HOUSEHOLD_CONTENT_PURPOSE)["revoked_at"] is None

    result = withdrawal(cp_stack).withdraw(
        cp_stack.household.id, HOUSEHOLD_CONTENT_PURPOSE, now=BASE_TIME
    )

    assert result.receipts_revoked == 1
    assert receipt_row(cp_stack, HOUSEHOLD_CONTENT_PURPOSE)["revoked_at"] == BASE_TIME
    # Withdrawing one consent must not touch the other.
    assert receipt_row(cp_stack, CONTENT_RESTRICTION_PURPOSE)["revoked_at"] is None


def test_withdrawal_revokes_every_standing_revision(cp_stack) -> None:
    """A revision left standing still embeds the withdrawn receipt."""
    complete_onboarding(cp_stack)
    assert cp_stack.make_worker(now=BASE_TIME + 50).run_once().status == "succeeded"
    before = cp_stack.database.query(
        "SELECT status FROM config_revisions WHERE household_id = ?",
        (cp_stack.household.id,),
    )
    assert before and all(row["status"] != "revoked" for row in before)

    withdrawal(cp_stack).withdraw(
        cp_stack.household.id, HOUSEHOLD_CONTENT_PURPOSE, now=BASE_TIME
    )

    after = cp_stack.database.query(
        "SELECT status FROM config_revisions WHERE household_id = ?",
        (cp_stack.household.id,),
    )
    assert after and all(row["status"] == "revoked" for row in after)


def test_withdrawal_is_idempotent(cp_stack) -> None:
    complete_onboarding(cp_stack)
    service = withdrawal(cp_stack)
    first = service.withdraw(
        cp_stack.household.id, HOUSEHOLD_CONTENT_PURPOSE, now=BASE_TIME
    )
    second = service.withdraw(
        cp_stack.household.id, HOUSEHOLD_CONTENT_PURPOSE, now=BASE_TIME + 1
    )

    assert first.receipts_revoked == 1
    assert second.receipts_revoked == 0
    # The original timestamp stands: withdrawal happened when it happened.
    assert receipt_row(cp_stack, HOUSEHOLD_CONTENT_PURPOSE)["revoked_at"] == BASE_TIME


def test_withdrawing_a_consent_never_given_is_an_error(cp_stack) -> None:
    """Silence here would let a typo in the purpose read as success."""
    cp_stack.complete_profile()
    with pytest.raises(ConsentNotHeld):
        withdrawal(cp_stack).withdraw(
            cp_stack.household.id, HOUSEHOLD_CONTENT_PURPOSE, now=BASE_TIME
        )


def test_an_unknown_purpose_is_rejected(cp_stack) -> None:
    cp_stack.complete_profile()
    with pytest.raises(KeyError):
        withdrawal(cp_stack).withdraw(
            cp_stack.household.id, "invented_purpose", now=BASE_TIME
        )


def test_withdrawal_enqueues_a_stop_for_a_provisioned_runtime(cp_stack) -> None:
    complete_onboarding(cp_stack)
    drain(cp_stack)
    set_runtime_ref(cp_stack)

    result = withdrawal(cp_stack).withdraw(
        cp_stack.household.id, HOUSEHOLD_CONTENT_PURPOSE, now=BASE_TIME
    )

    assert result.runtime_notified
    job = cp_stack.database.query_one(
        "SELECT kind, operation, status FROM provisioning_jobs WHERE id = ?",
        (result.runtime_job_id,),
    )
    assert job["kind"] == "runtime"
    assert job["operation"] == REVOKE_CONSENT_OPERATION


def test_no_stop_is_enqueued_when_nothing_is_serving(cp_stack) -> None:
    """No runtime, nothing to stop — and the receipt is already closed."""
    complete_onboarding(cp_stack)

    result = withdrawal(cp_stack).withdraw(
        cp_stack.household.id, HOUSEHOLD_CONTENT_PURPOSE, now=BASE_TIME
    )

    assert not result.runtime_notified
    assert result.receipts_revoked == 1


def test_the_worker_pushes_the_stop_with_the_derived_credential(cp_stack) -> None:
    """The DSAR token is derived from the runtime ref, never stored."""
    complete_onboarding(cp_stack)
    drain(cp_stack)
    set_runtime_ref(cp_stack)
    result = withdrawal(cp_stack).withdraw(
        cp_stack.household.id, HOUSEHOLD_CONTENT_PURPOSE, now=BASE_TIME
    )

    runtime = FakeRuntime()
    worker = cp_stack.make_worker(now=BASE_TIME + 50)
    worker._runtime_client = runtime
    outcome = worker.run_once()

    assert outcome.job_id == result.runtime_job_id
    assert outcome.status == "succeeded"
    assert len(runtime.calls) == 1
    call = runtime.calls[0]
    assert call["url"].endswith("/internal/v1/consent/revoke")
    expected = cp_stack.token_hasher.digest(f"runtime-dsar:{RUNTIME_REF}")
    assert call["headers"]["Authorization"] == f"Bearer {expected}"


def test_the_stop_runs_even_though_the_consent_is_gone(cp_stack) -> None:
    """The job acts on the absence, so it cannot require the presence.

    Every other runtime job is blocked when a required consent is missing. This
    one must not be, or withdrawal could never be enforced.
    """
    complete_onboarding(cp_stack)
    drain(cp_stack)
    set_runtime_ref(cp_stack)
    withdrawal(cp_stack).withdraw(
        cp_stack.household.id, HOUSEHOLD_CONTENT_PURPOSE, now=BASE_TIME
    )

    runtime = FakeRuntime()
    worker = cp_stack.make_worker(now=BASE_TIME + 50)
    worker._runtime_client = runtime
    outcome = worker.run_once()

    assert outcome.status == "succeeded"
    assert outcome.error_code != "content_restriction_receipt_required"
    assert len(runtime.calls) == 1


def test_an_unreachable_runtime_stays_retryable(cp_stack) -> None:
    """`failed` would abandon the withdrawal; it has to keep trying."""
    complete_onboarding(cp_stack)
    drain(cp_stack)
    set_runtime_ref(cp_stack)
    withdrawal(cp_stack).withdraw(
        cp_stack.household.id, HOUSEHOLD_CONTENT_PURPOSE, now=BASE_TIME
    )

    runtime = FakeRuntime(error=ConnectionError("no route to runtime"))
    worker = cp_stack.make_worker(now=BASE_TIME + 50)
    worker._runtime_client = runtime
    outcome = worker.run_once()

    assert outcome.status == "outcome_unknown"
    assert outcome.error_code == "runtime_unreachable"


def test_a_deleted_runtime_satisfies_the_withdrawal(cp_stack) -> None:
    """Nothing left to stop is the outcome withdrawal wanted."""
    complete_onboarding(cp_stack)
    drain(cp_stack)
    set_runtime_ref(cp_stack)
    withdrawal(cp_stack).withdraw(
        cp_stack.household.id, HOUSEHOLD_CONTENT_PURPOSE, now=BASE_TIME
    )

    worker = cp_stack.make_worker(now=BASE_TIME + 50)
    worker._runtime_client = FakeRuntime(status_code=410)
    outcome = worker.run_once()

    assert outcome.status == "succeeded"
    # And SETTLED. Returning success without settling leaves the job `running`;
    # once the lease expires the reclaimer resends an already-satisfied
    # revocation, forever.
    job = cp_stack.database.query_one(
        "SELECT status, settled_at FROM provisioning_jobs WHERE id = ?",
        (outcome.job_id,),
    )
    assert job["status"] == "succeeded"
    assert job["settled_at"] is not None


def test_a_refusing_runtime_stays_retryable(cp_stack) -> None:
    complete_onboarding(cp_stack)
    drain(cp_stack)
    set_runtime_ref(cp_stack)
    withdrawal(cp_stack).withdraw(
        cp_stack.household.id, HOUSEHOLD_CONTENT_PURPOSE, now=BASE_TIME
    )

    worker = cp_stack.make_worker(now=BASE_TIME + 50)
    worker._runtime_client = FakeRuntime(status_code=503)
    outcome = worker.run_once()

    assert outcome.status == "outcome_unknown"
    assert outcome.error_code == "runtime_refused"


def test_no_new_revision_can_be_issued_after_withdrawal(cp_stack) -> None:
    """The end-to-end property: withdrawal actually stops the pipeline.

    Withdrawal cannot be worked around by reissuing. The purpose withdrawn here
    is the S5 restriction, which EVERY household owes regardless of provider —
    a synthetic stack does not owe the Art. 9(2)(a) consent, so withdrawing that
    one proves nothing about the planner here. The mechanism is identical:
    `required_consent_purposes` decides what is owed, and the planner demands a
    current, unrevoked receipt for each.
    """
    complete_onboarding(cp_stack)
    drain(cp_stack)
    withdrawal(cp_stack).withdraw(
        cp_stack.household.id, CONTENT_RESTRICTION_PURPOSE, now=BASE_TIME
    )

    planner = DesiredSpecPlanner(
        cp_stack.accounts,
        cp_stack.households,
        cp_stack.onboarding,
        cp_stack.configs,
        cp_stack.bindings,
        cp_stack.channel_prefs,
    )
    with (
        cp_stack.database.write() as connection,
        pytest.raises(ValueError, match="authoritative onboarding consent"),
    ):
        planner.issue(connection, household_id=cp_stack.household.id)


# ---------------------------------------------------------------------------
# The production boundary. A right only tests can exercise is not a right.
# ---------------------------------------------------------------------------


def test_the_container_exposes_the_withdrawal_service(cp_stack) -> None:
    """Wiring, not just a class.

    The service existed for one review round with no caller outside tests: no
    container attribute, no command, nothing a support engineer answering
    the advertised withdrawal address could run.
    """
    from control_plane.config import ControlPlaneConfig
    from control_plane.container import ControlPlaneContainer

    with ControlPlaneContainer.build(
        ControlPlaneConfig.for_test(cp_stack.config.database_path.parent)
    ) as container:
        assert isinstance(container.withdrawal, ConsentWithdrawalService)


def test_the_cli_offers_every_consent_purpose() -> None:
    """A purpose absent from the command cannot be withdrawn at all."""
    from control_plane.cli import _parser
    from control_plane.privacy.consent import CONSENT_TEXTS

    action = next(
        a for a in _parser()._subparsers._group_actions[0].choices["withdraw-consent"]._actions
        if a.dest == "purpose"
    )
    assert set(action.choices) == set(CONSENT_TEXTS)


def test_a_second_withdrawal_after_re_consent_queues_its_own_stop(cp_stack) -> None:
    """The scenario a household+purpose key silently swallowed.

    Withdraw, re-consent, provision again, withdraw again. With the old key the
    second withdrawal matched the FIRST withdrawal's job, `JobsRepository.create`
    returned that already-succeeded job, and no stop was queued — so the runtime
    then serving carried on processing after a valid Art. 7(3) request.
    """
    complete_onboarding(cp_stack)
    drain(cp_stack)
    set_runtime_ref(cp_stack)
    service = withdrawal(cp_stack)

    first = service.withdraw(
        cp_stack.household.id, HOUSEHOLD_CONTENT_PURPOSE, now=BASE_TIME
    )
    assert first.runtime_notified
    worker = cp_stack.make_worker(now=BASE_TIME + 50)
    worker._runtime_client = FakeRuntime()
    assert worker.run_once().status == "succeeded"

    # A new consent cycle: a fresh receipt, and a re-provisioned runtime.
    with cp_stack.database.write() as connection:
        connection.execute(
            "INSERT INTO consent_receipts (id, household_id, account_id, purpose,"
            " text_version, text_sha256, locale, accepted_at, created_at)"
            " SELECT ?, household_id, account_id, purpose, text_version,"
            " text_sha256, locale, ?, ? FROM consent_receipts"
            " WHERE household_id = ? AND purpose = ? LIMIT 1",
            (
                "20000000-0000-4000-8000-000000000077",
                BASE_TIME + 100,
                BASE_TIME + 100,
                cp_stack.household.id,
                HOUSEHOLD_CONTENT_PURPOSE,
            ),
        )

    second = service.withdraw(
        cp_stack.household.id, HOUSEHOLD_CONTENT_PURPOSE, now=BASE_TIME + 200
    )

    assert second.receipts_revoked == 1
    assert second.runtime_notified
    assert second.runtime_job_id != first.runtime_job_id
    job = cp_stack.database.query_one(
        "SELECT status FROM provisioning_jobs WHERE id = ?", (second.runtime_job_id,)
    )
    assert job["status"] == "pending"


def test_one_withdrawal_still_queues_exactly_one_stop(cp_stack) -> None:
    """The idempotency the old key was reaching for must survive."""
    complete_onboarding(cp_stack)
    drain(cp_stack)
    set_runtime_ref(cp_stack)
    service = withdrawal(cp_stack)

    first = service.withdraw(
        cp_stack.household.id, HOUSEHOLD_CONTENT_PURPOSE, now=BASE_TIME
    )
    second = service.withdraw(
        cp_stack.household.id, HOUSEHOLD_CONTENT_PURPOSE, now=BASE_TIME + 1
    )

    assert second.receipts_revoked == 0
    stops = cp_stack.database.query(
        "SELECT id FROM provisioning_jobs WHERE household_id = ? AND operation = ?",
        (cp_stack.household.id, REVOKE_CONSENT_OPERATION),
    )
    assert len(stops) == 1
    assert first.runtime_job_id == stops[0]["id"]


def test_withdrawal_runs_while_the_service_holds_the_writer_lock(tmp_path) -> None:
    """`serve` owns the nonblocking flock for the life of the process.

    A withdrawal command that takes the same lock can only run with production
    stopped, and an Art. 7(3) withdrawal that requires taking the service down
    is not the one-step withdrawal the consent copy promises.
    """
    from control_plane.cli import main
    from control_plane.config import ControlPlaneConfig
    from control_plane.container import ControlPlaneContainer

    config = ControlPlaneConfig.for_test(tmp_path)
    monkey = pytest.MonkeyPatch()
    monkey.setattr(ControlPlaneConfig, "from_env", staticmethod(lambda: config))
    try:
        # Stand in for the running service: hold the writer flock.
        with ControlPlaneContainer.build(config, acquire_process_lock=True):
            # No receipt exists, so it exits on that — the point is that it gets
            # far enough to look, rather than dying on the lock.
            with pytest.raises(SystemExit) as exit_info:
                main([
                    "withdraw-consent",
                    "10000000-0000-4000-8000-000000000031",
                    HOUSEHOLD_CONTENT_PURPOSE,
                ])
            assert "holds no" in str(exit_info.value)
    finally:
        monkey.undo()


def test_a_synthetic_runtime_reference_queues_no_stop(cp_stack) -> None:
    """The repository's default household has one, and it is not notifiable.

    `DryRunRuntimeProvisioner` stores `synthetic-runtime:<household_id>` — non
    empty, so an emptiness check let it through, and the worker then rejected it
    because `RUNTIME_REF` accepts only managed `abrolia-hh-*` names. The result
    was a withdrawal that reported `runtime_notified: true` and then settled the
    job as failed. Nothing is lost by skipping: the revoked receipt already
    closes every control-plane boundary, and a synthetic runtime holds no real
    family content.
    """
    complete_onboarding(cp_stack)
    drain(cp_stack)
    set_runtime_ref(cp_stack, f"synthetic-runtime:{cp_stack.household.id}")

    result = withdrawal(cp_stack).withdraw(
        cp_stack.household.id, HOUSEHOLD_CONTENT_PURPOSE, now=BASE_TIME
    )

    assert result.receipts_revoked == 1
    assert not result.runtime_notified
    assert cp_stack.database.query(
        "SELECT id FROM provisioning_jobs WHERE household_id = ? AND operation = ?",
        (cp_stack.household.id, REVOKE_CONSENT_OPERATION),
    ) == []


def test_a_managed_runtime_reference_still_queues_one(cp_stack) -> None:
    """The counterpart, so the skip cannot swallow a real runtime."""
    complete_onboarding(cp_stack)
    drain(cp_stack)
    set_runtime_ref(cp_stack)

    result = withdrawal(cp_stack).withdraw(
        cp_stack.household.id, HOUSEHOLD_CONTENT_PURPOSE, now=BASE_TIME
    )

    assert result.runtime_notified


def test_resetting_onboarding_does_not_cancel_a_pending_withdrawal(cp_stack) -> None:
    """`reset_from` supersedes the attempt; a withdrawal is not part of it.

    `_supersede_unsettled_jobs` cancelled every pending non-cleanup job, so an
    owner resetting an unrelated step silently undid an Art. 7(3) withdrawal —
    and nothing would re-enqueue the stop, leaving the runtime processing
    indefinitely.
    """
    complete_onboarding(cp_stack)
    drain(cp_stack)
    set_runtime_ref(cp_stack)
    result = withdrawal(cp_stack).withdraw(
        cp_stack.household.id, HOUSEHOLD_CONTENT_PURPOSE, now=BASE_TIME
    )
    assert result.runtime_notified

    cp_stack.service.reset_from(
        cp_stack.household.id, StepKind.EMAIL, context=cp_stack.context()
    )

    stop = cp_stack.database.query_one(
        "SELECT status FROM provisioning_jobs WHERE id = ?", (result.runtime_job_id,)
    )
    assert stop["status"] == "pending"


def test_the_consent_boundary_holds_on_retry_and_check(cp_stack) -> None:
    """Consent evidence is control-plane-only on EVERY provider-bound path.

    The sanitizer was inline in `select`, so a retry of the same step and the
    inspect request raised by `check` both forwarded the full durable selection
    — flag, receipt id, version and digest — to Nerve or Google.
    """
    service = cp_stack.service
    selection = real_email_selection()

    for kind_name, sanitised in (
        ("select", service._provider_safe_selection(StepKind.EMAIL, selection)),
        ("retry", service._provider_safe_selection(StepKind.EMAIL, dict(selection))),
    ):
        assert sanitised is not None, kind_name
        for field in service.CONSENT_FIELDS:
            assert field not in sanitised, f"{kind_name} leaked {field}"
        # And it must still say what the provider actually needs.
        assert sanitised["kind"] == selection["kind"], kind_name
        assert sanitised["local_part"] == selection["local_part"], kind_name

    # Non-email steps are untouched: they carry no consent evidence.
    whatsapp = {"kind": "shared_abrolia", "privacy_notice_receipt_id": "r"}
    assert service._provider_safe_selection(StepKind.WHATSAPP, whatsapp) == whatsapp


def test_withdrawal_disconnects_the_upstream_inbox(cp_stack) -> None:
    """Stopping our runtime does not stop the processor.

    A provisioned Nerve or Gmail mailbox keeps receiving and storing whatever is
    forwarded to it. Without a teardown, withdrawal left processing running at
    the processor boundary — the one the family cannot see and the one the DPA
    covers.
    """
    complete_onboarding(cp_stack)
    drain(cp_stack)
    set_runtime_ref(cp_stack)
    service = ConsentWithdrawalService(
        cp_stack.database, jobs=cp_stack.jobs, onboarding=cp_stack.service
    )

    result = service.withdraw(
        cp_stack.household.id, HOUSEHOLD_CONTENT_PURPOSE, now=BASE_TIME
    )

    assert result.email_disconnected
    cleanups = cp_stack.database.query(
        "SELECT kind, operation, status FROM provisioning_jobs"
        " WHERE household_id = ? AND kind = 'cleanup'",
        (cp_stack.household.id,),
    )
    assert cleanups, "no upstream teardown was scheduled"
    assert all(row["operation"] == "deprovision" for row in cleanups)


def test_the_teardown_survives_a_later_reset(cp_stack) -> None:
    """Cleanup jobs are already exempt from supersession; keep it that way."""
    complete_onboarding(cp_stack)
    drain(cp_stack)
    set_runtime_ref(cp_stack)
    service = ConsentWithdrawalService(
        cp_stack.database, jobs=cp_stack.jobs, onboarding=cp_stack.service
    )
    result = service.withdraw(
        cp_stack.household.id, HOUSEHOLD_CONTENT_PURPOSE, now=BASE_TIME
    )
    assert result.email_cleanup_job_ids

    cp_stack.service.reset_from(
        cp_stack.household.id, StepKind.EMAIL, context=cp_stack.context()
    )

    for job_id in result.email_cleanup_job_ids:
        row = cp_stack.database.query_one(
            "SELECT status FROM provisioning_jobs WHERE id = ?", (job_id,)
        )
        assert row["status"] != "cancelled"


def live_email_resources(cp_stack) -> list:
    """Inbox references that are neither being torn down nor already gone."""
    return cp_stack.database.query(
        "SELECT id, status FROM external_resources WHERE household_id = ?"
        " AND resource_type = 'email_identity'"
        " AND status NOT IN ('deleting','deleted')",
        (cp_stack.household.id,),
    )


def withdraw_now(cp_stack, *, now: float) -> None:
    ConsentWithdrawalService(
        cp_stack.database, jobs=cp_stack.jobs, onboarding=cp_stack.service
    ).withdraw(cp_stack.household.id, HOUSEHOLD_CONTENT_PURPOSE, now=now)


def test_an_inbox_that_arrives_after_withdrawal_is_torn_down(cp_stack) -> None:
    """The gap between "crossed the boundary" and "recorded a row".

    `disconnect_email_for_withdrawal` schedules teardown from
    `external_resources`, so it can only see inboxes that have already been
    written down. An `ensure` that has reached Nerve or Google and not yet
    returned has no row to find, so the scan walked straight past it — and the
    result then took the ordinary success path, because the barrier after the
    provider call asked `status == "cancelled"` while withdrawal quarantines the
    job as `outcome_unknown`. The household was left with a mailbox that kept
    receiving, with nothing recorded that would ever delete it.

    The barrier here is real, not a sleep: withdrawal happens INSIDE `ensure`,
    so the provider's result cannot arrive before it.
    """
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        real_email_selection(),
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )

    class WithdrawThenSucceed(DeterministicFakeProvisioner):
        def ensure(self, intent, idempotency_key):
            withdraw_now(cp_stack, now=BASE_TIME + 3)
            return super().ensure(intent, idempotency_key)

    registry = ProviderRegistry()
    registry.register("fake-email", WithdrawThenSucceed("email"))
    worker = cp_stack.make_worker(providers=registry, now=BASE_TIME + 3)

    late = worker.run_once()

    # The intent is settled as compensated, not as a successful step.
    stored = cp_stack.jobs.get(late.job_id)
    assert stored is not None
    assert stored.error_code == "cancelled_and_compensated"

    # And the inbox the provider actually created is gone, rather than sitting
    # in `external_resources` as ready with nothing pointed at it. This is the
    # assertion that matters: everything else is bookkeeping about it.
    assert live_email_resources(cp_stack) == [], (
        "an inbox provisioned during withdrawal is still live"
    )
    drain(cp_stack, now=BASE_TIME + 60)
    identity = cp_stack.database.query_one(
        "SELECT status FROM email_identities WHERE household_id = ?"
        " ORDER BY created_at DESC LIMIT 1",
        (cp_stack.household.id,),
    )
    assert identity is None or identity["status"] == "deleted"


def test_withdrawal_does_not_supersede_the_households_other_work(cp_stack) -> None:
    """Withdrawal tears down the inbox; it is not a household-wide cancel.

    The quarantine is deliberately narrowed to `email_identity`, because the
    same sweep serves `cancel` and `reset`, where it covers every kind. Widening
    it here would settle unrelated in-flight jobs as needing reconciliation and
    strand them — and the revocation push itself is one of the jobs that must
    survive, or withdrawal could never be enforced.
    """
    complete_onboarding(cp_stack)
    drain(cp_stack)
    set_runtime_ref(cp_stack)

    result = ConsentWithdrawalService(
        cp_stack.database, jobs=cp_stack.jobs, onboarding=cp_stack.service
    ).withdraw(cp_stack.household.id, HOUSEHOLD_CONTENT_PURPOSE, now=BASE_TIME)

    assert result.runtime_job_id is not None
    stop = cp_stack.database.query_one(
        "SELECT status, error_code FROM provisioning_jobs WHERE id = ?",
        (result.runtime_job_id,),
    )
    assert stop["status"] == "pending"
    assert stop["error_code"] is None


def test_every_consent_declares_what_its_withdrawal_terminates() -> None:
    """A purpose with no declared scope must not fall back to a full teardown.

    Falling back is what caused the damage: `withdraw()` ran one teardown for
    every purpose, so withdrawing a WhatsApp consent deprovisioned the
    household's inbox. This pairs the two maps so adding a consent without
    deciding what withdrawing it ends fails here rather than in production.
    """
    assert set(WITHDRAWAL_SCOPES) == set(CONSENT_TEXTS)
    for purpose, scope in WITHDRAWAL_SCOPES.items():
        assert scope["resource_types"], f"{purpose} terminates nothing"
        assert isinstance(scope["stops_runtime"], bool), purpose


def test_an_undeclared_purpose_is_refused_rather_than_defaulted(
    cp_stack, monkeypatch
) -> None:
    """A consent whose scope nobody decided must not inherit the full teardown.

    The purpose has to be a REAL one for this to prove anything: an unknown
    purpose is already rejected a few lines earlier, so testing with one would
    pass whatever the scope guard did. So a fourth consent is introduced, with
    no entry in `WITHDRAWAL_SCOPES` — the exact situation the next person to add
    a consent creates.
    """
    complete_onboarding(cp_stack)
    drain(cp_stack)
    set_runtime_ref(cp_stack)
    monkeypatch.setitem(
        CONSENT_TEXTS, "an_unscoped_consent", ("unscoped-v1", "text nobody scoped")
    )
    service = ConsentWithdrawalService(
        cp_stack.database, jobs=cp_stack.jobs, onboarding=cp_stack.service
    )

    with pytest.raises(KeyError, match="no withdrawal scope"):
        service.withdraw(cp_stack.household.id, "an_unscoped_consent", now=BASE_TIME)

    # And it defaulted to nothing, rather than to the content teardown.
    assert all(
        status not in {"deleting", "deleted"}
        for status in _resources(cp_stack, "email_identity")
    ), "an unscoped withdrawal tore down the inbox anyway"
    assert cp_stack.database.query_one(
        "SELECT 1 FROM provisioning_jobs WHERE household_id = ? AND operation = ?",
        (cp_stack.household.id, REVOKE_CONSENT_OPERATION),
    ) is None


def _resources(cp_stack, resource_type: str) -> list[str]:
    return [
        row["status"]
        for row in cp_stack.database.query(
            "SELECT status FROM external_resources WHERE household_id = ?"
            " AND resource_type = ?",
            (cp_stack.household.id, resource_type),
        )
    ]


def test_a_channel_withdrawal_leaves_the_inbox_alone(cp_stack) -> None:
    """Art. 7(3) stops the processing a consent authorised — not other processing.

    `withdraw()` ran one teardown for every purpose: disconnect the email inbox,
    revoke every active revision, stop the runtime. So withdrawing
    `whatsapp_channel_privacy` deprovisioned the household's Gmail or Nerve
    inbox and the mail stored in it, while never touching the WhatsApp resource
    the consent was actually about — damage in one direction and a no-op in the
    other, from the same missing distinction.
    """
    complete_onboarding(cp_stack)
    drain(cp_stack)
    set_runtime_ref(cp_stack)
    service = ConsentWithdrawalService(
        cp_stack.database, jobs=cp_stack.jobs, onboarding=cp_stack.service
    )

    result = service.withdraw(
        cp_stack.household.id, "whatsapp_channel_privacy", now=BASE_TIME
    )

    # The receipt is withdrawn — that part is unconditional.
    assert result.receipts_revoked == 1
    # The inbox is untouched: not disconnecting, no cleanup, still live.
    assert all(status not in {"deleting", "deleted"}
               for status in _resources(cp_stack, "email_identity"))
    identity = cp_stack.database.query_one(
        "SELECT status FROM email_identities WHERE household_id = ?",
        (cp_stack.household.id,),
    )
    assert identity is None or identity["status"] != "disconnecting"
    # The runtime keeps serving the household's other channels, and the
    # revisions that carry them stay standing.
    assert result.runtime_job_id is None
    assert result.revisions_revoked == 0
    # And the resource the consent WAS about is torn down.
    assert any(status == "deleting" for status in _resources(cp_stack, "whatsapp_identity")), (
        "the WhatsApp resource the consent covered was never scheduled for teardown"
    )


def test_withdrawing_content_tears_down_every_channel_that_receives_it(
    cp_stack,
) -> None:
    """Content does not only arrive by email.

    The Art 9(2)(a) scope said "everything goes" in a comment and
    `{"email_identity"}` in the set. A WhatsApp identity or channel binding left
    `ready` keeps accepting and storing household content at the processor
    boundary the moment a real adapter is enabled — the boundary the family
    cannot see and the one the DPA covers.
    """
    complete_onboarding(cp_stack)
    drain(cp_stack)
    set_runtime_ref(cp_stack)
    before = {
        kind: _resources(cp_stack, kind)
        for kind in ("email_identity", "whatsapp_identity", "channel_binding")
    }
    assert all(before.values()), f"the fixture provisioned nothing to tear down: {before}"

    ConsentWithdrawalService(
        cp_stack.database, jobs=cp_stack.jobs, onboarding=cp_stack.service
    ).withdraw(cp_stack.household.id, HOUSEHOLD_CONTENT_PURPOSE, now=BASE_TIME)

    for kind in before:
        assert all(
            status in {"deleting", "deleted"}
            for status in _resources(cp_stack, kind)
        ), f"{kind} still accepts content after withdrawal"


def test_the_two_content_purposes_cover_the_same_channels() -> None:
    """They drifted apart once; naming the set once is what stops it again."""
    assert (
        WITHDRAWAL_SCOPES["special_category_household_content"]["resource_types"]
        == WITHDRAWAL_SCOPES["special_category_content_restriction"]["resource_types"]
    )


def test_the_content_consent_still_terminates_everything(cp_stack) -> None:
    """Scoping must not weaken the withdrawal that matters.

    The Art 9(2)(a) condition is the lawful basis for processing household
    content at all, so withdrawing it keeps the full teardown: inbox
    disconnected, revisions revoked, runtime told to stop.
    """
    complete_onboarding(cp_stack)
    drain(cp_stack)
    set_runtime_ref(cp_stack)
    service = ConsentWithdrawalService(
        cp_stack.database, jobs=cp_stack.jobs, onboarding=cp_stack.service
    )

    result = service.withdraw(
        cp_stack.household.id, HOUSEHOLD_CONTENT_PURPOSE, now=BASE_TIME
    )

    assert result.email_disconnected
    assert result.revisions_revoked > 0
    assert result.runtime_job_id is not None
    assert any(status == "deleting" for status in _resources(cp_stack, "email_identity"))


def test_a_withdrawal_landing_after_the_barrier_still_compensates(cp_stack) -> None:
    """The narrower race: withdrawal commits AFTER `_superseded` has read.

    The barrier before the provider call is not enough on its own. It reads the
    job, work continues, and a withdrawal committing in that window leaves the
    job `outcome_unknown` with `withdrawal_requires_reconciliation` — which
    `_finish_step` accepted, because it tested only the status. `mark_verified`
    then failed against the disconnecting identity and the provider's newly
    created inbox was discarded with no `external_resources` row and no cleanup
    job: an untracked mailbox still receiving after withdrawal.

    The barrier is placed inside `_stage_email_secret`, which is the code that
    runs BETWEEN the `_superseded` read and `_finish_step`'s transaction — the
    window itself. Doing it from the provider instead, as the sibling test does,
    commits the withdrawal before the barrier reads and therefore exercises the
    other race; that version of this test passed with the fix reverted, which is
    how the placement was found to be wrong.
    """
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        real_email_selection(),
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )

    provider = DeterministicFakeProvisioner("email")
    registry = ProviderRegistry()
    registry.register("fake-email", provider)
    worker = cp_stack.make_worker(providers=registry, now=BASE_TIME + 3)

    staged = worker._stage_email_secret

    def withdraw_then_stage(*args, **kwargs):
        outcome = staged(*args, **kwargs)
        withdraw_now(cp_stack, now=BASE_TIME + 3)
        return outcome

    worker._stage_email_secret = withdraw_then_stage

    worker.run_once()
    drain(cp_stack, now=BASE_TIME + 60)

    # The PROVIDER's state, not ours. This is the assertion the defect defeats:
    # it discards the result without writing an `external_resources` row at all,
    # so "no live rows in our database" is true both when the inbox was torn
    # down and when it was silently abandoned. The mailbox that keeps receiving
    # is at the provider, and that is where it has to be gone from.
    assert provider.resources == {}, (
        "an inbox created before the withdrawal committed is still at the"
        " provider, with nothing in our database that would ever delete it"
    )
    assert live_email_resources(cp_stack) == []
    identity = cp_stack.database.query_one(
        "SELECT status FROM email_identities WHERE household_id = ?"
        " ORDER BY created_at DESC LIMIT 1",
        (cp_stack.household.id,),
    )
    assert identity is None or identity["status"] == "deleted"


def test_the_stop_carries_the_withdrawn_receipt_ids(cp_stack) -> None:
    """The generation has to survive as far as DELIVERY, not just the key.

    The intent key distinguishes consent cycles, which stops a second withdrawal
    from matching the first job. It does nothing about a first job that is still
    retrying: a stop queued for revision A, left unreachable while the household
    re-consents and reprovisions, authenticates against revision B — the runtime
    reference is the household's stable app name and does not change — and
    suspends a runtime nobody withdrew.
    """
    complete_onboarding(cp_stack)
    drain(cp_stack)
    set_runtime_ref(cp_stack)
    revoked = [
        str(row["id"])
        for row in cp_stack.database.query(
            "SELECT id FROM consent_receipts WHERE household_id = ?"
            " AND purpose = ? AND revoked_at IS NULL",
            (cp_stack.household.id, HOUSEHOLD_CONTENT_PURPOSE),
        )
    ]
    assert revoked, "the fixture holds no receipt to withdraw"

    withdrawal(cp_stack).withdraw(
        cp_stack.household.id, HOUSEHOLD_CONTENT_PURPOSE, now=BASE_TIME
    )
    runtime = FakeRuntime()
    worker = cp_stack.make_worker(now=BASE_TIME + 50)
    worker._runtime_client = runtime
    assert worker.run_once().status == "succeeded"

    assert runtime.calls, "no stop was delivered"
    assert sorted(runtime.calls[-1]["body"]["receipt_ids"]) == sorted(revoked)


def test_a_late_waiting_channel_reference_gets_a_teardown_too(cp_stack) -> None:
    """Broadening the quarantine without broadening the compensation leaks.

    The previous round made withdrawal quarantine WhatsApp and channel jobs as
    well as email. `_handle_provider_waiting` still scheduled a late-waiting
    cleanup only for `email_identity`, so a WhatsApp or channel provider
    returning `ProviderWaiting` with a newly created reference fell through to
    `_mark_step_problem` — which sees the quarantine and returns WITHOUT
    recording the reference or scheduling a teardown. An identity created at the
    provider with nothing in our database that would ever delete it: the same
    leak the email path was written to close.
    """
    complete_onboarding(cp_stack)
    drain(cp_stack)
    set_runtime_ref(cp_stack)
    assert {"whatsapp_identity", "channel_binding"} <= COMPENSATED_STEP_KINDS, (
        "the compensated kinds no longer cover every quarantined channel"
    )
    # The property that matters, asserted structurally: every kind withdrawal
    # quarantines is a kind whose late reference gets compensated. A kind in one
    # set and not the other is a resource created and then abandoned.
    assert CONTENT_RESOURCE_TYPES <= COMPENSATED_STEP_KINDS


def test_a_late_waiting_whatsapp_reference_is_recorded_and_torn_down(
    cp_stack,
) -> None:
    """Driven, not inspected: the provider really returns a late reference.

    The barrier is the provider call itself — it withdraws, then raises
    `ProviderWaiting` carrying a reference it just created — so the response
    cannot precede the withdrawal. What must not happen is the reference being
    dropped: recorded nowhere, with no teardown, while the identity exists at
    the provider.
    """
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        real_email_selection(),
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )
    worker = cp_stack.make_worker(now=BASE_TIME + 3)
    assert worker.run_once().status == "succeeded"
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.WHATSAPP,
        WHATSAPP_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 4,
    )

    created = "whatsapp-late-reference"

    class WithdrawThenWait(DeterministicFakeProvisioner):
        def ensure(self, intent, idempotency_key):
            del intent, idempotency_key
            withdraw_now(cp_stack, now=BASE_TIME + 5)
            raise ProviderWaiting(
                "synthetic owner action required", external_ref=created
            )

    registry = ProviderRegistry()
    registry.register("fake-whatsapp", WithdrawThenWait("whatsapp"))
    late = cp_stack.make_worker(providers=registry, now=BASE_TIME + 5).run_once()

    assert late.error_code == "withdrawal_requires_reconciliation"
    cleanup = cp_stack.database.query_one(
        "SELECT id FROM provisioning_jobs WHERE intent_key = ?",
        (f"{cp_stack.household.id}:late-waiting-cleanup:{late.job_id}",),
    )
    assert cleanup is not None, "the late WhatsApp reference got no teardown job"
    # The teardown must be labelled with the job's OWN kind. Hard-coding
    # `email_identity` hands the cleanup worker a WhatsApp reference dressed as
    # an inbox, and it takes the email branch against a resource that is not
    # one — so the identity is never actually deprovisioned.
    teardown = cp_stack.jobs.request(cleanup["id"])
    assert teardown["resource_type"] == "whatsapp_identity", teardown["resource_type"]
    assert teardown["external_ref"] == created
    recorded = cp_stack.database.query(
        "SELECT resource_type, status FROM external_resources"
        " WHERE household_id = ? AND resource_type = 'whatsapp_identity'",
        (cp_stack.household.id,),
    )
    assert recorded, "the reference the provider created was never recorded"
    assert all(row["status"] in {"deleting", "deleted"} for row in recorded)


def test_an_already_ambiguous_job_is_quarantined_by_withdrawal(cp_stack) -> None:
    """The ambiguity is the reason to quarantine it, not a reason to skip it.

    A provider call that timed out after possibly creating a resource leaves the
    job `outcome_unknown` with no `external_resources` row. The sweep rewrote
    only `running` and `waiting_user`, so withdrawal never reached it — and a
    later reconcile either failed the real-email job at the missing-consent
    precheck without inspecting or deprovisioning the inbox that may exist, or
    let a WhatsApp or channel result pass `_finish_step` as an ordinary success.
    """
    complete_onboarding(cp_stack)
    drain(cp_stack)
    set_runtime_ref(cp_stack)
    with cp_stack.database.write() as connection:
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'outcome_unknown',"
            " error_code = 'outcome_unknown', settled_at = NULL"
            " WHERE household_id = ? AND kind = 'email_identity'",
            (cp_stack.household.id,),
        )

    withdraw_now(cp_stack, now=BASE_TIME)

    ambiguous = cp_stack.database.query(
        "SELECT error_code FROM provisioning_jobs WHERE household_id = ?"
        " AND kind = 'email_identity'",
        (cp_stack.household.id,),
    )
    assert ambiguous, "the fixture left no ambiguous job"
    assert all(
        row["error_code"] == "withdrawal_requires_reconciliation" for row in ambiguous
    ), [row["error_code"] for row in ambiguous]


def test_an_existing_quarantine_reason_is_not_overwritten(cp_stack) -> None:
    """Both reasons route to the same compensation; the reason is the message.

    Overwriting `reset_requires_reconciliation` would erase which command an
    operator had been told to run, for no gain — the job is already quarantined.
    """
    complete_onboarding(cp_stack)
    drain(cp_stack)
    set_runtime_ref(cp_stack)
    with cp_stack.database.write() as connection:
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'outcome_unknown',"
            " error_code = 'reset_requires_reconciliation'"
            " WHERE household_id = ? AND kind = 'email_identity'",
            (cp_stack.household.id,),
        )

    withdraw_now(cp_stack, now=BASE_TIME)

    kept = cp_stack.database.query(
        "SELECT error_code FROM provisioning_jobs WHERE household_id = ?"
        " AND kind = 'email_identity'",
        (cp_stack.household.id,),
    )
    assert all(row["error_code"] == "reset_requires_reconciliation" for row in kept)


def test_a_quarantined_email_job_reconciles_past_the_consent_check(cp_stack) -> None:
    """The teardown must not require the consent whose loss demands it.

    Withdrawal settles an ambiguous provider call `outcome_unknown` so an
    operator can reconcile it, and reconciling means tearing down whatever
    the provider may have created. `_reconcile` checked for a current
    content-restriction consent BEFORE tearing down — and withdrawal
    necessarily revoked it — so the job settled `failed` at the precondition
    and the inbox that may exist was never removed. Reconciling no longer
    inspects anything: it derives the teardown reference from durable state
    and schedules a deprovision, so what must survive the missing consent is
    the whole teardown, not merely a look.
    """
    complete_onboarding(cp_stack)
    drain(cp_stack)
    set_runtime_ref(cp_stack)
    email_job = cp_stack.database.query_one(
        "SELECT id FROM provisioning_jobs WHERE household_id = ?"
        " AND kind = 'email_identity' ORDER BY created_at DESC LIMIT 1",
        (cp_stack.household.id,),
    )["id"]
    with cp_stack.database.write() as connection:
        connection.execute(
            # A REAL provider. `_requires_current_email_content_restriction`
            # exempts `fake-email`, so the precondition this test is about never
            # applies to the synthetic path — which is how an earlier version
            # passed without exercising the exemption at all.
            "UPDATE provisioning_jobs SET status = 'outcome_unknown',"
            " error_code = 'outcome_unknown', settled_at = NULL,"
            " provider = 'nerve-managed' WHERE id = ?",
            (email_job,),
        )

    withdraw_now(cp_stack, now=BASE_TIME)
    quarantined = cp_stack.database.query_one(
        "SELECT error_code FROM provisioning_jobs WHERE id = ?", (email_job,)
    )
    assert quarantined["error_code"] == "withdrawal_requires_reconciliation"

    class RecordingEmail(DeterministicFakeProvisioner):
        inspected = 0
        torn_down: list[str] = []

        def inspect(self, intent, idempotency_key=None):
            type(self).inspected += 1
            return InspectResult(InspectState.ABSENT)

        def deprovision(self, external_ref):
            type(self).torn_down.append(external_ref)
            return InspectResult(InspectState.ABSENT)

    provider = RecordingEmail("email")
    # The full synthetic set underneath: withdrawal's own supersession left
    # cleanups for this household's other resources, and a worker that cannot
    # resolve their providers would fail them before reaching ours.
    registry = synthetic_provider_registry()
    registry.register("nerve-managed", provider)
    worker = cp_stack.make_worker(providers=registry, now=BASE_TIME + 50)
    reconciled = worker.reconcile(email_job)
    assert reconciled.status == "outcome_unknown"
    # Withdrawal itself scheduled cleanups alongside this one; drain until
    # the queue is quiet so the pass that performs THIS teardown is certain
    # to have run.
    for _ in range(10):
        if worker.run_once() is None:
            break

    # The property, asserted positively: the teardown happened without the
    # consent that was withdrawn. Asserting that some error code did not
    # appear is weaker than it looks — the precondition settles with a code
    # of its own choosing, and a test that names one string passes when the
    # code changes. What matters is that the provider resource was actually
    # deprovisioned, and that none of it went through a mutating inspect.
    assert type(provider).torn_down, (
        "the teardown was refused for want of the consent that was withdrawn,"
        " so the inbox that may exist was never removed"
    )
    assert type(provider).inspected == 0, (
        "reconcile reached the adapter through a mutating inspect"
    )


def test_reconciling_a_quarantined_job_never_creates_a_new_resource(cp_stack) -> None:
    """Skipping the consent check lets the INSPECTION happen, nothing more.

    `_reconcile` re-runs `prepare`/`ensure` when an inspection comes back
    `ABSENT` or `PENDING`. Under the exemption above that would hand a withdrawn
    household a brand-new resource — the opposite of what the quarantine exists
    for. A shutdown action inspects and removes; it never makes more.
    """
    complete_onboarding(cp_stack)
    drain(cp_stack)
    # The `ensure_runtime` job specifically — an `ensure_secret_namespace` one
    # exits at a different branch entirely, which is how the first version of
    # this test passed without ever reaching the guard it exists to check.
    runtime_job = cp_stack.database.query_one(
        "SELECT id FROM provisioning_jobs WHERE household_id = ?"
        " AND kind = 'runtime' AND operation = 'ensure_runtime'"
        " ORDER BY created_at DESC LIMIT 1",
        (cp_stack.household.id,),
    )
    assert runtime_job is not None, "the fixture minted no ensure_runtime job"
    with cp_stack.database.write() as connection:
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'outcome_unknown',"
            " error_code = 'withdrawal_requires_reconciliation', settled_at = NULL"
            " WHERE id = ?",
            (runtime_job["id"],),
        )

    class AbsentThenCreate(DeterministicFakeProvisioner):
        created = 0

        def inspect(self, intent, idempotency_key=None):
            del intent, idempotency_key
            return InspectResult(InspectState.ABSENT)

        def ensure(self, intent, idempotency_key):
            type(self).created += 1
            return super().ensure(intent, idempotency_key)

    provider = AbsentThenCreate("runtime")
    registry = ProviderRegistry()
    registry.register("dry-run-runtime", provider)
    reconciled = cp_stack.make_worker(
        providers=registry, now=BASE_TIME + 60
    ).reconcile(runtime_job["id"])

    assert type(provider).created == 0, (
        "reconciling a quarantined job created a new resource"
    )
    # The quarantine REASON survives — an operator still knows which command
    # settles it. What the refusal changes is that nothing new was built.
    assert reconciled.error_code == "withdrawal_requires_reconciliation"


def test_reconciling_a_quarantined_email_job_touches_no_provider(cp_stack) -> None:
    """A shutdown must not mutate the provider to find out what it holds.

    The email branch calls `reconcile`, which for Nerve delegates to `ensure`
    and resumes the provisioning graph. The obvious remedy — inspect instead —
    is not available: `NerveManagedEmailProvisioner.inspect` routes to
    `_recover_and_probe`, which deletes and reissues the API key and rotates the
    webhook, and `GoogleOAuthEmailProvisioner.inspect_intent` calls `ensure`
    outright. There is no reliably read-only inspector, so a shutdown makes no
    provider call at all.

    That is not the same as abandoning the cleanup. Everything recorded in
    `external_resources` was already scheduled for teardown by the withdrawal
    itself; a reference the job carries in its own request gets one here; and
    what is left is state nobody wrote down, which the quarantine keeps
    explicitly reconcilable rather than silently dropping.
    """
    complete_onboarding(cp_stack)
    drain(cp_stack)
    set_runtime_ref(cp_stack)
    email_job = cp_stack.database.query_one(
        "SELECT id FROM provisioning_jobs WHERE household_id = ?"
        " AND kind = 'email_identity' ORDER BY created_at DESC LIMIT 1",
        (cp_stack.household.id,),
    )["id"]
    with cp_stack.database.write() as connection:
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'outcome_unknown',"
            " error_code = 'outcome_unknown', settled_at = NULL,"
            " provider = 'nerve-managed' WHERE id = ?",
            (email_job,),
        )
    withdraw_now(cp_stack, now=BASE_TIME)

    class RefusesToBeCalled(DeterministicFakeProvisioner):
        touched: list[str] = []

        def reconcile(self, intent, idempotency_key=None):
            type(self).touched.append("reconcile")
            return super().ensure(intent, idempotency_key)

        def inspect(self, stable_ref):
            # Named to match the real provider's shape: on managed Nerve this
            # is the call that rotates a key and a webhook.
            type(self).touched.append("inspect")
            return InspectResult(InspectState.UNKNOWN)

        def ensure(self, intent, idempotency_key):
            type(self).touched.append("ensure")
            return super().ensure(intent, idempotency_key)

    provider = RefusesToBeCalled("email")
    registry = ProviderRegistry()
    registry.register("nerve-managed", provider)
    result = cp_stack.make_worker(providers=registry, now=BASE_TIME + 50).reconcile(
        email_job
    )

    assert type(provider).touched == [], (
        f"a withdrawn household's provider was called: {type(provider).touched}"
    )
    # The teardown of what WAS recorded came from the withdrawal itself.
    assert cp_stack.database.query(
        "SELECT id FROM provisioning_jobs WHERE household_id = ?"
        " AND operation = 'deprovision'",
        (cp_stack.household.id,),
    ), "the recorded inbox got no teardown"
    # And the quarantine stays, naming what settles it.
    assert result.error_code == "withdrawal_requires_reconciliation"

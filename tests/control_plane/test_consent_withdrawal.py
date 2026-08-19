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

import pytest

from control_plane.models import StepKind
from control_plane.privacy.consent import (
    CONTENT_RESTRICTION_PURPOSE,
    HOUSEHOLD_CONTENT_PURPOSE,
)
from control_plane.privacy.withdraw import (
    REVOKE_CONSENT_OPERATION,
    ConsentNotHeld,
    ConsentWithdrawalService,
)
from control_plane.provisioning.planner import DesiredSpecPlanner

from .test_art9_household_consent import complete_onboarding, real_email_selection

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

    def post(self, url, *, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": dict(headers or {})})
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
        cp_stack.accounts, cp_stack.households, cp_stack.onboarding, cp_stack.configs
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

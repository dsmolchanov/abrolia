"""The per-provider kill switches, asserted where they are supposed to bite.

`control_plane/feature_flags.py` is documented in two operator tables and in
`canon-execution-plan.md` as fail-closed, default-off gating for each provider.
It had no production caller: `ABROLIA_GMAIL_ENABLED=0` gated nothing, and the
runbook said otherwise. A flag that does not gate is worse than no flag,
because it is believed.

This module deliberately does NOT use `tests/control_plane/email/conftest.py`,
which turns both options on for the lifecycle suites.
"""

from __future__ import annotations

import pytest

from control_plane.db import new_id
from control_plane.feature_flags import (
    CUT_EMAIL_OPTIONS,
    GATED_EMAIL_OPTIONS,
    GATED_EMAIL_PROVIDERS,
)
from control_plane.models import StepKind
from control_plane.onboarding.contracts import InvalidTransition
from control_plane.privacy.consent import (
    CONTENT_RESTRICTION_PURPOSE,
    HOUSEHOLD_CONTENT_PURPOSE,
    consent_version_and_sha,
)
from control_plane.provisioning.contracts import (
    InspectResult,
    InspectState,
    ProviderRejected,
)
from control_plane.provisioning.fakes import synthetic_provider_registry

GATED = {
    "family_domain": (
        "ABROLIA_BYO_EMAIL_ENABLED",
        {"domain": "family.example.test", "local_part": "assistant"},
    ),
    "gmail_agent": (
        "ABROLIA_GMAIL_ENABLED",
        {"separate_agent_account_acknowledged": True},
    ),
}


def _selection(option: str) -> dict[str, object]:
    restriction_version, restriction_sha = consent_version_and_sha(
        CONTENT_RESTRICTION_PURPOSE
    )
    household_version, household_sha = consent_version_and_sha(
        HOUSEHOLD_CONTENT_PURPOSE
    )
    return {
        "kind": option,
        "special_category_restriction_acknowledged": True,
        "special_category_restriction_receipt_id": (
            "10000000-0000-4000-8000-0000000000b1"
        ),
        "special_category_restriction_text_version": restriction_version,
        "special_category_restriction_text_sha256": restriction_sha,
        "special_category_household_consent": True,
        "special_category_household_receipt_id": (
            "10000000-0000-4000-8000-0000000000b2"
        ),
        "special_category_household_text_version": household_version,
        "special_category_household_text_sha256": household_sha,
        **GATED[option][1],
    }


@pytest.mark.parametrize("option", sorted(GATED))
def test_a_disabled_email_option_is_refused(cp_stack, monkeypatch, option) -> None:
    """Default off means refused, even where the option routes to a fake.

    The switch is about which option the product OFFERS, not about whether real
    content can arrive — so it has to bite ahead of the synthetic early return
    in `_assert_email_rollout`. With `real_email_enabled` off, `family_domain`
    routes to `fake-email`; a gate that only guarded real providers would let it
    through and the kill switch would be decorative in exactly the configuration
    the pilot runs in.
    """
    monkeypatch.delenv(GATED[option][0], raising=False)
    cp_stack.complete_profile()
    _hold_the_content_restriction(cp_stack, now=1_760_000_000.0)

    with pytest.raises(InvalidTransition, match="disabled by flag"):
        cp_stack.service.select(
            cp_stack.household.id,
            StepKind.EMAIL,
            _selection(option),
            context=cp_stack.context(),
        )

    queued = cp_stack.database.query(
        "SELECT id FROM provisioning_jobs WHERE household_id = ?"
        " AND kind = 'email_identity'",
        (cp_stack.household.id,),
    )
    assert queued == [], "a refused option still queued provisioning work"


@pytest.mark.parametrize("option", sorted(GATED))
def test_an_enabled_email_option_is_admitted(cp_stack, monkeypatch, option) -> None:
    """And the switch is a switch: on, the option behaves as before."""
    monkeypatch.setenv(GATED[option][0], "1")
    cp_stack.complete_profile()
    _hold_the_content_restriction(cp_stack, now=1_760_000_000.0)

    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        _selection(option),
        context=cp_stack.context(),
    )

    queued = cp_stack.database.query(
        "SELECT id FROM provisioning_jobs WHERE household_id = ?"
        " AND kind = 'email_identity'",
        (cp_stack.household.id,),
    )
    assert queued, "an enabled option queued nothing"


def test_the_managed_option_is_not_gated(cp_stack, monkeypatch) -> None:
    """Deliberate, and worth pinning so the omission is not read as a miss.

    Canon has all six flags fail-closed. Wiring the managed one today would take
    email away from every deployment that has not set
    `ABROLIA_MANAGED_EMAIL_ENABLED` — including the synthetic app, which sets
    none of them. That is a separate step with a `fly.toml` change behind it.
    """
    monkeypatch.delenv("ABROLIA_MANAGED_EMAIL_ENABLED", raising=False)
    cp_stack.complete_profile()
    _hold_the_content_restriction(cp_stack, now=1_760_000_000.0)
    restriction_version, restriction_sha = consent_version_and_sha(
        CONTENT_RESTRICTION_PURPOSE
    )
    household_version, household_sha = consent_version_and_sha(
        HOUSEHOLD_CONTENT_PURPOSE
    )

    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        {
            "kind": "abrolia_managed",
            "local_part": "family-agent",
            "special_category_restriction_acknowledged": True,
            "special_category_restriction_receipt_id": (
                "10000000-0000-4000-8000-0000000000b3"
            ),
            "special_category_restriction_text_version": restriction_version,
            "special_category_restriction_text_sha256": restriction_sha,
            "special_category_household_consent": True,
            "special_category_household_receipt_id": (
                "10000000-0000-4000-8000-0000000000b4"
            ),
            "special_category_household_text_version": household_version,
            "special_category_household_text_sha256": household_sha,
        },
        context=cp_stack.context(),
    )

    queued = cp_stack.database.query(
        "SELECT id FROM provisioning_jobs WHERE household_id = ?"
        " AND kind = 'email_identity'",
        (cp_stack.household.id,),
    )
    assert queued, "the managed option is gated after all"


# ---------------------------------------------------------------------------
# The switch at the provider call.
#
# Asking at selection is not a kill switch. `select` runs once, then the work
# sits in a queue; an operator flipping `1 -> 0` mid-incident needs the NEXT
# provider call to stop, whichever path reaches it. These cover the three the
# worker has — a fresh `ensure`, a user-triggered `inspect`, and a reclaimed
# job whose lease expired — each enqueued while the option was ON and run after
# it went OFF, which is the ordering that makes the selection check useless.
# ---------------------------------------------------------------------------

PROVIDER_CASES = {
    "gmail_agent": ("google-oauth", "ABROLIA_GMAIL_ENABLED", "gmail"),
    "family_domain": ("nerve-byo-domain", "ABROLIA_BYO_EMAIL_ENABLED", "byo_email"),
}


class RecordingProvisioner:
    """Records every call rather than refusing them.

    The invariant is that the provider is not CALLED, which a provider that
    raises cannot show: a raising stub makes a blocked job and a reached-then-
    rejected job settle the same way, and the test would pass with the guard
    deleted.
    """

    email_public_provider = "gmail"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def ensure(self, request, intent_key):  # noqa: ANN001, ANN201
        self.calls.append("ensure")
        raise ProviderRejected("recording provisioner")

    def inspect(self, stable_ref):  # noqa: ANN001, ANN201
        self.calls.append("inspect")
        return InspectResult(state=InspectState.ABSENT)

    def deprovision(self, external_ref):  # noqa: ANN001, ANN201
        self.calls.append("deprovision")
        return InspectResult(state=InspectState.ABSENT)


def _hold_the_content_restriction(cp_stack, *, now: float) -> None:  # noqa: ANN001
    """Satisfy the Art. 9(2)(a) gate that runs BEFORE this one.

    Without it the worker stops at `content_restriction_receipt_required` and
    never reaches the kill switch, so every assertion below would pass with the
    switch deleted — the earlier gate would be doing all the work.
    """

    # Both purposes a real email provider owes: the S5 restriction and the
    # Art. 9(2)(a) household-content consent. Recording only the first leaves
    # the earlier gate firing on the second, which is how this first read as a
    # working test while proving nothing.
    with cp_stack.database.write() as connection:
        for purpose in (CONTENT_RESTRICTION_PURPOSE, HOUSEHOLD_CONTENT_PURPOSE):
            version, sha256 = consent_version_and_sha(purpose)
            connection.execute(
                "INSERT INTO consent_receipts (id, household_id, account_id, purpose,"
                " text_version, text_sha256, locale, accepted_at, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 'en', ?, ?)",
                (
                    new_id(),
                    cp_stack.household.id,
                    cp_stack.account.id,
                    purpose,
                    version,
                    sha256,
                    now,
                    now,
                ),
            )


def _queue_email_job(
    cp_stack,  # noqa: ANN001
    *,
    provider: str,
    operation: str,
    kind: str = "email_identity",
    now: float,
) -> str:
    """Enqueue the job the way the queue holds it, with the option still ON."""

    workflow = cp_stack.onboarding.workflow_for_household(cp_stack.household.id)
    with cp_stack.jobs.db.write() as connection:
        job_id, _created = cp_stack.jobs.create(
            connection,
            household_id=cp_stack.household.id,
            workflow_id=workflow.id,
            kind=kind,
            operation=operation,
            intent_key=f"{cp_stack.household.id}:kill-switch:{provider}:{operation}:{kind}",
            request={
                "household_id": cp_stack.household.id,
                "stable_ref": f"{cp_stack.household.id}:inbox",
                "resource_type": "email_identity",
                "external_ref": "already-created-at-the-provider",
            },
            provider=provider,
            now=now,
        )
    return job_id


def _expire_the_lease(cp_stack, job_id: str, *, now: float) -> None:  # noqa: ANN001
    """Make the job look like one a dead worker left behind."""

    with cp_stack.jobs.db.write() as connection:
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'running', leased_by = 'dead-worker',"
            " lease_until = ? WHERE id = ?",
            (now - 1.0, job_id),
        )


@pytest.mark.parametrize("option", sorted(PROVIDER_CASES))
@pytest.mark.parametrize("path", ["fresh_ensure", "user_inspect", "reclaimed_inspect"])
def test_disabling_an_option_stops_work_that_is_already_queued(
    cp_stack, monkeypatch: pytest.MonkeyPatch, option: str, path: str
) -> None:
    provider_name, env_name, flag = PROVIDER_CASES[option]
    cp_stack.complete_profile()
    _hold_the_content_restriction(cp_stack, now=1_760_000_000.0)

    # Enqueued while the option is ON — the state the selection check leaves.
    monkeypatch.setenv(env_name, "1")
    now = 1_760_000_000.0
    job_id = _queue_email_job(
        cp_stack,
        provider=provider_name,
        operation="ensure" if path == "fresh_ensure" else "inspect",
        now=now,
    )
    if path == "reclaimed_inspect":
        _expire_the_lease(cp_stack, job_id, now=now)

    # The operator flips the switch. Nothing re-enters `select`.
    monkeypatch.setenv(env_name, "0")

    recorder = RecordingProvisioner()
    registry = synthetic_provider_registry()
    registry.register(provider_name, recorder)
    result = cp_stack.make_worker(providers=registry, now=now + 10).run_once()

    assert result is not None
    assert result.job_id == job_id
    assert result.status == "failed"
    assert result.error_code == f"email_option_disabled:{flag}"
    assert recorder.calls == [], f"provider was called on the {path} path"


@pytest.mark.parametrize("option", sorted(PROVIDER_CASES))
def test_an_enabled_option_still_reaches_its_provider(
    cp_stack, monkeypatch: pytest.MonkeyPatch, option: str
) -> None:
    """The control the blocked cases need.

    Without it every assertion above holds just as well when the worker stops
    for an unrelated reason, and the guard could be deleted without a failure.
    """

    provider_name, env_name, _flag = PROVIDER_CASES[option]
    cp_stack.complete_profile()
    _hold_the_content_restriction(cp_stack, now=1_760_000_000.0)
    monkeypatch.setenv(env_name, "1")

    now = 1_760_000_000.0
    _queue_email_job(cp_stack, provider=provider_name, operation="ensure", now=now)

    recorder = RecordingProvisioner()
    registry = synthetic_provider_registry()
    registry.register(provider_name, recorder)
    cp_stack.make_worker(providers=registry, now=now + 10).run_once()

    assert recorder.calls != [], "the enabled path never reached the provider"


@pytest.mark.parametrize("option", sorted(PROVIDER_CASES))
def test_a_disabled_option_can_still_be_torn_down(
    cp_stack, monkeypatch: pytest.MonkeyPatch, option: str
) -> None:
    """Off must not strand what is already at the provider.

    A switch that blocked teardown would keep alive exactly the external
    resources turning it off is meant to remove — the reasoning
    `_is_shutdown_action` already gives for the consent precondition.
    """

    provider_name, env_name, flag = PROVIDER_CASES[option]
    cp_stack.complete_profile()
    _hold_the_content_restriction(cp_stack, now=1_760_000_000.0)
    monkeypatch.setenv(env_name, "0")

    now = 1_760_000_000.0
    job_id = _queue_email_job(
        cp_stack,
        provider=provider_name,
        operation="deprovision",
        kind="cleanup",
        now=now,
    )

    recorder = RecordingProvisioner()
    registry = synthetic_provider_registry()
    registry.register(provider_name, recorder)
    result = cp_stack.make_worker(providers=registry, now=now + 10).run_once()

    assert result is not None
    assert result.job_id == job_id
    assert result.error_code != f"email_option_disabled:{flag}"
    assert recorder.calls == ["deprovision"] or "deprovision" in recorder.calls


def test_the_two_views_of_the_cut_table_cannot_drift() -> None:
    """Selection and provisioning must gate the same set.

    They are asked in different vocabularies — selection kind vs provider name
    — and were one edit away from being two hand-written maps. If a row is ever
    added to only one, an option is cut from the screen but not from the queue,
    or the reverse.
    """

    assert len(GATED_EMAIL_OPTIONS) == len(CUT_EMAIL_OPTIONS)
    assert len(GATED_EMAIL_PROVIDERS) == len(CUT_EMAIL_OPTIONS)
    assert set(GATED_EMAIL_OPTIONS.values()) == set(GATED_EMAIL_PROVIDERS.values())
    assert set(GATED_EMAIL_OPTIONS) == set(GATED), "selection cases cover every cut option"
    assert set(GATED_EMAIL_PROVIDERS) == {
        provider for provider, _env, _flag in PROVIDER_CASES.values()
    }
    assert "nerve-managed" not in GATED_EMAIL_PROVIDERS
    assert "abrolia_managed" not in GATED_EMAIL_OPTIONS


@pytest.mark.parametrize("option", sorted(PROVIDER_CASES))
def test_operator_reconcile_also_stops_at_a_disabled_option(
    cp_stack, monkeypatch: pytest.MonkeyPatch, option: str
) -> None:
    """`reconcile` is a second entry point, and it calls providers too.

    `run_once` and `_reconcile` each dispatch to `ensure`/`inspect`, so gating
    one leaves the other open. Deleting the guard from `_reconcile` alone is
    invisible to every `run_once` case above.
    """

    provider_name, env_name, flag = PROVIDER_CASES[option]
    cp_stack.complete_profile()
    _hold_the_content_restriction(cp_stack, now=1_760_000_000.0)

    monkeypatch.setenv(env_name, "1")
    now = 1_760_000_000.0
    job_id = _queue_email_job(
        cp_stack, provider=provider_name, operation="inspect", now=now
    )
    # Reconcilable, but NOT quarantined: an ambiguous provider call an operator
    # is re-driving, which is forward work and stays gated.
    with cp_stack.jobs.db.write() as connection:
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'outcome_unknown',"
            " error_code = 'provider_outcome_unknown' WHERE id = ?",
            (job_id,),
        )
    monkeypatch.setenv(env_name, "0")

    recorder = RecordingProvisioner()
    registry = synthetic_provider_registry()
    registry.register(provider_name, recorder)
    result = cp_stack.make_worker(providers=registry, now=now + 10).reconcile(job_id)

    assert result.error_code == f"email_option_disabled:{flag}"
    assert recorder.calls == []


@pytest.mark.parametrize("option", sorted(PROVIDER_CASES))
def test_a_quarantined_job_is_not_blocked_by_a_disabled_option(
    cp_stack, monkeypatch: pytest.MonkeyPatch, option: str
) -> None:
    """Shutdown work must survive the switch going off.

    A job settled `outcome_unknown` with a reconcilable code exists to find and
    tear down whatever the provider may have created. Blocking it on the flag
    would strand that resource with no path to removal — the same reasoning
    `_is_shutdown_action` already carries for the consent precondition, which
    is why this asks through that predicate rather than restating it.
    """

    provider_name, env_name, flag = PROVIDER_CASES[option]
    cp_stack.complete_profile()
    _hold_the_content_restriction(cp_stack, now=1_760_000_000.0)

    monkeypatch.setenv(env_name, "1")
    now = 1_760_000_000.0
    job_id = _queue_email_job(
        cp_stack, provider=provider_name, operation="inspect", now=now
    )
    with cp_stack.jobs.db.write() as connection:
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'outcome_unknown',"
            " error_code = 'withdrawal_requires_reconciliation' WHERE id = ?",
            (job_id,),
        )
    monkeypatch.setenv(env_name, "0")

    recorder = RecordingProvisioner()
    registry = synthetic_provider_registry()
    registry.register(provider_name, recorder)
    result = cp_stack.make_worker(providers=registry, now=now + 10).reconcile(job_id)

    assert result.error_code != f"email_option_disabled:{flag}"
    assert recorder.calls != [], "the switch blocked shutdown work"

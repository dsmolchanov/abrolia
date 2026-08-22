from __future__ import annotations

from dataclasses import replace

import pytest

from control_plane.config import ConfigurationError, ControlPlaneConfig
from control_plane.container import ControlPlaneContainer
from control_plane.db import new_id
from control_plane.models import StepKind
from control_plane.onboarding.contracts import IdempotencyConflict, InvalidTransition
from control_plane.privacy.consent import (
    CONTENT_RESTRICTION_PURPOSE,
    HOUSEHOLD_CONTENT_PURPOSE,
    consent_version_and_sha,
)
from control_plane.privacy.delete import (
    DeletionService,
)
from control_plane.provisioning.contracts import (
    InspectResult,
    InspectState,
)
from control_plane.provisioning.fakes import synthetic_provider_registry
from control_plane.repositories.jobs import requires_reconciliation


def test_production_container_registers_real_nerve_providers_fail_closed(
    tmp_path,
) -> None:
    household_id = "10000000-0000-4000-8000-000000000001"
    config = replace(
        ControlPlaneConfig.for_test(tmp_path),
        real_email_enabled=True,
        real_email_household_allowlist=frozenset({household_id}),
        nerve_base_url="https://nerve.example.test",
        nerve_admin_key="synthetic-admin-key",
        nerve_platform_org_id="20000000-0000-4000-8000-000000000001",
        nerve_platform_domain_id="30000000-0000-4000-8000-000000000001",
    )

    with ControlPlaneContainer.build(config) as container:
        assert container.providers.health() == {
            "dry-run-runtime": "configured",
            "fake-channel": "configured",
            "fake-cleanup": "configured",
            "fake-email": "configured",
            "fake-whatsapp": "configured",
            "google-oauth": "configured",
            "nerve-byo-domain": "configured",
            "nerve-managed": "configured",
        }
        assert container.onboarding.email_provider == "nerve-managed"
        assert container.onboarding.byo_domain_provider == "nerve-byo-domain"
        assert container.onboarding.allow_real_email_domains



def test_the_real_email_brake_keeps_the_adapters_teardown_needs(tmp_path) -> None:
    """The brake stops new work. It must not strand what already exists.

    This asserted the opposite until 2026-08-22: that the brake removing both
    Nerve adapters was the point. It is the defect. Teardown resolves a provider
    by the job's durable `provider` column, so after
    enable -> provision -> disable -> restart, every cleanup and reconcile hit
    `ProviderRejected` and `DeletionService` recorded `unknown` — a household
    deletion that could never complete, with the external inbox still live.

    So registration follows CREDENTIALS, and the brake lives at forward
    dispatch, where shutdown work can be exempted.
    """

    household_id = "10000000-0000-4000-8000-000000000002"
    braked = replace(
        ControlPlaneConfig.for_test(tmp_path),
        real_email_enabled=False,
        real_email_household_allowlist=frozenset({household_id}),
        nerve_base_url="https://nerve.example.test",
        nerve_admin_key="synthetic-admin-key",
        nerve_platform_org_id="20000000-0000-4000-8000-000000000001",
        nerve_platform_domain_id="30000000-0000-4000-8000-000000000001",
    )

    with ControlPlaneContainer.build(braked) as container:
        # Present, so a cleanup job queued before the brake can still resolve
        # the provider it names and actually delete the inbox.
        assert container.providers.get("nerve-managed") is not None
        assert container.providers.get("nerve-byo-domain") is not None

        # But NEW work still routes synthetically, so the brake means what the
        # runbook says: no fresh Nerve provisioning.
        assert container.onboarding.email_provider == "fake-email"
        assert container.onboarding.byo_domain_provider == "fake-email"
        assert not container.onboarding.allow_real_email_domains


def test_unconfigured_nerve_registers_no_adapter(tmp_path) -> None:
    """Credentials, not the brake, decide whether the adapters exist.

    Without them there is nothing to construct — and nothing to tear down
    either, since no job can have been provisioned through an adapter that was
    never there.
    """

    config = ControlPlaneConfig.for_test(tmp_path)
    assert not config.nerve_configured

    with ControlPlaneContainer.build(config) as container:
        health = container.providers.health()
        assert "nerve-managed" not in health
        assert "nerve-byo-domain" not in health


def test_real_email_rollout_rejects_non_allowlisted_household(cp_stack) -> None:
    cp_stack.complete_profile()
    cp_stack.service.real_email_enabled = True
    # The container derives the providers from this flag; a test that moves
    # only the flag builds a configuration production cannot have.
    cp_stack.service.email_provider = "nerve-managed"
    cp_stack.service.real_email_household_allowlist = frozenset()

    with pytest.raises(InvalidTransition, match="not enabled for this household"):
        cp_stack.service.select(
            cp_stack.household.id,
            StepKind.EMAIL,
            {"kind": "abrolia_managed", "local_part": "family-agent"},
            context=cp_stack.context(),
        )


def test_real_email_rollout_requires_content_restriction_receipt(cp_stack) -> None:
    cp_stack.complete_profile()
    cp_stack.service.real_email_enabled = True
    cp_stack.service.email_provider = "nerve-managed"
    cp_stack.service.real_email_household_allowlist = frozenset({
        cp_stack.household.id
    })

    with pytest.raises(InvalidTransition, match="content restriction receipt"):
        cp_stack.service.select(
            cp_stack.household.id,
            StepKind.EMAIL,
            {"kind": "abrolia_managed", "local_part": "family-agent"},
            context=cp_stack.context(),
        )

    with pytest.raises(InvalidTransition, match="content restriction receipt"):
        cp_stack.service.select(
            cp_stack.household.id,
            StepKind.EMAIL,
            {
                "kind": "abrolia_managed",
                "local_part": "family-agent",
                "special_category_restriction_receipt_id": "10000000-0000-4000-8000-000000000005",
            },
            context=cp_stack.context(),
        )


def test_real_email_rollout_does_not_route_gmail_to_nerve(
    cp_stack, monkeypatch
) -> None:
    # `gmail_agent` is behind a fail-closed kill switch, so a test that selects
    # it turns it on. The routing contract below is what is under test here;
    # the switch itself is asserted in `test_email_option_flags.py`.
    monkeypatch.setenv("ABROLIA_GMAIL_ENABLED", "1")
    cp_stack.complete_profile()
    cp_stack.service.real_email_enabled = True
    cp_stack.service.gmail_provider = "google-oauth"
    cp_stack.service.real_email_household_allowlist = frozenset({
        cp_stack.household.id
    })

    restriction_version, restriction_sha = consent_version_and_sha(
        "special_category_content_restriction"
    )
    consent_version, consent_sha = consent_version_and_sha(
        "special_category_household_content"
    )
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        {
            "kind": "gmail_agent",
            "separate_agent_account_acknowledged": True,
            "special_category_restriction_acknowledged": True,
            "special_category_restriction_receipt_id": "10000000-0000-4000-8000-000000000006",
            "special_category_restriction_text_version": restriction_version,
            "special_category_restriction_text_sha256": restriction_sha,
            "special_category_household_consent": True,
            "special_category_household_receipt_id": "10000000-0000-4000-8000-000000000016",
            "special_category_household_text_version": consent_version,
            "special_category_household_text_sha256": consent_sha,
        },
        context=cp_stack.context(),
    )

    job = cp_stack.database.query_one(
        "SELECT id, provider FROM provisioning_jobs WHERE kind = 'email_identity'"
    )
    assert job["provider"] == "google-oauth"
    provider_selection = cp_stack.jobs.request(job["id"])["selection"]
    assert provider_selection == {
        "kind": "gmail_agent",
        "separate_agent_account_acknowledged": True,
    }
    receipts = cp_stack.database.query(
        "SELECT purpose, text_version FROM consent_receipts"
        " WHERE household_id = ? ORDER BY purpose",
        (cp_stack.household.id,),
    )
    assert [dict(receipt) for receipt in receipts] == [
        {
            "purpose": "special_category_content_restriction",
            "text_version": consent_version_and_sha(
            "special_category_content_restriction"
        )[0],
        },
        {
            "purpose": "special_category_household_content",
            "text_version": consent_version_and_sha(
            "special_category_household_content"
        )[0],
        },
    ]

    with cp_stack.database.write() as connection:
        connection.execute(
            "UPDATE consent_receipts SET text_sha256 = ? WHERE id = ?",
            ("0" * 64, "10000000-0000-4000-8000-000000000006"),
        )
        with pytest.raises(IdempotencyConflict):
            cp_stack.service._record_email_consent_receipt(
                connection,
                parsed={
                    "special_category_restriction_acknowledged": True,
                    "special_category_restriction_receipt_id": (
                        "10000000-0000-4000-8000-000000000006"
                    ),
                    "special_category_restriction_text_version": restriction_version,
                    "special_category_restriction_text_sha256": restriction_sha,
                },
                household_id=cp_stack.household.id,
                account_id=cp_stack.account.id,
                now=10.0,
                purpose="special_category_content_restriction",
                prefix="special_category_restriction",
                accepted_field="special_category_restriction_acknowledged",
                mismatch_error="restriction text version does not match",
            )


# ---------------------------------------------------------------------------
# enable -> provision -> disable -> tear down.
#
# The path the brake used to break. Each stage below is the one that failed:
# the worker's cleanup could not resolve a provider, `reconcile` could not
# settle an uncertain one, and the deletion sweep swallowed `ProviderRejected`
# into `unknown` so a household deletion never completed while its inbox
# stayed live.
# ---------------------------------------------------------------------------

def _hold_both_consents(cp_stack, *, now: float) -> None:  # noqa: ANN001
    """Satisfy the Art. 9(2)(a) gates that run BEFORE the brake.

    Without them the worker settles at `content_restriction_receipt_required`
    and never reaches the brake, so a "braked" assertion would be measuring the
    consent gate instead.
    """

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


NERVE_PROVIDERS = ["nerve-managed", "nerve-byo-domain"]


class _RecordingNerve:
    """Records teardown and refuses forward work.

    Forward calls raise, because if the brake ever lets one through that has to
    be a failure and not a silently-recorded success.
    """

    email_public_provider = "nerve"

    def __init__(self) -> None:
        self.torn_down: list[str] = []

    def ensure(self, request, intent_key):  # noqa: ANN001, ANN201
        raise AssertionError("forward work ran while the brake was on")

    def inspect(self, stable_ref):  # noqa: ANN001, ANN201
        raise AssertionError("forward work ran while the brake was on")

    def deprovision(self, external_ref):  # noqa: ANN001, ANN201
        self.torn_down.append(external_ref)
        return InspectResult(state=InspectState.ABSENT)


@pytest.mark.parametrize("provider", NERVE_PROVIDERS)
def test_teardown_still_reaches_the_provider_after_the_brake_goes_on(
    cp_stack, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    """Worker cleanup, with the brake on and the resource already created."""

    cp_stack.complete_profile()
    _hold_both_consents(cp_stack, now=1_760_000_000.0)
    monkeypatch.setenv("ABROLIA_REAL_EMAIL_ENABLED", "0")

    now = 1_760_000_000.0
    workflow = cp_stack.onboarding.workflow_for_household(cp_stack.household.id)
    with cp_stack.jobs.db.write() as connection:
        job_id, _created = cp_stack.jobs.create(
            connection,
            household_id=cp_stack.household.id,
            workflow_id=workflow.id,
            kind="cleanup",
            operation="deprovision",
            intent_key=f"{cp_stack.household.id}:brake-teardown:{provider}",
            request={
                "household_id": cp_stack.household.id,
                "resource_type": "email_identity",
                "external_ref": "nerve:org:created-before-the-brake",
                "stable_ref": f"{cp_stack.household.id}:inbox",
            },
            provider=provider,
            now=now,
        )

    recorder = _RecordingNerve()
    registry = synthetic_provider_registry()
    registry.register(provider, recorder)
    result = cp_stack.make_worker(providers=registry, now=now + 10).run_once()

    assert result is not None and result.job_id == job_id
    assert result.error_code != "real_email_disabled", (
        "the brake blocked teardown, stranding the external resource"
    )
    assert recorder.torn_down == ["nerve:org:created-before-the-brake"]


@pytest.mark.parametrize("provider", NERVE_PROVIDERS)
def test_forward_work_is_braked_while_teardown_is_not(
    cp_stack, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    """The two halves in one place, since the fix is that they differ.

    Registration cannot carry the brake any more — the adapters must stay
    resolvable for teardown — so this is the only thing standing between a
    queued Nerve job and a call to Nerve.
    """

    cp_stack.complete_profile()
    _hold_both_consents(cp_stack, now=1_760_000_000.0)
    monkeypatch.setenv("ABROLIA_REAL_EMAIL_ENABLED", "0")

    now = 1_760_000_000.0
    workflow = cp_stack.onboarding.workflow_for_household(cp_stack.household.id)
    with cp_stack.jobs.db.write() as connection:
        job_id, _created = cp_stack.jobs.create(
            connection,
            household_id=cp_stack.household.id,
            workflow_id=workflow.id,
            kind="email_identity",
            operation="ensure",
            intent_key=f"{cp_stack.household.id}:brake-forward:{provider}",
            request={
                "household_id": cp_stack.household.id,
                "stable_ref": f"{cp_stack.household.id}:inbox",
            },
            provider=provider,
            now=now,
        )

    recorder = _RecordingNerve()
    registry = synthetic_provider_registry()
    registry.register(provider, recorder)
    result = cp_stack.make_worker(providers=registry, now=now + 10).run_once()

    assert result is not None and result.job_id == job_id
    assert result.error_code == "real_email_disabled"
    assert recorder.torn_down == []


@pytest.mark.parametrize("provider", NERVE_PROVIDERS)
def test_household_deletion_completes_with_the_brake_on(
    cp_stack, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    """The GDPR end of the same defect.

    `DeletionService` resolves each external resource by its stored `provider`
    name and wraps the call in a bare `except Exception`, so an unregistered
    adapter did not raise — it was recorded as `unknown`, the sweep settled
    `outcome_unknown`, and the erasure request could never complete while the
    inbox it was supposed to remove stayed live. A silent partial failure, which
    is the worst shape for this particular obligation.
    """

    monkeypatch.setenv("ABROLIA_REAL_EMAIL_ENABLED", "0")
    now = 1_760_000_000.0
    external_ref = "nerve:org:created-before-the-brake"

    with cp_stack.database.write() as connection:
        encrypted = cp_stack.jobs.encrypt_json(
            "external_resources", "res-brake", "external_id", external_ref
        )
        connection.execute(
            "INSERT INTO external_resources (id, household_id, provider, resource_type,"
            " stable_name, external_id_ciphertext, encryption_key_version, status,"
            " created_at, updated_at)"
            " VALUES (?, ?, ?, 'email_identity', 'inbox', ?, ?, 'ready', ?, ?)",
            (
                "res-brake",
                cp_stack.household.id,
                provider,
                encrypted.ciphertext,
                encrypted.key_version,
                now,
                now,
            ),
        )

    recorder = _RecordingNerve()
    registry = synthetic_provider_registry()
    registry.register(provider, recorder)

    deletion = DeletionService(
        cp_stack.accounts,
        cp_stack.auth,
        cp_stack.households,
        cp_stack.jobs,
        registry,
        runtime=_AbsentRuntime(),
    )
    receipt = deletion.delete(
        cp_stack.account.id,
        cp_stack.household.id,
        idempotency_key=f"brake-delete-{provider}",
        now=now + 10,
    )

    assert recorder.torn_down == [external_ref], (
        "the brake stopped erasure from reaching the provider"
    )
    assert receipt.completion_status == "complete", (
        f"erasure could not complete: {receipt.completion_status} "
        f"{receipt.provider_statuses}"
    )
    assert all(
        state == InspectState.ABSENT.value
        for state in receipt.provider_statuses.values()
    ), receipt.provider_statuses


class _AbsentRuntime:
    def delete(self, runtime_ref: str) -> InspectState:  # noqa: ARG002
        return InspectState.ABSENT


class _MutatingInspectNerve:
    """An adapter whose `inspect` is a recovery path, like the real one.

    `NerveManagedEmailProvisioner.inspect` reissues the API key and rotates the
    webhook — by design, for recovery. Nothing in the `Provisioner` protocol
    says `inspect` is read-only, so a teardown that probes with it is one
    adapter away from handing a withdrawn household fresh live credentials.
    """

    email_public_provider = "nerve"

    def __init__(self) -> None:
        self.torn_down: list[str] = []
        self.credentials_reissued = 0

    def ensure(self, request, intent_key):  # noqa: ANN001, ANN201
        raise AssertionError("forward work ran during teardown")

    def inspect(self, stable_ref):  # noqa: ANN001, ANN201
        self.credentials_reissued += 1
        return InspectResult(state=InspectState.ABSENT)

    def deprovision(self, external_ref):  # noqa: ANN001, ANN201
        self.torn_down.append(external_ref)
        return InspectResult(state=InspectState.ABSENT)


@pytest.mark.parametrize("provider", NERVE_PROVIDERS)
@pytest.mark.parametrize("origin", ["withdrawal", "reset"])
def test_reconciling_a_cleanup_tears_down_instead_of_probing(
    cp_stack, monkeypatch: pytest.MonkeyPatch, provider: str, origin: str
) -> None:
    """An uncertain teardown is re-run, never inspected.

    The path: the first `deprovision` returns unknown, the job is quarantined,
    an operator calls `reconcile`. That branch used to probe with `inspect` for
    everything except runtime resources — so for `nerve-managed` it reissued the
    key and rotated the webhook on an inbox that was supposed to be going away.
    """

    cp_stack.complete_profile()
    _hold_both_consents(cp_stack, now=1_760_000_000.0)
    monkeypatch.setenv("ABROLIA_REAL_EMAIL_ENABLED", "0")

    now = 1_760_000_000.0
    external_ref = "nerve:org:created-before-the-withdrawal"
    workflow = cp_stack.onboarding.workflow_for_household(cp_stack.household.id)
    with cp_stack.jobs.db.write() as connection:
        job_id, _created = cp_stack.jobs.create(
            connection,
            household_id=cp_stack.household.id,
            workflow_id=workflow.id,
            kind="cleanup",
            operation="deprovision",
            intent_key=f"{cp_stack.household.id}:reconcile-teardown:{provider}:{origin}",
            request={
                "household_id": cp_stack.household.id,
                "resource_type": "email_identity",
                "external_ref": external_ref,
                "stable_ref": f"{cp_stack.household.id}:inbox",
            },
            provider=provider,
            now=now,
        )
        # The first deprovision did not answer, so the job was quarantined.
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'outcome_unknown',"
            " error_code = ? WHERE id = ?",
            (f"{origin}_requires_reconciliation", job_id),
        )

    recorder = _MutatingInspectNerve()
    registry = synthetic_provider_registry()
    registry.register(provider, recorder)
    cp_stack.make_worker(providers=registry, now=now + 10).reconcile(job_id)

    assert recorder.credentials_reissued == 0, (
        "teardown probed with inspect, which reissues credentials on the real adapter"
    )
    assert recorder.torn_down == [external_ref]


# ---------------------------------------------------------------------------
# Whatever `build` constructs, `validate` must already have vetted.
#
# Registering the adapters on `nerve_configured` widened what the container
# builds without widening what the config checks: brake OFF plus a complete but
# malformed Nerve block passed validation and then raised `ValueError` inside
# `NerveAdminClient`, turning a dormant provider into a startup outage.
# ---------------------------------------------------------------------------

MALFORMED = {
    "plain-http-origin": {"nerve_base_url": "http://nerve.internal"},
    "non-uuid-org": {"nerve_platform_org_id": "not-a-uuid"},
    "non-uuid-domain": {"nerve_platform_domain_id": "not-a-uuid"},
}


def _nerve_block(tmp_path, **overrides):  # noqa: ANN001, ANN201
    settings = {
        "nerve_base_url": "https://nerve.example.test",
        "nerve_admin_key": "synthetic-admin-key",
        "nerve_platform_org_id": "20000000-0000-4000-8000-000000000001",
        "nerve_platform_domain_id": "30000000-0000-4000-8000-000000000001",
    }
    settings.update(overrides)
    return replace(ControlPlaneConfig.for_test(tmp_path), **settings)


@pytest.mark.parametrize("brake_on", [True, False], ids=["brake-on", "brake-off"])
@pytest.mark.parametrize("flaw", sorted(MALFORMED))
def test_malformed_nerve_settings_are_refused_with_the_brake_either_way(
    tmp_path, brake_on: bool, flaw: str
) -> None:
    """`brake-on` is the case that regressed, and the one worth naming.

    It used to be unreachable — the adapters were not built — so the config was
    never vetted and never needed to be. It is reachable now.
    """

    household_id = "10000000-0000-4000-8000-000000000003"
    config = _nerve_block(
        tmp_path,
        real_email_enabled=not brake_on,
        real_email_household_allowlist=frozenset({household_id}),
        **MALFORMED[flaw],
    )

    with pytest.raises(ConfigurationError):
        config.validate()


@pytest.mark.parametrize("brake_on", [True, False], ids=["brake-on", "brake-off"])
def test_a_valid_nerve_block_builds_and_keeps_both_adapters(
    tmp_path, brake_on: bool
) -> None:
    """The other half: refusing malformed settings must not refuse good ones.

    Without this the test above is satisfied by rejecting every Nerve
    configuration, which would take teardown away again.
    """

    household_id = "10000000-0000-4000-8000-000000000004"
    config = _nerve_block(
        tmp_path,
        real_email_enabled=not brake_on,
        real_email_household_allowlist=frozenset({household_id}),
    ).validate()

    with ControlPlaneContainer.build(config) as container:
        assert container.providers.get("nerve-managed") is not None
        assert container.providers.get("nerve-byo-domain") is not None


@pytest.mark.parametrize("provider", NERVE_PROVIDERS)
def test_the_env_brake_cannot_authorize_what_the_config_refused(
    cp_stack, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    """A brake subtracts. It must never add.

    The worker reads `ABROLIA_REAL_EMAIL_ENABLED` live so `1 -> 0` can stop
    queued work without a restart. Read alone, that made `0 -> 1` an
    AUTHORIZATION: a process booted with the brake on — and therefore with an
    allowlist that was never consulted for this household — would dispatch a
    durable Nerve job the moment the variable flipped, bypassing the frozen
    configuration it was built from. The allowlist is enforced where it was
    validated, and no environment variable may route around it.
    """

    cp_stack.complete_profile()
    _hold_both_consents(cp_stack, now=1_760_000_000.0)

    now = 1_760_000_000.0
    workflow = cp_stack.onboarding.workflow_for_household(cp_stack.household.id)
    with cp_stack.jobs.db.write() as connection:
        job_id, _created = cp_stack.jobs.create(
            connection,
            household_id=cp_stack.household.id,
            workflow_id=workflow.id,
            kind="email_identity",
            operation="ensure",
            intent_key=f"{cp_stack.household.id}:unauthorized:{provider}",
            request={
                "household_id": cp_stack.household.id,
                "stable_ref": f"{cp_stack.household.id}:inbox",
            },
            provider=provider,
            now=now,
        )

    # The environment says yes. The configuration this worker was built from
    # never did.
    monkeypatch.setenv("ABROLIA_REAL_EMAIL_ENABLED", "1")
    recorder = _RecordingNerve()
    registry = synthetic_provider_registry()
    registry.register(provider, recorder)
    worker = cp_stack.make_worker(providers=registry, now=now + 10)
    worker.real_email_authorized_households = frozenset()

    result = worker.run_once()

    assert result is not None and result.job_id == job_id
    assert result.error_code == "real_email_disabled"
    assert recorder.torn_down == []


# ---------------------------------------------------------------------------
# Composition cases. Each focused piece below already passed on its own; these
# are the sequences that cross them.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", NERVE_PROVIDERS)
def test_a_household_removed_from_the_allowlist_cannot_still_dispatch(
    cp_stack, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    """Queued while allowlisted, narrowed afterwards.

    Selection checks `real_email_household_allowlist`, but a durable job
    outlives its selection: it carries a provider name and a household id, and
    nothing re-asked. Reducing the boot-time authorization to one global boolean
    meant H1 kept its authorization once the allowlist was narrowed to H2 —
    real email is still enabled *somewhere*, and that was the whole question
    dispatch asked.
    """

    cp_stack.complete_profile()
    _hold_both_consents(cp_stack, now=1_760_000_000.0)
    monkeypatch.setenv("ABROLIA_REAL_EMAIL_ENABLED", "1")

    now = 1_760_000_000.0
    workflow = cp_stack.onboarding.workflow_for_household(cp_stack.household.id)
    with cp_stack.jobs.db.write() as connection:
        job_id, _created = cp_stack.jobs.create(
            connection,
            household_id=cp_stack.household.id,
            workflow_id=workflow.id,
            kind="email_identity",
            operation="ensure",
            intent_key=f"{cp_stack.household.id}:narrowed:{provider}",
            request={
                "household_id": cp_stack.household.id,
                "stable_ref": f"{cp_stack.household.id}:inbox",
            },
            provider=provider,
            now=now,
        )

    recorder = _RecordingNerve()
    registry = synthetic_provider_registry()
    registry.register(provider, recorder)
    worker = cp_stack.make_worker(providers=registry, now=now + 10)
    # Restarted with real email still on, but the allowlist now names a
    # different household.
    worker.real_email_authorized_households = frozenset(
        {"20000000-0000-4000-8000-0000000000ff"}
    )

    result = worker.run_once()

    assert result is not None and result.job_id == job_id
    assert result.error_code == "real_email_disabled"


@pytest.mark.parametrize("provider", NERVE_PROVIDERS)
def test_membership_is_what_the_brake_asks(
    cp_stack, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    """The control, asked of the predicate rather than of a whole dispatch.

    Driving `run_once` proves the negative well — a braked job stops before any
    provider call whatever its shape. It is a poor control: the two Nerve
    providers need different request fields, so "no provider call" is satisfied
    by any unrelated early return, and the test would pass with the allowlist
    check deleted. The predicate is the thing the finding is about, so ask it.
    """

    cp_stack.complete_profile()
    _hold_both_consents(cp_stack, now=1_760_000_000.0)
    monkeypatch.setenv("ABROLIA_REAL_EMAIL_ENABLED", "1")
    # `nerve-byo-domain` answers to the per-option switch as well, and that one
    # is checked first. This test is about the allowlist, so the other gate is
    # opened rather than measured.
    monkeypatch.setenv("ABROLIA_BYO_EMAIL_ENABLED", "1")

    now = 1_760_000_000.0
    workflow = cp_stack.onboarding.workflow_for_household(cp_stack.household.id)
    with cp_stack.jobs.db.write() as connection:
        job_id, _created = cp_stack.jobs.create(
            connection,
            household_id=cp_stack.household.id,
            workflow_id=workflow.id,
            kind="email_identity",
            operation="ensure",
            intent_key=f"{cp_stack.household.id}:membership:{provider}",
            request={"household_id": cp_stack.household.id},
            provider=provider,
            now=now,
        )
    job = cp_stack.jobs.get(job_id)
    worker = cp_stack.make_worker(now=now + 10)

    worker.real_email_authorized_households = frozenset({cp_stack.household.id})
    assert worker._blocked_by_email_kill_switch(job) is None

    worker.real_email_authorized_households = frozenset(
        {"20000000-0000-4000-8000-0000000000ff"}
    )
    assert worker._blocked_by_email_kill_switch(job) == "real_email_disabled"

    # And the environment still subtracts from membership rather than adding.
    worker.real_email_authorized_households = frozenset({cp_stack.household.id})
    monkeypatch.setenv("ABROLIA_REAL_EMAIL_ENABLED", "0")
    assert worker._blocked_by_email_kill_switch(job) == "real_email_disabled"


class _ReconcilableNerve(_RecordingNerve):
    """Has `reconcile`, as all three real email adapters do.

    A stub without it falls past the shutdown guard to a tail that inspects, so
    omitting it would test a path production never takes.
    """

    def reconcile(self, request, idempotency_key):  # noqa: ANN001, ANN201
        raise AssertionError("shutdown work must never resume provisioning")


@pytest.mark.parametrize("provider", NERVE_PROVIDERS)
def test_deletion_can_tear_down_a_job_the_brake_settled(
    cp_stack, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    """Brake -> erase -> reconcile must reach teardown.

    The kill switch settles a reclaimed job `outcome_unknown/real_email_disabled`
    to keep it reconcilable. Deletion cancels only `pending` and `waiting_user`
    — correctly, since an ambiguous job may have created something upstream —
    so that job survived, and `reconcile` re-applied the brake to it forever.
    Erasure could never reach the provider for exactly the resource whose
    creation was uncertain: the sweep recorded `unknown` and the inbox stayed
    live with no path to removal.

    The fix is NOT to give the brake's own error code the reconciliation
    suffix — that code is written for live households too, and suffixing it
    would exempt every braked job from its own brake. Deletion reclassifies,
    and only its own household's jobs.
    """

    cp_stack.complete_profile()
    _hold_both_consents(cp_stack, now=1_760_000_000.0)

    now = 1_760_000_000.0
    external_ref = "nerve:org:created-before-the-brake"
    workflow = cp_stack.onboarding.workflow_for_household(cp_stack.household.id)
    with cp_stack.jobs.db.write() as connection:
        job_id, _created = cp_stack.jobs.create(
            connection,
            household_id=cp_stack.household.id,
            workflow_id=workflow.id,
            kind="email_identity",
            operation="ensure",
            intent_key=f"{cp_stack.household.id}:erase-braked:{provider}",
            request={
                "household_id": cp_stack.household.id,
                "stable_ref": f"{cp_stack.household.id}:inbox",
                "external_ref": external_ref,
                "email_identity_id": "40000000-0000-4000-8000-000000000001",
                "option": "managed_abrolia",
                "selection": {"kind": "abrolia_managed", "local_part": "family-agent"},
            },
            provider=provider,
            now=now,
        )
        # Reclaimed from a dead lease: it may have reached the provider.
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'running', leased_by = 'dead-worker',"
            " lease_until = ? WHERE id = ?",
            (now - 1.0, job_id),
        )

        # A ready secret namespace, as any job that reached a provider would
        # have. Without it the email reconcile path returns
        # `secret_namespace_not_ready` before the shutdown check, and the test
        # would exercise a state production does not reach.
        namespace = cp_stack.jobs.encrypt_json(
            "external_resources", "ns-erase", "external_id", "synthetic-namespace"
        )
        connection.execute(
            "INSERT INTO external_resources (id, household_id, provider, resource_type,"
            " stable_name, external_id_ciphertext, encryption_key_version, status,"
            " created_at, updated_at)"
            " VALUES ('ns-erase', ?, 'dry-run-runtime', 'secret_namespace', 'ns', ?, ?,"
            " 'ready', ?, ?)",
            (
                cp_stack.household.id,
                namespace.ciphertext,
                namespace.key_version,
                now,
                now,
            ),
        )

    monkeypatch.setenv("ABROLIA_REAL_EMAIL_ENABLED", "0")
    recorder = _ReconcilableNerve()
    registry = synthetic_provider_registry()
    registry.register(provider, recorder)

    braked = cp_stack.make_worker(providers=registry, now=now + 10).run_once()
    assert braked.status == "outcome_unknown"
    assert braked.error_code == "real_email_disabled"

    deletion = DeletionService(
        cp_stack.accounts,
        cp_stack.auth,
        cp_stack.households,
        cp_stack.jobs,
        registry,
        runtime=_AbsentRuntime(),
    )
    deletion.delete(
        cp_stack.account.id,
        cp_stack.household.id,
        idempotency_key=f"erase-braked-{provider}",
        now=now + 20,
    )

    # The job keeps the code it had. Ownership is the HOUSEHOLD's durable
    # status, so nothing had to be stamped onto the job and the quarantine
    # reason an operator was given survives.
    kept = cp_stack.jobs.get(job_id)
    assert kept is not None
    assert kept.error_code == "real_email_disabled"
    assert cp_stack.database.query_one(
        "SELECT status FROM households WHERE id = ?", (cp_stack.household.id,)
    )["status"] in {"deleting", "deleted"}

    cp_stack.make_worker(providers=registry, now=now + 30).reconcile(job_id)

    # The observable is the teardown, not the code. Reconciliation keeps the
    # job's own quarantine reason — ownership is the household's status, so
    # nothing is stamped onto the job — which means "was it braked?" cannot be
    # read off the error code. What distinguishes braked from reconciled is
    # whether anything was scheduled to remove the inbox.

    # Teardown is ARRANGED from durable state, never by asking the provider:
    # `inspect` reissues keys on the real Nerve adapter.
    # `_RecordingNerve.ensure`/`inspect` raise, so reaching either would have
    # surfaced as a failure above rather than silently here.
    assert recorder.torn_down == []
    cleanup = cp_stack.database.query_one(
        "SELECT id FROM provisioning_jobs WHERE household_id = ?"
        " AND kind = 'cleanup' ORDER BY created_at DESC LIMIT 1",
        (cp_stack.household.id,),
    )
    assert cleanup is not None, "erasure scheduled nothing to remove the inbox"


def test_the_brake_code_alone_is_never_reconciliation_work() -> None:
    """The trap the fix had to avoid.

    Suffixing `real_email_disabled` would have been one line and would have
    exempted every braked job — live households included — from its own brake.
    Ownership is asked of the household being erased instead, so the brake's own
    code never becomes reconciliation work for anyone.
    """

    assert not requires_reconciliation("real_email_disabled")


def test_the_container_authorizes_nobody_while_the_brake_is_on(tmp_path) -> None:
    """The allowlist is only an authorization when real email is enabled.

    Nothing forbids a configuration that carries an allowlist with the brake on
    — `validate` requires a non-empty one only when real email is enabled. If
    the container handed that list through regardless, the live environment
    going `0 -> 1` would authorize those households against a configuration that
    never did, which is the brake adding rather than subtracting.
    """

    household_id = "10000000-0000-4000-8000-000000000005"
    braked = _nerve_block(
        tmp_path,
        real_email_enabled=False,
        real_email_household_allowlist=frozenset({household_id}),
    ).validate()

    with ControlPlaneContainer.build(braked) as container:
        assert container.worker.real_email_authorized_households == frozenset()


def test_deletion_reclassifies_only_its_own_households_jobs(
    cp_stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trap named in review, asserted.

    Marking ambiguity as shutdown work is what lets erasure reach teardown past
    the brake. Doing it for every braked job — rather than only for the
    household being erased — would exempt live households from their own brake,
    turning one household's deletion into a global release.
    """

    now = 1_760_000_000.0
    cp_stack.complete_profile()
    _hold_both_consents(cp_stack, now=now)

    # A second household with its own braked, ambiguous job.
    other_account = cp_stack.accounts.create_verified("other@family.test", now=now)
    other = cp_stack.households.create_for_owner(other_account.id, now=now)
    other_workflow = cp_stack.onboarding.workflow_for_household(other.id)
    with cp_stack.jobs.db.write() as connection:
        other_job, _created = cp_stack.jobs.create(
            connection,
            household_id=other.id,
            workflow_id=other_workflow.id,
            kind="email_identity",
            operation="ensure",
            intent_key=f"{other.id}:live-braked",
            request={"household_id": other.id},
            provider="nerve-managed",
            now=now,
        )
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'outcome_unknown',"
            " error_code = 'real_email_disabled' WHERE id = ?",
            (other_job,),
        )

    registry = synthetic_provider_registry()
    registry.register("nerve-managed", _ReconcilableNerve())
    DeletionService(
        cp_stack.accounts,
        cp_stack.auth,
        cp_stack.households,
        cp_stack.jobs,
        registry,
        runtime=_AbsentRuntime(),
    ).delete(
        cp_stack.account.id,
        cp_stack.household.id,
        idempotency_key="scoped-delete",
        now=now + 20,
    )

    untouched = cp_stack.jobs.get(other_job)
    assert untouched is not None
    assert untouched.error_code == "real_email_disabled", (
        "deleting one household released another household's braked job"
    )
    assert not requires_reconciliation(untouched.error_code)
    assert cp_stack.database.query_one(
        "SELECT status FROM households WHERE id = ?", (other.id,)
    )["status"] not in {"deleting", "deleted"}


@pytest.mark.parametrize("provider", NERVE_PROVIDERS)
@pytest.mark.parametrize(
    "prior_code",
    ["real_email_disabled", "withdrawal_requires_reconciliation", None],
    ids=["braked", "already-quarantined", "ambiguous-after-deletion"],
)
def test_erasure_owns_every_ambiguous_job_however_it_got_there(
    cp_stack, monkeypatch: pytest.MonkeyPatch, provider: str, prior_code: str | None
) -> None:
    """Ownership cannot depend on when, or on what the job already said.

    An earlier fix had deletion stamp a code onto each ambiguous job in one
    transaction. That missed two ordinary siblings: a job already carrying
    `withdrawal_requires_reconciliation` was skipped by the guard against
    overwriting it, and a `running` provider call that timed out AFTER that
    statement was never stamped at all. Both then reconciled down the ordinary
    path, hit `secret_namespace_not_ready` — deletion having swept the namespace
    — and stayed there, inbox live and erasure permanently incomplete.

    `ambiguous-after-deletion` is that second sequence: the job becomes
    ambiguous only once the household is already `deleting`.
    """

    now = 1_760_000_000.0
    cp_stack.complete_profile()
    _hold_both_consents(cp_stack, now=now)

    external_ref = "nerve:org:created-before-the-brake"
    workflow = cp_stack.onboarding.workflow_for_household(cp_stack.household.id)
    with cp_stack.jobs.db.write() as connection:
        job_id, _created = cp_stack.jobs.create(
            connection,
            household_id=cp_stack.household.id,
            workflow_id=workflow.id,
            kind="email_identity",
            operation="ensure",
            intent_key=f"{cp_stack.household.id}:owned:{provider}:{prior_code}",
            request={
                "household_id": cp_stack.household.id,
                "stable_ref": f"{cp_stack.household.id}:inbox",
                "external_ref": external_ref,
            },
            provider=provider,
            now=now,
        )
        if prior_code is not None:
            connection.execute(
                "UPDATE provisioning_jobs SET status = 'outcome_unknown',"
                " error_code = ? WHERE id = ?",
                (prior_code, job_id),
            )
        else:
            # Still in flight when erasure begins.
            connection.execute(
                "UPDATE provisioning_jobs SET status = 'running',"
                " leased_by = 'worker', lease_until = ? WHERE id = ?",
                (now + 600, job_id),
            )

    monkeypatch.setenv("ABROLIA_REAL_EMAIL_ENABLED", "0")
    recorder = _ReconcilableNerve()
    registry = synthetic_provider_registry()
    registry.register(provider, recorder)

    DeletionService(
        cp_stack.accounts,
        cp_stack.auth,
        cp_stack.households,
        cp_stack.jobs,
        registry,
        runtime=_AbsentRuntime(),
    ).delete(
        cp_stack.account.id,
        cp_stack.household.id,
        idempotency_key=f"owned-{provider}-{prior_code}",
        now=now + 20,
    )

    if prior_code is None:
        # The call times out only now — after the deletion transaction that any
        # one-shot reclassification would already have run.
        with cp_stack.jobs.db.write() as connection:
            connection.execute(
                "UPDATE provisioning_jobs SET status = 'outcome_unknown',"
                " error_code = 'provider_outcome_unknown', lease_until = NULL"
                " WHERE id = ?",
                (job_id,),
            )

    cp_stack.make_worker(providers=registry, now=now + 40).reconcile(job_id)

    cleanup = cp_stack.database.query_one(
        "SELECT id FROM provisioning_jobs WHERE household_id = ?"
        " AND kind = 'cleanup' ORDER BY created_at DESC LIMIT 1",
        (cp_stack.household.id,),
    )
    assert cleanup is not None, (
        "erasure scheduled nothing to remove the inbox for this sequence"
    )
    # Never by asking the provider: `inspect` reissues keys on the real adapter.
    assert recorder.torn_down == []


@pytest.mark.parametrize(
    "provider", ["nerve-managed", "nerve-byo-domain", "google-oauth"]
)
def test_erasure_never_lets_forward_work_past_the_brake(
    cp_stack, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    """lease -> delete -> disable -> resume must not create anything.

    Deletion deliberately leaves `running` jobs alone: they have crossed the
    provider boundary and must be reconciled, not discarded. So a job leased
    just before erasure began is still forward work when the worker resumes.
    Exempting deletion-owned work inside `_blocked_by_email_kill_switch` — which
    `_run_once` shares — let exactly that job call `ensure` with the brake off,
    creating new upstream email state during an erasure.

    The teardown route now sits above the brake in `_reconcile` instead, so the
    exemption reaches only the path that cannot create anything. All three
    forward email routes are covered because the predicate is shared by all of
    them.
    """

    now = 1_760_000_000.0
    cp_stack.complete_profile()
    _hold_both_consents(cp_stack, now=now)
    monkeypatch.setenv("ABROLIA_GMAIL_ENABLED", "1")
    monkeypatch.setenv("ABROLIA_BYO_EMAIL_ENABLED", "1")

    workflow = cp_stack.onboarding.workflow_for_household(cp_stack.household.id)
    with cp_stack.jobs.db.write() as connection:
        job_id, _created = cp_stack.jobs.create(
            connection,
            household_id=cp_stack.household.id,
            workflow_id=workflow.id,
            kind="email_identity",
            operation="ensure",
            intent_key=f"{cp_stack.household.id}:forward-during-erasure:{provider}",
            request={
                "household_id": cp_stack.household.id,
                "stable_ref": f"{cp_stack.household.id}:inbox",
            },
            provider=provider,
            now=now,
        )
        # Leased just before erasure begins, and therefore left alone by it.
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'running', leased_by = 'dead-worker',"
            " lease_until = ? WHERE id = ?",
            (now - 1.0, job_id),
        )

    recorder = _ReconcilableNerve()
    registry = synthetic_provider_registry()
    registry.register(provider, recorder)
    DeletionService(
        cp_stack.accounts,
        cp_stack.auth,
        cp_stack.households,
        cp_stack.jobs,
        registry,
        runtime=_AbsentRuntime(),
    ).delete(
        cp_stack.account.id,
        cp_stack.household.id,
        idempotency_key=f"forward-erasure-{provider}",
        now=now + 20,
    )

    # The operator pulls the brake mid-erasure.
    monkeypatch.setenv("ABROLIA_REAL_EMAIL_ENABLED", "0")
    monkeypatch.setenv("ABROLIA_GMAIL_ENABLED", "0")
    monkeypatch.setenv("ABROLIA_BYO_EMAIL_ENABLED", "0")

    result = cp_stack.make_worker(providers=registry, now=now + 30).run_once()

    # `_ReconcilableNerve.ensure` raises, so reaching it would fail loudly here
    # rather than pass quietly.
    assert result is None or result.job_id != job_id or result.error_code in {
        "real_email_disabled",
        "email_option_disabled:gmail",
        "email_option_disabled:byo_email",
    }, f"forward work ran during erasure with the brake on: {result}"

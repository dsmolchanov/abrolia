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
from control_plane.privacy.delete import DeletionService
from control_plane.provisioning.contracts import InspectResult, InspectState
from control_plane.provisioning.fakes import synthetic_provider_registry


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
    worker.real_email_authorized = False

    result = worker.run_once()

    assert result is not None and result.job_id == job_id
    assert result.error_code == "real_email_disabled"
    assert recorder.torn_down == []

from __future__ import annotations

import json
import threading
from dataclasses import replace

import pytest

from control_plane.config import ConfigurationError, ControlPlaneConfig
from control_plane.container import ControlPlaneContainer
from control_plane.crypto import normalize_email
from control_plane.db import new_id
from control_plane.email.models import (
    EmailDnsPublicStatus,
    EmailGoogleOAuthPublicStatus,
    EmailNerveAttachmentPublicStatus,
)
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
from control_plane.providers.email.nerve_client import (
    email_org_external_ref,
    org_teardown_ref,
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


@pytest.mark.parametrize(
    "provider", ["nerve-managed", "nerve-byo-domain", "google-oauth"]
)
def test_erasure_teardown_reaches_a_terminal_state(
    cp_stack, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    """Scheduling the cleanup is not the same as finishing it.

    `_shutdown_probe` schedules a cleanup; `_cleanup` deprovisions and then
    deletes the provider secret — but only for an identity marked
    `disconnecting`, the state the ordinary disconnect flow sets. Deletion never
    entered that flow, so the cleanup settled
    `outcome_unknown/secret_cleanup_unknown`, the parent job was never settled,
    and `resume` saw unresolved work forever with the provider resource and the
    secret namespace both already gone.
    """

    now = 1_760_000_000.0
    cp_stack.complete_profile()
    _hold_both_consents(cp_stack, now=now)

    identity_id = "40000000-0000-4000-8000-00000000000e"
    with cp_stack.jobs.db.write() as connection:
        connection.execute(
            "INSERT INTO email_identities (id, household_id, option, status,"
            " secret_binding_ref, encryption_key_version, version, created_at, updated_at)"
            " VALUES (?, ?, 'managed_abrolia', 'outcome_unknown', NULL, 'v1', 1, ?, ?)",
            (identity_id, cp_stack.household.id, now, now),
        )

    class _Unresolved(_ReconcilableNerve):
        """Teardown that does not answer, so the deletion cannot complete.

        The completed path purges the household row and cascades the identity
        away, which is the case where nothing is left to settle. The defect
        lives in the OTHER case: deletion stays open, its scheduled cleanup has
        to finish later, and that cleanup needs the identity in a state it can
        act on.
        """

        def deprovision(self, external_ref):  # noqa: ANN001, ANN201
            return InspectResult(state=InspectState.UNKNOWN)

    registry = synthetic_provider_registry()
    registry.register(provider, _Unresolved())
    with cp_stack.jobs.db.write() as connection:
        resource = cp_stack.jobs.encrypt_json(
            "external_resources", "res-terminal", "external_id", "nerve:org:live"
        )
        connection.execute(
            "INSERT INTO external_resources (id, household_id, provider, resource_type,"
            " stable_name, external_id_ciphertext, encryption_key_version, status,"
            " created_at, updated_at)"
            " VALUES ('res-terminal', ?, ?, 'email_identity', 'inbox', ?, ?, 'ready', ?, ?)",
            (
                cp_stack.household.id,
                provider,
                resource.ciphertext,
                resource.key_version,
                now,
                now,
            ),
        )

    receipt = DeletionService(
        cp_stack.accounts,
        cp_stack.auth,
        cp_stack.households,
        cp_stack.jobs,
        registry,
        runtime=_AbsentRuntime(),
    ).delete(
        cp_stack.account.id,
        cp_stack.household.id,
        idempotency_key=f"terminal-{provider}",
        now=now + 20,
    )
    assert receipt.completion_status != "complete", "fixture did not stay open"

    identity = cp_stack.database.query_one(
        "SELECT status FROM email_identities WHERE id = ?", (identity_id,)
    )
    assert identity is not None
    assert identity["status"] == "disconnecting", (
        "erasure never entered the disconnect lifecycle, so the cleanup it"
        " schedules cannot delete the provider secret and never settles"
    )


@pytest.mark.parametrize(
    "provider", ["nerve-managed", "nerve-byo-domain", "google-oauth"]
)
@pytest.mark.parametrize(
    "origin", ["braked", "pre-quarantined", "late-ambiguous"]
)
def test_erasure_sequence_settles_every_job_and_completes_deletion(
    cp_stack, monkeypatch: pytest.MonkeyPatch, provider: str, origin: str
) -> None:
    """The whole erasure-with-brake sequence, driven to the end.

    The terminal-state test above stops at the `disconnecting` transition, and
    so did every test before it: nothing ran the scheduled cleanup, settled the
    parent, or resumed the deletion. This drives every link in order — the
    ambiguous job's origin (braked, pre-quarantined by withdrawal, or a
    timeout discovered after the deletion transaction), deletion, reconcile,
    the cleanup it schedules, and the resume that must now find nothing left.
    """

    now = 1_760_000_000.0
    cp_stack.complete_profile()
    _hold_both_consents(cp_stack, now=now)

    identity_id = "42000000-0000-4000-8000-00000000000a"
    workflow = cp_stack.onboarding.workflow_for_household(cp_stack.household.id)
    with cp_stack.jobs.db.write() as connection:
        connection.execute(
            "INSERT INTO email_identities (id, household_id, option, status,"
            " secret_binding_ref, encryption_key_version, version, created_at, updated_at)"
            " VALUES (?, ?, 'managed_abrolia', 'outcome_unknown',"
            " 'ABROLIA_EMAIL_PROVIDER_KEY', 'v1', 1, ?, ?)",
            (identity_id, cp_stack.household.id, now, now),
        )
        job_id, _created = cp_stack.jobs.create(
            connection,
            household_id=cp_stack.household.id,
            workflow_id=workflow.id,
            kind="email_identity",
            operation="ensure",
            intent_key=f"{cp_stack.household.id}:erasure-sequence:{provider}:{origin}",
            request={
                "household_id": cp_stack.household.id,
                "email_identity_id": identity_id,
                "stable_ref": f"{cp_stack.household.id}:inbox",
                # Deliberately NO `external_ref`: the hard case is a call that
                # timed out without answering, which recorded nothing — so
                # teardown has to DERIVE the reference (`nerve-org:<...>` /
                # `google-oauth:<identity_id>`), not read it.
                "option": "managed_abrolia",
                "selection": {"kind": "abrolia_managed", "local_part": "family-agent"},
            },
            provider=provider,
            now=now,
        )
        if origin == "braked":
            # Reclaimed from a dead lease: the brake preserves ambiguity for
            # exactly this shape, because the call may have reached Nerve.
            connection.execute(
                "UPDATE provisioning_jobs SET status = 'running',"
                " leased_by = 'dead-worker', lease_until = ? WHERE id = ?",
                (now - 1.0, job_id),
            )
        elif origin == "pre-quarantined":
            cp_stack.jobs.settle(
                connection,
                job_id,
                status="outcome_unknown",
                error_code="withdrawal_requires_reconciliation",
                now=now,
            )
        else:  # late-ambiguous
            connection.execute(
                "UPDATE provisioning_jobs SET status = 'running',"
                " leased_by = 'dead-worker', lease_until = ? WHERE id = ?",
                (now - 1.0, job_id),
            )
        resource = cp_stack.jobs.encrypt_json(
            "external_resources", f"res-sequence-{origin}", "external_id", "nerve:org:live"
        )
        connection.execute(
            "INSERT INTO external_resources (id, household_id, provider, resource_type,"
            " stable_name, external_id_ciphertext, encryption_key_version, status,"
            " created_at, updated_at)"
            " VALUES (?, ?, ?, 'email_identity', 'inbox', ?, ?, 'ready', ?, ?)",
            (
                f"res-sequence-{origin}",
                cp_stack.household.id,
                provider,
                resource.ciphertext,
                resource.key_version,
                now,
                now,
            ),
        )

    recorder = _ReconcilableNerve()
    registry = synthetic_provider_registry()
    registry.register(provider, recorder)

    if origin == "braked":
        monkeypatch.setenv("ABROLIA_REAL_EMAIL_ENABLED", "0")
        monkeypatch.setenv("ABROLIA_GMAIL_ENABLED", "0")
        monkeypatch.setenv("ABROLIA_BYO_EMAIL_ENABLED", "0")
        braked = cp_stack.make_worker(providers=registry, now=now + 10).run_once()
        assert braked is not None and braked.job_id == job_id
        assert braked.status == "outcome_unknown", braked
        assert braked.error_code in {
            "real_email_disabled",
            "email_option_disabled:gmail",
            "email_option_disabled:byo_email",
        }

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
        idempotency_key=f"erasure-sequence-{provider}-{origin}",
        now=now + 20,
    )
    assert receipt.completion_status != "complete", "fixture did not stay open"

    if origin == "late-ambiguous":
        # The call times out AFTER the deletion transaction: the case no
        # one-shot reclassification can see and household ownership answers.
        with cp_stack.jobs.db.write() as connection:
            cp_stack.jobs.settle(
                connection,
                job_id,
                status="outcome_unknown",
                error_code="reconcile_inconclusive",
                now=now + 25,
            )

    identity_row = cp_stack.database.query_one(
        "SELECT status FROM email_identities WHERE id = ?", (identity_id,)
    )
    assert identity_row is not None
    assert identity_row["status"] == "disconnecting", (
        "erasure never entered the disconnect lifecycle, so the cleanup it"
        " schedules cannot delete the provider secret and never settles"
    )

    worker = cp_stack.make_worker(providers=registry, now=now + 30)
    worker.reconcile(job_id)
    for _ in range(4):
        if worker.run_once() is None:
            break

    parent = cp_stack.jobs.get(job_id)
    assert parent is not None
    assert (parent.status, parent.error_code) == (
        "cancelled",
        "cancelled_and_compensated",
    ), f"parent never settled: {parent.status}/{parent.error_code}"
    cleanup_row = cp_stack.database.query_one(
        "SELECT status, error_code FROM provisioning_jobs"
        " WHERE household_id = ? AND kind = 'cleanup'",
        (cp_stack.household.id,),
    )
    assert cleanup_row is not None, "reconciliation scheduled nothing"
    assert cleanup_row["status"] == "succeeded", (
        f"cleanup never finished: {cleanup_row['status']}/{cleanup_row['error_code']}"
    )
    expected_ref = (
        f"google-oauth:{identity_id}"
        if provider == "google-oauth"
        else None
    )
    torn = recorder.torn_down
    if expected_ref is not None:
        assert expected_ref in torn, f"teardown used a derived reference; got {torn}"
    else:
        assert any(ref.startswith("nerve-org:") for ref in torn), (
            f"teardown used a derived org reference; got {torn}"
        )

    final = deletion.resume(cp_stack.household.id, now=now + 40)
    assert final.completion_status == "complete", (
        f"deletion never completed: {final.completion_status} {final.provider_statuses}"
    )
    assert (
        cp_stack.database.query_one(
            "SELECT id FROM households WHERE id = ?", (cp_stack.household.id,)
        )
        is None
    ), "a complete deletion left the household row behind"


class _SpySyntheticEmail(DeterministicFakeProvisioner):
    """The real synthetic email provisioner, with its calls counted.

    Shutdown may neither ask the provider what it holds (`inspect`) nor resume
    provisioning (`ensure`): teardown comes from the derived reference alone,
    and the counts prove it.
    """

    def __init__(self) -> None:
        super().__init__("email")
        self.inspected: list[str] = []
        self.torn_down: list[str] = []

    def inspect(self, stable_ref: str) -> InspectResult:
        self.inspected.append(stable_ref)
        return super().inspect(stable_ref)

    def deprovision(self, external_ref: str) -> InspectResult:
        self.torn_down.append(external_ref)
        return super().deprovision(external_ref)


@pytest.mark.parametrize("origin", ["cancel", "reset", "withdrawal", "deletion"])
def test_an_ambiguous_synthetic_job_tears_down_the_reference_it_can_derive(
    cp_stack, origin: str
) -> None:
    """The synthetic contract IS derivable arithmetic — so derive it.

    Every quarantined email job routes to `_shutdown_probe`, but the derived
    reference knew only Google's and Nerve's contracts. The synthetic
    provisioner names its resource `synthetic-email:<identity_id>` — the very
    string `_validate_email_external_ref` enforces at settle time — yet an
    ambiguous synthetic job with nothing recorded repeated its reconciliation
    error forever while the identity stayed held, because derivation refused a
    reference the worker's own validator treats as arithmetic. This drives all
    four shutdown origins through reconcile, the derived-reference cleanup and
    the disconnect lifecycle to terminal states, with the provider never asked.
    """

    now = 1_760_000_000.0
    cp_stack.complete_profile()
    _hold_both_consents(cp_stack, now=now)

    identity_id = "43000000-0000-4000-8000-00000000000c"
    workflow = cp_stack.onboarding.workflow_for_household(cp_stack.household.id)
    quarantine_code = {
        "cancel": "cancel_requires_reconciliation",
        "reset": "reset_requires_reconciliation",
        "withdrawal": "withdrawal_requires_reconciliation",
    }.get(origin)
    with cp_stack.jobs.db.write() as connection:
        connection.execute(
            "INSERT INTO email_identities (id, household_id, option, status,"
            " secret_binding_ref, encryption_key_version, version, created_at, updated_at)"
            " VALUES (?, ?, 'managed_abrolia', 'outcome_unknown', NULL, 'v1', 1, ?, ?)",
            (identity_id, cp_stack.household.id, now, now),
        )
        connection.execute(
            "INSERT INTO email_address_reservations (id, normalized_domain,"
            " normalized_local_part, household_id, email_identity_id, status,"
            " expires_at, created_at)"
            " VALUES (?, 'abrolia.com', 'family-agent', ?, ?, 'held', ?, ?)",
            (f"res-synthetic-{origin}", cp_stack.household.id, identity_id,
             now + 3_600.0, now),
        )
        # No `external_resources` row for the three command origins: the case
        # under test is a call that was accepted and then timed out without
        # answering, which recorded NOTHING durable — that is why teardown has
        # to derive. Erasure is the exception: deletion awaits an outstanding
        # email resource, so it keeps one (the sequence test does the same).
        if origin == "deletion":
            resource = cp_stack.jobs.encrypt_json(
                "external_resources",
                f"res-synthetic-job-{origin}",
                "external_id",
                f"synthetic-email:{identity_id}",
            )
            connection.execute(
                "INSERT INTO external_resources (id, household_id, provider,"
                " resource_type, stable_name, external_id_ciphertext,"
                " encryption_key_version, status, created_at, updated_at)"
                " VALUES (?, ?, 'fake-email', 'email_identity', 'inbox', ?, ?,"
                " 'ready', ?, ?)",
                (
                    f"res-synthetic-job-{origin}",
                    cp_stack.household.id,
                    resource.ciphertext,
                    resource.key_version,
                    now,
                    now,
                ),
            )
        job_id, _created = cp_stack.jobs.create(
            connection,
            household_id=cp_stack.household.id,
            workflow_id=workflow.id,
            kind="email_identity",
            operation="ensure",
            intent_key=f"{cp_stack.household.id}:synthetic-teardown:{origin}",
            request={
                "household_id": cp_stack.household.id,
                "email_identity_id": identity_id,
                "stable_ref": f"{cp_stack.household.id}:inbox",
                # Deliberately NO `external_ref`: the call timed out without
                # answering, which recorded nothing — teardown has to DERIVE
                # the reference, not read it.
                "option": "managed_abrolia",
                "selection": {"kind": "abrolia_managed", "local_part": "family-agent"},
            },
            provider="fake-email",
            now=now,
        )
        if quarantine_code is not None:
            # What the real producers do around the quarantine:
            # `_supersede_unsettled_jobs` stamps the reason, and cancel, reset
            # and withdrawal each begin the disconnect this cleanup finishes.
            connection.execute(
                "UPDATE provisioning_jobs SET status = 'outcome_unknown',"
                " settled_at = ?, updated_at = ?, error_code = ? WHERE id = ?",
                (now, now, quarantine_code, job_id),
            )
            connection.execute(
                "UPDATE email_identities SET status = 'disconnecting',"
                " version = version + 1, updated_at = ? WHERE id = ?",
                (now, identity_id),
            )
        else:  # deletion: ambiguity discovered only after that transaction
            connection.execute(
                "UPDATE provisioning_jobs SET status = 'running',"
                " leased_by = 'dead-worker', lease_until = ? WHERE id = ?",
                (now - 1.0, job_id),
            )

    fake = _SpySyntheticEmail()
    # The whole synthetic registry with the spy standing in for `fake-email`:
    # erasure drives the runtime and cleanup adapters too, so a bare
    # single-name registry leaves their provider statuses unresolved and the
    # deletion never completes.
    base = synthetic_provider_registry()
    registry = ProviderRegistry()
    registry.register("fake-email", fake)
    for name in ("fake-whatsapp", "fake-channel", "fake-cleanup", "dry-run-runtime"):
        registry.register(name, base.get(name))

    if origin == "deletion":
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
            idempotency_key=f"synthetic-teardown-{origin}",
            now=now + 20,
        )
        assert receipt.completion_status != "complete", "fixture did not stay open"
        with cp_stack.jobs.db.write() as connection:
            cp_stack.jobs.settle(
                connection,
                job_id,
                status="outcome_unknown",
                error_code="reconcile_inconclusive",
                now=now + 25,
            )

    worker = cp_stack.make_worker(providers=registry, now=now + 30)
    worker.reconcile(job_id)

    assert fake.inspected == [], "shutdown asked the provider what it holds"
    assert fake.ensure_calls == 0, "shutdown resumed provisioning"

    for _ in range(6):
        if worker.run_once() is None:
            break

    parent = cp_stack.jobs.get(job_id)
    assert parent is not None
    assert (parent.status, parent.error_code) == (
        "cancelled",
        "cancelled_and_compensated",
    ), f"parent never settled: {parent.status}/{parent.error_code}"
    cleanup_row = cp_stack.database.query_one(
        "SELECT status, error_code FROM provisioning_jobs"
        " WHERE household_id = ? AND kind = 'cleanup'",
        (cp_stack.household.id,),
    )
    assert cleanup_row is not None, "reconciliation scheduled nothing"
    assert cleanup_row["status"] == "succeeded", (
        f"cleanup never finished: {cleanup_row['status']}/{cleanup_row['error_code']}"
    )
    assert f"synthetic-email:{identity_id}" in fake.torn_down, (
        f"teardown did not use the derived reference; got {fake.torn_down}"
    )

    if origin == "deletion":
        final = deletion.resume(cp_stack.household.id, now=now + 40)
        assert final.completion_status == "complete", (
            f"deletion never completed: {final.completion_status}"
            f" {final.provider_statuses}"
        )
        assert (
            cp_stack.database.query_one(
                "SELECT id FROM households WHERE id = ?", (cp_stack.household.id,)
            )
            is None
        ), "a complete deletion left the household row behind"
    else:
        identity_row = cp_stack.database.query_one(
            "SELECT status FROM email_identities WHERE id = ?", (identity_id,)
        )
        assert identity_row is not None, "the disconnect lost the identity row"
        assert identity_row["status"] == "deleted", (
            "the disconnect never finished after the derived-reference teardown:"
            f" {identity_row['status']}"
        )
        reservation = cp_stack.database.query_one(
            "SELECT status FROM email_address_reservations WHERE email_identity_id = ?",
            (identity_id,),
        )
        assert reservation is not None, "the reservation row vanished instead of releasing"
        assert reservation["status"] == "released", (
            "the local part stayed held after the inbox finished disconnecting:"
            f" {reservation['status']}"
        )


class _InFlightEmailCall:
    """An adapter parked inside `ensure` until the test releases its gate.

    The run under test: the call leases, erasure begins while it is in
    flight, and only then does the provider answer — the one shape no
    statement of delete() can reach, because the job was `running` when the
    deletion transaction committed. Whatever the contract's waiting shape is —
    a durable reference, a DNS wait that carries none, or a synthetic answer
    forbidden to carry one — erasure must own what comes back.
    """

    def __init__(
        self,
        *,
        public_provider: str,
        entered: threading.Event,
        release: threading.Event,
        public_result: dict,
        waiting_ref: str | None,
    ) -> None:
        self.email_public_provider = public_provider
        self.torn_down: list[str] = []
        self._entered = entered
        self._release = release
        self._public_result = public_result
        self._waiting_ref = waiting_ref

    def ensure(self, request, intent_key):  # noqa: ANN001, ANN201
        self._entered.set()
        if not self._release.wait(timeout=30.0):
            raise AssertionError("the test never released the in-flight call")
        raise ProviderWaiting(
            "provider waits for user action",
            public_result=self._public_result,
            external_ref=self._waiting_ref,
        )

    def deprovision(self, external_ref):  # noqa: ANN001, ANN201
        self.torn_down.append(external_ref)
        return InspectResult(state=InspectState.ABSENT)


def _late_waiting_contract(
    provider: str, *, household_id: str, identity_id: str
) -> dict:
    """Each provider contract's real waiting shape, and what teardown must use.

    Google OAuth and managed Nerve answer `ProviderWaiting` with a durable
    reference (Nerve's being canonical JSON). A BYO-DNS wait carries no
    reference — no org has been named yet. The synthetic validator refuses a
    reference on a waiting answer outright, so its teardown can only ever be
    derived.
    """
    if provider == "google-oauth":
        public = EmailGoogleOAuthPublicStatus(
            state="oauth_required",
            disclosure=(
                "Abrolia reads and sends mail only for this dedicated agent"
                " mailbox; Google data is not used to train a general model."
            ),
        ).model_dump(mode="json", exclude_none=True)
        return {
            "option": "gmail",
            "selection": {"kind": "gmail"},
            "declares": "gmail",
            "public_result": public,
            "waiting_ref": f"google-oauth:{identity_id}",
            "torn_down": f"google-oauth:{identity_id}",
        }
    if provider == "nerve-managed":
        org_id = "46000000-0000-4000-8000-000000000001"
        reference = {
            "household_id": household_id,
            "stable_ref": f"{household_id}:inbox",
            "org_id": org_id,
            "grant_id": "46000000-0000-4000-8000-000000000002",
            "inbox_id": "46000000-0000-4000-8000-000000000003",
            "key_id": "46000000-0000-4000-8000-000000000004",
            "webhook_id": "46000000-0000-4000-8000-000000000005",
            "address": normalize_email("family-agent@abrolia.com"),
            "org_external_ref": email_org_external_ref(household_id, identity_id),
        }
        waiting_ref = json.dumps(reference, sort_keys=True, separators=(",", ":"))
        public = EmailNerveAttachmentPublicStatus.model_validate(
            {
                "nerve_org_id": org_id,
                "operator_action": {
                    "arguments": [
                        "set",
                        "attachments",
                        "--org",
                        org_id,
                        "--enabled=true",
                    ]
                },
            }
        ).model_dump(mode="json", exclude_none=True)
        return {
            "option": "managed_abrolia",
            "selection": {"kind": "abrolia_managed", "local_part": "family-agent"},
            "declares": "nerve",
            "public_result": public,
            "waiting_ref": waiting_ref,
            "torn_down": waiting_ref,
        }
    if provider == "nerve-byo-domain":
        public = EmailDnsPublicStatus.model_validate(
            {
                "domain": "family.test",
                "dns_records": [
                    {
                        "type": "TXT",
                        "host": "_arbolia.family.test",
                        "value": "ownership-token",
                    }
                ],
            }
        ).model_dump(mode="json", exclude_none=True)
        return {
            "option": "own_domain",
            "selection": {"kind": "own_domain", "domain": "family.test"},
            "declares": "nerve",
            "public_result": public,
            "waiting_ref": None,
            # Nothing recorded, so the probe derives the org contract.
            "torn_down": org_teardown_ref(household_id, identity_id),
        }
    assert provider == "fake-email"
    return {
        "option": "managed_abrolia",
        "selection": {"kind": "abrolia_managed", "local_part": "family-agent"},
        "declares": "synthetic",
        "public_result": {},
        "waiting_ref": None,
        "torn_down": f"synthetic-email:{identity_id}",
    }


@pytest.mark.parametrize(
    "provider",
    ["google-oauth", "nerve-managed", "nerve-byo-domain", "fake-email"],
)
def test_a_waiting_answer_arriving_after_erasure_begins_is_still_torn_down(
    cp_stack, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    """The late `ProviderWaiting` race, across every provider contract.

    A call in flight when the deletion transaction commits is invisible to
    every statement delete() runs: the sweep cancels only
    `pending`/`waiting_user`, and `resume` reads only
    `running`/`outcome_unknown`. If the answer that arrives afterwards is a
    `ProviderWaiting`, settling it `waiting_user` strands a live resource
    outside an erasure that then completes. Each contract parks its call on a
    gate until erasure has begun, answers, and the sequence must still end in
    the resource torn down, the parent compensated, and the household gone.
    """

    # The call has to REACH the provider, so the incident brake is lifted for
    # the forward leg (the household itself is allowlisted by the fixture);
    # teardown was never braked anyway.
    monkeypatch.setenv("ABROLIA_REAL_EMAIL_ENABLED", "1")
    monkeypatch.setenv("ABROLIA_GMAIL_ENABLED", "1")
    monkeypatch.setenv("ABROLIA_BYO_EMAIL_ENABLED", "1")

    now = 1_760_000_000.0
    cp_stack.complete_profile()
    _hold_both_consents(cp_stack, now=now)

    identity_id = "45000000-0000-4000-8000-00000000000d"
    workflow = cp_stack.onboarding.workflow_for_household(cp_stack.household.id)
    contract = _late_waiting_contract(
        provider, household_id=cp_stack.household.id, identity_id=identity_id
    )
    with cp_stack.jobs.db.write() as connection:
        # An own-domain identity carries its verified domain's lookup HMAC;
        # every other option leaves it NULL.
        domain_hmac = (
            cp_stack.lookup.digest("email-domain:family.test")
            if provider == "nerve-byo-domain"
            else None
        )
        connection.execute(
            "INSERT INTO email_identities (id, household_id, option, status,"
            " secret_binding_ref, encryption_key_version, version, created_at,"
            " updated_at, domain_lookup_hmac)"
            " VALUES (?, ?, ?, 'active', 'ABROLIA_EMAIL_PROVIDER_KEY', 'v1', 1, ?, ?, ?)",
            (
                identity_id,
                cp_stack.household.id,
                contract["option"],
                now,
                now,
                domain_hmac,
            ),
        )
        connection.execute(
            "INSERT INTO email_address_reservations (id, normalized_domain,"
            " normalized_local_part, household_id, email_identity_id, status,"
            " expires_at, created_at)"
            " VALUES (?, 'abrolia.com', 'family-agent', ?, ?, 'held', ?, ?)",
            (
                f"res-late-waiting-{provider}",
                cp_stack.household.id,
                identity_id,
                now + 3_600.0,
                now,
            ),
        )
        # Erasure awaits an outstanding email resource (the sequence test
        # keeps one for the same reason); its reference is the contract's own,
        # so resume's direct deprovision reaches this very adapter.
        resource = cp_stack.jobs.encrypt_json(
            "external_resources",
            f"res-late-waiting-row-{provider}",
            "external_id",
            contract["torn_down"],
        )
        connection.execute(
            "INSERT INTO external_resources (id, household_id, provider,"
            " resource_type, stable_name, external_id_ciphertext,"
            " encryption_key_version, status, created_at, updated_at)"
            " VALUES (?, ?, ?, 'email_identity', 'inbox', ?, ?, 'ready', ?, ?)",
            (
                f"res-late-waiting-row-{provider}",
                cp_stack.household.id,
                provider,
                resource.ciphertext,
                resource.key_version,
                now,
                now,
            ),
        )
        job_id, _created = cp_stack.jobs.create(
            connection,
            household_id=cp_stack.household.id,
            workflow_id=workflow.id,
            kind="email_identity",
            operation="ensure",
            intent_key=f"{cp_stack.household.id}:late-waiting:{provider}",
            request={
                "household_id": cp_stack.household.id,
                "email_identity_id": identity_id,
                "stable_ref": f"{cp_stack.household.id}:inbox",
                "option": contract["option"],
                "selection": contract["selection"],
            },
            provider=provider,
            now=now,
        )

    entered, release = threading.Event(), threading.Event()
    adapter = _InFlightEmailCall(
        public_provider=contract["declares"],
        entered=entered,
        release=release,
        public_result=contract["public_result"],
        waiting_ref=contract["waiting_ref"],
    )
    base = synthetic_provider_registry()
    registry = ProviderRegistry()
    registry.register(provider, adapter)
    for name in ("fake-whatsapp", "fake-channel", "fake-cleanup", "dry-run-runtime"):
        registry.register(name, base.get(name))

    worker = cp_stack.make_worker(providers=registry, now=now + 10)

    outcomes = []

    def call():  # noqa: ANN001, ANN201
        outcomes.append(worker.run_once())

    in_flight = threading.Thread(target=call, daemon=True)
    in_flight.start()
    assert entered.wait(timeout=30.0), "the adapter was never reached"

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
        idempotency_key=f"late-waiting-{provider}",
        now=now + 20,
    )
    assert receipt.completion_status != "complete", "fixture did not stay open"

    # The answer arrives now — after the deletion transaction committed.
    release.set()
    in_flight.join(timeout=30.0)
    assert not in_flight.is_alive(), "the in-flight call never finished"
    late = outcomes[0]
    assert late is not None and late.job_id == job_id
    # The whole point: NOT `waiting_user` — that state is invisible to an
    # erasure whose cancellation sweep already ran.
    assert late.status == "outcome_unknown", late
    if contract["waiting_ref"] is not None:
        # A contract that ANSWERED with its reference has its teardown
        # scheduled by the very worker that received it — not deferred to a
        # later reconciliation that would have to re-derive what it was told.
        scheduled = cp_stack.database.query_one(
            "SELECT status FROM provisioning_jobs"
            " WHERE household_id = ? AND kind = 'cleanup'",
            (cp_stack.household.id,),
        )
        assert scheduled is not None, (
            "the answering worker never scheduled the teardown it owed"
        )

    worker.reconcile(job_id)
    for _ in range(6):
        if worker.run_once() is None:
            break

    parent = cp_stack.jobs.get(job_id)
    assert parent is not None
    assert (parent.status, parent.error_code) == (
        "cancelled",
        "cancelled_and_compensated",
    ), f"parent never settled: {parent.status}/{parent.error_code}"
    cleanup_row = cp_stack.database.query_one(
        "SELECT status, error_code FROM provisioning_jobs"
        " WHERE household_id = ? AND kind = 'cleanup'",
        (cp_stack.household.id,),
    )
    assert cleanup_row is not None, "nothing scheduled the teardown"
    assert cleanup_row["status"] == "succeeded", (
        f"cleanup never finished: {cleanup_row['status']}/{cleanup_row['error_code']}"
    )
    recorded = cp_stack.database.query_one(
        "SELECT status FROM external_resources WHERE stable_name = ?",
        (f"{cp_stack.household.id}:late-waiting:{provider}",),
    )
    assert recorded is not None, "the late reference was never recorded"
    assert recorded["status"] == "deleted", (
        f"the recorded resource never finished deleting: {recorded['status']}"
    )
    assert contract["torn_down"] in adapter.torn_down, (
        f"teardown did not use the contract's reference; got {adapter.torn_down}"
    )
    identity_row = cp_stack.database.query_one(
        "SELECT status FROM email_identities WHERE id = ?", (identity_id,)
    )
    assert identity_row is not None, "the disconnect lost the identity row"
    assert identity_row["status"] == "deleted", (
        f"the identity never finished disconnecting: {identity_row['status']}"
    )
    reservation = cp_stack.database.query_one(
        "SELECT status FROM email_address_reservations WHERE email_identity_id = ?",
        (identity_id,),
    )
    assert reservation is not None, "the reservation row vanished instead of releasing"
    assert reservation["status"] == "released", (
        f"the local part stayed held: {reservation['status']}"
    )

    final = deletion.resume(cp_stack.household.id, now=now + 40)
    assert final.completion_status == "complete", (
        f"deletion never completed: {final.completion_status}"
        f" {final.provider_statuses}"
    )
    assert (
        cp_stack.database.query_one(
            "SELECT id FROM households WHERE id = ?", (cp_stack.household.id,)
        )
        is None
    ), "a complete deletion left the household row behind"


class _NoReconcileEmail:
    """An email adapter that never defined `reconcile`.

    The shape the old tail served: the worker discovered `reconcile` with
    `getattr` and an adapter omitting it fell through to `provider.inspect` —
    a recovery path that reissues credentials on the real adapters. Every
    method here raises or records, so reaching the old tail fails loudly
    instead of passing quietly.
    """

    email_public_provider = "nerve"

    def __init__(self) -> None:
        self.inspected: list[object] = []
        self.ensured = 0

    def ensure(self, request, intent_key):  # noqa: ANN001, ANN201
        self.ensured += 1
        raise AssertionError("reconcile resumed provisioning")

    def inspect(self, stable_ref):  # noqa: ANN001, ANN201
        self.inspected.append(stable_ref)
        raise AssertionError("reconcile probed with inspect")

    def deprovision(self, external_ref):  # noqa: ANN001, ANN201
        return InspectResult(state=InspectState.ABSENT)


def _quarantine_job(
    cp_stack, provider: str, intent_key: str, error_code: str,  # noqa: ANN001
    *, kind: str = "email_identity",
) -> str:
    now = 1_760_000_000.0
    identity_id = "43000000-0000-4000-8000-00000000000b"
    with cp_stack.jobs.db.write() as connection:
        if kind == "email_identity":
            connection.execute(
                "INSERT INTO email_identities (id, household_id, option, status,"
                " secret_binding_ref, encryption_key_version, version, created_at, updated_at)"
                " VALUES (?, ?, 'managed_abrolia', 'outcome_unknown', NULL, 'v1', 1, ?, ?)",
                (identity_id, cp_stack.household.id, now, now),
            )
        workflow = cp_stack.onboarding.workflow_for_household(cp_stack.household.id)
        request = {
            "household_id": cp_stack.household.id,
            "stable_ref": f"{cp_stack.household.id}:inbox",
        }
        if kind == "email_identity":
            request.update(
                {
                    "email_identity_id": identity_id,
                    "option": "managed_abrolia",
                    "selection": {
                        "kind": "abrolia_managed",
                        "local_part": "family-agent",
                    },
                }
            )
        job_id, _created = cp_stack.jobs.create(
            connection,
            household_id=cp_stack.household.id,
            workflow_id=workflow.id,
            kind=kind,
            operation="ensure",
            intent_key=intent_key,
            request=request,
            provider=provider,
            now=now,
        )
        cp_stack.jobs.settle(
            connection,
            job_id,
            status="outcome_unknown",
            error_code=error_code,
            now=now,
        )
    return job_id


@pytest.mark.parametrize(
    "provider", ["nerve-managed", "nerve-byo-domain", "google-oauth", "fake-email"]
)
def test_a_quarantined_job_reconciles_without_the_adapters_reconcile_method(
    cp_stack, provider: str
) -> None:
    """Shutdown routing is the WORKER's decision, not the adapter's shape.

    The route used to live inside the branch gated on
    `callable(getattr(provider, "reconcile"))`, so an adapter without the
    method sent a quarantined job to a tail whose first act was
    `provider.inspect` — a recovery path that reissues the API key and
    rotates the webhook on Nerve, and calls `ensure` on Google OAuth. A
    withdrawn household's inbox got fresh live credentials from the very
    pass that was supposed to remove it.
    """

    cp_stack.complete_profile()
    _hold_both_consents(cp_stack, now=1_760_000_000.0)
    job_id = _quarantine_job(
        cp_stack,
        provider,
        f"{cp_stack.household.id}:no-reconcile-shutdown:{provider}",
        "withdrawal_requires_reconciliation",
    )

    adapter = _NoReconcileEmail()
    # Exactly one provider, the shape under test: the synthetic registry
    # already holds `fake-email`, and `register` refuses duplicates.
    registry = ProviderRegistry()
    registry.register(provider, adapter)
    result = cp_stack.make_worker(
        providers=registry, now=1_760_000_010.0
    ).reconcile(job_id)

    assert adapter.inspected == [], "reconcile probed with a mutating inspect"
    assert adapter.ensured == 0, "reconcile resumed provisioning"
    if provider == "fake-email":
        # This STUB declares no derivable teardown contract: its
        # `email_public_provider` says "nerve", whose references are not this
        # adapter's to accept by arithmetic — and deriving from the registry
        # NAME would schedule cleanups against deprovisioners that never
        # accepted such references. The real synthetic provisioner declares
        # "synthetic" and IS derived for
        # (`test_an_ambiguous_synthetic_job_tears_down_the_reference_it_can_derive`).
        # Refusing an adapter that declares no contract beats scheduling a
        # cleanup its deprovisioner would refuse — a failed job standing in
        # for a teardown is worse than a quarantine, because the inbox is
        # live either way.
        assert result is not None and result.status == "outcome_unknown", result
        assert result.error_code == "withdrawal_requires_reconciliation"
        return
    cleanup = cp_stack.database.query_one(
        "SELECT request_ciphertext FROM provisioning_jobs"
        " WHERE household_id = ? AND kind = 'cleanup'",
        (cp_stack.household.id,),
    )
    assert cleanup is not None, "shutdown reconciled neither down nor out"


@pytest.mark.parametrize(
    "provider", ["nerve-managed", "nerve-byo-domain", "google-oauth", "fake-email"]
)
def test_forward_reconcile_without_the_method_fails_closed(
    cp_stack, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    """No adapter shape buys a mutating probe for forward work either.

    An ambiguous job on a LIVE household may legitimately resume — through
    the adapter's own `reconcile`. An adapter that does not implement it is
    refused visibly; the worker never substitutes `inspect`, which on every
    real email adapter mutates provider state. The switches are ON here so
    the job reaches that gate instead of stopping at the brake — this test
    asks one question, about adapter shape, not about the brake.
    """

    monkeypatch.setenv("ABROLIA_REAL_EMAIL_ENABLED", "1")
    monkeypatch.setenv("ABROLIA_GMAIL_ENABLED", "1")
    monkeypatch.setenv("ABROLIA_BYO_EMAIL_ENABLED", "1")
    cp_stack.complete_profile()
    _hold_both_consents(cp_stack, now=1_760_000_000.0)
    job_id = _quarantine_job(
        cp_stack,
        provider,
        f"{cp_stack.household.id}:no-reconcile-forward:{provider}",
        "reconcile_inconclusive",
    )

    adapter = _NoReconcileEmail()
    # Exactly one provider, the shape under test: the synthetic registry
    # already holds `fake-email`, and `register` refuses duplicates.
    registry = ProviderRegistry()
    registry.register(provider, adapter)
    result = cp_stack.make_worker(
        providers=registry, now=1_760_000_010.0
    ).reconcile(job_id)

    assert adapter.inspected == [], "reconcile probed with a mutating inspect"
    assert adapter.ensured == 0, "reconcile resumed provisioning"
    assert result is not None and result.status == "failed", result
    assert result.error_code == "provider_cannot_reconcile"


@pytest.mark.parametrize(
    ("kind", "provider"),
    [("whatsapp_identity", "fake-whatsapp"), ("channel_binding", "fake-channel")],
)
def test_a_kind_without_a_reconcile_path_is_refused_not_probed(
    cp_stack, kind: str, provider: str
) -> None:
    """A schema-permitted kind must not acquire an inspect path by omission.

    Phase E will create `whatsapp_identity` and `channel_binding` jobs. Under
    the old unguarded tail they fell through every branch into
    `provider.inspect(...)` — a call whose contract this worker does not know,
    on adapters where it mutates. The end of `_reconcile` is now a visible
    refusal, so introducing a kind without writing its reconcile path fails
    loudly here instead of probing quietly in production.
    """

    cp_stack.complete_profile()
    _hold_both_consents(cp_stack, now=1_760_000_000.0)
    job_id = _quarantine_job(
        cp_stack,
        provider,
        f"{cp_stack.household.id}:kind-gap:{kind}",
        "reconcile_inconclusive",
        kind=kind,
    )

    adapter = _NoReconcileEmail()  # shape is irrelevant; nothing may be called
    registry = ProviderRegistry()
    registry.register(provider, adapter)
    result = cp_stack.make_worker(
        providers=registry, now=1_760_000_010.0
    ).reconcile(job_id)

    assert adapter.inspected == [], "the kind gap fell through to inspect"
    assert adapter.ensured == 0, "the kind gap fell through to ensure"
    assert result is not None and result.status == "outcome_unknown", result
    assert result.error_code == "reconcile_unsupported"

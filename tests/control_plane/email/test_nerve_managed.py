from __future__ import annotations

import json

import pytest

from control_plane.email.models import EmailOption, EmailProvisionIntent
from control_plane.models import StepKind
from control_plane.providers.email.nerve_managed import (
    NERVE_SECRET_BINDING,
    NerveManagedEmailProvisioner,
)
from control_plane.provisioning.contracts import (
    InspectState,
    OutcomeUnknown,
    ProviderRegistry,
    ProviderWaiting,
)
from control_plane.provisioning.secrets import InMemorySecretSink


class FakeNerveAdmin:
    def __init__(
        self, *, replay_credentials: bool = False, attachments_enabled: bool = True
    ) -> None:
        self.replay_credentials = replay_credentials
        self.attachments_enabled = attachments_enabled
        self.key_calls = 0
        self.webhook_calls = 0
        self.probed_keys: list[tuple[str, str]] = []
        self.deleted: list[tuple[str, str | None]] = []

    def ensure_org(self, *, household_id):
        return {"org_id": f"org-{household_id}"}

    def get_org(self, *, household_id):
        return {"org_id": f"org-{household_id}"}

    def ensure_grant(self, *, org_id, external_ref):
        return {"id": "grant-1", "external_ref": external_ref}

    def ensure_inbox(self, *, org_id, address, external_ref):
        return {
            "inbox": {
                "id": "inbox-1",
                "address": address,
                "external_ref": external_ref,
            }
        }

    def issue_key(self, *, org_id, external_ref):
        self.key_calls += 1
        if self.replay_credentials and self.key_calls == 1:
            return {
                "id": "key-old",
                "external_ref": external_ref,
                "secret_available": False,
            }
        return {
            "id": "key-1",
            "key": "synthetic-nerve-key",
            "external_ref": external_ref,
            "secret_available": True,
        }

    def ensure_webhook(self, *, org_id, url, external_ref):
        self.webhook_calls += 1
        result = {
            "id": "webhook-1",
            "external_ref": external_ref,
            "url": url,
            "secret_available": not self.replay_credentials,
        }
        if not self.replay_credentials:
            result["secret"] = "synthetic-signing-key"
        return result

    def probe_attachment_readiness(self, *, org_id, runtime_key):
        self.probed_keys.append((org_id, runtime_key))
        return self.attachments_enabled

    def rotate_webhook(self, *, org_id, webhook_id):
        return {"id": webhook_id, "secret": "synthetic-rotated-signing-key"}

    def list_grants(self, *, org_id):
        return [{"id": "grant-1", "external_ref": "arbolia:email:identity-1:grant"}]

    def list_inboxes(self, *, org_id):
        return [{
            "id": "inbox-1",
            "address": "family-agent@" + "abrolia.com",
            "external_ref": "arbolia:email:identity-1:inbox",
        }]

    def list_keys(self, *, org_id):
        return [{
            "id": "key-old",
            "external_ref": "arbolia:email:identity-1:key",
        }]

    def list_webhooks(self, *, org_id):
        return [{
            "id": "webhook-1",
            "external_ref": "arbolia:email:identity-1:webhook",
        }]

    def delete(self, path, *, org_id=None):
        self.deleted.append((path, org_id))


class LostInboxResponseNerveAdmin(FakeNerveAdmin):
    def __init__(self) -> None:
        super().__init__()
        self.inbox_calls = 0

    def ensure_inbox(self, *, org_id, address, external_ref):
        self.inbox_calls += 1
        if self.inbox_calls == 1:
            raise OutcomeUnknown("synthetic lost response")
        return super().ensure_inbox(
            org_id=org_id, address=address, external_ref=external_ref
        )


def _intent() -> dict:
    return EmailProvisionIntent(
        identity_id="identity-1",
        household_id="household-1",
        option=EmailOption.MANAGED_ABROLIA,
        selection={"kind": "abrolia_managed", "local_part": "family-agent"},
        secret_namespace_ref="abrolia-hh-synthetic",
    ).model_dump(mode="json")


def test_managed_provider_builds_isolated_resources_and_one_secret_bundle() -> None:
    client = FakeNerveAdmin()
    result = NerveManagedEmailProvisioner(client).ensure(
        _intent(), "household-1:email_identity:identity-1:abrolia_managed:1"
    )

    assert result.public_result["agent_inbox"] == "family-agent@" + "abrolia.com"
    assert result.public_result["granted_scopes"] == [
        "nerve:email.read",
        "nerve:email.send",
    ]
    bundle = json.loads(bytes(result.secret_material.items()[0][1]))
    assert bundle == {
        "api_key": "synthetic-nerve-key",
        "webhook_signing_key": "synthetic-signing-key",
    }
    assert result.secret_material.items()[0][0] == NERVE_SECRET_BINDING
    assert "synthetic-nerve-key" not in result.external_ref
    assert "synthetic-signing-key" not in repr(result.public_result)
    assert result.public_result["attachment_capability"] == "ready"
    assert client.probed_keys == [
        ("org-household-1", "synthetic-nerve-key")
    ]
    assert client.webhook_calls == 1


def test_flag_off_revokes_probe_key_and_waits_without_webhook_or_secret() -> None:
    client = FakeNerveAdmin(attachments_enabled=False)

    with pytest.raises(ProviderWaiting) as raised:
        NerveManagedEmailProvisioner(client).ensure(
            _intent(), "household-1:email_identity:identity-1:abrolia_managed:1"
        )

    assert raised.value.public_result == {
        "state": "attachment_activation_required",
        "provider": "nerve",
        "provider_subject": "org-household-1",
        "attachment_capability": "pending",
    }
    assert raised.value.external_ref is not None
    assert ("/v1/keys/key-1", "org-household-1") in client.deleted
    assert client.probed_keys == [
        ("org-household-1", "synthetic-nerve-key")
    ]
    assert client.webhook_calls == 0


def test_flag_convergence_resumes_same_intent_and_returns_ready() -> None:
    client = FakeNerveAdmin(attachments_enabled=False)
    provider = NerveManagedEmailProvisioner(client)

    with pytest.raises(ProviderWaiting):
        provider.ensure(
            _intent(), "household-1:email_identity:identity-1:abrolia_managed:1"
        )
    client.attachments_enabled = True
    result = provider.reconcile(
        _intent(), "household-1:email_identity:identity-1:abrolia_managed:1"
    )

    assert result.public_result["attachment_capability"] == "ready"
    assert not result.secret_material.is_empty


def test_probe_error_never_projects_ready_or_revokes_ambiguous_key() -> None:
    class FailingProbeNerveAdmin(FakeNerveAdmin):
        def probe_attachment_readiness(self, *, org_id, runtime_key):
            raise OutcomeUnknown("synthetic probe failure")

    client = FailingProbeNerveAdmin()

    with pytest.raises(OutcomeUnknown):
        NerveManagedEmailProvisioner(client).ensure(
            _intent(), "household-1:email_identity:identity-1:abrolia_managed:1"
        )

    assert client.deleted == []


def test_replayed_one_time_credentials_are_revoked_or_rotated() -> None:
    client = FakeNerveAdmin(replay_credentials=True)
    result = NerveManagedEmailProvisioner(client).ensure(
        _intent(), "household-1:email_identity:identity-1:abrolia_managed:1"
    )

    bundle = json.loads(bytes(result.secret_material.items()[0][1]))
    assert bundle["api_key"] == "synthetic-nerve-key"
    assert bundle["webhook_signing_key"] == "synthetic-rotated-signing-key"
    assert ("/v1/keys/key-old", "org-household-1") in client.deleted
    assert client.key_calls == 2


def test_reconcile_rotates_credentials_before_returning_ready() -> None:
    client = FakeNerveAdmin()
    inspected = NerveManagedEmailProvisioner(client).inspect(
        "household-1:email_identity:identity-1:abrolia_managed:1"
    )

    assert inspected.state is InspectState.READY
    assert inspected.result is not None
    assert not inspected.result.secret_material.is_empty
    assert ("/v1/keys/key-old", "org-household-1") in client.deleted


def test_cleanup_never_deletes_shared_platform_domain() -> None:
    client = FakeNerveAdmin()
    provider = NerveManagedEmailProvisioner(client)
    result = provider.ensure(
        _intent(), "household-1:email_identity:identity-1:abrolia_managed:1"
    )

    cleaned = provider.deprovision(result.external_ref)

    assert cleaned.state is InspectState.ABSENT
    paths = [path for path, _ in client.deleted]
    assert paths == [
        "/v1/webhooks/webhook-1",
        "/v1/keys/key-1",
        "/v1/inboxes/inbox-1",
        "/v1/domain-grants/grant-1",
        "/v1/orgs/org-household-1",
    ]
    assert all("domains" not in path for path in paths)


def test_managed_provider_runs_through_durable_worker_and_secret_sink(cp_stack) -> None:
    cp_stack.complete_profile()
    cp_stack.service.email_provider = "nerve-managed"
    client = FakeNerveAdmin()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        {"kind": "abrolia_managed", "local_part": "family-agent"},
        context=cp_stack.context(),
    )
    registry = ProviderRegistry()
    registry.register("nerve-managed", NerveManagedEmailProvisioner(client))
    sink = InMemorySecretSink()

    worker = cp_stack.make_worker(providers=registry, secret_sink=sink)
    result = worker.run_once()

    assert result is not None and result.status == "succeeded"
    identity = cp_stack.email_identities.current_for_household(cp_stack.household.id)
    assert identity is not None and identity.address == "family-agent@" + "abrolia.com"
    namespace = cp_stack.database.query_one(
        "SELECT id, external_id_ciphertext, encryption_key_version"
        " FROM external_resources WHERE resource_type = 'secret_namespace'"
    )
    runtime_ref = cp_stack.jobs.decrypt_json(
        "external_resources",
        namespace["id"],
        "external_id",
        namespace["external_id_ciphertext"],
        namespace["encryption_key_version"],
    )
    assert sink.get(runtime_ref, NERVE_SECRET_BINDING) is not None
    assert b"synthetic-nerve-key" not in cp_stack.database.path.read_bytes()

    cp_stack.service.reset_from(
        cp_stack.household.id,
        StepKind.EMAIL,
        context=cp_stack.context(),
    )
    cleaned = worker.run_once()

    assert cleaned is not None and cleaned.status == "succeeded"
    assert sink.get(runtime_ref, NERVE_SECRET_BINDING) is None
    assert all("/v1/domains/" not in path for path, _ in client.deleted)


def test_worker_keeps_flag_off_pending_then_converges_without_secret_leak(cp_stack) -> None:
    cp_stack.complete_profile()
    cp_stack.service.email_provider = "nerve-managed"
    client = FakeNerveAdmin(attachments_enabled=False)
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        {"kind": "abrolia_managed", "local_part": "family-agent"},
        context=cp_stack.context(),
    )
    registry = ProviderRegistry()
    registry.register("nerve-managed", NerveManagedEmailProvisioner(client))
    sink = InMemorySecretSink()
    worker = cp_stack.make_worker(providers=registry, secret_sink=sink)

    waiting = worker.run_once()
    snapshot = cp_stack.onboarding.snapshot(cp_stack.household.id)
    email_step = next(step for step in snapshot.steps if step.kind is StepKind.EMAIL)

    assert waiting is not None and waiting.status == "waiting_user"
    assert email_step.public_status["state"] == "attachment_activation_required"
    assert email_step.public_status["attachment_capability"] == "pending"
    assert client.webhook_calls == 0
    assert not sink._installed

    client.attachments_enabled = True
    cp_stack.service.check(
        cp_stack.household.id, StepKind.EMAIL, context=cp_stack.context()
    )
    ready = worker.run_once()

    assert ready is not None and ready.status == "succeeded"
    identity = cp_stack.email_identities.current_for_household(cp_stack.household.id)
    assert identity is not None and identity.address == "family-agent@" + "abrolia.com"
    assert client.webhook_calls == 1


def test_reset_while_attachment_activation_waits_cleans_partial_graph(cp_stack) -> None:
    cp_stack.complete_profile()
    cp_stack.service.email_provider = "nerve-managed"
    client = FakeNerveAdmin(attachments_enabled=False)
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        {"kind": "abrolia_managed", "local_part": "family-agent"},
        context=cp_stack.context(),
    )
    registry = ProviderRegistry()
    registry.register("nerve-managed", NerveManagedEmailProvisioner(client))
    worker = cp_stack.make_worker(providers=registry)

    assert worker.run_once().status == "waiting_user"
    cp_stack.service.reset_from(
        cp_stack.household.id, StepKind.EMAIL, context=cp_stack.context()
    )
    cleaned = worker.run_once()

    assert cleaned is not None and cleaned.status == "succeeded"
    assert client.deleted[-3:] == [
        ("/v1/inboxes/inbox-1", "org-" + cp_stack.household.id),
        ("/v1/domain-grants/grant-1", None),
        ("/v1/orgs/org-" + cp_stack.household.id, None),
    ]
    assert all(path not in {"/v1/keys/", "/v1/webhooks/"} for path, _ in client.deleted)


def test_worker_reconciles_lost_create_response_from_durable_intent(cp_stack) -> None:
    cp_stack.complete_profile()
    cp_stack.service.email_provider = "nerve-managed"
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        {"kind": "abrolia_managed", "local_part": "family-agent"},
        context=cp_stack.context(),
    )
    client = LostInboxResponseNerveAdmin()
    registry = ProviderRegistry()
    registry.register("nerve-managed", NerveManagedEmailProvisioner(client))
    sink = InMemorySecretSink()
    worker = cp_stack.make_worker(providers=registry, secret_sink=sink)

    unknown = worker.run_once()
    reconciled = worker.reconcile(unknown.job_id)

    assert unknown.status == "outcome_unknown"
    assert reconciled.status == "succeeded"
    assert client.inbox_calls == 2
    identity = cp_stack.email_identities.current_for_household(cp_stack.household.id)
    assert identity is not None and identity.address == "family-agent@" + "abrolia.com"

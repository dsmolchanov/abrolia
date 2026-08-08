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
    ProviderRejected,
    ProviderWaiting,
    ProvisionResult,
)
from control_plane.provisioning.secrets import InMemorySecretSink

ORG_ID = "00000000-0000-4000-8000-000000000001"
GRANT_ID = "00000000-0000-4000-8000-000000000002"
INBOX_ID = "00000000-0000-4000-8000-000000000003"
KEY_ID = "00000000-0000-4000-8000-000000000004"
OLD_KEY_ID = "00000000-0000-4000-8000-000000000005"
WEBHOOK_ID = "00000000-0000-4000-8000-000000000006"


class FakeNerveAdmin:
    def __init__(
        self,
        *,
        replay_credentials: bool = False,
        attachments_enabled: bool = True,
        attachment_probe_error: Exception | None = None,
    ) -> None:
        self.replay_credentials = replay_credentials
        self.attachments_enabled = attachments_enabled
        self.attachment_probe_error = attachment_probe_error
        self.attachment_probe_calls: list[tuple[str, str]] = []
        self.key_calls = 0
        self.deleted: list[tuple[str, str | None]] = []
        self.org_external_refs: list[str] = []
        self.grants = [
            {"id": GRANT_ID, "external_ref": "arbolia:email:identity-1:grant"}
        ]
        self.inboxes = [{
            "id": INBOX_ID,
            "address": "family-agent@" + "abrolia.com",
            "external_ref": "arbolia:email:identity-1:inbox",
        }]
        self.keys = [
            {"id": OLD_KEY_ID, "external_ref": "arbolia:email:identity-1:key"}
        ]
        self.webhooks = [{
            "id": WEBHOOK_ID,
            "external_ref": "arbolia:email:identity-1:webhook",
        }]

    @staticmethod
    def _replace(items, value):
        items[:] = [item for item in items if item["external_ref"] != value["external_ref"]]
        items.append(value)

    def ensure_org(self, *, household_id, identity_id):
        self.org_external_refs.append(
            f"arbolia:household:{household_id}:email:{identity_id}"
        )
        return {"org_id": ORG_ID}

    def get_org(self, *, external_ref):
        self.org_external_refs.append(external_ref)
        return {"org_id": ORG_ID}

    def ensure_grant(self, *, org_id, external_ref):
        result = {"id": GRANT_ID, "external_ref": external_ref}
        self._replace(self.grants, result)
        return result

    def ensure_inbox(self, *, org_id, address, external_ref):
        result = {
            "inbox": {
                "id": INBOX_ID,
                "address": address,
                "external_ref": external_ref,
            }
        }
        self._replace(self.inboxes, result["inbox"])
        return result

    def issue_key(self, *, org_id, external_ref):
        self.key_calls += 1
        if self.replay_credentials and self.key_calls == 1:
            result = {
                "id": OLD_KEY_ID,
                "external_ref": external_ref,
                "secret_available": False,
            }
            self._replace(self.keys, result)
            return result
        result = {
            "id": KEY_ID,
            "key": "synthetic-nerve-key",
            "external_ref": external_ref,
            "secret_available": True,
        }
        self._replace(self.keys, result)
        return result

    def ensure_webhook(self, *, org_id, url, external_ref):
        result = {
            "id": WEBHOOK_ID,
            "external_ref": external_ref,
            "url": url,
            "secret_available": not self.replay_credentials,
        }
        if not self.replay_credentials:
            result["secret"] = "synthetic-signing-key"
        self._replace(self.webhooks, result)
        return result

    def rotate_webhook(self, *, org_id, webhook_id):
        return {"id": webhook_id, "secret": "synthetic-rotated-signing-key"}

    def attachment_feature_enabled(self, *, api_key, expected_org_id):
        self.attachment_probe_calls.append((api_key, expected_org_id))
        if self.attachment_probe_error is not None:
            raise self.attachment_probe_error
        return self.attachments_enabled

    def list_grants(self, *, org_id):
        return list(self.grants)

    def list_inboxes(self, *, org_id):
        return list(self.inboxes)

    def list_keys(self, *, org_id):
        return list(self.keys)

    def list_webhooks(self, *, org_id):
        return list(self.webhooks)

    def delete(self, path, *, org_id=None):
        self.deleted.append((path, org_id))
        if path.startswith("/v1/keys/"):
            key_id = path.rsplit("/", 1)[-1]
            self.keys[:] = [item for item in self.keys if item["id"] != key_id]


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


class LifecycleNerveAdmin(FakeNerveAdmin):
    def __init__(self) -> None:
        super().__init__()
        self.orgs: dict[str, str] = {}
        self.tombstoned: set[str] = set()

    def ensure_org(self, *, household_id, identity_id):
        external_ref = f"arbolia:household:{household_id}:email:{identity_id}"
        self.org_external_refs.append(external_ref)
        if external_ref in self.tombstoned:
            raise ProviderRejected("tombstoned org external ref")
        if external_ref not in self.orgs:
            suffix = len(self.orgs) + 1
            self.orgs[external_ref] = (
                f"20000000-0000-4000-8000-{suffix:012d}"
            )
        return {"org_id": self.orgs[external_ref]}

    def get_org(self, *, external_ref):
        if external_ref in self.tombstoned or external_ref not in self.orgs:
            return {}
        return {"org_id": self.orgs[external_ref]}

    def delete(self, path, *, org_id=None):
        super().delete(path, org_id=org_id)
        if path.startswith("/v1/orgs/"):
            deleted_org_id = path.rsplit("/", 1)[-1]
            self.tombstoned.update(
                ref for ref, candidate in self.orgs.items()
                if candidate == deleted_org_id
            )


def _intent(*, identity_id: str = "identity-1") -> dict:
    return EmailProvisionIntent(
        identity_id=identity_id,
        household_id="household-1",
        option=EmailOption.MANAGED_ABROLIA,
        selection={"kind": "abrolia_managed", "local_part": "family-agent"},
        secret_namespace_ref="abrolia-hh-synthetic",
    ).model_dump(mode="json")


def test_reconnect_uses_fresh_org_without_resurrecting_tombstone() -> None:
    client = LifecycleNerveAdmin()
    provider = NerveManagedEmailProvisioner(client)
    first_key = "household-1:email_identity:identity-1:abrolia_managed:1"
    first = provider.ensure(_intent(), first_key)

    assert provider.deprovision(first.external_ref).state is InspectState.ABSENT
    with pytest.raises(ProviderRejected, match="tombstoned"):
        provider.ensure(_intent(), first_key)

    second = provider.ensure(
        _intent(identity_id="identity-2"),
        "household-1:email_identity:identity-2:abrolia_managed:1",
    )

    assert second.public_result["provider_subject"] != first.public_result[
        "provider_subject"
    ]
    assert set(client.orgs) == {
        "arbolia:household:household-1:email:identity-1",
        "arbolia:household:household-1:email:identity-2",
    }


def test_pre_generation_reference_fails_closed_during_inspection() -> None:
    client = FakeNerveAdmin()
    provider = NerveManagedEmailProvisioner(client)
    result = provider.ensure(
        _intent(), "household-1:email_identity:identity-1:abrolia_managed:1"
    )
    legacy = json.loads(result.external_ref)
    legacy.pop("org_external_ref")

    inspected = provider.inspect(
        json.dumps(legacy, sort_keys=True, separators=(",", ":"))
    )

    assert inspected.state is InspectState.UNKNOWN


def test_managed_provider_builds_isolated_resources_and_one_secret_bundle() -> None:
    client = FakeNerveAdmin()
    result = NerveManagedEmailProvisioner(client).ensure(
        _intent(), "household-1:email_identity:identity-1:abrolia_managed:1"
    )

    assert client.org_external_refs == [
        "arbolia:household:household-1:email:identity-1"
    ]
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
    assert client.attachment_probe_calls == [
        ("synthetic-nerve-key", ORG_ID)
    ]


def test_managed_provider_waits_for_audited_attachment_activation() -> None:
    client = FakeNerveAdmin(attachments_enabled=False)

    with pytest.raises(ProviderWaiting) as raised:
        NerveManagedEmailProvisioner(client).ensure(
            _intent(), "household-1:email_identity:identity-1:abrolia_managed:1"
        )

    assert raised.value.code == "nerve_attachment_flag_pending"
    assert raised.value.public_result == {
        "readiness": "attachments_flag_pending",
        "nerve_org_id": ORG_ID,
        "operator_action": {
            "tool": "nerve-flags",
            "arguments": [
                "set",
                "attachments",
                "--org",
                ORG_ID,
                "--enabled=true",
            ],
            "audit_actor_required": True,
        },
        "next_action": "enable the flag, wait for convergence, then check again",
    }
    assert raised.value.external_ref is not None
    assert "synthetic-nerve-key" not in raised.value.external_ref


def test_managed_provider_converges_from_flag_off_to_ready() -> None:
    client = FakeNerveAdmin(attachments_enabled=False)
    provider = NerveManagedEmailProvisioner(client)
    with pytest.raises(ProviderWaiting) as raised:
        provider.ensure(
            _intent(), "household-1:email_identity:identity-1:abrolia_managed:1"
        )

    client.attachments_enabled = True
    inspected = provider.inspect(str(raised.value.external_ref))

    assert inspected.state is InspectState.READY
    assert inspected.result is not None
    assert not inspected.result.secret_material.is_empty
    assert client.attachment_probe_calls[-1] == (
        "synthetic-nerve-key",
        ORG_ID,
    )


def test_managed_provider_probe_errors_fail_closed() -> None:
    client = FakeNerveAdmin(
        attachment_probe_error=OutcomeUnknown("synthetic probe unavailable")
    )

    with pytest.raises(OutcomeUnknown):
        NerveManagedEmailProvisioner(client).ensure(
            _intent(), "household-1:email_identity:identity-1:abrolia_managed:1"
        )


def test_replayed_one_time_credentials_are_revoked_or_rotated() -> None:
    client = FakeNerveAdmin(replay_credentials=True)
    result = NerveManagedEmailProvisioner(client).ensure(
        _intent(), "household-1:email_identity:identity-1:abrolia_managed:1"
    )

    bundle = json.loads(bytes(result.secret_material.items()[0][1]))
    assert bundle["api_key"] == "synthetic-nerve-key"
    assert bundle["webhook_signing_key"] == "synthetic-rotated-signing-key"
    assert (f"/v1/keys/{OLD_KEY_ID}", ORG_ID) in client.deleted
    assert client.key_calls == 2


def test_reconcile_rotates_credentials_before_returning_ready() -> None:
    client = FakeNerveAdmin()
    inspected = NerveManagedEmailProvisioner(client).inspect(
        "household-1:email_identity:identity-1:abrolia_managed:1"
    )

    assert inspected.state is InspectState.READY
    assert inspected.result is not None
    assert not inspected.result.secret_material.is_empty
    assert (f"/v1/keys/{OLD_KEY_ID}", ORG_ID) in client.deleted


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
        f"/v1/webhooks/{WEBHOOK_ID}",
        f"/v1/keys/{KEY_ID}",
        f"/v1/inboxes/{INBOX_ID}",
        f"/v1/domain-grants/{GRANT_ID}",
        f"/v1/orgs/{ORG_ID}",
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


def test_worker_keeps_onboarding_pending_until_attachment_flag_converges(
    cp_stack,
) -> None:
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

    assert waiting is not None and waiting.status == "waiting_user"
    assert waiting.error_code == "nerve_attachment_flag_pending"
    snapshot = cp_stack.onboarding.snapshot(cp_stack.household.id)
    step = next(step for step in snapshot.steps if step.kind is StepKind.EMAIL)
    assert step.public_status["readiness"] == "attachments_flag_pending"
    assert step.public_status["nerve_org_id"] == ORG_ID
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
    assert sink.get(runtime_ref, NERVE_SECRET_BINDING) is None

    client.attachments_enabled = True
    cp_stack.service.check(
        cp_stack.household.id,
        StepKind.EMAIL,
        context=cp_stack.context(),
    )
    converged = worker.run_once()

    assert converged is not None and converged.status == "succeeded"
    identity = cp_stack.email_identities.current_for_household(cp_stack.household.id)
    assert identity is not None
    assert identity.address == "family-agent@" + "abrolia.com"
    assert sink.get(runtime_ref, NERVE_SECRET_BINDING) is not None


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


def test_managed_provisioner_never_calls_service_tokens() -> None:
    """B-01 regression: Abrolia provisioner must never invoke /v1/service-tokens."""
    calls: list[str] = []

    class CapturingNerveAdmin(FakeNerveAdmin):
        def _record(self, path: str) -> None:
            calls.append(path)

        def ensure_org(self, *, household_id, identity_id):
            self._record("POST /v1/orgs")
            return super().ensure_org(household_id=household_id, identity_id=identity_id)

        def ensure_grant(self, *, org_id, external_ref):
            self._record("POST /v1/domain-grants")
            return super().ensure_grant(org_id=org_id, external_ref=external_ref)

        def ensure_inbox(self, *, org_id, address, external_ref):
            self._record("POST /v1/inboxes")
            return super().ensure_inbox(org_id=org_id, address=address, external_ref=external_ref)

        def issue_key(self, *, org_id, external_ref):
            self._record("POST /v1/keys")
            return super().issue_key(org_id=org_id, external_ref=external_ref)

        def ensure_webhook(self, *, org_id, url, external_ref):
            self._record("POST /v1/webhooks")
            return super().ensure_webhook(org_id=org_id, url=url, external_ref=external_ref)

    client = CapturingNerveAdmin()
    result = NerveManagedEmailProvisioner(client).ensure(
        _intent(), "household-1:email_identity:identity-1:abrolia_managed:1"
    )
    assert result is not None
    assert not any("service-tokens" in path for path in calls), f"service-tokens called: {calls}"
    assert "POST /v1/orgs" in calls
    assert "POST /v1/domain-grants" in calls
    assert "POST /v1/inboxes" in calls
    assert "POST /v1/keys" in calls
    assert "POST /v1/webhooks" in calls


def test_worker_rejects_duplicate_key_noncanonical_nerve_reference(cp_stack) -> None:
    cp_stack.complete_profile()
    cp_stack.service.email_provider = "nerve-managed"
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        {"kind": "abrolia_managed", "local_part": "family-agent"},
        context=cp_stack.context(),
    )

    class DuplicateKeyReferenceProvisioner(NerveManagedEmailProvisioner):
        def _result(self, refs, *, secret=None):
            result = super()._result(refs, secret=secret)
            duplicate = (
                '{"org_id":"short-lived-provider-password",'
                + result.external_ref[1:]
            )
            return ProvisionResult(
                external_ref=duplicate,
                public_result=result.public_result,
                secret_material=result.secret_material,
            )

    registry = ProviderRegistry()
    registry.register(
        "nerve-managed", DuplicateKeyReferenceProvisioner(FakeNerveAdmin())
    )

    failed = cp_stack.make_worker(
        providers=registry, secret_sink=InMemorySecretSink()
    ).run_once()

    assert failed.status == "outcome_unknown"
    assert failed.error_code == "outcome_unknown"

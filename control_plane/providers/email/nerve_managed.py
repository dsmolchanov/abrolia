from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from control_plane.crypto import SecretMaterial
from control_plane.email.models import (
    NERVE_EMAIL_SCOPES,
    NERVE_EMAIL_SECRET_BINDING,
    EmailOption,
    EmailProvisionIntent,
)
from control_plane.providers.email.nerve_client import (
    ORG_TEARDOWN_REF_PREFIX,
    NerveAdminClient,
    email_org_external_ref,
)
from control_plane.provisioning.contracts import (
    InspectResult,
    InspectState,
    OutcomeUnknown,
    ProviderRejected,
    ProviderWaiting,
    ProvisionResult,
)

NERVE_SECRET_BINDING = NERVE_EMAIL_SECRET_BINDING
NERVE_SCOPES = NERVE_EMAIL_SCOPES


class AttachmentFlagPending(ProviderWaiting):
    code = "nerve_attachment_flag_pending"


@dataclass(frozen=True)
class _Refs:
    household_id: str
    stable_ref: str
    org_id: str
    grant_id: str
    inbox_id: str
    key_id: str
    webhook_id: str
    address: str
    org_external_ref: str = ""

    def encode(self) -> str:
        return json.dumps(self.__dict__, sort_keys=True, separators=(",", ":"))

    @classmethod
    def decode(cls, value: str) -> _Refs:
        try:
            payload = json.loads(value)
            return cls(**payload)
        except (TypeError, ValueError) as error:
            raise ProviderRejected("invalid managed Nerve resource reference") from error


class NerveManagedEmailProvisioner:
    email_public_provider = "nerve"

    def __init__(self, client: NerveAdminClient) -> None:
        self.client = client

    @staticmethod
    def _resource_ref(identity_id: str, kind: str) -> str:
        return f"arbolia:email:{identity_id}:{kind}"

    @staticmethod
    def _one(items: list[dict[str, Any]], external_ref: str) -> dict[str, Any] | None:
        matches = [item for item in items if item.get("external_ref") == external_ref]
        if len(matches) > 1:
            raise ProviderRejected("Nerve returned duplicate managed resources")
        return matches[0] if matches else None

    @staticmethod
    def _identity_id(stable_ref: str) -> str | None:
        parts = stable_ref.split(":")
        if len(parts) != 5 or parts[1] != "email_identity":
            return None
        return parts[2]

    @staticmethod
    def _pending(refs: _Refs) -> AttachmentFlagPending:
        return AttachmentFlagPending(
            "Nerve attachments must be activated for this household",
            external_ref=refs.encode(),
            public_result={
                "readiness": "attachments_flag_pending",
                "nerve_org_id": refs.org_id,
                "operator_action": {
                    "tool": "nerve-flags",
                    "arguments": [
                        "set",
                        "attachments",
                        "--org",
                        refs.org_id,
                        "--enabled=true",
                    ],
                    "audit_actor_required": True,
                },
                "next_action": "enable the flag, wait for convergence, then check again",
            },
        )

    def _probe_or_wait(self, refs: _Refs, api_key: str) -> None:
        if not self.client.attachment_feature_enabled(
            api_key=api_key, expected_org_id=refs.org_id
        ):
            raise self._pending(refs)

    def ensure(self, intent: dict[str, Any], idempotency_key: str) -> ProvisionResult:
        parsed = EmailProvisionIntent.model_validate(intent)
        if parsed.option is not EmailOption.MANAGED_ABROLIA:
            raise ProviderRejected("managed Nerve provider received another email option")
        local_part = parsed.selection.get("local_part")
        if not isinstance(local_part, str):
            raise ProviderRejected("managed address is missing")
        address = f"{local_part}@abrolia.com"
        org_external_ref = email_org_external_ref(
            parsed.household_id, parsed.identity_id
        )
        org = self.client.ensure_org(
            household_id=parsed.household_id,
            identity_id=parsed.identity_id,
        )
        org_id = str(org.get("org_id", ""))
        if not org_id:
            raise OutcomeUnknown("Nerve org identity is missing")
        grant_ref = self._resource_ref(parsed.identity_id, "grant")
        grant = self.client.ensure_grant(org_id=org_id, external_ref=grant_ref)
        inbox_ref = self._resource_ref(parsed.identity_id, "inbox")
        inbox_envelope = self.client.ensure_inbox(
            org_id=org_id, address=address, external_ref=inbox_ref
        )
        inbox = inbox_envelope.get("inbox", inbox_envelope)
        key_ref = self._resource_ref(parsed.identity_id, "key")
        key = self.client.issue_key(org_id=org_id, external_ref=key_ref)
        webhook_ref = self._resource_ref(parsed.identity_id, "webhook")
        webhook = self.client.ensure_webhook(
            org_id=org_id,
            url=f"https://{parsed.secret_namespace_ref}.fly.dev/v1/email/nerve/webhook",
            external_ref=webhook_ref,
        )
        if not key.get("secret_available") or not key.get("key"):
            self.client.delete(f"/v1/keys/{key.get('id', '')}", org_id=org_id)
            key = self.client.issue_key(
                org_id=org_id, external_ref=f"{key_ref}:recovery"
            )
        if not webhook.get("secret_available") or not webhook.get("secret"):
            webhook = self.client.rotate_webhook(
                org_id=org_id, webhook_id=str(webhook.get("id", ""))
            )
            webhook["secret_available"] = bool(webhook.get("secret"))
        if not key.get("secret_available") or not key.get("key"):
            raise OutcomeUnknown("Nerve key recovery lost one-time credential")
        if not webhook.get("secret_available") or not webhook.get("secret"):
            raise OutcomeUnknown("Nerve webhook recovery lost one-time credential")
        refs = _Refs(
            household_id=parsed.household_id,
            stable_ref=idempotency_key,
            org_id=org_id,
            grant_id=str(grant.get("id", "")),
            inbox_id=str(inbox.get("id", "")),
            key_id=str(key.get("id", "")),
            webhook_id=str(webhook.get("id", "")),
            address=str(inbox.get("address", address)),
            org_external_ref=org_external_ref,
        )
        if not all((refs.grant_id, refs.inbox_id, refs.key_id, refs.webhook_id)):
            raise OutcomeUnknown("Nerve resource identity is incomplete")
        self._probe_or_wait(refs, str(key["key"]))
        bundle = json.dumps(
            {"api_key": key["key"], "webhook_signing_key": webhook["secret"]},
            sort_keys=True,
            separators=(",", ":"),
        )
        return self._result(refs, secret=bundle)

    def reconcile(
        self, intent: dict[str, Any], idempotency_key: str
    ) -> ProvisionResult:
        """Resume the idempotent graph using the original durable intent."""
        return self.ensure(intent, idempotency_key)

    def _result(self, refs: _Refs, *, secret: str | None = None) -> ProvisionResult:
        return ProvisionResult(
            external_ref=refs.encode(),
            public_result={
                "agent_inbox": refs.address,
                "provider": "nerve",
                "provider_subject": refs.org_id,
                "provider_refs": {
                    "org_id": refs.org_id,
                    "grant_id": refs.grant_id,
                    "inbox_id": refs.inbox_id,
                    "key_id": refs.key_id,
                    "webhook_id": refs.webhook_id,
                },
                "secret_binding_ref": NERVE_SECRET_BINDING,
                "granted_scopes": list(NERVE_SCOPES),
                "masked_external_ref": refs.inbox_id[-8:],
            },
            secret_material=(
                SecretMaterial.from_mapping({NERVE_SECRET_BINDING: secret})
                if secret is not None
                else SecretMaterial()
            ),
        )

    def inspect(self, stable_ref: str) -> InspectResult:
        if stable_ref.startswith("{"):
            refs = _Refs.decode(stable_ref)
            if not refs.org_external_ref:
                return InspectResult(InspectState.UNKNOWN)
            org = self.client.get_org(external_ref=refs.org_external_ref)
            if org.get("org_id") != refs.org_id:
                return InspectResult(InspectState.ABSENT)
            identity_id = self._identity_id(refs.stable_ref)
            if identity_id is None:
                return InspectResult(InspectState.UNKNOWN)
            return self._recover_and_probe(refs, identity_id)
        identity_id = self._identity_id(stable_ref)
        if identity_id is None:
            return InspectResult(InspectState.UNKNOWN)
        household_id = stable_ref.split(":", 1)[0]
        org_external_ref = email_org_external_ref(household_id, identity_id)
        org = self.client.get_org(external_ref=org_external_ref)
        org_id = str(org.get("org_id", ""))
        if not org_id:
            return InspectResult(InspectState.ABSENT)
        grant = self._one(
            self.client.list_grants(org_id=org_id),
            self._resource_ref(identity_id, "grant"),
        )
        inbox = self._one(
            self.client.list_inboxes(org_id=org_id),
            self._resource_ref(identity_id, "inbox"),
        )
        key = self._one(
            self.client.list_keys(org_id=org_id),
            self._resource_ref(identity_id, "key"),
        ) or self._one(
            self.client.list_keys(org_id=org_id),
            f"{self._resource_ref(identity_id, 'key')}:recovery",
        )
        webhook = self._one(
            self.client.list_webhooks(org_id=org_id),
            self._resource_ref(identity_id, "webhook"),
        )
        if not all((grant, inbox, key, webhook)):
            return InspectResult(InspectState.UNKNOWN)
        refs = _Refs(
            household_id=household_id,
            stable_ref=stable_ref,
            org_id=org_id,
            grant_id=str(grant["id"]),
            inbox_id=str(inbox["id"]),
            key_id=str(key["id"]),
            webhook_id=str(webhook["id"]),
            address=str(inbox["address"]),
            org_external_ref=org_external_ref,
        )
        return self._recover_and_probe(refs, identity_id)

    def _recover_and_probe(self, refs: _Refs, identity_id: str) -> InspectResult:
        key_ref = self._resource_ref(identity_id, "key")
        keys = self.client.list_keys(org_id=refs.org_id)
        key = self._one(keys, key_ref) or self._one(keys, f"{key_ref}:recovery")
        webhook = self._one(
            self.client.list_webhooks(org_id=refs.org_id),
            self._resource_ref(identity_id, "webhook"),
        )
        if key is None or webhook is None:
            return InspectResult(InspectState.UNKNOWN)
        self.client.delete(f"/v1/keys/{key['id']}", org_id=refs.org_id)
        recovered_key = self.client.issue_key(
            org_id=refs.org_id, external_ref=f"{key_ref}:recovery",
        )
        rotated = self.client.rotate_webhook(
            org_id=refs.org_id, webhook_id=str(webhook["id"])
        )
        if not recovered_key.get("key") or not rotated.get("secret"):
            return InspectResult(
                InspectState.UNKNOWN, error_code="credential_recovery_unknown"
            )
        recovered_refs = _Refs(
            household_id=refs.household_id,
            stable_ref=refs.stable_ref,
            org_id=refs.org_id,
            grant_id=refs.grant_id,
            inbox_id=refs.inbox_id,
            key_id=str(recovered_key["id"]),
            webhook_id=str(webhook["id"]),
            address=refs.address,
            org_external_ref=refs.org_external_ref,
        )
        if not self.client.attachment_feature_enabled(
            api_key=str(recovered_key["key"]), expected_org_id=refs.org_id
        ):
            pending = self._pending(recovered_refs)
            # The reference carries the ROTATED key id: the probe above
            # consumed the old one. Dropping it here left the worker with no
            # reference to validate, and a pending flag became outcome_unknown.
            return InspectResult(
                InspectState.PENDING,
                error_code=pending.code,
                public_result=pending.public_result,
                external_ref=pending.external_ref,
            )
        bundle = json.dumps(
            {
                "api_key": recovered_key["key"],
                "webhook_signing_key": rotated["secret"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return InspectResult(
            InspectState.READY, self._result(recovered_refs, secret=bundle)
        )

    def _teardown_by_org_ref(self, org_external_ref: str) -> InspectResult:
        """Delete everything under an org named by its computed reference.

        The path a withdrawal takes when the provider call that created the org
        never recorded what it made. `get_org` is a plain GET and every list
        below is read-only: this discovers, it does not provision, and it
        issues no key and rotates no webhook — which is what made `inspect`
        unusable here.

        Idempotent by construction. A missing org means nothing was created, and
        every delete tolerates a 404, so an operator can run this twice.
        """
        org = self.client.get_org(external_ref=org_external_ref)
        org_id = str(org.get("org_id", ""))
        if not org_id:
            return InspectResult(InspectState.ABSENT)
        for webhook in self.client.list_webhooks(org_id=org_id):
            self.client.delete(f"/v1/webhooks/{webhook['id']}", org_id=org_id)
        for key in self.client.list_keys(org_id=org_id):
            self.client.delete(f"/v1/keys/{key['id']}", org_id=org_id)
        for inbox in self.client.list_inboxes(org_id=org_id):
            self.client.delete(f"/v1/inboxes/{inbox['id']}", org_id=org_id)
        for grant in self.client.list_grants(org_id=org_id):
            self.client.delete(f"/v1/domain-grants/{grant['id']}")
        self.client.delete(f"/v1/orgs/{org_id}")
        return InspectResult(InspectState.ABSENT)

    def deprovision(self, external_ref: str) -> InspectResult:
        if external_ref.startswith(ORG_TEARDOWN_REF_PREFIX):
            return self._teardown_by_org_ref(
                external_ref[len(ORG_TEARDOWN_REF_PREFIX) :]
            )
        refs = _Refs.decode(external_ref)
        identity_id = self._identity_id(refs.stable_ref)
        self.client.delete(f"/v1/webhooks/{refs.webhook_id}", org_id=refs.org_id)
        key_ids = {refs.key_id}
        if identity_id is not None:
            key_ref = self._resource_ref(identity_id, "key")
            key_ids.update(
                str(item["id"])
                for item in self.client.list_keys(org_id=refs.org_id)
                if item.get("external_ref") in {key_ref, f"{key_ref}:recovery"}
            )
        for key_id in sorted(key_ids):
            self.client.delete(f"/v1/keys/{key_id}", org_id=refs.org_id)
        self.client.delete(f"/v1/inboxes/{refs.inbox_id}", org_id=refs.org_id)
        self.client.delete(f"/v1/domain-grants/{refs.grant_id}")
        self.client.delete(f"/v1/orgs/{refs.org_id}")
        return InspectResult(InspectState.ABSENT)

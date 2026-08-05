from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from control_plane.crypto import SecretMaterial
from control_plane.email.models import EmailOption, EmailProvisionIntent
from control_plane.providers.email.nerve_client import NerveAdminClient
from control_plane.provisioning.contracts import (
    InspectResult,
    InspectState,
    OutcomeUnknown,
    ProviderRejected,
    ProvisionResult,
)

NERVE_SECRET_BINDING = "ABROLIA_NERVE_EMAIL_CREDENTIALS"
NERVE_SCOPES = ("nerve:email.read", "nerve:email.send")


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

    def ensure(self, intent: dict[str, Any], idempotency_key: str) -> ProvisionResult:
        parsed = EmailProvisionIntent.model_validate(intent)
        if parsed.option is not EmailOption.MANAGED_ABROLIA:
            raise ProviderRejected("managed Nerve provider received another email option")
        local_part = parsed.selection.get("local_part")
        if not isinstance(local_part, str):
            raise ProviderRejected("managed address is missing")
        address = f"{local_part}@abrolia.com"
        org = self.client.ensure_org(household_id=parsed.household_id)
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
        )
        if not all((refs.grant_id, refs.inbox_id, refs.key_id, refs.webhook_id)):
            raise OutcomeUnknown("Nerve resource identity is incomplete")
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
            org = self.client.get_org(household_id=refs.household_id)
            return (
                InspectResult(InspectState.READY, self._result(refs))
                if org.get("org_id") == refs.org_id
                else InspectResult(InspectState.ABSENT)
            )
        parts = stable_ref.split(":")
        if len(parts) != 5 or parts[1] != "email_identity":
            return InspectResult(InspectState.UNKNOWN)
        household_id, _, identity_id, _, _ = parts
        org = self.client.get_org(household_id=household_id)
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
        self.client.delete(f"/v1/keys/{key['id']}", org_id=org_id)
        recovered_key = self.client.issue_key(
            org_id=org_id,
            external_ref=f"{self._resource_ref(identity_id, 'key')}:recovery",
        )
        rotated = self.client.rotate_webhook(
            org_id=org_id, webhook_id=str(webhook["id"])
        )
        if not recovered_key.get("key") or not rotated.get("secret"):
            return InspectResult(
                InspectState.UNKNOWN, error_code="credential_recovery_unknown"
            )
        refs = _Refs(
            household_id=household_id,
            stable_ref=stable_ref,
            org_id=org_id,
            grant_id=str(grant["id"]),
            inbox_id=str(inbox["id"]),
            key_id=str(recovered_key["id"]),
            webhook_id=str(webhook["id"]),
            address=str(inbox["address"]),
        )
        bundle = json.dumps(
            {
                "api_key": recovered_key["key"],
                "webhook_signing_key": rotated["secret"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return InspectResult(InspectState.READY, self._result(refs, secret=bundle))

    def deprovision(self, external_ref: str) -> InspectResult:
        refs = _Refs.decode(external_ref)
        self.client.delete(f"/v1/webhooks/{refs.webhook_id}", org_id=refs.org_id)
        self.client.delete(f"/v1/keys/{refs.key_id}", org_id=refs.org_id)
        self.client.delete(f"/v1/inboxes/{refs.inbox_id}", org_id=refs.org_id)
        self.client.delete(f"/v1/domain-grants/{refs.grant_id}")
        self.client.delete(f"/v1/orgs/{refs.org_id}")
        return InspectResult(InspectState.ABSENT)

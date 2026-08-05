from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from control_plane.crypto import SecretMaterial
from control_plane.email.domain_policy import canonicalize_mailbox, domain_guidance
from control_plane.email.models import (
    EmailDnsPublicStatus,
    EmailOption,
    EmailProvisionIntent,
)
from control_plane.providers.email.nerve_client import (
    NerveAdminClient,
    email_org_external_ref,
)
from control_plane.providers.email.nerve_managed import (
    NERVE_SCOPES,
    NERVE_SECRET_BINDING,
)
from control_plane.provisioning.contracts import (
    InspectResult,
    InspectState,
    OutcomeUnknown,
    ProviderRejected,
    ProviderWaiting,
    ProvisionResult,
)


@dataclass(frozen=True)
class _ByoRefs:
    household_id: str
    stable_ref: str
    org_id: str
    domain_id: str
    domain: str
    inbox_id: str = ""
    key_id: str = ""
    webhook_id: str = ""
    address: str = ""
    org_external_ref: str = ""

    def encode(self) -> str:
        return json.dumps(self.__dict__, sort_keys=True, separators=(",", ":"))

    @classmethod
    def decode(cls, value: str) -> _ByoRefs:
        try:
            return cls(**json.loads(value))
        except (TypeError, ValueError) as error:
            raise ProviderRejected("invalid Nerve domain resource reference") from error


class NerveByoDomainProvisioner:
    email_public_provider = "nerve"

    def __init__(self, client: NerveAdminClient) -> None:
        self.client = client

    @staticmethod
    def _resource_ref(identity_id: str, kind: str) -> str:
        return f"arbolia:email:{identity_id}:{kind}"

    @staticmethod
    def _public_dns(
        *, domain: str, records: list[dict[str, Any]], checks: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        allowed = {"type", "host", "value", "priority", "purpose", "required"}
        safe_records = [
            {key: record[key] for key in allowed if key in record}
            for record in records
            if isinstance(record, dict)
        ]
        safe_checks = {
            key: bool(value)
            for key, value in (checks or {}).items()
            if key in {"ownership", "mx", "spf", "dkim", "dmarc"}
        }
        public = {
            "state": "dns_required",
            "domain": domain,
            "dns_records": safe_records,
            "record_status": safe_checks,
        }
        if domain_guidance(domain).apex_mx_risk:
            public["mx_change_warning"] = (
                "Changing apex MX may interrupt existing family mail; use a dedicated subdomain."
            )
        try:
            return EmailDnsPublicStatus.model_validate(public).model_dump(
                mode="json", exclude_none=True
            )
        except ValueError as error:
            raise OutcomeUnknown("Nerve returned invalid DNS instructions") from error

    def _domain_and_dns(
        self, parsed: EmailProvisionIntent
    ) -> tuple[_ByoRefs, dict[str, Any], dict[str, Any]]:
        domain, local_part = canonicalize_mailbox(
            str(parsed.selection.get("domain", "")),
            str(parsed.selection.get("local_part", "")),
        )
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
        envelope = self.client.ensure_domain(
            org_id=org_id,
            domain=domain,
            external_ref=self._resource_ref(parsed.identity_id, "domain"),
        )
        domain_result = envelope.get("domain", envelope)
        domain_id = str(domain_result.get("id", ""))
        if not domain_id:
            raise OutcomeUnknown("Nerve domain identity is missing")
        dns = self.client.domain_dns(org_id=org_id, domain_id=domain_id)
        refs = _ByoRefs(
            household_id=parsed.household_id,
            stable_ref="",
            org_id=org_id,
            domain_id=domain_id,
            domain=domain,
            address=f"{local_part}@{domain}",
            org_external_ref=org_external_ref,
        )
        return refs, domain_result, dns

    def ensure(self, intent: dict[str, Any], idempotency_key: str) -> ProvisionResult:
        parsed = EmailProvisionIntent.model_validate(intent)
        if parsed.option is not EmailOption.OWN_DOMAIN:
            raise ProviderRejected("BYO-domain provider received another email option")
        refs, domain_result, dns = self._domain_and_dns(parsed)
        refs = _ByoRefs(**{**refs.__dict__, "stable_ref": idempotency_key})
        if domain_result.get("status") != "active":
            raise ProviderWaiting(
                "DNS verification is required",
                public_result=self._public_dns(
                    domain=refs.domain,
                    records=list(dns.get("dns_records", [])),
                ),
                external_ref=refs.encode(),
            )
        return self._finish(parsed, refs)

    def reconcile(
        self, intent: dict[str, Any], idempotency_key: str
    ) -> ProvisionResult:
        return self.ensure(intent, idempotency_key)

    def inspect_intent(self, request: dict[str, Any], stable_ref: str) -> InspectResult:
        parsed = EmailProvisionIntent.model_validate({
            "identity_id": request["email_identity_id"],
            "household_id": request["household_id"],
            "option": request["option"],
            "selection": request["selection"],
            "secret_namespace_ref": request["secret_namespace_ref"],
        })
        refs, _, dns = self._domain_and_dns(parsed)
        verified = self.client.verify_domain(
            org_id=refs.org_id, domain_id=refs.domain_id
        )
        domain_result = verified.get("domain", verified)
        raw_checks = verified.get("checks", {})
        record_checks = {
            "ownership": bool(raw_checks.get("ownership_verified")),
            "mx": bool(domain_result.get("mx_verified")),
            "spf": bool(domain_result.get("spf_verified")),
            "dkim": bool(domain_result.get("dkim_verified")),
            "dmarc": bool(domain_result.get("dmarc_verified")),
        }
        if domain_result.get("status") != "active":
            return InspectResult(
                InspectState.PENDING,
                public_result=self._public_dns(
                    domain=refs.domain,
                    records=list(dns.get("dns_records", [])),
                    checks=record_checks,
                ),
            )
        refs = _ByoRefs(**{**refs.__dict__, "stable_ref": stable_ref})
        return InspectResult(InspectState.READY, self._finish(parsed, refs))

    def _finish(
        self, parsed: EmailProvisionIntent, refs: _ByoRefs
    ) -> ProvisionResult:
        inbox_ref = self._resource_ref(parsed.identity_id, "inbox")
        inbox_envelope = self.client.ensure_inbox(
            org_id=refs.org_id,
            address=refs.address,
            external_ref=inbox_ref,
            domain_id=refs.domain_id,
        )
        inbox = inbox_envelope.get("inbox", inbox_envelope)
        key_ref = self._resource_ref(parsed.identity_id, "key")
        key = self.client.issue_key(org_id=refs.org_id, external_ref=key_ref)
        webhook_ref = self._resource_ref(parsed.identity_id, "webhook")
        webhook = self.client.ensure_webhook(
            org_id=refs.org_id,
            url=f"https://{parsed.secret_namespace_ref}.fly.dev/v1/email/nerve/webhook",
            external_ref=webhook_ref,
        )
        if not key.get("key"):
            self.client.delete(f"/v1/keys/{key.get('id', '')}", org_id=refs.org_id)
            key = self.client.issue_key(
                org_id=refs.org_id, external_ref=f"{key_ref}:recovery"
            )
        if not webhook.get("secret"):
            webhook = self.client.rotate_webhook(
                org_id=refs.org_id, webhook_id=str(webhook.get("id", ""))
            )
        if not key.get("key") or not webhook.get("secret"):
            raise OutcomeUnknown("Nerve credential recovery is incomplete")
        complete = _ByoRefs(
            **{
                **refs.__dict__,
                "inbox_id": str(inbox.get("id", "")),
                "key_id": str(key.get("id", "")),
                "webhook_id": str(webhook.get("id", "")),
                "address": str(inbox.get("address", refs.address)),
            }
        )
        if not all((complete.inbox_id, complete.key_id, complete.webhook_id)):
            raise OutcomeUnknown("Nerve resource identity is incomplete")
        bundle = json.dumps(
            {"api_key": key["key"], "webhook_signing_key": webhook["secret"]},
            sort_keys=True,
            separators=(",", ":"),
        )
        return ProvisionResult(
            external_ref=complete.encode(),
            public_result={
                "agent_inbox": complete.address,
                "provider": "nerve",
                "provider_subject": complete.org_id,
                "provider_refs": {
                    "org_id": complete.org_id,
                    "domain_id": complete.domain_id,
                    "inbox_id": complete.inbox_id,
                    "key_id": complete.key_id,
                    "webhook_id": complete.webhook_id,
                },
                "secret_binding_ref": NERVE_SECRET_BINDING,
                "granted_scopes": list(NERVE_SCOPES),
                "masked_external_ref": complete.inbox_id[-8:],
            },
            secret_material=SecretMaterial.from_mapping({
                NERVE_SECRET_BINDING: bundle
            }),
        )

    def inspect(self, stable_ref: str) -> InspectResult:
        if not stable_ref.startswith("{"):
            return InspectResult(InspectState.UNKNOWN)
        refs = _ByoRefs.decode(stable_ref)
        if not refs.org_external_ref:
            return InspectResult(InspectState.UNKNOWN)
        org = self.client.get_org(external_ref=refs.org_external_ref)
        return (
            InspectResult(InspectState.READY)
            if org.get("org_id") == refs.org_id
            else InspectResult(InspectState.ABSENT)
        )

    def deprovision(self, external_ref: str) -> InspectResult:
        refs = _ByoRefs.decode(external_ref)
        if refs.webhook_id:
            self.client.delete(f"/v1/webhooks/{refs.webhook_id}", org_id=refs.org_id)
        if refs.key_id:
            self.client.delete(f"/v1/keys/{refs.key_id}", org_id=refs.org_id)
        if refs.inbox_id:
            self.client.delete(f"/v1/inboxes/{refs.inbox_id}", org_id=refs.org_id)
        self.client.delete(f"/v1/domains/{refs.domain_id}", org_id=refs.org_id)
        self.client.delete(f"/v1/orgs/{refs.org_id}")
        return InspectResult(InspectState.ABSENT)

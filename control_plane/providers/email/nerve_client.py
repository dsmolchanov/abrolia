from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from control_plane.provisioning.contracts import (
    OutcomeUnknown,
    ProviderRateLimited,
    ProviderRejected,
)


def email_org_external_ref(household_id: str, identity_id: str) -> str:
    """Bind a Nerve org to one reconnect-safe email identity generation."""
    return f"arbolia:household:{household_id}:email:{identity_id}"


#: A teardown reference the control plane can COMPUTE, for the state a provider
#: call created before it timed out. `deprovision` normally takes ids the
#: provider assigned, which nothing on this side can reconstruct — so a
#: withdrawal had no way to reach an org whose creation was never recorded.
#: This form carries the org's `external_ref` instead, which is derived from the
#: household and the identity before the first call, and both Nerve
#: provisioners resolve it with a read-only lookup.
ORG_TEARDOWN_REF_PREFIX = "nerve-org:"


def org_teardown_ref(household_id: str, identity_id: str) -> str:
    return f"{ORG_TEARDOWN_REF_PREFIX}{email_org_external_ref(household_id, identity_id)}"


@dataclass(frozen=True)
class NerveAdminSettings:
    base_url: str
    admin_key: str = field(repr=False)
    platform_org_id: str
    platform_domain_id: str


class NerveAdminClient:
    """Small synchronous client for Nerve's durable bootstrap-admin contract."""

    def __init__(
        self,
        settings: NerveAdminSettings,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        if not settings.base_url.startswith("https://"):
            raise ValueError("Nerve admin origin must use HTTPS")
        if not all((settings.admin_key, settings.platform_org_id, settings.platform_domain_id)):
            raise ValueError("Nerve admin settings are incomplete")
        self.settings = settings
        self.client = client or httpx.Client(timeout=20.0)

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        allow_not_found: bool = False,
    ) -> dict[str, Any]:
        try:
            response = self.client.request(
                method,
                f"{self.settings.base_url.rstrip('/')}{path}",
                headers={"X-API-Key": self.settings.admin_key},
                json=json,
                params=params,
            )
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise OutcomeUnknown("Nerve request outcome is unknown") from error
        if response.status_code == 404 and allow_not_found:
            return {}
        if response.status_code == 429:
            try:
                retry_after = float(response.headers.get("Retry-After", "30"))
            except ValueError:
                retry_after = 30.0
            raise ProviderRateLimited(max(1.0, retry_after))
        if response.status_code >= 500:
            raise OutcomeUnknown("Nerve request outcome is unknown")
        if response.status_code in {401, 403, 409, 422}:
            raise ProviderRejected("Nerve rejected the managed identity request")
        if not 200 <= response.status_code < 300:
            raise ProviderRejected(f"Nerve request failed with HTTP {response.status_code}")
        if response.status_code == 204 or not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as error:
            raise OutcomeUnknown("Nerve returned an unreadable response") from error
        if not isinstance(payload, dict):
            raise OutcomeUnknown("Nerve returned an unexpected response")
        return payload

    def ensure_org(self, *, household_id: str, identity_id: str) -> dict[str, Any]:
        return self.request(
            "POST",
            "/v1/orgs",
            json={
                "name": f"Abrolia household {household_id}",
                "external_ref": email_org_external_ref(household_id, identity_id),
            },
        )

    def get_org(self, *, external_ref: str) -> dict[str, Any]:
        return self.request(
            "GET",
            "/v1/orgs",
            params={"external_ref": external_ref},
            allow_not_found=True,
        )

    def ensure_grant(self, *, org_id: str, external_ref: str) -> dict[str, Any]:
        return self.request(
            "POST",
            "/v1/domain-grants",
            json={
                "owner_org_id": self.settings.platform_org_id,
                "domain_id": self.settings.platform_domain_id,
                "grantee_org_id": org_id,
                "external_ref": external_ref,
            },
        )

    def list_grants(self, *, org_id: str) -> list[dict[str, Any]]:
        payload = self.request(
            "GET", "/v1/domain-grants", params={"grantee_org_id": org_id}
        )
        return list(payload.get("domain_grants", []))

    def ensure_inbox(
        self,
        *,
        org_id: str,
        address: str,
        external_ref: str,
        domain_id: str | None = None,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            "/v1/inboxes",
            json={
                "org_id": org_id,
                "address": address,
                "domain_id": domain_id or self.settings.platform_domain_id,
                "external_ref": external_ref,
            },
        )

    def list_inboxes(self, *, org_id: str) -> list[dict[str, Any]]:
        payload = self.request("GET", "/v1/inboxes", params={"org_id": org_id})
        return list(payload.get("inboxes", []))

    def ensure_domain(
        self, *, org_id: str, domain: str, external_ref: str
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            "/v1/domains",
            json={
                "org_id": org_id,
                "domain": domain,
                "dkim_method": "cname",
                "external_ref": external_ref,
            },
        )

    def list_domains(self, *, org_id: str) -> list[dict[str, Any]]:
        return list(
            self.request("GET", "/v1/domains", params={"org_id": org_id}).get(
                "domains", []
            )
        )

    def domain_dns(self, *, org_id: str, domain_id: str) -> dict[str, Any]:
        return self.request(
            "GET",
            "/v1/domains/dns",
            params={"org_id": org_id, "domain_id": domain_id},
        )

    def verify_domain(self, *, org_id: str, domain_id: str) -> dict[str, Any]:
        return self.request(
            "POST",
            "/v1/domains/verify",
            json={"org_id": org_id, "domain_id": domain_id},
        )

    def issue_key(self, *, org_id: str, external_ref: str) -> dict[str, Any]:
        return self.request(
            "POST",
            "/v1/keys",
            json={
                "org_id": org_id,
                "label": "abrolia-household-runtime",
                "scopes": ["nerve:email.read", "nerve:email.send"],
                "external_ref": external_ref,
            },
        )

    def list_keys(self, *, org_id: str) -> list[dict[str, Any]]:
        return list(
            self.request("GET", "/v1/keys", params={"org_id": org_id}).get(
                "keys", []
            )
        )

    def ensure_webhook(
        self, *, org_id: str, url: str, external_ref: str
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            "/v1/webhooks",
            json={
                "org_id": org_id,
                "url": url,
                "events": ["email.received"],
                "external_ref": external_ref,
            },
        )

    def list_webhooks(self, *, org_id: str) -> list[dict[str, Any]]:
        return list(
            self.request("GET", "/v1/webhooks", params={"org_id": org_id}).get(
                "webhooks", []
            )
        )

    def rotate_webhook(self, *, org_id: str, webhook_id: str) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/v1/webhooks/{webhook_id}/rotate-secret",
            params={"org_id": org_id},
        )

    def attachment_feature_enabled(
        self, *, api_key: str, expected_org_id: str
    ) -> bool:
        """Resolve the attachment gate as the household runtime principal."""
        try:
            response = self.client.get(
                f"{self.settings.base_url.rstrip('/')}/internal/feature-flags/attachments",
                headers={"X-Nerve-Cloud-Key": api_key},
            )
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise OutcomeUnknown("Nerve attachment readiness is unknown") from error
        if response.status_code == 429:
            try:
                retry_after = float(response.headers.get("Retry-After", "30"))
            except ValueError:
                retry_after = 30.0
            raise ProviderRateLimited(max(1.0, retry_after))
        if response.status_code >= 500:
            raise OutcomeUnknown("Nerve attachment readiness is unknown")
        if response.status_code in {401, 403}:
            raise ProviderRejected("Nerve rejected the household readiness probe")
        if response.status_code != 200:
            raise ProviderRejected(
                f"Nerve readiness probe failed with HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise OutcomeUnknown("Nerve returned unreadable attachment readiness") from error
        if (
            not isinstance(payload, dict)
            or payload.get("flag") != "attachments"
            or payload.get("org_id") != expected_org_id
            or not isinstance(payload.get("enabled"), bool)
            or not isinstance(payload.get("cache_ttl_seconds"), int)
            or payload["cache_ttl_seconds"] <= 0
        ):
            raise OutcomeUnknown("Nerve returned mismatched attachment readiness")
        return payload["enabled"]

    def delete(self, path: str, *, org_id: str | None = None) -> None:
        params = {"org_id": org_id} if org_id else None
        self.request("DELETE", path, params=params, allow_not_found=True)

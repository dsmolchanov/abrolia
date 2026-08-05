from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from control_plane.provisioning.contracts import (
    OutcomeUnknown,
    ProviderRateLimited,
    ProviderRejected,
)


@dataclass(frozen=True)
class NerveAdminSettings:
    base_url: str
    admin_key: str
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

    def ensure_org(self, *, household_id: str) -> dict[str, Any]:
        return self.request(
            "POST",
            "/v1/orgs",
            json={
                "name": f"Abrolia household {household_id}",
                "external_ref": f"arbolia:household:{household_id}",
            },
        )

    def get_org(self, *, household_id: str) -> dict[str, Any]:
        return self.request(
            "GET",
            "/v1/orgs",
            params={"external_ref": f"arbolia:household:{household_id}"},
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
        self, *, org_id: str, address: str, external_ref: str
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            "/v1/inboxes",
            json={
                "org_id": org_id,
                "address": address,
                "domain_id": self.settings.platform_domain_id,
                "external_ref": external_ref,
            },
        )

    def list_inboxes(self, *, org_id: str) -> list[dict[str, Any]]:
        payload = self.request("GET", "/v1/inboxes", params={"org_id": org_id})
        return list(payload.get("inboxes", []))

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

    def delete(self, path: str, *, org_id: str | None = None) -> None:
        params = {"org_id": org_id} if org_id else None
        self.request("DELETE", path, params=params, allow_not_found=True)

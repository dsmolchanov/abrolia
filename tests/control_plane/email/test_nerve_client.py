from __future__ import annotations

import httpx
import pytest

from control_plane.providers.email.nerve_client import (
    NerveAdminClient,
    NerveAdminSettings,
)
from control_plane.provisioning.contracts import (
    OutcomeUnknown,
    ProviderRateLimited,
    ProviderRejected,
)


def _client(handler) -> NerveAdminClient:
    return NerveAdminClient(
        NerveAdminSettings(
            base_url="https://nerve.example.test",
            admin_key="synthetic-admin-key",
            platform_org_id="platform-org",
            platform_domain_id="platform-domain",
        ),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_client_uses_bootstrap_admin_and_exact_platform_grant_contract() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["header"] = request.headers["X-API-Key"]
        captured["body"] = request.read()
        return httpx.Response(201, json={"id": "grant-1", "created": True})

    result = _client(handler).ensure_grant(
        org_id="household-org", external_ref="arbolia:email:identity-1:grant"
    )

    assert result["id"] == "grant-1"
    assert captured == {
        "method": "POST",
        "path": "/v1/domain-grants",
        "header": "synthetic-admin-key",
        "body": (
            b'{"owner_org_id":"platform-org","domain_id":"platform-domain",'
            b'"grantee_org_id":"household-org",'
            b'"external_ref":"arbolia:email:identity-1:grant"}'
        ),
    }


@pytest.mark.parametrize(
    ("status", "error"),
    ((403, ProviderRejected), (409, ProviderRejected), (503, OutcomeUnknown)),
)
def test_client_maps_provider_failures_without_response_body_leak(status, error) -> None:
    client = _client(
        lambda request: httpx.Response(
            status, content=b"private-provider-response", request=request
        )
    )

    with pytest.raises(error) as raised:
        client.ensure_org(household_id="household-1")

    assert "private-provider-response" not in str(raised.value)


def test_client_preserves_bounded_rate_limit_hint() -> None:
    client = _client(
        lambda request: httpx.Response(
            429, headers={"Retry-After": "7"}, request=request
        )
    )

    with pytest.raises(ProviderRateLimited) as raised:
        client.ensure_org(household_id="household-1")

    assert raised.value.retry_after == 7


@pytest.mark.parametrize("enabled", (False, True))
def test_client_probes_exact_household_attachment_contract(enabled: bool) -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(
            method=request.method,
            path=request.url.path,
            runtime_key=request.headers.get("X-Nerve-Cloud-Key"),
            admin_key=request.headers.get("X-API-Key"),
        )
        return httpx.Response(
            200,
            json={
                "flag": "attachments",
                "org_id": "household-org",
                "enabled": enabled,
                "cache_ttl_seconds": 30,
            },
        )

    assert (
        _client(handler).attachment_feature_enabled(
            api_key="synthetic-runtime-key", expected_org_id="household-org"
        )
        is enabled
    )
    assert captured == {
        "method": "GET",
        "path": "/internal/feature-flags/attachments",
        "runtime_key": "synthetic-runtime-key",
        "admin_key": None,
    }


@pytest.mark.parametrize(
    "response",
    (
        httpx.Response(503),
        httpx.Response(
            200,
            json={
                "flag": "attachments",
                "org_id": "another-org",
                "enabled": True,
                "cache_ttl_seconds": 30,
            },
        ),
        httpx.Response(200, content=b"not-json"),
    ),
)
def test_client_attachment_probe_failures_never_report_ready(response) -> None:
    client = _client(lambda request: response)

    with pytest.raises(OutcomeUnknown):
        client.attachment_feature_enabled(
            api_key="synthetic-runtime-key", expected_org_id="household-org"
        )

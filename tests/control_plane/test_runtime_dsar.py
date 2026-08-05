from __future__ import annotations

import httpx
import pytest

from control_plane.crypto import LookupHasher
from control_plane.privacy.runtime import (
    PrivateRuntimeDsarClient,
    RuntimeBoundaryError,
)
from control_plane.provisioning.contracts import InspectState

RUNTIME_REF = "abrolia-hh-aaaaaaaaaaaaaaaaaaaaaaaaaa"


def test_private_runtime_export_uses_bound_hmac_and_internal_dns_only() -> None:
    hasher = LookupHasher(b"d" * 32)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"exported_at": 123.0, "tables": {"events": []}})

    client = PrivateRuntimeDsarClient(
        hasher, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    payload = client.export(RUNTIME_REF)

    assert payload["tables"] == {"events": []}
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == (
        f"http://{RUNTIME_REF}.internal:8080/internal/v1/dsar/export"
    )
    expected = hasher.digest(f"runtime-dsar:{RUNTIME_REF}")
    assert request.headers["authorization"] == f"Bearer {expected}"
    assert expected not in repr(client)


def test_runtime_dsar_refuses_redirects_unmanaged_refs_and_unproven_delete() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.endswith("/delete"):
            return httpx.Response(503, json={"detail": "provider-body-secret-canary"})
        return httpx.Response(307, headers={"Location": "https://foreign.invalid"})

    client = PrivateRuntimeDsarClient(
        LookupHasher(b"e" * 32),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.delete(RUNTIME_REF) is InspectState.UNKNOWN
    with pytest.raises(RuntimeBoundaryError) as redirected:
        client.export(RUNTIME_REF)
    assert "provider-body-secret-canary" not in str(redirected.value)
    with pytest.raises(RuntimeBoundaryError):
        client.export("foreign-runtime")
    assert len(seen) == 2


def test_runtime_dsar_accepts_only_explicit_absent_delete_receipt() -> None:
    responses = iter(
        [
            httpx.Response(200, json={"state": "present"}),
            httpx.Response(200, json={"state": "absent"}),
        ]
    )
    client = PrivateRuntimeDsarClient(
        LookupHasher(b"f" * 32),
        client=httpx.Client(
            transport=httpx.MockTransport(lambda _request: next(responses))
        ),
    )

    assert client.delete(RUNTIME_REF) is InspectState.UNKNOWN
    assert client.delete(RUNTIME_REF) is InspectState.ABSENT

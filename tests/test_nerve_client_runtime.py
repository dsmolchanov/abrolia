from __future__ import annotations

import json

import httpx
import pytest

from hermes_cloud.email.nerve_client import (
    MCP_PROTOCOL_VERSION,
    NerveAttachmentPending,
    NerveCredentialRevoked,
    NerveEmailClient,
)


def test_client_uses_tenant_key_exact_rest_and_mcp_contract() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/threads/thread-1"):
            return httpx.Response(
                200,
                json={"thread": {"id": "thread-1"}, "messages": []},
                request=request,
            )
        body = json.loads(request.content)
        if body["method"] == "initialize":
            return httpx.Response(
                200,
                headers={"MCP-Session-Id": "session-1"},
                json={"jsonrpc": "2.0", "id": body["id"], "result": {}},
                request=request,
            )
        assert body["method"] == "tools/call"
        assert body["params"]["name"] == "compose_email"
        assert body["params"]["arguments"]["idempotency_key"] == "effect-child"
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"message_id": "message-1", "status": "queued"},
            },
            request=request,
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = NerveEmailClient(
        api_key="tenant-key",
        runtime_url="https://runtime.example.test",
        rest_url="https://rest.example.test",
        client=http,
    )

    assert client.get_thread("inbox-1", "thread-1")["thread"]["id"] == "thread-1"
    result = client.compose_email(
        inbox_id="inbox-1",
        to="school@example.test",
        subject="Trip",
        body="Approved",
        html=None,
        idempotency_key="effect-child",
        attachments=[],
    )

    assert result["message_id"] == "message-1"
    assert all(request.headers["X-Nerve-Cloud-Key"] == "tenant-key" for request in seen)
    assert seen[-1].headers["MCP-Protocol-Version"] == MCP_PROTOCOL_VERSION
    assert seen[-1].headers["MCP-Session-Id"] == "session-1"


def test_attachment_pending_and_revoked_credentials_are_typed() -> None:
    statuses = iter((202, 401))

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(statuses)
        return httpx.Response(
            status,
            headers={"Retry-After": "2.5"},
            request=request,
        )

    client = NerveEmailClient(
        api_key="tenant-key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(NerveAttachmentPending) as pending:
        client.get_attachment("message-1", "attachment-1")
    assert pending.value.retry_after == 2.5
    with pytest.raises(NerveCredentialRevoked):
        client.get_attachment("message-1", "attachment-1")

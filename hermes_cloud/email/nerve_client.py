"""Small synchronous client for the pinned Nerve 0.2.0 wire contract."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

MCP_PROTOCOL_VERSION = "2025-11-25"
DEFAULT_RUNTIME_URL = "https://nerve-runtime.fly.dev"
DEFAULT_REST_URL = "https://nerve.email"


class NerveError(RuntimeError):
    """A redacted provider failure safe to persist as a type/code only."""


class NerveCredentialRevoked(NerveError):
    pass


class NerveAttachmentPending(NerveError):
    def __init__(self, retry_after: float = 1.0) -> None:
        super().__init__("attachment_pending")
        self.retry_after = retry_after


class NerveAttachmentUnavailable(NerveError):
    pass


class NerveTransportUnknown(NerveError):
    pass


class NerveEmailClient:
    """REST reads plus idempotent MCP compose using one tenant API key."""

    def __init__(
        self,
        *,
        api_key: str,
        runtime_url: str = DEFAULT_RUNTIME_URL,
        rest_url: str = DEFAULT_REST_URL,
        timeout: float = 30.0,
        max_attempts: int = 3,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key or not runtime_url or not rest_url:
            raise ValueError("Nerve client configuration is incomplete")
        self.api_key = api_key
        self.runtime_url = runtime_url.rstrip("/")
        self.rest_url = rest_url.rstrip("/")
        self.timeout = timeout
        self.max_attempts = max(1, max_attempts)
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None
        self._session_id: str | None = None
        self._request_id = 0
        self._sleep = sleeper

    @property
    def _auth_headers(self) -> dict[str, str]:
        return {"X-Nerve-Cloud-Key": self.api_key}

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> NerveEmailClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def get_thread(self, inbox_id: str, thread_id: str) -> dict[str, Any]:
        response = self._request(
            "GET",
            f"{self.rest_url}/v1/inboxes/{inbox_id}/threads/{thread_id}",
        )
        try:
            payload = response.json()
        except ValueError as error:
            raise NerveError("invalid_thread_response") from error
        if not isinstance(payload, dict):
            raise NerveError("invalid_thread_response")
        return payload

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        response = self._request(
            "GET",
            f"{self.rest_url}/v1/messages/{message_id}/attachments/{attachment_id}",
            attachment=True,
        )
        return response.content

    def compose_email(
        self,
        *,
        inbox_id: str,
        to: str,
        subject: str,
        body: str,
        html: str | None,
        idempotency_key: str,
        attachments: list[dict[str, str]],
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "inbox_id": inbox_id,
            "to": to,
            "subject": subject,
            "body": body,
            "idempotency_key": idempotency_key,
        }
        if html:
            arguments["html"] = html
        if attachments:
            arguments["attachments"] = attachments
        result = self._call_tool("compose_email", arguments)
        if not isinstance(result, dict):
            raise NerveError("invalid_compose_response")
        return result

    def health_check(self) -> bool:
        try:
            self._ensure_session()
        except NerveError:
            return False
        return True

    def _request(
        self, method: str, url: str, *, attachment: bool = False
    ) -> httpx.Response:
        try:
            response = self._client.request(method, url, headers=self._auth_headers)
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise NerveTransportUnknown("nerve_transport_unknown") from error
        if response.status_code in {401, 403}:
            raise NerveCredentialRevoked("nerve_credential_rejected")
        if attachment and response.status_code == 202:
            try:
                retry_after = float(response.headers.get("Retry-After", "1"))
            except ValueError:
                retry_after = 1.0
            raise NerveAttachmentPending(retry_after)
        if attachment and response.status_code in {409, 410}:
            raise NerveAttachmentUnavailable("attachment_unavailable")
        if response.status_code >= 400:
            raise NerveError(f"nerve_http_{response.status_code}")
        return response

    def _ensure_session(self) -> None:
        if self._session_id:
            return
        self._rpc(
            "initialize",
            {
                "clientInfo": {"name": "abrolia-runtime", "version": "0.1.0"},
                "protocolVersion": MCP_PROTOCOL_VERSION,
            },
        )
        if not self._session_id:
            raise NerveError("nerve_session_missing")

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self._ensure_session()
        return self._rpc("tools/call", {"name": name, "arguments": arguments})

    def _rpc(self, method: str, params: dict[str, Any]) -> Any:
        self._request_id += 1
        body = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }
        headers = {
            **self._auth_headers,
            "Content-Type": "application/json",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        }
        if self._session_id:
            headers["MCP-Session-Id"] = self._session_id
        for attempt in range(self.max_attempts):
            try:
                response = self._client.post(
                    f"{self.runtime_url}/mcp", json=body, headers=headers
                )
            except (httpx.TimeoutException, httpx.TransportError) as error:
                if attempt + 1 < self.max_attempts:
                    continue
                raise NerveTransportUnknown("nerve_transport_unknown") from error
            if response.status_code in {401, 403}:
                raise NerveCredentialRevoked("nerve_credential_rejected")
            try:
                payload = response.json()
            except ValueError as error:
                raise NerveError("invalid_mcp_response") from error
            if method == "initialize":
                self._session_id = response.headers.get("MCP-Session-Id")
            error_payload = payload.get("error") if isinstance(payload, dict) else None
            if error_payload:
                code = error_payload.get("code") if isinstance(error_payload, dict) else None
                data = error_payload.get("data", {}) if isinstance(error_payload, dict) else {}
                if code == -32042 and attempt + 1 < self.max_attempts:
                    self._sleep(float(data.get("retry_after_seconds", 1)))
                    continue
                if code == -32000 and "session" in str(error_payload).casefold():
                    self._session_id = None
                raise NerveError(f"nerve_mcp_{code}")
            if response.status_code >= 400:
                raise NerveError(f"nerve_http_{response.status_code}")
            return payload.get("result") if isinstance(payload, dict) else None
        raise NerveTransportUnknown("nerve_retry_exhausted")

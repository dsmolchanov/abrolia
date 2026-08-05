"""Redacted Google token refresh and Gmail REST client."""

from __future__ import annotations

import time
from typing import Any

import httpx

from hermes_cloud.email.google_grant import (
    GoogleGrantStore,
    RefreshedAccess,
)
from hermes_cloud.ingest.gmail_api import (
    GmailAuthRevoked,
    GmailError,
    GmailHistoryExpired,
    GmailQuotaExceeded,
)

TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_URL = "https://gmail.googleapis.com/gmail/v1/users/me"


class GoogleRefreshClient:
    def __init__(self, client_id: str, client_secret: str, http: httpx.Client) -> None:
        self.client_id = client_id
        self._client_secret = client_secret
        self.http = http

    def refresh(self, refresh_credential: str) -> RefreshedAccess:
        try:
            response = self.http.post(
                TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": refresh_credential,
                    "grant_type": "refresh_token",
                },
            )
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise GmailError("gmail_refresh_unavailable") from error
        if response.status_code in {400, 401, 403}:
            raise GmailAuthRevoked("gmail_auth_revoked")
        if response.status_code != 200:
            raise GmailError("gmail_refresh_rejected")
        try:
            body = response.json()
            token = str(body["access_token"])
            expires_in = max(1, int(body.get("expires_in", 3600)))
            rotated = body.get("refresh_token")
        except (KeyError, TypeError, ValueError) as error:
            raise GmailError("gmail_refresh_malformed") from error
        return RefreshedAccess(
            token,
            time.time() + expires_in,
            str(rotated) if rotated else None,
        )


class GmailHttpClient:
    def __init__(
        self,
        grant_store: GoogleGrantStore,
        *,
        identity_id: str,
        revision: int,
        client_id: str,
        client_secret: str,
        http: httpx.Client | None = None,
        clock=time.time,
    ) -> None:
        self.grants = grant_store
        self.identity_id = identity_id
        self.revision = revision
        self.http = http or httpx.Client(timeout=30.0)
        self.refresher = GoogleRefreshClient(client_id, client_secret, self.http)
        self.clock = clock
        self._access: RefreshedAccess | None = None

    def _token(self) -> str:
        if self._access is None or self._access.expires_at <= self.clock() + 30:
            self._access = self.grants.access_token(self.identity_id, self.revision, self.refresher)
        return self._access.access_token

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        headers = {**kwargs.pop("headers", {}), "Authorization": f"Bearer {self._token()}"}
        try:
            response = self.http.request(method, f"{GMAIL_URL}{path}", headers=headers, **kwargs)
        except httpx.TimeoutException as error:
            raise TimeoutError("gmail_timeout") from error
        except httpx.TransportError as error:
            raise ConnectionError("gmail_transport") from error
        if response.status_code in {401, 403}:
            self._access = None
            raise GmailAuthRevoked("gmail_auth_revoked")
        if response.status_code == 404 and path.startswith("/history"):
            raise GmailHistoryExpired("gmail_history_expired")
        if response.status_code == 429:
            try:
                retry_after = float(response.headers.get("Retry-After", "60"))
            except ValueError:
                retry_after = 60.0
            raise GmailQuotaExceeded(retry_after)
        if response.status_code >= 400:
            raise GmailError(f"gmail_http_{response.status_code}")
        try:
            body = response.json()
        except ValueError as error:
            raise GmailError("gmail_response_malformed") from error
        if not isinstance(body, dict):
            raise GmailError("gmail_response_malformed")
        return body

    def profile(self) -> dict[str, Any]:
        return self._request("GET", "/profile")

    def history(self, start_history_id: str, page_token: str | None = None) -> dict[str, Any]:
        params = {
            "startHistoryId": start_history_id,
            "historyTypes": "messageAdded",
            "labelId": "INBOX",
        }
        if page_token:
            params["pageToken"] = page_token
        return self._request("GET", "/history", params=params)

    def message(self, message_id: str) -> dict[str, Any]:
        return self._request("GET", f"/messages/{message_id}", params={"format": "raw"})

    def list_inbox(self, page_token: str | None = None, *, max_results: int) -> dict[str, Any]:
        params: dict[str, Any] = {"labelIds": "INBOX", "maxResults": max_results}
        if page_token:
            params["pageToken"] = page_token
        return self._request("GET", "/messages", params=params)

    def send_raw(self, raw: str) -> dict[str, Any]:
        return self._request("POST", "/messages/send", json={"raw": raw})

    def search_sent(self, query: str) -> list[dict[str, Any]]:
        listed = self._request("GET", "/messages", params={"labelIds": "SENT", "q": query, "maxResults": 10})
        results: list[dict[str, Any]] = []
        for item in listed.get("messages", []):
            message_id = str(item.get("id") or "")
            if not message_id:
                continue
            metadata = self._request(
                "GET",
                f"/messages/{message_id}",
                params={"format": "metadata", "metadataHeaders": "Message-ID"},
            )
            headers = metadata.get("payload", {}).get("headers", [])
            rfc822_id = next(
                (
                    str(header.get("value") or "")
                    for header in headers
                    if str(header.get("name") or "").casefold() == "message-id"
                ),
                "",
            )
            results.append({"id": message_id, "rfc822_message_id": rfc822_id})
        return results

"""Redacted Google token refresh and Gmail REST client."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from hermes_cloud.core.db import Database
from hermes_cloud.email.contracts import EmailBinding
from hermes_cloud.email.google_grant import (
    GoogleGrantError,
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
GMAIL_REQUIRED_SCOPES = frozenset({
    "openid",
    "email",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
})


class GmailConfigurationError(RuntimeError):
    """The provisioned Gmail secret bundle is absent or malformed."""


@dataclass(frozen=True, repr=False)
class GmailCredentialBundle:
    client_id: str
    client_secret: str
    refresh_credential: str
    provider_subject: str
    scopes: tuple[str, ...]
    wrapping_key: bytes


def load_gmail_credential_bundle(
    binding: EmailBinding,
    env: Mapping[str, str],
) -> GmailCredentialBundle:
    if len(binding.secret_names) != 1:
        raise GmailConfigurationError("Gmail credential binding is invalid")
    try:
        payload = json.loads(env.get(binding.secret_names[0], ""))
    except (TypeError, json.JSONDecodeError) as error:
        raise GmailConfigurationError("Gmail credential bundle is unavailable") from error
    required = {
        "client_id",
        "client_secret",
        "refresh_credential",
        "provider_subject",
        "scopes",
        "wrapping_key",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise GmailConfigurationError("Gmail credential bundle is invalid")
    scopes = payload.get("scopes")
    if not isinstance(scopes, list) or not scopes or not all(
        isinstance(item, str) and item for item in scopes
    ) or set(scopes) != GMAIL_REQUIRED_SCOPES:
        raise GmailConfigurationError("Gmail scope bundle is invalid")
    encoded_key = payload.get("wrapping_key")
    try:
        wrapping_key = base64.urlsafe_b64decode(
            str(encoded_key) + "=" * (-len(str(encoded_key)) % 4)
        )
    except (ValueError, TypeError) as error:
        raise GmailConfigurationError("Gmail grant key is invalid") from error
    values = {
        name: payload.get(name)
        for name in ("client_id", "client_secret", "refresh_credential", "provider_subject")
    }
    if len(wrapping_key) != 32 or any(
        not isinstance(value, str) or not value for value in values.values()
    ):
        raise GmailConfigurationError("Gmail credential bundle is invalid")
    return GmailCredentialBundle(
        client_id=values["client_id"],
        client_secret=values["client_secret"],
        refresh_credential=values["refresh_credential"],
        provider_subject=values["provider_subject"],
        scopes=tuple(scopes),
        wrapping_key=wrapping_key,
    )


def ensure_gmail_grant(
    database: Database,
    binding: EmailBinding,
    bundle: GmailCredentialBundle,
) -> GoogleGrantStore:
    store = GoogleGrantStore(database, {1: bundle.wrapping_key}, active_version=1)
    row = database.query_one(
        "SELECT revoked_at FROM oauth_grants WHERE binding_identity_id = ?"
        " AND binding_revision = ?",
        (binding.identity_id, binding.revision),
    )
    if row is None:
        store.put(
            identity_id=binding.identity_id,
            revision=binding.revision,
            refresh_credential=bundle.refresh_credential,
            provider_subject=bundle.provider_subject,
            scopes=bundle.scopes,
        )
    else:
        # A revoked or corrupted durable grant must never be resurrected merely
        # because the original Fly secret still exists during cleanup.
        try:
            store.load(binding.identity_id, binding.revision)
        except GoogleGrantError as error:
            raise GmailConfigurationError("Gmail grant is unavailable") from error
    return store


def build_gmail_client(
    database: Database,
    binding: EmailBinding,
    bundle: GmailCredentialBundle,
    *,
    client_factory: Callable[..., Any] | None = None,
):
    store = ensure_gmail_grant(database, binding, bundle)
    factory = client_factory or GmailHttpClient
    return factory(
        store,
        identity_id=binding.identity_id,
        revision=binding.revision,
        client_id=bundle.client_id,
        client_secret=bundle.client_secret,
    )


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

    def close(self) -> None:
        self.http.close()

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

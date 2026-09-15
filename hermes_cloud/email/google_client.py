"""Redacted Google token refresh and a send-only Gmail REST client.

The client can send and nothing else. Owner decision 2026-09-13: the agent
Gmail grant is `gmail.send` only, so that Google's sensitive-scope verification
suffices and the restricted-scope CASA assessment is never owed. Every read
method — profile, history, message, inbox and Sent listing — went with the
restricted read scope; a call to one would now be an insufficient-scope 403,
which is why none exists to make.
"""

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

TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_URL = "https://gmail.googleapis.com/gmail/v1/users/me"
#: Must equal `control_plane.email.models.GMAIL_EMAIL_SCOPES`;
#: `tests/test_gmail_scope_consistency.py` holds the two together.
GMAIL_REQUIRED_SCOPES = frozenset({
    "openid",
    "email",
    "https://www.googleapis.com/auth/gmail.send",
})
#: Gmail's 403 reasons for usage limits (developers.google.com/workspace/gmail/api/guides/handle-errors).
QUOTA_REASONS = frozenset({
    "rateLimitExceeded",
    "userRateLimitExceeded",
    "dailyLimitExceeded",
    "quotaExceeded",
})


class GmailError(RuntimeError):
    pass


class GmailQuotaExceeded(GmailError):
    def __init__(self, retry_after: float = 60.0) -> None:
        super().__init__("gmail_quota")
        self.retry_after = retry_after


class GmailAuthRevoked(GmailError):
    """Google no longer honours the grant: the family removed access, or it expired."""


class GmailScopeInsufficient(GmailError):
    """The grant is live but does not cover the call.

    Google answers a call outside the granted scopes with 403 and the reason
    `insufficientPermissions` (spike S4, 2026-09-14). That is not a revoked
    grant, and treating it as one would tell the family to reconnect a mailbox
    that is connected — the fault is Abrolia's, a call the scope set never
    allowed.
    """


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
        # Only `invalid_grant` says the refresh credential itself is dead
        # (revoked by the family, expired, or invalidated by a password
        # change). The caller zeroes the durable grant on this answer, so a
        # rejected client secret (`invalid_client`) or any other refusal must
        # stay a plain error: the family's grant is not the thing at fault.
        if response.status_code in {400, 401} and _oauth_error(response) == "invalid_grant":
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
            try:
                self._access = self.grants.access_token(
                    self.identity_id, self.revision, self.refresher
                )
            except GmailAuthRevoked:
                self._revoked()
                raise
        return self._access.access_token

    def _revoked(self) -> None:
        """Close the grant durably the moment Google proves it revoked.

        The Gmail poller used to write `auth_revoked` into `email_sync_state`
        on this exception, and `/readyz` read it back. With the poller gone,
        nothing else observes a revocation: every caller — activation health,
        the send path, a refresh — comes through this client, and if it only
        raised, `oauth_grants.revoked_at` would stay NULL and `/readyz` would
        answer `send_only` with 200 for ever, keeping workers eligible for
        mail that can no longer be sent. Zeroing the row makes
        `_sync_email_binding` refuse, `/readyz` fail closed, and the control
        plane's runtime health mark the household `needs_attention`. Recovery
        is a reconnect, which is the only recovery a revoked grant has.
        """
        self._access = None
        self.grants.revoke(self.identity_id, self.revision)

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        headers = {**kwargs.pop("headers", {}), "Authorization": f"Bearer {self._token()}"}
        try:
            response = self.http.request(method, f"{GMAIL_URL}{path}", headers=headers, **kwargs)
        except httpx.TimeoutException as error:
            raise TimeoutError("gmail_timeout") from error
        except httpx.TransportError as error:
            raise ConnectionError("gmail_transport") from error
        reason = _error_reason(response) if response.status_code == 403 else ""
        if reason == "insufficientPermissions":
            raise GmailScopeInsufficient("gmail_scope_insufficient")
        if response.status_code == 429 or reason in QUOTA_REASONS:
            # Gmail answers a burst with 403 as readily as with 429; neither
            # says anything about the grant, and zeroing it for a rate limit
            # would turn a busy afternoon into a reconnect.
            try:
                retry_after = float(response.headers.get("Retry-After", "60"))
            except ValueError:
                retry_after = 60.0
            raise GmailQuotaExceeded(retry_after)
        if response.status_code in {401, 403}:
            self._revoked()
            raise GmailAuthRevoked("gmail_auth_revoked")
        if response.status_code >= 400:
            raise GmailError(f"gmail_http_{response.status_code}")
        try:
            body = response.json()
        except ValueError as error:
            raise GmailError("gmail_response_malformed") from error
        if not isinstance(body, dict):
            raise GmailError("gmail_response_malformed")
        return body

    def verify_access(self) -> None:
        """Prove the grant still refreshes, without a Gmail read.

        `users.getProfile` was the activation probe, and it needs a read scope.
        A refresh is the only call a send-only grant can make that does not
        send mail: it raises `GmailAuthRevoked` when Google has withdrawn the
        grant and `GmailError` when the token endpoint is unreachable.
        """
        self._token()

    def send_raw(self, raw: str) -> dict[str, Any]:
        return self._request("POST", "/messages/send", json={"raw": raw})


def _oauth_error(response: httpx.Response) -> str:
    """The `error` code of an OAuth token-endpoint refusal, or an empty string."""
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return ""
    return error if isinstance(error, str) else ""


def _error_reason(response: httpx.Response) -> str:
    """The first `reason` in a Google API error body, or an empty string."""
    try:
        errors = response.json().get("error", {}).get("errors", [])
    except (ValueError, AttributeError):
        return ""
    if not isinstance(errors, list):
        return ""
    for item in errors:
        if isinstance(item, dict) and item.get("reason"):
            return str(item["reason"])
    return ""

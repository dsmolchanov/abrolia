"""Send-only Gmail: a 200 is the receipt, and a timeout is never settled by reading.

The reconciliation that used to search Sent for the Message-ID needed
`gmail.readonly`. With the send-only grant the adapter has no second look, so
`EmailSender` records `outcome_unknown` and refuses to replay — the same rule
every unknown outcome already follows.
"""

import base64
import json
from pathlib import Path

import httpx
import pytest

from hermes_cloud.core.db import open_database
from hermes_cloud.email.contracts import EmailBinding, EmailSendRequest
from hermes_cloud.email.google_client import (
    GMAIL_URL,
    TOKEN_URL,
    GmailAuthRevoked,
    GmailError,
    GmailHttpClient,
    GmailQuotaExceeded,
    GmailScopeInsufficient,
)
from hermes_cloud.email.google_grant import GoogleGrantStore
from hermes_cloud.email.receipts import EmailBindingStore, EmailSendStore
from hermes_cloud.execute.email_send import EmailOutcomeUnknown, EmailSender, Outgoing
from hermes_cloud.execute.gmail_api_send import GmailSendProvider

REQUEST = EmailSendRequest(
    "effect-1",
    "approval-1",
    EmailBinding("identity-1", 1, "gmail", "agent@example.test"),
    "<effect-1@hermes-cloud.invalid>",
    b"Message-ID: <effect-1@hermes-cloud.invalid>\r\n\r\nBody",
    "a" * 64,
)


class Client:
    def __init__(self):
        self.sent = []
        self.fail = False

    def send_raw(self, raw):
        self.sent.append(raw)
        if self.fail:
            raise TimeoutError
        return {"id": "gmail-message-1"}


def test_send_uses_base64url_raw_and_returns_receipt() -> None:
    client = Client()
    receipt = GmailSendProvider(client, clock=lambda: 10.0).send(REQUEST)
    assert base64.urlsafe_b64decode(client.sent[0] + "==") == REQUEST.mime_bytes
    assert receipt.provider_ref == "gmail-message-1"


def test_provider_offers_no_reconciliation() -> None:
    provider = GmailSendProvider(Client())
    assert provider.supports_idempotent_reconcile is False
    assert not hasattr(provider, "reconcile")


def test_sender_records_a_timeout_as_unknown_and_never_retries(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HERMES_EMAIL_SEND", "1")
    with open_database(tmp_path / "runtime.db") as database:
        binding = REQUEST.binding
        bindings = EmailBindingStore(database)
        bindings.activate(binding)
        client = Client()
        client.fail = True
        sender = EmailSender(
            GmailSendProvider(client),
            sender=binding.address,
            identity_id=binding.identity_id,
            binding_revision=binding.revision,
            provider=binding.provider,
            binding_store=bindings,
            send_store=EmailSendStore(database),
        )
        letter = Outgoing(
            to="school@example.test",
            subject="Approved",
            body="Body",
            from_identity_id=binding.identity_id,
            binding_revision=binding.revision,
            from_address=binding.address,
        )

        with pytest.raises(EmailOutcomeUnknown):
            sender.send(letter, approval_id="parent-approval", effect_id="child-effect")
        client.fail = False
        with pytest.raises(EmailOutcomeUnknown):
            sender.send(letter, approval_id="parent-approval", effect_id="child-effect")

        assert len(client.sent) == 1
        assert EmailSendStore(database).get("child-effect").status == "outcome_unknown"


def _grant_store(tmp_path: Path) -> GoogleGrantStore:
    database = open_database(tmp_path / "runtime.db")
    store = GoogleGrantStore(database, {1: b"k" * 32}, active_version=1)
    store.put(
        identity_id="identity-1",
        revision=1,
        refresh_credential="refresh-canary",
        provider_subject="subject-1",
        scopes=("openid", "email", "https://www.googleapis.com/auth/gmail.send"),
    )
    return store


def _grant_is_live(store: GoogleGrantStore) -> bool:
    row = store.db.query_one("SELECT revoked_at, encrypted_refresh_credential FROM oauth_grants")
    return row["revoked_at"] is None and len(bytes(row["encrypted_refresh_credential"])) > 0


def _http_client(tmp_path: Path, handler) -> GmailHttpClient:
    return GmailHttpClient(
        _grant_store(tmp_path),
        identity_id="identity-1",
        revision=1,
        client_id="client-id",
        client_secret="client-secret-canary",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _google(status: int, body: dict, *, token=(200, {"access_token": "access-canary", "expires_in": 3600})):
    """Google's token endpoint answers `token`, then Gmail answers `status` with `body`."""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            return httpx.Response(token[0], json=token[1])
        assert str(request.url) == f"{GMAIL_URL}/messages/send"
        assert json.loads(request.content) == {"raw": "raw"}
        return httpx.Response(status, json=body)

    return handler


def test_insufficient_scope_is_reported_as_its_own_fault(tmp_path: Path) -> None:
    """The 403 Google returned in spike S4 (2026-09-14) for a call outside the
    granted set. The grant is live; telling the family to reconnect would be
    wrong, so it is not `GmailAuthRevoked`."""
    client = _http_client(
        tmp_path,
        _google(
            403,
            {
                "error": {
                    "code": 403,
                    "message": "Request had insufficient authentication scopes.",
                    "errors": [
                        {
                            "message": "Insufficient Permission",
                            "domain": "global",
                            "reason": "insufficientPermissions",
                        }
                    ],
                    "status": "PERMISSION_DENIED",
                }
            },
        ),
    )
    with pytest.raises(GmailScopeInsufficient):
        client.send_raw("raw")


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (401, {"error": {"code": 401, "status": "UNAUTHENTICATED"}}),
        (403, {"error": {"code": 403, "errors": [{"reason": "forbidden"}]}}),
        (403, {"error": "not the API shape"}),
        (403, {}),
    ],
)
def test_other_401_and_403_answers_still_mean_a_revoked_grant(
    tmp_path: Path, status: int, body: dict
) -> None:
    """Revocation is persisted where it is observed.

    The poller used to write `auth_revoked` into `email_sync_state` and
    `/readyz` read it back; without the poller, a send that exposes the
    revocation is the only observer left, so the client zeroes the durable
    grant itself. `_sync_email_binding` then refuses, `/readyz` fails closed
    and the control plane marks the household `needs_attention`.
    """
    client = _http_client(tmp_path, _google(status, body))
    with pytest.raises(GmailAuthRevoked):
        client.send_raw("raw")
    assert not _grant_is_live(client.grants)
    # A later call finds no grant, not a stale cached token.
    with pytest.raises(Exception, match="grant"):
        client.send_raw("raw")


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}),
        (401, {"error": "invalid_grant"}),
    ],
)
def test_a_dead_refresh_credential_revokes_the_grant_durably(
    tmp_path: Path, status: int, body: dict
) -> None:
    client = _http_client(tmp_path, _google(200, {"id": "m1"}, token=(status, body)))
    with pytest.raises(GmailAuthRevoked):
        client.verify_access()
    assert not _grant_is_live(client.grants)


@pytest.mark.parametrize(
    ("status", "body"),
    [
        # A rejected client secret is Abrolia's misconfiguration, not the
        # family's grant; zeroing the grant for it would demand a reconnect
        # that fixes nothing.
        (401, {"error": "invalid_client"}),
        (400, {"error": "invalid_request"}),
        (403, {}),
        (500, {}),
    ],
)
def test_other_token_endpoint_refusals_keep_the_grant(
    tmp_path: Path, status: int, body: dict
) -> None:
    client = _http_client(tmp_path, _google(200, {"id": "m1"}, token=(status, body)))
    with pytest.raises(GmailError) as raised:
        client.verify_access()
    assert not isinstance(raised.value, GmailAuthRevoked)
    assert _grant_is_live(client.grants)


@pytest.mark.parametrize(
    "reason", ["userRateLimitExceeded", "rateLimitExceeded", "dailyLimitExceeded", "quotaExceeded"]
)
def test_a_gmail_usage_limit_403_is_quota_and_keeps_the_grant(tmp_path: Path, reason: str) -> None:
    """Gmail answers a burst with 403 as readily as with 429."""
    client = _http_client(
        tmp_path,
        _google(403, {"error": {"code": 403, "errors": [{"domain": "usageLimits", "reason": reason}]}}),
    )
    with pytest.raises(GmailQuotaExceeded):
        client.send_raw("raw")
    assert _grant_is_live(client.grants)


def test_an_insufficient_scope_403_keeps_the_grant(tmp_path: Path) -> None:
    client = _http_client(
        tmp_path,
        _google(403, {"error": {"errors": [{"reason": "insufficientPermissions"}]}}),
    )
    with pytest.raises(GmailScopeInsufficient):
        client.send_raw("raw")
    assert _grant_is_live(client.grants)


def test_client_has_no_read_method(tmp_path: Path) -> None:
    client = _http_client(tmp_path, _google(200, {"id": "m1"}))
    for name in ("profile", "history", "message", "list_inbox", "search_sent"):
        assert not hasattr(client, name)
    assert client.send_raw("raw") == {"id": "m1"}

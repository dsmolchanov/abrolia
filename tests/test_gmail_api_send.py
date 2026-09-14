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
    GmailHttpClient,
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


def _http_client(tmp_path: Path, handler) -> GmailHttpClient:
    database = open_database(tmp_path / "runtime.db")
    store = GoogleGrantStore(database, {1: b"k" * 32}, active_version=1)
    store.put(
        identity_id="identity-1",
        revision=1,
        refresh_credential="refresh-canary",
        provider_subject="subject-1",
        scopes=("openid", "email", "https://www.googleapis.com/auth/gmail.send"),
    )
    return GmailHttpClient(
        store,
        identity_id="identity-1",
        revision=1,
        client_id="client-id",
        client_secret="client-secret-canary",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _google(status: int, body: dict):
    """Google's token endpoint answers, then Gmail answers `status` with `body`."""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            return httpx.Response(200, json={"access_token": "access-canary", "expires_in": 3600})
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
    client = _http_client(tmp_path, _google(status, body))
    with pytest.raises(GmailAuthRevoked):
        client.send_raw("raw")


def test_client_has_no_read_method(tmp_path: Path) -> None:
    client = _http_client(tmp_path, _google(200, {"id": "m1"}))
    for name in ("profile", "history", "message", "list_inbox", "search_sent"):
        assert not hasattr(client, name)
    assert client.send_raw("raw") == {"id": "m1"}

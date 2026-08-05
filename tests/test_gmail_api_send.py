import base64

import pytest

from hermes_cloud.email.contracts import EmailBinding, EmailSendRequest
from hermes_cloud.execute.email_send import EmailOutcomeUnknown
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
        self.matches = []
        self.fail = False

    def send_raw(self, raw):
        self.sent.append(raw)
        if self.fail:
            raise TimeoutError
        return {"id": "gmail-message-1"}

    def search_sent(self, query):
        assert query == "rfc822msgid:<effect-1@hermes-cloud.invalid>"
        return self.matches


def test_send_uses_base64url_raw_and_returns_receipt() -> None:
    client = Client()
    receipt = GmailSendProvider(client, clock=lambda: 10.0).send(REQUEST)
    assert base64.urlsafe_b64decode(client.sent[0] + "==") == REQUEST.mime_bytes
    assert receipt.provider_ref == "gmail-message-1"


def test_timeout_reconciles_only_one_exact_sent_message() -> None:
    client = Client()
    provider = GmailSendProvider(client)
    client.fail = True
    with pytest.raises(EmailOutcomeUnknown):
        provider.send(REQUEST)

    client.matches = [{"id": "gmail-message-1", "rfc822_message_id": REQUEST.message_id}]
    assert provider.reconcile(REQUEST).provider_ref == "gmail-message-1"
    for matches in ([], client.matches * 2, [{"id": "x", "rfc822_message_id": "<other>"}]):
        client.matches = matches
        with pytest.raises(EmailOutcomeUnknown):
            provider.reconcile(REQUEST)

import base64
from pathlib import Path

import pytest

from hermes_cloud.core.db import open_database
from hermes_cloud.email.contracts import EmailBinding, EmailSendRequest
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
        self.matches = []
        self.fail = False
        self.expected_message_id = REQUEST.message_id

    def send_raw(self, raw):
        self.sent.append(raw)
        if self.fail:
            raise TimeoutError
        return {"id": "gmail-message-1"}

    def search_sent(self, query):
        assert query == f"rfc822msgid:{self.expected_message_id}"
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


def test_sender_reconciles_timeout_before_persisting_unknown(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HERMES_EMAIL_SEND", "1")
    with open_database(tmp_path / "runtime.db") as database:
        binding = REQUEST.binding
        bindings = EmailBindingStore(database)
        bindings.activate(binding)
        client = Client()
        client.fail = True
        client.expected_message_id = "<hermes-child-effect@hermes-cloud.invalid>"
        sender = EmailSender(
            GmailSendProvider(client, clock=lambda: 10.0),
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
        client.matches = [
            {
                "id": "gmail-message-1",
                "rfc822_message_id": "<hermes-child-effect@hermes-cloud.invalid>",
            }
        ]

        receipt = sender.send(
            letter,
            approval_id="parent-approval",
            effect_id="child-effect",
        )

        assert receipt.status == "accepted"
        assert EmailSendStore(database).get("child-effect").provider_ref == "gmail-message-1"


def test_sender_does_not_retry_when_timeout_is_absent_from_sent(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HERMES_EMAIL_SEND", "1")
    with open_database(tmp_path / "runtime.db") as database:
        binding = REQUEST.binding
        bindings = EmailBindingStore(database)
        bindings.activate(binding)
        client = Client()
        client.fail = True
        client.expected_message_id = "<hermes-child-effect@hermes-cloud.invalid>"
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
        with pytest.raises(EmailOutcomeUnknown):
            sender.send(letter, approval_id="parent-approval", effect_id="child-effect")

        assert len(client.sent) == 1
        assert EmailSendStore(database).get("child-effect").status == "outcome_unknown"

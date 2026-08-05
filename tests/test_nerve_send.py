from __future__ import annotations

import hashlib
from email.message import EmailMessage
from pathlib import Path

import pytest

from hermes_cloud.core.db import open_database
from hermes_cloud.email.contracts import EmailBinding, EmailSendRequest
from hermes_cloud.email.nerve_client import NerveTransportUnknown
from hermes_cloud.email.receipts import EmailBindingStore, EmailSendStore
from hermes_cloud.execute.email_send import (
    EmailOutcomeUnknown,
    EmailSender,
    Outgoing,
    build_message,
)
from hermes_cloud.execute.nerve_send import NerveSendProvider


class FakeComposeClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail_unknown = False

    def compose_email(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_unknown:
            raise NerveTransportUnknown("timeout")
        return {"thread_id": "thread-1", "message_id": "provider-message-1", "status": "queued"}


def request(effect_id: str = "effect-child-1") -> EmailSendRequest:
    message = EmailMessage()
    message["From"] = "agent@abrolia.test"
    message["To"] = "school@example.test"
    message["Subject"] = "Permission"
    message["Message-ID"] = "<stable@abrolia.invalid>"
    message.set_content("Approved body")
    message.add_attachment(
        b"synthetic-pdf",
        maintype="application",
        subtype="pdf",
        filename="permission.pdf",
    )
    return EmailSendRequest(
        effect_id=effect_id,
        approval_id="reusable-parent-approval",
        binding=EmailBinding(
            "identity-1", 4, "nerve-managed", "agent@abrolia.test"
        ),
        message_id="<stable@abrolia.invalid>",
        mime_bytes=message.as_bytes(),
        request_sha256="a" * 64,
    )


@pytest.mark.parametrize("provider_kind", ["nerve-managed", "nerve-byo-domain"])
def test_managed_and_byo_send_use_child_effect_id(provider_kind: str) -> None:
    client = FakeComposeClient()
    outbound = request()
    outbound = EmailSendRequest(
        **{
            **outbound.__dict__,
            "binding": EmailBinding(
                "identity-1", 4, provider_kind, "agent@abrolia.test"
            ),
        }
    )

    receipt = NerveSendProvider(
        client, inbox_id="inbox-1", clock=lambda: 123.0
    ).send(outbound)

    assert client.calls[0]["idempotency_key"] == "effect-child-1"
    assert client.calls[0]["idempotency_key"] != outbound.approval_id
    assert client.calls[0]["attachments"][0]["filename"] == "permission.pdf"
    assert receipt.provider_ref == "provider-message-1"
    assert receipt.status == "accepted" and receipt.accepted_at == 123.0


def test_unknown_compose_outcome_is_not_reported_as_failure() -> None:
    client = FakeComposeClient()
    client.fail_unknown = True

    with pytest.raises(EmailOutcomeUnknown):
        NerveSendProvider(client, inbox_id="inbox-1").send(request())

    assert len(client.calls) == 1


def test_provider_receipt_is_persisted_against_the_child_effect(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HERMES_EMAIL_SEND", "1")
    with open_database(tmp_path / "runtime.db") as database:
        active = EmailBinding(
            "identity-1", 4, "nerve-managed", "agent@abrolia.test"
        )
        bindings = EmailBindingStore(database)
        bindings.activate(active)
        sender = EmailSender(
            NerveSendProvider(FakeComposeClient(), inbox_id="inbox-1"),
            sender=active.address,
            identity_id=active.identity_id,
            binding_revision=active.revision,
            provider=active.provider,
            binding_store=bindings,
            send_store=EmailSendStore(database),
        )

        receipt = sender.send(
            Outgoing(
                to="school@example.test",
                subject="Approved",
                body="Body",
                from_identity_id=active.identity_id,
                binding_revision=active.revision,
                from_address=active.address,
            ),
            approval_id="parent-approval",
            effect_id="child-effect",
        )

        stored = database.query_one(
            "SELECT * FROM email_sends WHERE effect_id = ?", ("child-effect",)
        )
        assert receipt.provider_ref == "provider-message-1"
        assert stored["approval_id"] == "parent-approval"
        assert stored["provider_idempotency_key"] == "child-effect"
        assert stored["state"] == "accepted"


def test_crash_left_pending_send_reconciles_by_the_same_effect_id(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HERMES_EMAIL_SEND", "1")
    with open_database(tmp_path / "runtime.db") as database:
        active = EmailBinding(
            "identity-1", 4, "nerve-managed", "agent@abrolia.test"
        )
        bindings = EmailBindingStore(database)
        bindings.activate(active)
        sends = EmailSendStore(database)
        letter = Outgoing(
            to="school@example.test",
            subject="Approved",
            body="Body",
            from_identity_id=active.identity_id,
            binding_revision=active.revision,
            from_address=active.address,
        )
        raw = build_message(
            letter, sender=active.address, approval_id="child-effect"
        ).as_bytes()
        sends.begin(
            effect_id="child-effect",
            approval_id="parent-approval",
            binding=active,
            request_sha256=hashlib.sha256(raw).hexdigest(),
            message_id="<hermes-child-effect@hermes-cloud.invalid>",
        )
        client = FakeComposeClient()
        sender = EmailSender(
            NerveSendProvider(client, inbox_id="inbox-1"),
            sender=active.address,
            identity_id=active.identity_id,
            binding_revision=active.revision,
            provider=active.provider,
            binding_store=bindings,
            send_store=sends,
        )

        receipt = sender.send(
            letter,
            approval_id="parent-approval",
            effect_id="child-effect",
        )

        assert receipt.status == "accepted"
        assert len(client.calls) == 1
        assert client.calls[0]["idempotency_key"] == "child-effect"
        assert sends.get("child-effect").provider_ref == "provider-message-1"

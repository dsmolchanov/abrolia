"""Provider-neutral email bindings, ingress and delivery safety."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cloud.channels.telegram import FakeTransport
from hermes_cloud.core.approvals import ApprovalStore
from hermes_cloud.core.db import open_database
from hermes_cloud.core.dsar import export_household
from hermes_cloud.core.events import EventStore
from hermes_cloud.core.runcontext import Household, build_run_context
from hermes_cloud.email.contracts import (
    EmailBinding,
    EmailDeliveryReceipt,
    InboundEmail,
)
from hermes_cloud.email.receipts import EmailBindingStore, EmailSendStore
from hermes_cloud.email.service import EmailRuntimeService
from hermes_cloud.execute.email_send import (
    ENV_KILL_SWITCH,
    EmailOutcomeUnknown,
    EmailSender,
    FakeSmtp,
    Outgoing,
)
from hermes_cloud.execute.reminder import ReminderStore
from hermes_cloud.ingest.rfc822 import ingest_rfc822
from hermes_cloud.runner.bundle import Item, bundle_payload
from hermes_cloud.runner.card import ACTION_CONFIRM, KIND_BUNDLE, KIND_EMAIL
from hermes_cloud.runner.pipeline import Pipeline

CHAT = "-100990000101"
OWNER = "990000001"
ADDRESS = "assistant@example.test"
RAW = (
    b"From: School <school@example.test>\r\n"
    b"To: Assistant <assistant@example.test>\r\n"
    b"Subject: Hello\r\n"
    b"Message-ID: <Canonical-1@Example.Test>\r\n"
    b"References: <Thread-1@Example.Test>\r\n\r\nBody\r\n"
)


def binding(revision: int) -> EmailBinding:
    return EmailBinding(
        identity_id="identity-1",
        revision=revision,
        provider="fake-provider",
        address=ADDRESS,
        provider_ref="provider-binding-1",
        secret_names=("HERMES_EMAIL_RUNTIME_KEY",),
    )


def test_same_rfc822_from_every_provider_materializes_once(tmp_path: Path) -> None:
    database = open_database(tmp_path / "hermes.db")
    events = EventStore(database)

    results = [
        ingest_rfc822(
            events,
            source=source,
            provider_event_id=f"{source}-event-1",
            raw_bytes=RAW,
        )
        for source in ("inject", "nerve", "gmail")
    ]

    assert [item.created for item in results] == [True, False, False]
    assert len({item.event_id for item in results}) == 1
    assert {item.parsed.message_id for item in results} == {
        "canonical-1@example.test"
    }
    assert {item.parsed.thread_key for item in results} == {"thread-1@example.test"}
    assert database.query_one("SELECT COUNT(*) AS n FROM email_ingress_receipts")["n"] == 3


class FakeSource:
    provider = "fake-provider"

    def poll(self):
        return [InboundEmail("fake-provider", "event-1", RAW, 123.0)]


def test_runtime_service_loop_reports_binding_health(tmp_path: Path) -> None:
    database = open_database(tmp_path / "hermes.db")
    service = EmailRuntimeService(database, [FakeSource()], clock=lambda: 456.0)
    service.activate(binding(1))

    assert service.run_once() == 1
    assert service.health().status == "ready"
    assert service.health().provider == "fake-provider"
    assert service.health().binding_revision == 1


def test_binding_revision_is_immutable(tmp_path: Path) -> None:
    database = open_database(tmp_path / "hermes.db")
    bindings = EmailBindingStore(database)
    bindings.activate(binding(1))

    with pytest.raises(ValueError, match="immutable"):
        bindings.activate(
            EmailBinding(
                **{**binding(1).__dict__, "address": "other@example.test"}
            )
        )


def email_world(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(ENV_KILL_SWITCH, "1")
    database = open_database(tmp_path / "hermes.db")
    bindings = EmailBindingStore(database)
    active = bindings.activate(binding(1))
    smtp = FakeSmtp()
    pipeline = Pipeline(
        approvals=ApprovalStore(database),
        reminders=ReminderStore(database),
        transport=FakeTransport(),
        extractor=None,
        chat=CHAT,
        mail=EmailSender(
            smtp,
            sender=active.address,
            identity_id=active.identity_id,
            binding_revision=active.revision,
            provider=active.provider,
            binding_store=bindings,
            send_store=EmailSendStore(database),
        ),
    )
    payload = bundle_payload(
        [
            Item(
                payload={
                    "kind": KIND_EMAIL,
                    "from_identity_id": active.identity_id,
                    "binding_revision": active.revision,
                    "from_address": active.address,
                    "to": "school@example.test",
                    "subject": "Reply",
                    "body": "Confirmed body",
                }
            )
        ],
        header="Reply",
    )
    staged = pipeline.approvals.stage(
        kind=KIND_BUNDLE, payload=payload, chat=CHAT, actor=OWNER
    )
    household = Household(
        owner=OWNER, family=frozenset({OWNER}), allowed_chats=frozenset({CHAT})
    )
    context = build_run_context(household=household, actor_id=OWNER, chat_id=CHAT)
    return database, bindings, pipeline, smtp, staged, context


def test_binding_revision_change_invalidates_approved_sender(
    tmp_path: Path, monkeypatch
) -> None:
    _database, bindings, pipeline, smtp, staged, context = email_world(
        tmp_path, monkeypatch
    )
    bindings.activate(binding(2))

    handled = pipeline.handle_callback(
        action=ACTION_CONFIRM, approval_id=staged.id, context=context
    )

    assert handled.executed is None
    assert "адрес отправителя изменился" in handled.message
    assert smtp.sent == []
    assert pipeline.approvals.get(staged.id).status == "failed"


def test_effect_identity_and_provider_receipt_are_persisted(
    tmp_path: Path, monkeypatch
) -> None:
    database, _bindings, pipeline, smtp, staged, context = email_world(
        tmp_path, monkeypatch
    )

    handled = pipeline.handle_callback(
        action=ACTION_CONFIRM, approval_id=staged.id, context=context
    )

    assert handled.executed == KIND_BUNDLE and len(smtp.sent) == 1
    effect = next(
        item for item in pipeline.effects.for_run(staged.id) if item.kind == KIND_EMAIL
    )
    send = database.query_one("SELECT * FROM email_sends WHERE effect_id = ?", (effect.id,))
    receipt = EmailSendStore(database).get(effect.id)
    assert send["provider_idempotency_key"] == effect.id
    assert send["binding_revision"] == 1
    assert receipt is not None and receipt.status == "accepted"
    assert receipt.message_id == smtp.sent[0]["Message-ID"]


class FakeProvider:
    provider = "contract-fixture"

    def __init__(self) -> None:
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        return EmailDeliveryReceipt(
            effect_id=request.effect_id,
            approval_id=request.approval_id,
            message_id=request.message_id,
            provider_ref="provider-message-1",
            accepted_at=10.0,
            status="accepted",
        )

    def reconcile(self, request):
        return self.send(request)


def test_common_provider_receipt_uses_the_same_send_contract(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(ENV_KILL_SWITCH, "1")
    database = open_database(tmp_path / "hermes.db")
    bindings = EmailBindingStore(database)
    active = bindings.activate(binding(1))
    provider = FakeProvider()
    sender = EmailSender(
        provider,
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
            subject="Reply",
            body="Body",
            from_identity_id=active.identity_id,
            binding_revision=active.revision,
            from_address=active.address,
        ),
        approval_id="approval-1",
        effect_id="effect-1",
    )

    assert receipt.provider_ref == "provider-message-1"
    assert provider.requests[0].idempotency_key == "effect-1"
    assert provider.requests[0].binding == active


def test_interrupted_pending_send_is_not_replayed(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(ENV_KILL_SWITCH, "1")
    database = open_database(tmp_path / "hermes.db")
    bindings = EmailBindingStore(database)
    active = bindings.activate(binding(1))
    provider = FakeProvider()
    sends = EmailSendStore(database)
    sender = EmailSender(
        provider,
        sender=active.address,
        identity_id=active.identity_id,
        binding_revision=active.revision,
        provider=active.provider,
        binding_store=bindings,
        send_store=sends,
    )
    letter = Outgoing(
        to="school@example.test",
        subject="Reply",
        body="Body",
        from_identity_id=active.identity_id,
        binding_revision=active.revision,
        from_address=active.address,
    )
    # Simulate a process death after durable intent and before a receipt.
    import hashlib

    from hermes_cloud.execute.email_send import build_message

    message = build_message(letter, sender=active.address, approval_id="effect-1")
    sends.begin(
        effect_id="effect-1",
        approval_id="approval-1",
        binding=active,
        request_sha256=hashlib.sha256(message.as_bytes()).hexdigest(),
        message_id=str(message["Message-ID"]),
    )

    with pytest.raises(EmailOutcomeUnknown):
        sender.send(letter, approval_id="approval-1", effect_id="effect-1")

    assert provider.requests == []
    assert sends.get("effect-1").status == "outcome_unknown"


def test_oauth_credential_is_absent_from_dsar(tmp_path: Path) -> None:
    database = open_database(tmp_path / "hermes.db")
    canary = b"refresh-token-secret-canary"
    with database.write() as connection:
        connection.execute(
            "INSERT INTO oauth_grants (binding_identity_id, binding_revision,"
            " encrypted_refresh_credential, key_version, provider_subject, scopes_json,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("identity-1", 1, canary, 1, "subject-1", '[]', 1.0, 1.0),
        )

    assert "refresh-token-secret-canary" not in str(export_household(database))

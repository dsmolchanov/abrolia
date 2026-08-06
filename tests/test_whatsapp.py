from __future__ import annotations

import json
from email import policy
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from hermes_cloud.channels.telegram import FakeTransport
from hermes_cloud.core.approvals import ApprovalStore
from hermes_cloud.core.db import open_database
from hermes_cloud.core.events import EventStore
from hermes_cloud.core.runcontext import (
    ROLE_FAMILY,
    SCOPE_PERSONAL,
    WRITE_WHATSAPP,
    Household,
    RunContext,
)
from hermes_cloud.execute.reminder import ReminderStore
from hermes_cloud.execute.whatsapp import (
    OutgoingWhatsApp,
    WhatsAppOutcomeUnknown,
    WhatsAppRejected,
    WhatsAppSender,
)
from hermes_cloud.ingest.whatsapp_webhook import (
    WhatsAppWebhookReceiver,
    WhatsAppWebhookRejected,
    sign_webhook,
)
from hermes_cloud.runner.card import ACTION_CONFIRM, KIND_BUNDLE, KIND_WHATSAPP
from hermes_cloud.runner.pipeline import Pipeline
from hermes_cloud.runner.tools import REGISTRY, Services

SECRET = "synthetic-household-relay-secret"
INSTANCE = "family-a"


def inbound_payload() -> bytes:
    return json.dumps(
        {
            "instance": INSTANCE,
            "data": {
                "key": {
                    "id": "wamid-1",
                    "remoteJid": "999123456@s.whatsapp.invalid",
                    "fromMe": False,
                },
                "pushName": "Klassenlehrerin",
                "message": {"conversation": "Ausflug ist am 19. September."},
            },
        },
        separators=(",", ":"),
    ).encode()


def test_signed_inbound_is_durable_deduplicated_and_email_shaped(tmp_path: Path) -> None:
    body = inbound_payload()
    with open_database(tmp_path / "wa.db") as database:
        receiver = WhatsAppWebhookReceiver(
            EventStore(database),
            signing_secret=SECRET,  # gitleaks:allow -- synthetic fixture
            instance=INSTANCE,
        )

        first = receiver.receive(body, sign_webhook(body, SECRET))
        second = receiver.receive(body, sign_webhook(body, SECRET))

        assert first.created is True
        assert second.created is False
        assert first.event.id == second.event.id
        assert first.event.source == "whatsapp"
        assert first.event.context_key == "whatsapp:999123456@s.whatsapp.invalid"
        parsed = BytesParser(policy=policy.default).parsebytes(first.event.raw)
        assert parsed["X-Abrolia-WhatsApp-Actor"] == "+999123456"
        assert "19. September" in parsed.get_content()


def test_normalized_relay_shape_is_accepted(tmp_path: Path) -> None:
    body = json.dumps(
        {
            "instance": INSTANCE,
            "message": {
                "id": "normalized-1",
                "from": "999123456",
                "remote_jid": "999123456@s.whatsapp.invalid",
                "push_name": "Teacher",
                "text": "Termin verschoben.",
            },
        },
        separators=(",", ":"),
    ).encode()
    with open_database(tmp_path / "wa.db") as database:
        accepted = WhatsAppWebhookReceiver(
            EventStore(database),
            signing_secret=SECRET,  # gitleaks:allow -- synthetic fixture
            instance=INSTANCE,
        ).receive(body, sign_webhook(body, SECRET))
        assert accepted.created is True
        assert accepted.event.external_id.endswith(":normalized-1")


@pytest.mark.parametrize("signature", ["", "sha256=" + "0" * 64, "bad"])
def test_webhook_fails_closed_for_missing_or_bad_hmac(tmp_path: Path, signature: str) -> None:
    with open_database(tmp_path / "wa.db") as database:
        receiver = WhatsAppWebhookReceiver(
            EventStore(database),
            signing_secret=SECRET,  # gitleaks:allow -- synthetic fixture
            instance=INSTANCE,
        )
        with pytest.raises(WhatsAppWebhookRejected) as caught:
            receiver.receive(inbound_payload(), signature)
        assert caught.value.status_code == 401
        assert EventStore(database).counts() == {}


def _sender(handler) -> WhatsAppSender:
    return WhatsAppSender(
        api_url="https://evolution.invalid",
        instance=INSTANCE,
        api_key="synthetic-api-key",
        enabled=True,
        client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False),
    )


def test_sender_uses_household_instance_and_effect_id() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(201, json={"key": {"id": "provider-message-1"}})

    receipt = _sender(handler).send(
        OutgoingWhatsApp(to="+999123456", text="Hallo"), effect_id="effect-1"
    )

    request = seen["request"]
    assert request.url.path == f"/message/sendText/{INSTANCE}"
    assert request.headers["apikey"] == "synthetic-api-key"
    assert request.headers["Idempotency-Key"] == "effect-1"
    assert json.loads(request.content) == {"number": "999123456", "text": "Hallo"}
    assert receipt.message_id == "provider-message-1"


def test_redirect_and_connect_error_are_explicit_failures() -> None:
    redirect = _sender(lambda request: httpx.Response(307, headers={"location": "/other"}))
    with pytest.raises(WhatsAppRejected):
        redirect.send(OutgoingWhatsApp("+999123456", "Hallo"), effect_id="one")

    def disconnected(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    with pytest.raises(WhatsAppRejected):
        _sender(disconnected).send(
            OutgoingWhatsApp("+999123456", "Hallo"), effect_id="two"
        )


def test_read_timeout_has_unknown_outcome() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("late", request=request)

    with pytest.raises(WhatsAppOutcomeUnknown):
        _sender(timeout).send(
            OutgoingWhatsApp("+999123456", "Hallo"), effect_id="effect-unknown"
        )


def test_tool_only_stages_whatsapp_and_exposes_recipient(tmp_path: Path) -> None:
    sent = []

    class FakeSender:
        def send(self, message, *, effect_id):
            sent.append((message, effect_id))
            return SimpleNamespace(message_id="provider-1")

    with open_database(tmp_path / "wa.db") as database:
        context = RunContext(
            household_id="home",
            actor_id="parent",
            chat_id="control-chat",
            thread_id=None,
            role=ROLE_FAMILY,
            scope=SCOPE_PERSONAL,
            mutate_caps=frozenset({WRITE_WHATSAPP}),
        )
        result = REGISTRY.invoke(
            context,
            "propose_whatsapp",
            {"to": "+999123456", "text": "Bitte Termin bestätigen."},
            services=Services.on(database),
        )

        approval = ApprovalStore(database).get(result["proposal_id"])
        assert approval is not None and approval.kind == KIND_BUNDLE
        assert approval.payload["items"] == [
            {
                "kind": KIND_WHATSAPP,
                "to": "+999123456",
                "text": "Bitte Termin bestätigen.",
                "enabled": True,
            }
        ]
        assert sent == []

        pipeline = Pipeline(
            approvals=ApprovalStore(database),
            reminders=ReminderStore(database),
            transport=FakeTransport(),
            extractor=object(),
            chat=context.chat_id,
            whatsapp=FakeSender(),
        )
        pipeline.handle_callback(
            action=ACTION_CONFIRM,
            approval_id=approval.id,
            context=context,
        )
        assert len(sent) == 1
        assert sent[0][0].to == "+999123456"


def test_known_family_dialogue_runs_under_context_and_stages_the_reply(tmp_path: Path) -> None:
    body = inbound_payload()
    seen = {}

    class FakeLoop:
        def run(self, context, text):
            seen.update(context=context, text=text)
            return SimpleNamespace(text="Ja, ich habe den neuen Termin notiert.")

    with open_database(tmp_path / "dialogue.db") as database:
        accepted = WhatsAppWebhookReceiver(
            EventStore(database),
            signing_secret=SECRET,  # gitleaks:allow -- synthetic fixture
            instance=INSTANCE,
        ).receive(body, sign_webhook(body, SECRET))
        transport = FakeTransport()
        household = Household(
            household_id="home",
            family=frozenset({"+999123456"}),
            allowed_chats=frozenset({"999123456@s.whatsapp.invalid"}),
            verified_bindings=frozenset(
                {("+999123456", "999123456@s.whatsapp.invalid")}
            ),
        )
        pipeline = Pipeline(
            approvals=ApprovalStore(database),
            reminders=ReminderStore(database),
            transport=transport,
            extractor=object(),
            chat="telegram-control",
            loop=FakeLoop(),
            household=household,
        )

        handled = pipeline.handle_event(accepted.event)

        assert seen["context"].is_known is True
        assert "19. September" in seen["text"]
        approval = ApprovalStore(database).get(handled.approval_id)
        assert approval is not None
        assert approval.payload["items"][0]["kind"] == KIND_WHATSAPP
        assert approval.payload["items"][0]["to"] == "+999123456"
        assert len(transport.messages) == 1
        assert any(label.startswith("✅") for label, _data in transport.messages[0].buttons)

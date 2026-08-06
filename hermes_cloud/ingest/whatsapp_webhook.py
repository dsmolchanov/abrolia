"""Authenticated WhatsApp relay ingress into the durable event queue."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from typing import Any

from hermes_cloud.core.events import Accepted, EventStore
from hermes_cloud.core.runcontext import Household, RunContext, build_run_context

MAX_WHATSAPP_WEBHOOK_BYTES = 256 * 1024


class WhatsAppWebhookRejected(ValueError):
    def __init__(self, code: str, status_code: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class WhatsAppInbound:
    message_id: str
    chat_id: str
    actor_id: str
    text: str
    display_name: str | None
    instance: str


def sign_webhook(payload: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def verify_webhook(payload: bytes, signature: str, secret: str) -> None:
    if not secret or not signature:
        raise WhatsAppWebhookRejected("unauthorized", 401)
    supplied = signature.removeprefix("sha256=").strip().lower()
    expected = sign_webhook(payload, secret)
    if len(supplied) != len(expected) or not hmac.compare_digest(supplied, expected):
        raise WhatsAppWebhookRejected("unauthorized", 401)


def _text(value: Any, field: str, *, limit: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WhatsAppWebhookRejected(f"invalid_{field}")
    result = value.strip()
    if len(result) > limit:
        raise WhatsAppWebhookRejected(f"invalid_{field}")
    return result


def parse_webhook(payload: bytes, *, expected_instance: str) -> WhatsAppInbound:
    if not payload or len(payload) > MAX_WHATSAPP_WEBHOOK_BYTES:
        raise WhatsAppWebhookRejected("payload_too_large", 413)
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WhatsAppWebhookRejected("invalid_payload") from error
    if not isinstance(document, dict):
        raise WhatsAppWebhookRejected("invalid_payload")
    instance = _text(document.get("instance"), "instance", limit=128)
    if not expected_instance or not hmac.compare_digest(instance, expected_instance):
        raise WhatsAppWebhookRejected("wrong_instance", 403)
    normalized = document.get("message")
    if isinstance(normalized, dict):
        if normalized.get("from_me") is True:
            raise WhatsAppWebhookRejected("ignored_message", 409)
        message_id = _text(normalized.get("id"), "message_id", limit=256)
        actor_id = _text(normalized.get("from"), "actor_id", limit=64).removeprefix("+")
        chat_id = _text(
            normalized.get("remote_jid") or f"{actor_id}@s.whatsapp.net",
            "chat_id",
            limit=256,
        )
        text = normalized.get("text")
        display_name = normalized.get("push_name")
    else:
        data = document.get("data")
        if not isinstance(data, dict):
            raise WhatsAppWebhookRejected("invalid_payload")
        key = data.get("key")
        if not isinstance(key, dict) or key.get("fromMe") is True:
            raise WhatsAppWebhookRejected("ignored_message", 409)
        message_id = _text(key.get("id"), "message_id", limit=256)
        chat_id = _text(key.get("remoteJid"), "chat_id", limit=256)
        actor_id = str(key.get("participant") or chat_id).split("@", 1)[0]
        message = data.get("message")
        if not isinstance(message, dict):
            raise WhatsAppWebhookRejected("unsupported_message")
        text = message.get("conversation")
        if not text and isinstance(message.get("extendedTextMessage"), dict):
            text = message["extendedTextMessage"].get("text")
        display_name = data.get("pushName")
    return WhatsAppInbound(
        message_id=message_id,
        chat_id=chat_id,
        actor_id=_text(actor_id, "actor_id", limit=64),
        text=_text(text, "text"),
        display_name=(str(display_name).strip() or None) if display_name else None,
        instance=instance,
    )


def as_eml(inbound: WhatsAppInbound) -> bytes:
    """Canonical email-shaped payload lets all ingress doors share one extractor."""
    message = EmailMessage()
    sender = inbound.actor_id.replace("+", "") or "unknown"
    message["From"] = f"{inbound.display_name or 'WhatsApp'} <{sender}@whatsapp.invalid>"
    message["To"] = "assistant@whatsapp.invalid"
    message["Subject"] = f"WhatsApp: {inbound.display_name or inbound.actor_id}"
    message["Message-ID"] = f"<{inbound.message_id}@{inbound.instance}.whatsapp.invalid>"
    actor = inbound.actor_id if inbound.actor_id.startswith("+") else f"+{inbound.actor_id}"
    message["X-Abrolia-WhatsApp-Actor"] = actor
    message["X-Abrolia-WhatsApp-Chat"] = inbound.chat_id
    message.set_content(inbound.text)
    return message.as_bytes()


def trusted_run_context(raw: bytes, household: Household) -> RunContext:
    """Rebuild rights from relay-authenticated provenance, never message text."""
    message = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
    actor = str(message.get("X-Abrolia-WhatsApp-Actor") or "")
    chat = str(message.get("X-Abrolia-WhatsApp-Chat") or "")
    if not actor or not chat:
        raise WhatsAppWebhookRejected("missing_trusted_provenance")
    return build_run_context(household=household, actor_id=actor, chat_id=chat)


class WhatsAppWebhookReceiver:
    def __init__(self, store: EventStore, *, signing_secret: str, instance: str) -> None:
        self.store = store
        self.signing_secret = signing_secret
        self.instance = instance

    def receive(self, payload: bytes, signature: str) -> Accepted:
        verify_webhook(payload, signature, self.signing_secret)
        inbound = parse_webhook(payload, expected_instance=self.instance)
        return self.store.append(
            source="whatsapp",
            external_id=f"whatsapp:{inbound.instance}:{inbound.message_id}",
            context_key=f"whatsapp:{inbound.chat_id}",
            raw=as_eml(inbound),
        )

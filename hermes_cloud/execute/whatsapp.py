"""Evolution WhatsApp delivery after a staged approval.

The sender deliberately has no approval API: callers only receive this object
inside the executor, after ``ApprovalStore.claim``.  Network failures preserve
the donor taxonomy so an ambiguous send is never retried automatically.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

ENV_SEND_ENABLED = "HERMES_WHATSAPP_SEND"
ENV_API_URL = "HERMES_WHATSAPP_API_URL"
ENV_INSTANCE = "HERMES_WHATSAPP_INSTANCE"
ENV_API_KEY = "HERMES_WHATSAPP_API_KEY"

_E164 = re.compile(r"\+[1-9][0-9]{7,14}")


class WhatsAppRejected(RuntimeError):
    """Provider explicitly rejected the send; it is safe not to retry."""


class WhatsAppOutcomeUnknown(RuntimeError):
    """The request may have reached the provider; blind retry is forbidden."""


@dataclass(frozen=True)
class WhatsAppReceipt:
    message_id: str


@dataclass(frozen=True)
class OutgoingWhatsApp:
    to: str
    text: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> OutgoingWhatsApp:
        return cls(to=str(payload.get("to") or ""), text=str(payload.get("text") or ""))


def validate(message: OutgoingWhatsApp) -> OutgoingWhatsApp:
    if not _E164.fullmatch(message.to):
        raise WhatsAppRejected("получатель WhatsApp должен быть в формате E.164")
    text = message.text.strip()
    if not text:
        raise WhatsAppRejected("текст WhatsApp обязателен")
    if len(text) > 4096:
        raise WhatsAppRejected("текст WhatsApp длиннее 4096 символов")
    return OutgoingWhatsApp(to=message.to, text=text)


class WhatsAppSender:
    """Per-household Evolution instance with fail-closed configuration."""

    def __init__(
        self,
        *,
        api_url: str,
        instance: str,
        api_key: str,
        enabled: bool,
        client: httpx.Client | None = None,
        timeout: float = 20.0,
    ) -> None:
        if not api_url or not instance or not api_key:
            raise ValueError("WhatsApp provider configuration is incomplete")
        self.api_url = api_url.rstrip("/")
        self.instance = instance
        self.api_key = api_key
        self.enabled = enabled
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=False)
        self._owns_client = client is None

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, *, client: httpx.Client | None = None
    ) -> WhatsAppSender:
        source = dict(os.environ if env is None else env)
        return cls(
            api_url=source.get(ENV_API_URL, ""),
            instance=source.get(ENV_INSTANCE, ""),
            api_key=source.get(ENV_API_KEY, ""),
            enabled=source.get(ENV_SEND_ENABLED, "").casefold() in {"1", "true", "yes", "on"},
            client=client,
        )

    def send(self, message: OutgoingWhatsApp, *, effect_id: str) -> WhatsAppReceipt:
        message = validate(message)
        if not self.enabled:
            raise WhatsAppRejected("отправка WhatsApp выключена")
        try:
            response = self.client.post(
                f"{self.api_url}/message/sendText/{self.instance}",
                headers={"apikey": self.api_key, "Idempotency-Key": effect_id},
                json={"number": message.to.removeprefix("+"), "text": message.text},
                follow_redirects=False,
            )
        except httpx.ReadTimeout as error:
            raise WhatsAppOutcomeUnknown("таймаут чтения ответа Evolution") from error
        except (httpx.ConnectError, httpx.ConnectTimeout) as error:
            raise WhatsAppRejected("Evolution недоступен до отправки") from error
        if 300 <= response.status_code < 400:
            raise WhatsAppRejected("Evolution вернул перенаправление")
        if response.status_code >= 400:
            raise WhatsAppRejected(f"Evolution отклонил отправку: HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as error:
            raise WhatsAppRejected("Evolution вернул некорректный ответ") from error
        message_id = body.get("key", {}).get("id") if isinstance(body, dict) else None
        if not isinstance(message_id, str) or not message_id:
            raise WhatsAppRejected("Evolution не вернул id сообщения")
        return WhatsAppReceipt(message_id=message_id)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

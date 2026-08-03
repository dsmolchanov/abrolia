"""Минимум Telegram: разбор апдейта, allowlist, отправка карточки и файла.

Транспорт отделён от логики интерфейсом `Transport`: тесты гоняют конвейер на
`FakeTransport`, а рантайм — на HTTP. Это не абстракция ради абстракции —
без неё каждый тест карточки требовал бы живого бота.

Разбор апдейта — часть границы доверия (`docs/SECURITY.md`, T4): актор,
чат и тред берутся **из проверенного апдейта**, а не из текста сообщения. На
этой же границе собирается `RunContext` — права хода; ниже по конвейеру их
никто не пересчитывает и не расширяет. Неизвестный участник группы не получает
ни tools, ни возможности подтвердить чужое предложение.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from hermes_cloud.core.runcontext import Household, RunContext, build_run_context

logger = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"
DEFAULT_TIMEOUT = 20.0


@dataclass(frozen=True)
class IncomingMessage:
    context: RunContext
    text: str
    message_id: int


@dataclass(frozen=True)
class IncomingCallback:
    """Нажатие кнопки: несёт действие и id предложения, но не код."""

    context: RunContext
    action: str
    approval_id: str
    callback_id: str
    message_id: int | None = None


class Transport(Protocol):
    """То немногое, что конвейеру нужно от канала."""

    def send_message(
        self, *, chat: str, text: str, thread: int | None = None,
        buttons: tuple[tuple[str, str], ...] = (),
    ) -> str: ...

    def send_document(
        self, *, chat: str, filename: str, content: bytes, caption: str = "",
        thread: int | None = None,
    ) -> str: ...

    def answer_callback(self, callback_id: str, text: str = "") -> None: ...

    def get_updates(self, *, offset: int | None = None, timeout: int = 25) -> list[dict]: ...


@dataclass
class SentMessage:
    chat: str
    text: str
    thread: int | None
    buttons: tuple[tuple[str, str], ...]


@dataclass
class SentDocument:
    chat: str
    filename: str
    content: bytes
    caption: str
    thread: int | None


@dataclass
class FakeTransport:
    """Транспорт для тестов: всё отправленное складывается в списки."""

    messages: list[SentMessage] = field(default_factory=list)
    documents: list[SentDocument] = field(default_factory=list)
    answered: list[tuple[str, str]] = field(default_factory=list)
    updates: list[dict[str, Any]] = field(default_factory=list)

    def send_message(
        self, *, chat: str, text: str, thread: int | None = None,
        buttons: tuple[tuple[str, str], ...] = (),
    ) -> str:
        self.messages.append(SentMessage(chat, text, thread, buttons))
        return f"fake-{len(self.messages)}"

    def send_document(
        self, *, chat: str, filename: str, content: bytes, caption: str = "",
        thread: int | None = None,
    ) -> str:
        self.documents.append(SentDocument(chat, filename, content, caption, thread))
        return f"fake-doc-{len(self.documents)}"

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.answered.append((callback_id, text))

    def get_updates(self, *, offset: int | None = None, timeout: int = 25) -> list[dict]:
        pending, self.updates = self.updates, []
        return pending


class TelegramTransport:
    """HTTP-транспорт поверх Bot API. Токен приходит из secrets, не из кода."""

    def __init__(self, token: str, *, api_root: str = API_ROOT,
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        if not token:
            raise ValueError("пустой токен Telegram")
        self._token = token
        self._api_root = api_root
        self._timeout = timeout

    def _call(
        self, method: str, payload: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self._api_root}/bot{self._token}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self._timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            # Ответ сервера получен: отправки не было и не будет. Повторять
            # такое бессмысленно — чаще всего это неверный chat_id или токен.
            raise TransportError(f"{method}: HTTP {error.code} {error.reason}") from error
        except urllib.error.URLError as error:
            # Ответа нет: отправка могла дойти до Telegram — исход неизвестен,
            # и молча повторять её нельзя (донорская семантика).
            raise SendOutcomeUnknown(f"{method}: {error}") from error
        if not body.get("ok"):
            raise TransportError(f"{method}: {body.get('description')}")
        return body["result"]

    def send_message(
        self, *, chat: str, text: str, thread: int | None = None,
        buttons: tuple[tuple[str, str], ...] = (),
    ) -> str:
        payload: dict[str, Any] = {"chat_id": chat, "text": text}
        if thread is not None:
            payload["message_thread_id"] = thread
        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": label, "callback_data": data}] for label, data in buttons
                ]
            }
        return str(self._call("sendMessage", payload)["message_id"])

    def send_document(
        self, *, chat: str, filename: str, content: bytes, caption: str = "",
        thread: int | None = None,
    ) -> str:
        """multipart/form-data вручную: ради одного вызова тянуть зависимость незачем."""
        boundary = "hermes" + uuid.uuid4().hex
        fields: list[tuple[str, str]] = [("chat_id", chat)]
        if thread is not None:
            fields.append(("message_thread_id", str(thread)))
        if caption:
            fields.append(("caption", caption))
        parts: list[bytes] = []
        for name, value in fields:
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                f"{value}\r\n".encode()
            )
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\";"
            f" filename=\"{filename}\"\r\nContent-Type: text/calendar\r\n\r\n".encode()
        )
        parts.append(content)
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        request = urllib.request.Request(
            f"{self._api_root}/bot{self._token}/sendDocument",
            data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise TransportError(f"sendDocument: HTTP {error.code} {error.reason}") from error
        except urllib.error.URLError as error:
            raise SendOutcomeUnknown(f"sendDocument: {error}") from error
        if not body.get("ok"):
            raise TransportError(f"sendDocument: {body.get('description')}")
        return str(body["result"]["message_id"])

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    def get_updates(self, *, offset: int | None = None, timeout: int = 25) -> list[dict]:
        """Long-polling. Webhook — Фаза 4; здесь достаточно опроса.

        Таймаут сокета обязан быть больше запрошенного времени ожидания:
        при long-poll Telegram молчит до `timeout` секунд, и клиент с меньшим
        таймаутом обрывает соединение раньше ответа.
        """
        payload: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        return list(
            self._call("getUpdates", payload, timeout=timeout + self._timeout)
        )


class TransportError(RuntimeError):
    """Канал отказал явно — отправки не было."""


class SendOutcomeUnknown(RuntimeError):
    """Связь оборвалась: отправка могла пройти. Слепой повтор запрещён."""


def parse_update(
    update: dict[str, Any], household: Household
) -> IncomingMessage | IncomingCallback | None:
    """Разобрать апдейт Bot API. None — апдейт нам не интересен.

    Актор и чат берутся только из проверенных полей апдейта: то, что написано
    в тексте сообщения, на права не влияет.
    """
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        message = callback.get("message") or {}
        chat = str((message.get("chat") or {}).get("id", ""))
        actor = str((callback.get("from") or {}).get("id", ""))
        data = str(callback.get("data") or "")
        if not chat or not actor or ":" not in data:
            return None
        action, _, approval_id = data.partition(":")
        return IncomingCallback(
            context=build_run_context(
                household=household, actor_id=actor, chat_id=chat,
                thread_id=message.get("message_thread_id"),
            ),
            action=action,
            approval_id=approval_id,
            callback_id=str(callback.get("id", "")),
            message_id=message.get("message_id"),
        )

    message = update.get("message") or update.get("edited_message")
    if isinstance(message, dict):
        chat = str((message.get("chat") or {}).get("id", ""))
        actor = str((message.get("from") or {}).get("id", ""))
        if not chat or not actor:
            return None
        return IncomingMessage(
            context=build_run_context(
                household=household, actor_id=actor, chat_id=chat,
                thread_id=message.get("message_thread_id"),
            ),
            text=str(message.get("text") or message.get("caption") or ""),
            message_id=int(message.get("message_id", 0)),
        )
    return None

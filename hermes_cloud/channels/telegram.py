"""Минимум Telegram: разбор апдейта, allowlist, отправка карточки и файла.

Транспорт отделён от логики интерфейсом `Transport`: тесты гоняют конвейер на
`FakeTransport`, а рантайм — на HTTP. Это не абстракция ради абстракции —
без неё каждый тест карточки требовал бы живого бота.

Разбор апдейта — часть границы доверия (`docs/SECURITY.md`, T4): актор,
чат и тред берутся **из проверенного апдейта**, а не из текста сообщения, и
чат сверяется с allowlist household'а. Неизвестный участник группы не получает
ни tools, ни возможности подтвердить чужое предложение.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"
DEFAULT_TIMEOUT = 20.0


@dataclass(frozen=True)
class Origin:
    """Откуда пришёл ход: проверенные транспортом чат, тред и актор."""

    chat: str
    thread: int | None
    actor: str
    is_known: bool


@dataclass(frozen=True)
class IncomingMessage:
    origin: Origin
    text: str
    message_id: int


@dataclass(frozen=True)
class IncomingCallback:
    """Нажатие кнопки: несёт действие и id предложения, но не код."""

    origin: Origin
    action: str
    approval_id: str
    callback_id: str
    message_id: int | None = None


@dataclass(frozen=True)
class HouseholdChannels:
    """Кому и где разрешено действовать. В Фазе 5 приедет из household.toml."""

    allowed_chats: frozenset[str] = frozenset()
    family_actors: frozenset[str] = frozenset()

    def origin_for(self, chat: str, thread: int | None, actor: str) -> Origin:
        known = (not self.allowed_chats or str(chat) in self.allowed_chats) and (
            not self.family_actors or str(actor) in self.family_actors
        )
        return Origin(chat=str(chat), thread=thread, actor=str(actor), is_known=known)


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


class TelegramTransport:
    """HTTP-транспорт поверх Bot API. Токен приходит из secrets, не из кода."""

    def __init__(self, token: str, *, api_root: str = API_ROOT,
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        if not token:
            raise ValueError("пустой токен Telegram")
        self._token = token
        self._api_root = api_root
        self._timeout = timeout

    def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self._api_root}/bot{self._token}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as error:
            # Отправка могла дойти до Telegram — исход неизвестен, и молча
            # повторять её нельзя (донорская семантика SendOutcomeUnknown).
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
    ) -> str:  # pragma: no cover - требует multipart и живого бота
        raise NotImplementedError(
            "отправка файла в Telegram подключается вместе с ботом (ручная проверка Фазы 1)"
        )

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})


class TransportError(RuntimeError):
    """Канал отказал явно — отправки не было."""


class SendOutcomeUnknown(RuntimeError):
    """Связь оборвалась: отправка могла пройти. Слепой повтор запрещён."""


def parse_update(
    update: dict[str, Any], channels: HouseholdChannels
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
            origin=channels.origin_for(chat, message.get("message_thread_id"), actor),
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
            origin=channels.origin_for(chat, message.get("message_thread_id"), actor),
            text=str(message.get("text") or message.get("caption") or ""),
            message_id=int(message.get("message_id", 0)),
        )
    return None

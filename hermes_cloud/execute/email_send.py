"""Исходящее письмо: единственное действие, которое нельзя отозвать.

Напоминание можно удалить, событие в календаре — поправить, запись в памяти —
заменить. Отправленное письмо живёт у получателя, и никакая наша логика этого не
меняет. Отсюда всё устройство модуля.

**Получатель — часть подтверждения.** Карточка показывает адрес отдельной
строкой, а `payload_sha` привязывает подтверждение к точному payload'у: подмена
адреса после показа карточки делает подтверждение недействительным
(`core/approvals.py`).

**Kill-switch проверяется здесь, а не выше.** `HERMES_EMAIL_SEND=1` —
единственный способ включить исходящую почту, и флаг перечитывается
непосредственно перед транспортом. Проверка «где-то раньше по пути» защищает
ровно до первого нового вызывающего.

**Оборванная связь — не «не отправлено».** SMTP мог принять письмо и умереть до
подтверждения; повторная отправка означала бы второе письмо школе. Такой исход
называется своим именем и не повторяется (донорская семантика).

**Заголовки не склеиваются из пользовательского текста.** Перевод строки в
адресе или теме — это инъекция заголовка, то есть чужие Bcc и Reply-To в нашем
письме. Проверка стоит до сборки сообщения.
"""

from __future__ import annotations

import logging
import os
import re
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Protocol

logger = logging.getLogger(__name__)

DEFAULT_HOST = "smtp.gmail.com"
DEFAULT_PORT = 465
ENV_KILL_SWITCH = "HERMES_EMAIL_SEND"
# Домен для Message-ID: не резолвится намеренно (RFC 2606), а идентичность
# письма всё равно наша.
MESSAGE_ID_DOMAIN = "hermes-cloud.invalid"

MAX_SUBJECT = 200
MAX_BODY = 20_000

_ADDRESS = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[A-Za-z]{2,}$")


class EgressBlocked(RuntimeError):
    """Исходящая почта выключена флагом. Это не сбой, а решение."""


class EmailRejected(ValueError):
    """Письмо не проходит проверку: адрес, тема или тело негодны."""


class EmailOutcomeUnknown(RuntimeError):
    """Связь оборвалась во время отправки. Повторять запрещено."""


@dataclass(frozen=True)
class Outgoing:
    """Письмо, каким его подтвердил человек."""

    to: str
    subject: str
    body: str
    in_reply_to: str | None = None
    references: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Outgoing:
        return cls(
            to=str(payload.get("to") or ""),
            subject=str(payload.get("subject") or ""),
            body=str(payload.get("body") or ""),
            in_reply_to=payload.get("in_reply_to") or None,
            references=payload.get("references") or None,
        )


def validate(letter: Outgoing) -> None:
    """Проверить письмо до сборки заголовков. Отказ — исключение, не правка."""
    if not _ADDRESS.match(letter.to):
        raise EmailRejected(f"негодный адрес получателя: {letter.to!r}")
    for name, value in (("to", letter.to), ("subject", letter.subject),
                        ("in_reply_to", letter.in_reply_to or "")):
        if "\n" in value or "\r" in value:
            # Перевод строки в заголовке — это чужие Bcc и Reply-To в нашем
            # письме, а не «странное форматирование».
            raise EmailRejected(f"инъекция заголовка в {name!r}")
    if not letter.subject.strip():
        raise EmailRejected("пустая тема")
    if len(letter.subject) > MAX_SUBJECT:
        raise EmailRejected(f"тема длиннее {MAX_SUBJECT} символов")
    if not letter.body.strip():
        raise EmailRejected("пустое тело письма")
    if len(letter.body) > MAX_BODY:
        raise EmailRejected(f"тело длиннее {MAX_BODY} символов")


def message_id_for(approval_id: str) -> str:
    """Детерминированный Message-ID: у письма одного подтверждения он один."""
    return f"<hermes-{approval_id}@{MESSAGE_ID_DOMAIN}>"


def build_message(letter: Outgoing, *, sender: str, approval_id: str) -> EmailMessage:
    validate(letter)
    message = EmailMessage()
    message["From"] = sender
    message["To"] = letter.to
    message["Subject"] = letter.subject
    message["Message-ID"] = message_id_for(approval_id)
    if letter.in_reply_to:
        # Ответ в исходную цепочку: получатель увидит письмо там, где ждёт,
        # а не отдельной веткой.
        message["In-Reply-To"] = letter.in_reply_to
        message["References"] = letter.references or letter.in_reply_to
    message.set_content(letter.body)
    return message


class SmtpBackend(Protocol):
    def send(self, message: EmailMessage) -> None: ...


@dataclass
class FakeSmtp:
    """SMTP для тестов и `--console`: письма складываются в список."""

    sent: list[EmailMessage] = None  # type: ignore[assignment]
    fail_with: BaseException | None = None

    def __post_init__(self) -> None:
        if self.sent is None:
            self.sent = []

    def send(self, message: EmailMessage) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append(message)


class SmtpSsl:
    """Живой SMTP. Хост и порт — из конфига household'а, не из кода."""

    def __init__(
        self,
        *,
        address: str,
        password: str,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        timeout: float = 30.0,
    ) -> None:
        self.address = address
        self.password = password
        self.host = host
        self.port = port
        self.timeout = timeout

    def send(self, message: EmailMessage) -> None:
        with smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout) as server:
            server.login(self.address, self.password)
            try:
                server.send_message(message)
            except (smtplib.SMTPServerDisconnected, TimeoutError, OSError) as error:
                # Сервер мог принять письмо и оборвать соединение до ответа.
                raise EmailOutcomeUnknown(
                    "связь с SMTP оборвалась во время отправки: приняли или нет — неизвестно"
                ) from error


class EmailSender:
    """Отправка подтверждённого письма. Ничего не решает — только исполняет."""

    def __init__(self, backend: SmtpBackend, *, sender: str) -> None:
        self.backend = backend
        self.sender = sender

    def send(self, letter: Outgoing, *, approval_id: str) -> str:
        """Отправить и вернуть Message-ID. Флаг перечитывается здесь."""
        if os.environ.get(ENV_KILL_SWITCH, "0") != "1":
            raise EgressBlocked(
                f"исходящая почта выключена (${ENV_KILL_SWITCH}=0) — письмо не отправлено"
            )
        message = build_message(letter, sender=self.sender, approval_id=approval_id)
        self.backend.send(message)
        logger.info("письмо по подтверждению %s отправлено", approval_id)
        return message["Message-ID"]

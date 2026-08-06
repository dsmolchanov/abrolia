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

import base64
import binascii
import hashlib
import logging
import os
import re
import smtplib
import time
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Protocol

from hermes_cloud.email.contracts import (
    EmailBinding,
    EmailDeliveryReceipt,
    EmailSendRequest,
)
from hermes_cloud.email.receipts import (
    BindingRevisionChanged,
    EmailBindingStore,
    EmailSendStore,
)

logger = logging.getLogger(__name__)

DEFAULT_HOST = "smtp.gmail.com"
DEFAULT_PORT = 465
ENV_KILL_SWITCH = "HERMES_EMAIL_SEND"
# Домен для Message-ID: не резолвится намеренно (RFC 2606), а идентичность
# письма всё равно наша.
MESSAGE_ID_DOMAIN = "hermes-cloud.invalid"

MAX_SUBJECT = 200
MAX_BODY = 20_000
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENT_TOTAL_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENT_COUNT = 10
ALLOWED_ATTACHMENT_TYPES = frozenset({
    "application/pdf",
    "image/jpeg",
    "image/png",
    "text/plain",
})

_ADDRESS = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[A-Za-z]{2,}$")


class EgressBlocked(RuntimeError):
    """Исходящая почта выключена флагом. Это не сбой, а решение."""


class EmailRejected(ValueError):
    """Письмо не проходит проверку: адрес, тема или тело негодны."""


class EmailOutcomeUnknown(RuntimeError):
    """Связь оборвалась во время отправки. Повторять запрещено."""


class EmailBindingChanged(BindingRevisionChanged):
    """The approved sender identity is no longer the active binding."""


@dataclass(frozen=True)
class OutgoingAttachment:
    filename: str
    content_type: str
    content_base64: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> OutgoingAttachment:
        return cls(
            filename=str(payload.get("filename") or ""),
            content_type=str(payload.get("content_type") or "").casefold(),
            content_base64=str(payload.get("content_base64") or ""),
        )

    def content(self) -> bytes:
        try:
            return base64.b64decode(self.content_base64, validate=True)
        except (ValueError, binascii.Error) as error:
            raise EmailRejected("вложение не является корректным base64") from error

    def to_payload(self) -> dict[str, str]:
        return {
            "filename": self.filename,
            "content_type": self.content_type,
            "content_base64": self.content_base64,
        }


@dataclass(frozen=True)
class Outgoing:
    """Письмо, каким его подтвердил человек."""

    to: str
    subject: str
    body: str
    in_reply_to: str | None = None
    references: str | None = None
    from_identity_id: str | None = None
    binding_revision: int | None = None
    from_address: str | None = None
    attachments: tuple[OutgoingAttachment, ...] = ()

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Outgoing:
        raw_attachments = payload.get("attachments", ())
        if not isinstance(raw_attachments, (list, tuple)) or any(
            not isinstance(item, dict) for item in raw_attachments
        ):
            raise EmailRejected("негодный список вложений")
        return cls(
            to=str(payload.get("to") or ""),
            subject=str(payload.get("subject") or ""),
            body=str(payload.get("body") or ""),
            in_reply_to=payload.get("in_reply_to") or None,
            references=payload.get("references") or None,
            from_identity_id=payload.get("from_identity_id") or None,
            binding_revision=(
                int(payload["binding_revision"])
                if payload.get("binding_revision") is not None
                else None
            ),
            from_address=payload.get("from_address") or None,
            attachments=tuple(
                OutgoingAttachment.from_payload(item)
                for item in raw_attachments
            ),
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
    if len(letter.attachments) > MAX_ATTACHMENT_COUNT:
        raise EmailRejected("слишком много вложений")
    total = 0
    for attachment in letter.attachments:
        if (
            not attachment.filename
            or attachment.filename in {".", ".."}
            or "/" in attachment.filename
            or "\\" in attachment.filename
            or "\r" in attachment.filename
            or "\n" in attachment.filename
        ):
            raise EmailRejected("негодное имя вложения")
        if attachment.content_type not in ALLOWED_ATTACHMENT_TYPES:
            raise EmailRejected("тип вложения не разрешён")
        content = attachment.content()
        if not content or len(content) > MAX_ATTACHMENT_BYTES:
            raise EmailRejected("негодный размер вложения")
        total += len(content)
    if total > MAX_ATTACHMENT_TOTAL_BYTES:
        raise EmailRejected("общий размер вложений превышен")


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
    for attachment in letter.attachments:
        maintype, subtype = attachment.content_type.split("/", 1)
        message.add_attachment(
            attachment.content(),
            maintype=maintype,
            subtype=subtype,
            filename=attachment.filename,
        )
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

    def __init__(
        self,
        backend: SmtpBackend,
        *,
        sender: str,
        identity_id: str | None = None,
        binding_revision: int = 1,
        provider: str = "smtp-test",
        binding_store: EmailBindingStore | None = None,
        send_store: EmailSendStore | None = None,
    ) -> None:
        self.backend = backend
        self.sender = sender
        self.binding = EmailBinding(
            identity_id=identity_id or f"legacy-smtp:{sender.casefold()}",
            revision=binding_revision,
            provider=provider,
            address=sender,
        )
        self.binding_store = binding_store
        self.send_store = send_store

    def send(
        self,
        letter: Outgoing,
        *,
        approval_id: str,
        effect_id: str | None = None,
    ) -> EmailDeliveryReceipt:
        """Send once and return a provider-neutral delivery receipt."""
        if os.environ.get(ENV_KILL_SWITCH, "0") != "1":
            raise EgressBlocked(
                f"исходящая почта выключена (${ENV_KILL_SWITCH}=0) — письмо не отправлено"
            )
        effect_id = effect_id or approval_id
        identity_id = letter.from_identity_id or self.binding.identity_id
        revision = letter.binding_revision or self.binding.revision
        if self.binding_store is not None:
            try:
                binding = self.binding_store.require_current(identity_id, revision)
            except BindingRevisionChanged as error:
                raise EmailBindingChanged(str(error)) from error
        else:
            binding = self.binding
            if identity_id != binding.identity_id or revision != binding.revision:
                raise EmailBindingChanged(
                    "email binding changed after approval; cancel and stage a new message"
                )
        if letter.from_address and letter.from_address.casefold() != binding.address.casefold():
            raise EmailBindingChanged("approved From address is no longer active")
        message = build_message(letter, sender=binding.address, approval_id=effect_id)
        raw = message.as_bytes()
        request_sha256 = hashlib.sha256(raw).hexdigest()
        provider_request = (
            EmailSendRequest(
                effect_id=effect_id,
                approval_id=approval_id,
                binding=binding,
                message_id=str(message["Message-ID"]),
                mime_bytes=raw,
                request_sha256=request_sha256,
            )
            if getattr(self.backend, "provider", None)
            else None
        )
        if self.send_store is not None:
            previous, fresh = self.send_store.begin(
                effect_id=effect_id,
                approval_id=approval_id,
                binding=binding,
                request_sha256=request_sha256,
                message_id=str(message["Message-ID"]),
            )
            if not fresh:
                receipt = self.send_store.get(effect_id)
                if receipt is not None:
                    if receipt.status == "outcome_unknown":
                        raise EmailOutcomeUnknown(
                            "previous send outcome is unknown; automatic replay is forbidden"
                        )
                    if receipt.status == "accepted":
                        return receipt
                    raise EmailRejected("previous email send failed definitively")
                if previous == "pending":
                    if (
                        provider_request is not None
                        and getattr(self.backend, "supports_idempotent_reconcile", False)
                        and hasattr(self.backend, "reconcile")
                    ):
                        try:
                            reconciled = self.backend.reconcile(provider_request)  # type: ignore[attr-defined]
                        except EmailOutcomeUnknown:
                            reconciled = EmailDeliveryReceipt(
                                effect_id=effect_id,
                                approval_id=approval_id,
                                message_id=str(message["Message-ID"]),
                                provider_ref=None,
                                accepted_at=None,
                                status="outcome_unknown",
                            )
                        except Exception as error:
                            unknown = EmailDeliveryReceipt(
                                effect_id=effect_id,
                                approval_id=approval_id,
                                message_id=str(message["Message-ID"]),
                                provider_ref=None,
                                accepted_at=None,
                                status="outcome_unknown",
                            )
                            self.send_store.settle(unknown, error_code=type(error).__name__)
                            raise EmailOutcomeUnknown(
                                "provider reconciliation could not determine send outcome"
                            ) from error
                        if (
                            reconciled.effect_id != effect_id
                            or reconciled.approval_id != approval_id
                            or reconciled.message_id != str(message["Message-ID"])
                        ):
                            self.send_store.settle(
                                EmailDeliveryReceipt(
                                    effect_id=effect_id,
                                    approval_id=approval_id,
                                    message_id=str(message["Message-ID"]),
                                    provider_ref=None,
                                    accepted_at=None,
                                    status="failed",
                                ),
                                error_code="mismatched_receipt",
                            )
                            raise EmailRejected("email provider returned a mismatched receipt")
                        self.send_store.settle(reconciled)
                        if reconciled.status == "accepted":
                            return reconciled
                        if reconciled.status == "outcome_unknown":
                            raise EmailOutcomeUnknown(
                                "provider reconciliation could not determine send outcome"
                            )
                        raise EmailRejected("provider reconciliation rejected the message")
                    unknown = EmailDeliveryReceipt(
                        effect_id=effect_id,
                        approval_id=approval_id,
                        message_id=str(message["Message-ID"]),
                        provider_ref=None,
                        accepted_at=None,
                        status="outcome_unknown",
                    )
                    self.send_store.settle(unknown, error_code="interrupted_before_receipt")
                    raise EmailOutcomeUnknown(
                        "previous send was interrupted; automatic replay is forbidden"
                    )
        try:
            if getattr(self.backend, "provider", None):
                assert provider_request is not None
                receipt = self.backend.send(provider_request)  # type: ignore[arg-type]
                if (
                    receipt.effect_id != effect_id
                    or receipt.approval_id != approval_id
                    or receipt.message_id != str(message["Message-ID"])
                ):
                    raise EmailRejected("email provider returned a mismatched receipt")
                if receipt.status == "outcome_unknown":
                    raise EmailOutcomeUnknown("email provider could not determine send outcome")
                if receipt.status != "accepted":
                    raise EmailRejected("email provider rejected the message")
            else:
                self.backend.send(message)
                receipt = EmailDeliveryReceipt(
                    effect_id=effect_id,
                    approval_id=approval_id,
                    message_id=str(message["Message-ID"]),
                    provider_ref=None,
                    accepted_at=time.time(),
                    status="accepted",
                )
        except EmailOutcomeUnknown as send_error:
            if (
                provider_request is not None
                and getattr(self.backend, "supports_idempotent_reconcile", False)
                and hasattr(self.backend, "reconcile")
            ):
                try:
                    reconciled = self.backend.reconcile(provider_request)  # type: ignore[attr-defined]
                    if (
                        reconciled.effect_id != effect_id
                        or reconciled.approval_id != approval_id
                        or reconciled.message_id != str(message["Message-ID"])
                        or reconciled.status != "accepted"
                    ):
                        raise EmailOutcomeUnknown(
                            "provider reconciliation returned no exact accepted receipt"
                        )
                except Exception as reconcile_error:
                    if self.send_store is not None:
                        self.send_store.settle(
                            EmailDeliveryReceipt(
                                effect_id=effect_id,
                                approval_id=approval_id,
                                message_id=str(message["Message-ID"]),
                                provider_ref=None,
                                accepted_at=None,
                                status="outcome_unknown",
                            ),
                            error_code=type(reconcile_error).__name__,
                        )
                    raise EmailOutcomeUnknown(
                        "provider reconciliation could not determine send outcome"
                    ) from reconcile_error
                if self.send_store is not None:
                    self.send_store.settle(reconciled)
                return reconciled
            if self.send_store is not None:
                self.send_store.settle(
                    EmailDeliveryReceipt(
                        effect_id=effect_id,
                        approval_id=approval_id,
                        message_id=str(message["Message-ID"]),
                        provider_ref=None,
                        accepted_at=None,
                        status="outcome_unknown",
                    ),
                    error_code="transport_unknown",
                )
            raise send_error
        except Exception as error:
            if self.send_store is not None:
                self.send_store.settle(
                    EmailDeliveryReceipt(
                        effect_id=effect_id,
                        approval_id=approval_id,
                        message_id=str(message["Message-ID"]),
                        provider_ref=None,
                        accepted_at=None,
                        status="failed",
                    ),
                    error_code=type(error).__name__,
                )
            raise
        if self.send_store is not None:
            self.send_store.settle(receipt)
        logger.info("email effect %s accepted by %s", effect_id, binding.provider)
        return receipt

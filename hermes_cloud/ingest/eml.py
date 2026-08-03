"""Разбор письма, включая пересланную цепочку.

Зачем это отдельный модуль: семья **пересылает** письмо школы ассистенту,
поэтому `From` входящего — родитель, а не школа. Ответ на такое письмо без
разбора цепочки уйдёт обратно родителю (донорская семантика `send_reply`,
см. канонический план, «Семантика reply»). Значит, оригинального отправителя
надо извлечь из пересылки — тремя разными способами, потому что почтовые
клиенты оформляют пересылку по-разному:

* **вложением** `message/rfc822` (Apple Mail «Forward as Attachment»,
  Outlook) — самый надёжный случай: внутри настоящие заголовки;
* **встроенным блоком** после маркера («---------- Forwarded message ----------»
  у Gmail, «Begin forwarded message:» у Apple Mail, «-----Original Message-----»
  у Outlook) — заголовки превращены в текст и локализованы;
* **без маркера**, только по теме `Fwd:`/`WG:` — слабый случай.

`confidence` здесь — не вероятность и не калиброванная величина: это метка
способа извлечения, и по плану она влияет **только на рендер карточки**
(показывать ли «проверьте получателя»), но никогда на автоисполнение.

Содержимое письма — недоверенные данные (`docs/SECURITY.md`, T1): парсер
ничего не исполняет, не ходит в сеть и не пишет файлы, а вложения только
описывает.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import hashlib
import re
from dataclasses import dataclass, field
from email.message import Message

MAX_FORWARD_HEADER_LINES = 40  # блок заголовков пересылки не бывает длиннее

# Маркеры начала пересланного блока. Клиенты локализуют их, поэтому список
# расширяется по мере появления фикстур, а не «на всякий случай».
FORWARD_MARKERS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^\s*[-_]{2,}\s*forwarded message\s*[-_]{2,}",
        r"^\s*[-_]{2,}\s*weitergeleitete nachricht\s*[-_]{2,}",
        r"^\s*[-_]{2,}\s*mensaje reenviado\s*[-_]{2,}",
        r"^\s*[-_]{2,}\s*messaggio inoltrato\s*[-_]{2,}",
        r"^\s*[-_]{2,}\s*doorgestuurd bericht\s*[-_]{2,}",
        r"^\s*[-_]{2,}\s*message transf[ée]r[ée]\s*[-_]{2,}",
        r"^\s*begin forwarded message\s*:",
        r"^\s*anfang der weitergeleiteten nachricht\s*:",
        r"^\s*[-_]{2,}\s*original message\s*[-_]{2,}",
        r"^\s*[-_]{2,}\s*urspr(?:ü|ue|u)ngliche nachricht\s*[-_]{2,}",
    )
)

# Тема пересылки: слабый сигнал, используется только вместе с найденным From.
SUBJECT_FORWARD_RE = re.compile(r"^\s*(fwd?|wg|rv|i|dw|vs)\s*:", re.IGNORECASE)

# `From:` / `*Von:*` / `De :` — метка, двоеточие, значение.
LABELLED_LINE_RE = re.compile(r"^\s*[*_]{0,2}([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ .]{1,20}?)[*_]{0,2}\s*:\s*(.+)$")

FROM_LABELS = {"from", "von", "de", "da", "van", "fra", "od", "от"}
DATE_LABELS = {
    "date", "datum", "sent", "gesendet", "fecha", "data", "verzonden",
    "enviado", "inviato", "envoyé", "envoye", "дата",
}
SUBJECT_LABELS = {
    "subject", "betreff", "asunto", "oggetto", "onderwerp", "objet", "тема",
}
TO_LABELS = {"to", "an", "para", "a", "aan", "à", "кому"}

# Метка способа извлечения → «уверенность» для рендера карточки.
CONFIDENCE_ATTACHMENT = 0.95
CONFIDENCE_MARKER = 0.85
CONFIDENCE_SUBJECT_ONLY = 0.55


@dataclass(frozen=True)
class Attachment:
    filename: str | None
    content_type: str
    size: int
    inline: bool = False


@dataclass(frozen=True)
class OriginalSender:
    """Отправитель, стоявший в начале пересланной цепочки."""

    email: str
    name: str | None
    confidence: float
    method: str  # attachment | marker | subject-only
    date: str | None = None
    subject: str | None = None


@dataclass(frozen=True)
class ParsedEmail:
    message_id: str | None
    subject: str
    from_email: str
    from_name: str | None
    to: tuple[str, ...]
    date: str | None
    text: str
    attachments: tuple[Attachment, ...] = ()
    original_sender: OriginalSender | None = None
    thread_key: str | None = None
    nested_messages: tuple[ParsedEmail, ...] = field(default=(), repr=False)

    @property
    def is_forwarded(self) -> bool:
        return self.original_sender is not None


def _decode(payload: bytes, charset: str | None) -> str:
    for candidate in (charset or "utf-8", "utf-8", "latin-1"):
        try:
            return payload.decode(candidate, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def _header(message: Message, name: str) -> str:
    value = message.get(name)
    return "" if value is None else str(value).strip()


def _collect(message: Message) -> tuple[list[str], list[Attachment], list[Message]]:
    """Текстовые части, метаданные вложений и вложенные письма.

    Обход ручной, а не `walk()`: `message/rfc822` — тоже multipart, и walk
    просто спускается внутрь, растворяя вложенное письмо в теле внешнего.
    Тогда пересылка вложением выглядит как обычный текст, а настоящие
    заголовки оригинала теряются.
    """
    texts: list[str] = []
    attachments: list[Attachment] = []
    nested: list[Message] = []
    _collect_part(message, texts, attachments, nested)
    return texts, attachments, nested


def _collect_part(
    part: Message,
    texts: list[str],
    attachments: list[Attachment],
    nested: list[Message],
) -> None:
    content_type = part.get_content_type()
    if content_type == "message/rfc822":
        payload = part.get_payload()
        if isinstance(payload, list):
            nested.extend(item for item in payload if isinstance(item, Message))
        return
    if part.is_multipart():
        for sub in part.get_payload():
            if isinstance(sub, Message):
                _collect_part(sub, texts, attachments, nested)
        return
    disposition = (part.get_content_disposition() or "").lower()
    try:
        raw = part.get_payload(decode=True) or b""
    except Exception:  # pragma: no cover - битое кодирование
        raw = b""
    if content_type == "text/plain" and disposition != "attachment":
        texts.append(_decode(raw, part.get_content_charset()))
        return
    if content_type == "text/html" and disposition != "attachment":
        texts.append(_html_to_text(_decode(raw, part.get_content_charset())))
        return
    attachments.append(
        Attachment(
            filename=part.get_filename(),
            content_type=content_type,
            size=len(raw),
            inline=disposition == "inline",
        )
    )


def _html_to_text(html_text: str) -> str:
    """Грубое снятие разметки: нам нужен текст обязательства, не вёрстка."""
    without_blocks = re.sub(
        r"<(script|style)\b.*?</\1>", " ", html_text, flags=re.IGNORECASE | re.DOTALL
    )
    with_breaks = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", without_blocks)
    text = re.sub(r"<[^>]*>", "", with_breaks)
    return re.sub(r"[ \t]+\n", "\n", text)


def _address(value: str) -> tuple[str, str | None]:
    name, address = email.utils.parseaddr(value)
    return address.strip().lower(), (name.strip() or None)


def _forward_block(lines: list[str]) -> dict[str, str] | None:
    """Найти блок заголовков пересылки и вернуть его поля."""
    for index, line in enumerate(lines):
        if not any(marker.match(line) for marker in FORWARD_MARKERS):
            continue
        fields: dict[str, str] = {}
        for candidate in lines[index + 1 : index + 1 + MAX_FORWARD_HEADER_LINES]:
            if not candidate.strip():
                if fields:
                    break  # пустая строка после блока = конец заголовков
                continue
            match = LABELLED_LINE_RE.match(candidate)
            if not match:
                if fields:
                    break
                continue
            label = match.group(1).strip().lower().rstrip(".")
            value = match.group(2).strip()
            if label in FROM_LABELS:
                fields.setdefault("from", value)
            elif label in DATE_LABELS:
                fields.setdefault("date", value)
            elif label in SUBJECT_LABELS:
                fields.setdefault("subject", value)
            elif label in TO_LABELS:
                fields.setdefault("to", value)
        if fields.get("from"):
            return fields
    return None


def _sender_from_lines(lines: list[str]) -> dict[str, str] | None:
    """Заголовки пересылки без маркера — Outlook иногда обходится без него."""
    fields: dict[str, str] = {}
    for candidate in lines[:MAX_FORWARD_HEADER_LINES]:
        match = LABELLED_LINE_RE.match(candidate)
        if not match:
            continue
        label = match.group(1).strip().lower().rstrip(".")
        value = match.group(2).strip()
        if label in FROM_LABELS:
            fields.setdefault("from", value)
        elif label in DATE_LABELS:
            fields.setdefault("date", value)
        elif label in SUBJECT_LABELS:
            fields.setdefault("subject", value)
    return fields if fields.get("from") else None


def _original_sender(
    subject: str, text: str, nested: list[ParsedEmail]
) -> OriginalSender | None:
    # 1. Вложенное письмо: внутри настоящие заголовки, гадать не о чем.
    for message in nested:
        if message.from_email:
            return OriginalSender(
                email=message.from_email,
                name=message.from_name,
                confidence=CONFIDENCE_ATTACHMENT,
                method="attachment",
                date=message.date,
                subject=message.subject or None,
            )

    lines = text.splitlines()
    # 2. Блок после маркера пересылки.
    block = _forward_block(lines)
    method = "marker"
    if block is None and SUBJECT_FORWARD_RE.match(subject):
        # 3. Маркера нет, но тема говорит «Fwd:» — принимаем слабый сигнал.
        block = _sender_from_lines(lines)
        method = "subject-only"
    if not block:
        return None

    address, name = _address(block["from"])
    if "@" not in address:
        return None
    confidence = CONFIDENCE_MARKER if method == "marker" else CONFIDENCE_SUBJECT_ONLY
    return OriginalSender(
        email=address,
        name=name,
        confidence=confidence,
        method=method,
        date=block.get("date"),
        subject=block.get("subject"),
    )


def _thread_key(message: Message, message_id: str | None) -> str | None:
    """Ключ цепочки: корень References → In-Reply-To → собственный Message-ID."""
    references = _header(message, "References").split()
    if references:
        return references[0].strip("<>") or None
    in_reply_to = _header(message, "In-Reply-To").strip("<>")
    if in_reply_to:
        return in_reply_to
    return message_id


def parse_message(message: Message) -> ParsedEmail:
    texts, attachments, nested_raw = _collect(message)
    nested = [parse_message(item) for item in nested_raw]
    text = "\n".join(texts).strip()
    subject = _header(message, "Subject")
    from_email, from_name = _address(_header(message, "From"))
    to = tuple(
        address for _, address in email.utils.getaddresses([_header(message, "To")]) if address
    )
    message_id = _header(message, "Message-ID").strip("<>") or None
    return ParsedEmail(
        message_id=message_id,
        subject=subject,
        from_email=from_email,
        from_name=from_name,
        to=to,
        date=_header(message, "Date") or None,
        text=text,
        attachments=tuple(attachments),
        original_sender=_original_sender(subject, text, nested),
        thread_key=_thread_key(message, message_id),
        nested_messages=tuple(nested),
    )


def parse_eml(raw: bytes) -> ParsedEmail:
    """Разобрать .eml. Битые заголовки не должны ронять приём письма."""
    try:
        message = email.message_from_bytes(raw, policy=email.policy.default)
    except Exception:  # pragma: no cover - защита от совсем битого письма
        message = email.message_from_bytes(raw, policy=email.policy.compat32)
    return parse_message(message)


def external_id(parsed: ParsedEmail, raw: bytes) -> str:
    """Ключ дедупликации. Message-ID, а без него — хэш содержимого.

    Пространство имён обязательно: `external_id` уникален глобально по всем
    каналам, и `<abc@mail>` из письма не должен столкнуться с id из WhatsApp.
    """
    if parsed.message_id:
        return f"eml:{parsed.message_id}"
    return f"eml:sha256:{hashlib.sha256(raw).hexdigest()}"

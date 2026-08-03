"""Генерация ICS: событие уезжает в календарь семьи файлом, а не через API.

В Фазе 1 у нас нет доступа к Google-календарю (это Фаза 4), но семья должна
получить работающий результат уже сейчас: файл `.ics` открывается в Google
Calendar и Apple Calendar одним нажатием.

UID детерминированный — от id подтверждения. Повторная генерация того же
подтверждения даёт файл с тем же UID, поэтому календарь обновит событие, а не
заведёт второе (та же логика, что и client-generated event ID в Фазе 4).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

PRODUCT_ID = "-//Hermes Cloud//Family Ops Assistant//RU"
DEFAULT_DURATION = timedelta(hours=1)
UID_DOMAIN = "hermes-cloud.invalid"

# RFC 5545, 3.3.11: в текстовых значениях экранируются \ ; , и перевод строки.
_ESCAPES = (("\\", "\\\\"), (";", "\\;"), (",", "\\,"), ("\n", "\\n"))


@dataclass(frozen=True)
class CalendarEvent:
    uid: str
    title: str
    start: datetime
    end: datetime
    description: str | None = None
    location: str | None = None


def escape_text(value: str) -> str:
    for raw, escaped in _ESCAPES:
        value = value.replace(raw, escaped)
    return value


def fold_line(line: str, *, limit: int = 75) -> list[str]:
    """RFC 5545, 3.1: длинные строки переносятся с пробелом в начале."""
    if len(line) <= limit:
        return [line]
    chunks = [line[:limit]]
    rest = line[limit:]
    while rest:
        chunks.append(" " + rest[: limit - 1])
        rest = rest[limit - 1 :]
    return chunks


def _stamp(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def build_event(
    *,
    approval_id: str,
    title: str,
    start: datetime,
    end: datetime | None = None,
    description: str | None = None,
    location: str | None = None,
) -> CalendarEvent:
    return CalendarEvent(
        uid=f"{approval_id}@{UID_DOMAIN}",
        title=title,
        start=start,
        end=end or start + DEFAULT_DURATION,
        description=description,
        location=location,
    )


def render_ics(event: CalendarEvent, *, now: datetime | None = None) -> str:
    """Собрать календарный файл. CRLF обязателен по RFC 5545."""
    stamp = _stamp(now or datetime.now(UTC))
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODUCT_ID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{event.uid}",
        f"DTSTAMP:{stamp}",
        f"DTSTART:{_stamp(event.start)}",
        f"DTEND:{_stamp(event.end)}",
        f"SUMMARY:{escape_text(event.title)}",
    ]
    if event.description:
        lines.append(f"DESCRIPTION:{escape_text(event.description)}")
    if event.location:
        lines.append(f"LOCATION:{escape_text(event.location)}")
    lines.extend(("END:VEVENT", "END:VCALENDAR"))

    folded: list[str] = []
    for line in lines:
        folded.extend(fold_line(line))
    return "\r\n".join(folded) + "\r\n"


def filename_for(title: str) -> str:
    """Имя файла из заголовка: только безопасные символы, без путей."""
    safe = "".join(char if char.isalnum() or char in " -_" else "_" for char in title)
    return (safe.strip().replace(" ", "_")[:60] or "event") + ".ics"

"""Опция (c): читаем ящик семьи — но только письма с ярлыком `Hermes`.

Это самый деликатный вход из трёх: мы смотрим в личную почту, где лежит всё
подряд. Поэтому граница проведена не обещанием, а запросом: IMAP-поиск идёт по
`X-GM-RAW "label:Hermes"`, и письмо без ярлыка **не выбирается вовсе** — его
заголовки и тело не покидают Gmail. Семья решает, что мы видим, движением
ярлыка, а не настройкой в нашем интерфейсе.

Порт `mailwatch.py` донора с сохранением его инвариантов:

* **Идентичность письма — Message-ID, а не UID.** UID переживает не всё: смена
  `UIDVALIDITY` перенумеровывает ящик, и курсор по UID показал бы всю почту
  заново. Курсор — ограниченный список Message-ID.
* **Смена `UIDVALIDITY` — явная ошибка, а не тихий сброс.** Вызывающий обязан
  перебазироваться осознанно; молча переиграть ящик значит завалить семью
  карточками из прошлого года.
* **Ошибка транспорта никогда не означает «писем нет».** Она означает «не
  знаю» — и приводит к повтору позже, а не к выводу.

Первый запуск на живом ящике делается через `baseline()`: всё, что уже есть под
ярлыком, помечается как виденное. Иначе подключение обернулось бы сотней
карточек по письмам, которые семья давно прочитала.
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import json
import logging
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from hermes_cloud.core.db import Database
from hermes_cloud.core.events import EventStore
from hermes_cloud.ingest.rfc822 import ingest_rfc822

logger = logging.getLogger(__name__)

IMAP_HOST = "imap.gmail.com"
DEFAULT_LABEL = "Hermes"
# Курсор ограничен: список виденных Message-ID не должен расти без предела.
SEEN_LIMIT = 200
# Сколько свежих совпадений разбираем за один опрос.
MATCH_LIMIT = 50
CURSOR_KEY = "gmail_cursor"
SOURCE = "gmail"


class MailError(RuntimeError):
    """Транспорт или поиск отказали. Это «не знаю», а не «писем нет»."""


class UidValidityChanged(MailError):
    """Ящик перенумеровал UID. Перебазирование — только осознанное."""

    def __init__(self, previous: Any, current: Any) -> None:
        super().__init__(
            f"UIDVALIDITY изменился {previous!r} → {current!r}: нужно перебазирование"
        )
        self.previous = previous
        self.current = current


@dataclass
class Cursor:
    """Что мы уже видели. Хранится в базе, а не в памяти процесса."""

    uidvalidity: str | None = None
    seen: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.seen is None:
            self.seen = []

    def to_json(self) -> str:
        return json.dumps(
            {"uidvalidity": self.uidvalidity, "seen": self.seen[-SEEN_LIMIT:]},
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str | None) -> Cursor:
        if not raw:
            return cls()
        data = json.loads(raw)
        return cls(uidvalidity=data.get("uidvalidity"), seen=list(data.get("seen") or []))


def load_cursor(database: Database) -> Cursor:
    row = database.query_one(
        "SELECT value FROM channel_state WHERE key = ?", (CURSOR_KEY,)
    )
    return Cursor.from_json(row["value"] if row else None)


def save_cursor(database: Database, cursor: Cursor, *, now: float | None = None) -> None:
    now = time.time() if now is None else now
    with database.write() as connection:
        connection.execute(
            "INSERT INTO channel_state (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at",
            (CURSOR_KEY, cursor.to_json(), now),
        )


# --- IMAP ---------------------------------------------------------------------


def connect(address: str, app_password: str, *, host: str = IMAP_HOST) -> imaplib.IMAP4_SSL:
    if not address or not app_password:
        raise MailError("не заданы адрес и app password ящика семьи")
    mailbox = imaplib.IMAP4_SSL(host)
    mailbox.login(address, app_password)
    return mailbox


def all_mail_folder(mailbox: Any) -> str:
    """Папка «Вся почта» (RFC 6154 `\\All`): её имя локализовано."""
    _, data = mailbox.list()
    for line in data or []:
        text = line.decode(errors="replace") if isinstance(line, bytes) else str(line)
        if "\\All" in text:
            name = text.rsplit(' "/" ', 1)[-1].strip()
            return name if name.startswith('"') else f'"{name}"'
    raise MailError("папка «Вся почта» не найдена")


def uidvalidity(mailbox: Any, folder: str) -> str | None:
    """Текущий UIDVALIDITY. None — сервер не сказал, и это не «изменился»."""
    status = getattr(mailbox, "status", None)
    if status is None:
        return None
    try:
        typ, data = status(folder, "(UIDVALIDITY)")
    except Exception:  # noqa: BLE001 — библиотека кидает всё подряд
        return None
    if typ != "OK" or not data:
        return None
    text = data[0].decode(errors="replace") if isinstance(data[0], bytes) else str(data[0])
    marker = "UIDVALIDITY "
    if marker not in text:
        return None
    return text.split(marker, 1)[1].split(")")[0].strip()


def search_labelled(mailbox: Any, label: str) -> list[bytes]:
    """Только письма с ярлыком. Всё остальное нас не касается."""
    query = f'"label:{label}"'.replace("\\", "")
    typ, data = mailbox.uid("SEARCH", "X-GM-RAW", query)
    if typ != "OK":
        raise MailError(f"поиск по ярлыку не удался: {typ}")
    return (data[0] or b"").split()


def _fetch(mailbox: Any, uid: bytes, what: str) -> bytes | None:
    typ, fetched = mailbox.uid("fetch", uid, what)
    if typ != "OK" or not fetched or not isinstance(fetched[0], tuple):
        return None
    return fetched[0][1]


def message_id_of(raw: bytes) -> str:
    parsed = email.message_from_bytes(raw, policy=email.policy.default)
    return str(parsed.get("Message-ID", "")).strip()


@dataclass(frozen=True)
class PollResult:
    accepted: int = 0
    skipped: int = 0
    cursor: Cursor | None = None


class GmailPoller:
    """Опрос ящика семьи. Ничего не удаляет и не помечает прочитанным."""

    def __init__(
        self,
        events: EventStore,
        *,
        address: str,
        app_password: str,
        label: str = DEFAULT_LABEL,
        connector=None,
    ) -> None:
        self.events = events
        self.address = address
        self.app_password = app_password
        self.label = label
        self._connect = connector or (
            lambda: connect(self.address, self.app_password)
        )

    def poll(self, *, ingest: bool = True, now: float | None = None) -> PollResult:
        """Один проход. `ingest=False` — только пометить виденное (baseline)."""
        database = self.events.db
        cursor = load_cursor(database)
        mailbox = self._connect()
        try:
            folder = all_mail_folder(mailbox)
            current = uidvalidity(mailbox, folder)
            if (
                cursor.uidvalidity is not None
                and current is not None
                and str(cursor.uidvalidity) != str(current)
            ):
                raise UidValidityChanged(cursor.uidvalidity, current)
            mailbox.select(folder, readonly=True)
            uids = search_labelled(mailbox, self.label)

            seen = set(cursor.seen)
            fresh: list[str] = []
            accepted = skipped = 0
            for uid in uids[-MATCH_LIMIT:]:
                raw = _fetch(mailbox, uid, "(BODY.PEEK[])")
                if raw is None:
                    continue
                message_id = message_id_of(raw)
                if not message_id:
                    # Без Message-ID нет идентичности: повторно принять такое
                    # письмо мы не сможем отличить от нового.
                    logger.warning("письмо без Message-ID пропущено")
                    continue
                fresh.append(message_id)
                if message_id in seen:
                    skipped += 1
                    continue
                if ingest:
                    result = ingest_rfc822(
                        self.events,
                        source=SOURCE,
                        provider_event_id=f"imap:{current or 'unknown'}:{uid.decode()}",
                        raw_bytes=raw,
                        received_at=now,
                    )
                    accepted += int(result.created)
        finally:
            # Разлогиниться важно, но неудача выхода уже ничего не меняет.
            with suppress(Exception):
                mailbox.logout()

        merged = list(dict.fromkeys([*cursor.seen, *fresh]))[-SEEN_LIMIT:]
        updated = Cursor(uidvalidity=current or cursor.uidvalidity, seen=merged)
        save_cursor(database, updated, now=now)
        return PollResult(accepted=accepted, skipped=skipped, cursor=updated)

    def baseline(self, *, now: float | None = None) -> PollResult:
        """Первый запуск: то, что уже под ярлыком, — не новости.

        Без этого подключение к живому ящику вывалило бы семье карточки по
        письмам, прочитанным полгода назад.
        """
        return self.poll(ingest=False, now=now)

    def rebaseline(self, *, now: float | None = None) -> PollResult:
        """Осознанное перебазирование после смены UIDVALIDITY."""
        save_cursor(self.events.db, Cursor(), now=now)
        return self.baseline(now=now)

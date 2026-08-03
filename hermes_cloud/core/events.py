"""Долговечный ingress: приём события, аренда, DLQ, replay.

Конвейер строится вокруг одного правила: **событие, за которое мы ответили
«принято», не теряется и не обрабатывается дважды.** Отсюда три механики:

* приём фиксирует событие в транзакции с fsync и только потом отвечает
  вызывающему (`append`);
* обработчик берёт событие в аренду на время (`lease`), а не «забирает
  насовсем»: упавший воркер не уносит событие с собой — по истечении аренды
  его подберёт следующий;
* внутри одного `context_key` (сессия/тред) одновременно обрабатывается ровно
  одно событие — иначе два письма одной цепочки разъезжаются по порядку.

Неудачи считаются: после `max_attempts` событие уходит в `dlq` и ждёт
человека (`replay`), а не крутится в бесконечном повторе.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from hermes_cloud.core.db import Database, new_id

STATUS_RECEIVED = "received"
STATUS_PROCESSING = "processing"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_DLQ = "dlq"

DEFAULT_LEASE_SECONDS = 5 * 60
DEFAULT_MAX_ATTEMPTS = 5


@dataclass(frozen=True)
class Event:
    id: str
    source: str
    external_id: str
    context_key: str | None
    raw: bytes
    received_at: float
    status: str
    attempts: int
    lease_until: float | None
    leased_by: str | None
    last_error: str | None

    @classmethod
    def from_row(cls, row: Any) -> Event:
        return cls(
            id=row["id"],
            source=row["source"],
            external_id=row["external_id"],
            context_key=row["context_key"],
            raw=row["raw"],
            received_at=row["received_at"],
            status=row["status"],
            attempts=row["attempts"],
            lease_until=row["lease_until"],
            leased_by=row["leased_by"],
            last_error=row["last_error"],
        )


@dataclass(frozen=True)
class Accepted:
    """Результат приёма: событие и признак того, что оно новое."""

    event: Event
    created: bool


class EventStore:
    def __init__(self, database: Database, *, clock=time.time) -> None:
        self.db = database
        self.clock = clock

    # --- приём --------------------------------------------------------------

    def append(
        self,
        *,
        source: str,
        external_id: str,
        raw: bytes,
        context_key: str | None = None,
        received_at: float | None = None,
    ) -> Accepted:
        """Зафиксировать входящее событие. Повтор `external_id` — no-op.

        Возврат управления означает, что данные на диске: коммит выполняется с
        `synchronous=FULL`, поэтому вызывающий может отвечать «принято»
        каналу, не рискуя потерять письмо при падении процесса.
        """
        now = self.clock() if received_at is None else received_at
        event_id = new_id()
        with self.db.write() as connection:
            connection.execute(
                "INSERT INTO events (id, source, external_id, context_key, raw,"
                " received_at, status, attempts, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)"
                " ON CONFLICT (external_id) DO NOTHING",
                (event_id, source, external_id, context_key, raw, now,
                 STATUS_RECEIVED, now),
            )
        row = self.db.query_one(
            "SELECT * FROM events WHERE external_id = ?", (external_id,)
        )
        assert row is not None  # только что вставили или уже было
        event = Event.from_row(row)
        return Accepted(event=event, created=event.id == event_id)

    # --- обработка ----------------------------------------------------------

    def lease(
        self,
        worker: str,
        *,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        now: float | None = None,
    ) -> Event | None:
        """Взять в работу самое старое доступное событие.

        Доступно: новое (`received`) или брошенное (`processing` с истёкшей
        арендой). Событие не берётся, если в его `context_key` уже есть
        событие в работе с живой арендой — так сохраняется порядок внутри
        цепочки писем.
        """
        now = self.clock() if now is None else now
        with self.db.write() as connection:
            row = connection.execute(
                "SELECT * FROM events"
                " WHERE (status = ? OR (status = ? AND lease_until <= ?))"
                "   AND (context_key IS NULL OR context_key NOT IN ("
                "        SELECT context_key FROM events"
                "         WHERE status = ? AND lease_until > ?"
                "           AND context_key IS NOT NULL))"
                " ORDER BY received_at, id"
                " LIMIT 1",
                (STATUS_RECEIVED, STATUS_PROCESSING, now, STATUS_PROCESSING, now),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE events SET status = ?, lease_until = ?, leased_by = ?,"
                " updated_at = ? WHERE id = ?",
                (STATUS_PROCESSING, now + lease_seconds, worker, now, row["id"]),
            )
        return self.get(row["id"])

    def mark_done(self, event_id: str, *, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        with self.db.write() as connection:
            connection.execute(
                "UPDATE events SET status = ?, lease_until = NULL, leased_by = NULL,"
                " last_error = NULL, updated_at = ? WHERE id = ?",
                (STATUS_DONE, now, event_id),
            )

    def mark_failed(
        self,
        event_id: str,
        error: str,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        now: float | None = None,
    ) -> Event:
        """Учесть неудачу: вернуть в очередь или увести в DLQ после N попыток."""
        now = self.clock() if now is None else now
        with self.db.write() as connection:
            row = connection.execute(
                "SELECT attempts FROM events WHERE id = ?", (event_id,)
            ).fetchone()
            if row is None:
                raise KeyError(event_id)
            attempts = row["attempts"] + 1
            status = STATUS_DLQ if attempts >= max_attempts else STATUS_RECEIVED
            connection.execute(
                "UPDATE events SET status = ?, attempts = ?, last_error = ?,"
                " lease_until = NULL, leased_by = NULL, updated_at = ?"
                " WHERE id = ?",
                (status, attempts, error[:2000], now, event_id),
            )
        event = self.get(event_id)
        assert event is not None
        return event

    def replay(self, event_id: str, *, now: float | None = None) -> Event:
        """Вернуть событие в очередь вручную — из DLQ, done или зависшего."""
        now = self.clock() if now is None else now
        with self.db.write() as connection:
            updated = connection.execute(
                "UPDATE events SET status = ?, attempts = 0, last_error = NULL,"
                " lease_until = NULL, leased_by = NULL, updated_at = ?"
                " WHERE id = ?",
                (STATUS_RECEIVED, now, event_id),
            ).rowcount
            if not updated:
                raise KeyError(event_id)
        event = self.get(event_id)
        assert event is not None
        return event

    # --- чтение -------------------------------------------------------------

    def get(self, event_id: str) -> Event | None:
        row = self.db.query_one("SELECT * FROM events WHERE id = ?", (event_id,))
        return Event.from_row(row) if row else None

    def by_external_id(self, external_id: str) -> Event | None:
        row = self.db.query_one(
            "SELECT * FROM events WHERE external_id = ?", (external_id,)
        )
        return Event.from_row(row) if row else None

    def counts(self) -> dict[str, int]:
        rows = self.db.query("SELECT status, COUNT(*) AS n FROM events GROUP BY status")
        return {row["status"]: row["n"] for row in rows}

    def dead_letters(self, limit: int = 50) -> list[Event]:
        rows = self.db.query(
            "SELECT * FROM events WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
            (STATUS_DLQ, limit),
        )
        return [Event.from_row(row) for row in rows]

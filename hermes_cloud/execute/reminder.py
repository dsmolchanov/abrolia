"""Напоминания: порт донорского ReminderStore на транзакционный слой.

Донор хранил напоминания в JSON под flock и страдал от неатомарной записи
(известный дефект, зафиксированный в плане). Здесь то же поведение — создать,
выбрать созревшие, доставить, подтвердить — но запись атомарна, а доставка
идёт через аренду: упавший доставщик не теряет и не дублирует напоминание.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from datetime import time as clock_time
from typing import Any

from hermes_cloud.core.db import Database, new_id

STATUS_PENDING = "pending"
STATUS_DELIVERING = "delivering"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"

DEFAULT_LEASE_SECONDS = 120
# Дедлайн без времени — напоминаем утром того же дня, а не в полночь.
DEFAULT_HOUR = 9


@dataclass(frozen=True)
class Reminder:
    id: str
    approval_id: str | None
    chat: str
    thread: int | None
    text: str
    due_at: float
    status: str
    attempts: int

    @classmethod
    def from_row(cls, row: Any) -> Reminder:
        return cls(
            id=row["id"],
            approval_id=row["approval_id"],
            chat=row["chat"],
            thread=row["thread"],
            text=row["text"],
            due_at=row["due_at"],
            status=row["status"],
            attempts=row["attempts"],
        )


def due_timestamp(due: date | datetime, *, hour: int = DEFAULT_HOUR) -> float:
    """Дата без времени → утро того же дня в UTC."""
    if isinstance(due, datetime):
        moment = due if due.tzinfo else due.replace(tzinfo=UTC)
    else:
        moment = datetime.combine(due, clock_time(hour=hour), tzinfo=UTC)
    return moment.timestamp()


class ReminderStore:
    def __init__(self, database: Database, *, clock=time.time) -> None:
        self.db = database
        self.clock = clock

    def create(
        self,
        *,
        chat: str,
        text: str,
        due_at: float,
        thread: int | None = None,
        approval_id: str | None = None,
        now: float | None = None,
    ) -> Reminder:
        now = self.clock() if now is None else now
        reminder_id = new_id()
        with self.db.write() as connection:
            connection.execute(
                "INSERT INTO reminders (id, approval_id, chat, thread, text, due_at,"
                " status, attempts, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (reminder_id, approval_id, str(chat), thread, text, due_at,
                 STATUS_PENDING, now, now),
            )
        reminder = self.get(reminder_id)
        assert reminder is not None
        return reminder

    def claim_due(
        self,
        *,
        now: float | None = None,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> Reminder | None:
        """Взять созревшее напоминание в доставку под аренду."""
        now = self.clock() if now is None else now
        with self.db.write() as connection:
            row = connection.execute(
                "SELECT * FROM reminders"
                " WHERE due_at <= ?"
                "   AND (status = ? OR (status = ? AND lease_until <= ?))"
                " ORDER BY due_at, id LIMIT 1",
                (now, STATUS_PENDING, STATUS_DELIVERING, now),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE reminders SET status = ?, lease_until = ?, attempts = attempts + 1,"
                " updated_at = ? WHERE id = ?",
                (STATUS_DELIVERING, now + lease_seconds, now, row["id"]),
            )
        return self.get(row["id"])

    def mark_delivered(self, reminder_id: str, *, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        with self.db.write() as connection:
            connection.execute(
                "UPDATE reminders SET status = ?, lease_until = NULL, updated_at = ?"
                " WHERE id = ?",
                (STATUS_DONE, now, reminder_id),
            )

    def cancel(self, reminder_id: str, *, now: float | None = None) -> bool:
        now = self.clock() if now is None else now
        with self.db.write() as connection:
            changed = connection.execute(
                "UPDATE reminders SET status = ?, lease_until = NULL, updated_at = ?"
                " WHERE id = ? AND status IN (?, ?)",
                (STATUS_CANCELLED, now, reminder_id, STATUS_PENDING, STATUS_DELIVERING),
            ).rowcount
        return bool(changed)

    def get(self, reminder_id: str) -> Reminder | None:
        row = self.db.query_one("SELECT * FROM reminders WHERE id = ?", (reminder_id,))
        return Reminder.from_row(row) if row else None

    def pending(self, *, limit: int = 50) -> list[Reminder]:
        rows = self.db.query(
            "SELECT * FROM reminders WHERE status = ? ORDER BY due_at LIMIT ?",
            (STATUS_PENDING, limit),
        )
        return [Reminder.from_row(row) for row in rows]

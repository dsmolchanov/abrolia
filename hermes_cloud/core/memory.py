"""Память семьи: не свободный текст, а утверждения со статусом и версией.

Почему не «просто заметки». Память — единственное место, где написанное один
раз влияет на все последующие ходы. Значит, туда нельзя попасть иначе как через
человека: иначе письмо, притворившееся инструкцией, однажды поселится в памяти
и будет действовать после того, как само письмо давно удалено
(`docs/SECURITY.md`, T1 — persistent injection).

Отсюда устройство:

* запись всегда начинается как `candidate` и становится `confirmed` только
  через подтверждение человека — тем же кодом и тем же контуром, что и
  действия;
* `supersedes` вместо перезаписи: «мы теперь ходим на плавание по вторникам» не
  стирает «по понедельникам», а заменяет её, и обе видны;
* `valid_from/valid_to` — только у `routine` и `preference`. У факта нет срока
  действия, и делать вид, что есть, — врать структурой.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from hermes_cloud.core.commitments import (
    CLAIMED_STATUSES,
    STATUS_CANDIDATE,
    STATUS_CONFIRMED,
    STATUS_REJECTED,
    STATUS_SUPERSEDED,
    CommitmentError,
)
from hermes_cloud.core.db import Database, new_id

KIND_FACT = "fact"
KIND_ROUTINE = "routine"
KIND_PREFERENCE = "preference"

# Виды, у которых срок действия осмыслен. У факта его нет.
TIMED_KINDS = frozenset({KIND_ROUTINE, KIND_PREFERENCE})
KINDS = frozenset({KIND_FACT, KIND_ROUTINE, KIND_PREFERENCE})

MAX_TEXT = 500


@dataclass(frozen=True)
class Statement:
    id: str
    kind: str
    subject: str | None
    text: str
    status: str
    supersedes: str | None
    extraction_run_id: str | None
    actor: str | None
    approval_id: str | None
    observed_at: float | None
    recorded_at: float
    valid_from: float | None
    valid_to: float | None
    updated_at: float

    @classmethod
    def from_row(cls, row: Any) -> Statement:
        return cls(
            id=row["id"],
            kind=row["kind"],
            subject=row["subject"],
            text=row["text"],
            status=row["status"],
            supersedes=row["supersedes"],
            extraction_run_id=row["extraction_run_id"],
            actor=row["actor"],
            approval_id=row["approval_id"],
            observed_at=row["observed_at"],
            recorded_at=row["recorded_at"],
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
            updated_at=row["updated_at"],
        )


class MemoryStore:
    def __init__(self, database: Database, *, clock=time.time) -> None:
        self.db = database
        self.clock = clock

    def propose(
        self,
        *,
        text: str,
        kind: str = KIND_FACT,
        subject: str | None = None,
        actor: str | None = None,
        extraction_run_id: str | None = None,
        supersedes: str | None = None,
        observed_at: float | None = None,
        valid_from: float | None = None,
        valid_to: float | None = None,
        now: float | None = None,
    ) -> Statement:
        """Предложить запись. Ничего ещё не запомнено — это кандидат."""
        if kind not in KINDS:
            raise CommitmentError(f"неизвестный вид записи памяти: {kind!r}")
        text = text.strip()
        if not text:
            raise CommitmentError("пустую запись не запоминаем")
        if len(text) > MAX_TEXT:
            raise CommitmentError(f"запись длиннее {MAX_TEXT} символов")
        if kind not in TIMED_KINDS and (valid_from is not None or valid_to is not None):
            raise CommitmentError("срок действия бывает у распорядка и предпочтения, не у факта")
        now = self.clock() if now is None else now
        statement_id = new_id()
        with self.db.write() as connection:
            if supersedes is not None:
                previous = connection.execute(
                    "SELECT id FROM memory_statements WHERE id = ?", (supersedes,)
                ).fetchone()
                if previous is None:
                    raise CommitmentError(f"нет записи {supersedes!r}, которую заменяем")
            connection.execute(
                "INSERT INTO memory_statements (id, kind, subject, text, status, supersedes,"
                " extraction_run_id, actor, observed_at, recorded_at, valid_from, valid_to,"
                " updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    statement_id, kind, subject, text, STATUS_CANDIDATE, supersedes,
                    extraction_run_id, actor, observed_at, now, valid_from, valid_to, now,
                ),
            )
        return self.get(statement_id)  # type: ignore[return-value]

    def confirm(self, statement_id: str, approval: Any, *, now: float | None = None) -> Statement:
        """Запомнить — только после «да» человека. Заменяемая версия уходит в `superseded`."""
        if getattr(approval, "status", None) not in CLAIMED_STATUSES:
            raise CommitmentError("запомнить можно только по подтверждённому approval'у")
        now = self.clock() if now is None else now
        with self.db.write() as connection:
            row = connection.execute(
                "SELECT supersedes FROM memory_statements WHERE id = ? AND status = ?",
                (statement_id, STATUS_CANDIDATE),
            ).fetchone()
            if row is None:
                raise CommitmentError(f"запись {statement_id} не в состоянии {STATUS_CANDIDATE}")
            connection.execute(
                "UPDATE memory_statements SET status = ?, approval_id = ?, updated_at = ?"
                " WHERE id = ?",
                (STATUS_CONFIRMED, approval.id, now, statement_id),
            )
            if row["supersedes"] is not None:
                # Прежняя версия помечается только теперь: пока новая была
                # кандидатом, старая оставалась действующей.
                connection.execute(
                    "UPDATE memory_statements SET status = ?, updated_at = ? WHERE id = ?",
                    (STATUS_SUPERSEDED, now, row["supersedes"]),
                )
        return self.get(statement_id)  # type: ignore[return-value]

    def reject(self, statement_id: str, *, now: float | None = None) -> bool:
        now = self.clock() if now is None else now
        with self.db.write() as connection:
            changed = connection.execute(
                "UPDATE memory_statements SET status = ?, updated_at = ?"
                " WHERE id = ? AND status = ?",
                (STATUS_REJECTED, now, statement_id, STATUS_CANDIDATE),
            ).rowcount
        return bool(changed)

    # --- чтение -------------------------------------------------------------

    def get(self, statement_id: str) -> Statement | None:
        row = self.db.query_one(
            "SELECT * FROM memory_statements WHERE id = ?", (statement_id,)
        )
        return Statement.from_row(row) if row else None

    def recall(
        self,
        *,
        query: str | None = None,
        kind: str | None = None,
        limit: int = 20,
        now: float | None = None,
    ) -> list[Statement]:
        """Действующая память: подтверждённое, не заменённое, не просроченное."""
        now = self.clock() if now is None else now
        sql = (
            "SELECT * FROM memory_statements WHERE status = ?"
            " AND (valid_from IS NULL OR valid_from <= ?)"
            " AND (valid_to IS NULL OR valid_to > ?)"
        )
        params: list[Any] = [STATUS_CONFIRMED, now, now]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        if query:
            sql += " AND (text LIKE ? OR subject LIKE ?)"
            needle = f"%{query.strip()}%"
            params.extend([needle, needle])
        sql += " ORDER BY recorded_at DESC LIMIT ?"
        params.append(limit)
        return [Statement.from_row(row) for row in self.db.query(sql, tuple(params))]

    def chain(self, statement_id: str) -> list[Statement]:
        chain: list[Statement] = []
        current = self.get(statement_id)
        seen: set[str] = set()
        while current is not None and current.id not in seen:
            chain.append(current)
            seen.add(current.id)
            current = self.get(current.supersedes) if current.supersedes else None
        return list(reversed(chain))

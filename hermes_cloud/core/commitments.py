"""Обязательства семьи как квалифицированные утверждения.

Таблица тонкая нарочно: она связывает извлечение с порождёнными артефактами и
хранит то, чего нет ни в журнале, ни в напоминании, — **статус факта и его
версию**. Напоминание отвечает на «когда напомнить», обязательство — на «во что
мы верим, с каких пор и на каком основании».

Инварианты держатся здесь, а не в схеме, потому что они про переходы:

* модель создаёт только `candidate` — записать `confirmed` напрямую нельзя;
* `confirmed` появляется исключительно из подтверждения человека, и это
  подтверждение **факта**, а не разрешение на действие: разрешение — отдельный
  approval со своим кодом;
* `superseded` ничего не удаляет: v2 ссылается на v1 через `supersedes`, и
  цепочка версий читается целиком — «сумма изменилась» должно быть видно, а не
  затёрто;
* `confidence` влияет только на рендер карточки. Порога, после которого что-то
  происходит само, нет и не будет (Locked Decisions → «Модели»).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from hermes_cloud.core.approvals import (
    STATUS_CLAIMED,
    STATUS_DONE,
    STATUS_EXECUTING,
)
from hermes_cloud.core.db import Database, new_id

STATUS_CANDIDATE = "candidate"
STATUS_CONFIRMED = "confirmed"
STATUS_REJECTED = "rejected"
STATUS_SUPERSEDED = "superseded"

# Статусы подтверждения, означающие «человек уже сказал да».
CLAIMED_STATUSES = frozenset({STATUS_CLAIMED, STATUS_EXECUTING, STATUS_DONE})

KIND_PAYMENT = "payment"
KIND_EVENT = "event"
KIND_TASK = "task"


class CommitmentError(RuntimeError):
    """Недопустимый переход статуса — не «странные данные», а нарушенный инвариант."""


@dataclass(frozen=True)
class Commitment:
    id: str
    extraction_run_id: str | None
    kind: str
    payload: dict[str, Any]
    status: str
    supersedes: str | None
    confidence: float | None
    observed_at: float | None
    recorded_at: float
    approval_id: str | None
    reminder_id: str | None
    calendar_event_id: str | None
    updated_at: float

    @property
    def is_belief(self) -> bool:
        """Можно ли ссылаться на это как на факт семьи."""
        return self.status == STATUS_CONFIRMED

    @classmethod
    def from_row(cls, row: Any) -> Commitment:
        return cls(
            id=row["id"],
            extraction_run_id=row["extraction_run_id"],
            kind=row["kind"],
            payload=json.loads(row["payload"]),
            status=row["status"],
            supersedes=row["supersedes"],
            confidence=row["confidence"],
            observed_at=row["observed_at"],
            recorded_at=row["recorded_at"],
            approval_id=row["approval_id"],
            reminder_id=row["reminder_id"],
            calendar_event_id=row["calendar_event_id"],
            updated_at=row["updated_at"],
        )


class CommitmentStore:
    def __init__(self, database: Database, *, clock=time.time) -> None:
        self.db = database
        self.clock = clock

    # --- создание -----------------------------------------------------------

    def propose(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        extraction_run_id: str | None = None,
        confidence: float | None = None,
        observed_at: float | None = None,
        supersedes: str | None = None,
        now: float | None = None,
    ) -> Commitment:
        """Кандидат. Единственный статус, который может появиться из модели."""
        now = self.clock() if now is None else now
        commitment_id = new_id()
        with self.db.write() as connection:
            if supersedes is not None:
                previous = connection.execute(
                    "SELECT status FROM commitments WHERE id = ?", (supersedes,)
                ).fetchone()
                if previous is None:
                    raise CommitmentError(f"нет версии {supersedes!r}, которую заменяем")
                # v1 не удаляется и не переписывается — только помечается.
                connection.execute(
                    "UPDATE commitments SET status = ?, updated_at = ? WHERE id = ?",
                    (STATUS_SUPERSEDED, now, supersedes),
                )
            connection.execute(
                "INSERT INTO commitments (id, extraction_run_id, kind, payload, status,"
                " supersedes, confidence, observed_at, recorded_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    commitment_id, extraction_run_id, kind,
                    json.dumps(payload, ensure_ascii=False), STATUS_CANDIDATE,
                    supersedes, confidence, observed_at, now, now,
                ),
            )
        return self.get(commitment_id)  # type: ignore[return-value]

    # --- переходы -----------------------------------------------------------

    def confirm(self, commitment_id: str, approval: Any, *, now: float | None = None) -> Commitment:
        """Факт становится фактом семьи — только с подтверждением человека.

        `approval` обязан быть уже подтверждённым: подтверждение факта проходит
        через тот же `claim`, что и разрешение на действие, и подделать его,
        передав сюда «просто объект», нельзя.
        """
        if getattr(approval, "status", None) not in CLAIMED_STATUSES:
            raise CommitmentError(
                "подтвердить обязательство можно только по подтверждённому approval'у"
            )
        now = self.clock() if now is None else now
        with self.db.write() as connection:
            changed = connection.execute(
                "UPDATE commitments SET status = ?, approval_id = ?, updated_at = ?"
                " WHERE id = ? AND status = ?",
                (STATUS_CONFIRMED, approval.id, now, commitment_id, STATUS_CANDIDATE),
            ).rowcount
        if not changed:
            raise CommitmentError(
                f"обязательство {commitment_id} не в состоянии {STATUS_CANDIDATE}"
            )
        return self.get(commitment_id)  # type: ignore[return-value]

    def reject(self, commitment_id: str, *, now: float | None = None) -> bool:
        now = self.clock() if now is None else now
        with self.db.write() as connection:
            changed = connection.execute(
                "UPDATE commitments SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                (STATUS_REJECTED, now, commitment_id, STATUS_CANDIDATE),
            ).rowcount
        return bool(changed)

    def link(
        self,
        commitment_id: str,
        *,
        reminder_id: str | None = None,
        calendar_event_id: str | None = None,
        now: float | None = None,
    ) -> None:
        """Связать с порождённым артефактом. Сам артефакт живёт в своём сторе."""
        now = self.clock() if now is None else now
        with self.db.write() as connection:
            connection.execute(
                "UPDATE commitments SET reminder_id = COALESCE(?, reminder_id),"
                " calendar_event_id = COALESCE(?, calendar_event_id), updated_at = ?"
                " WHERE id = ?",
                (reminder_id, calendar_event_id, now, commitment_id),
            )

    # --- чтение -------------------------------------------------------------

    def get(self, commitment_id: str) -> Commitment | None:
        row = self.db.query_one("SELECT * FROM commitments WHERE id = ?", (commitment_id,))
        return Commitment.from_row(row) if row else None

    def for_approval(self, approval_id: str) -> Commitment | None:
        row = self.db.query_one(
            "SELECT * FROM commitments WHERE approval_id = ? ORDER BY recorded_at LIMIT 1",
            (approval_id,),
        )
        return Commitment.from_row(row) if row else None

    def confirmed(
        self, *, kind: str | None = None, limit: int = 100
    ) -> list[Commitment]:
        """Только подтверждённое. Кандидаты сюда не попадают никогда.

        Этим методом пользуются ответы семье и дайджесты: `candidate` не должен
        прозвучать как факт — это и есть смысл статусной модели.
        """
        sql = "SELECT * FROM commitments WHERE status = ?"
        params: list[Any] = [STATUS_CONFIRMED]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY recorded_at DESC LIMIT ?"
        params.append(limit)
        return [Commitment.from_row(row) for row in self.db.query(sql, tuple(params))]

    def candidates(self, *, limit: int = 100) -> list[Commitment]:
        rows = self.db.query(
            "SELECT * FROM commitments WHERE status = ? ORDER BY recorded_at DESC LIMIT ?",
            (STATUS_CANDIDATE, limit),
        )
        return [Commitment.from_row(row) for row in rows]

    def chain(self, commitment_id: str) -> list[Commitment]:
        """Цепочка версий от самой ранней к этой. `superseded` не теряется."""
        chain: list[Commitment] = []
        current = self.get(commitment_id)
        seen: set[str] = set()
        while current is not None and current.id not in seen:
            chain.append(current)
            seen.add(current.id)
            current = self.get(current.supersedes) if current.supersedes else None
        return list(reversed(chain))

    def latest_version_of(self, commitment_id: str) -> Commitment | None:
        """Последняя версия факта: идём вперёд по ссылкам `supersedes`."""
        current = self.get(commitment_id)
        while current is not None:
            row = self.db.query_one(
                "SELECT * FROM commitments WHERE supersedes = ? ORDER BY recorded_at LIMIT 1",
                (current.id,),
            )
            if row is None:
                return current
            current = Commitment.from_row(row)
        return None

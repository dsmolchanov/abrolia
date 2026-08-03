"""Журнал эффектов: единственный источник правды о том, что уже сделано.

Три правила, из которых следует всё остальное.

**Запись раньше действия.** Строка эффекта появляется в той же транзакции, что
и решение действовать (claim подтверждения, разбор `tool_use`), и только потом
вызывается исполнитель. Падение между записью и действием оставляет `pending` —
состояние «неизвестно», которое можно разобрать. Обратный порядок оставил бы
пустоту, неотличимую от «не начинали».

**Повтор не исполняет.** Ключ `(run_id, tool_use_id)` уникален: тот же ход с
тем же `tool_use_id` получает прежний результат из журнала. Модель, повторившая
вызов, не создаёт второе событие в календаре.

**Слепого повтора нет.** Аренда истекла — это не разрешение сделать ещё раз.
Что делать с зависшим эффектом, решает стратегия его вида (`reconcile.py`):
локальный и идемпотентный — доделать, наружный — `outcome_unknown` и честный
рассказ человеку.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from hermes_cloud.core.db import Database, new_id

STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_OUTCOME_UNKNOWN = "outcome_unknown"

# Аренда исполнителя: дольше самого медленного вызова канала, но заметно
# короче человеческого терпения — зависшее должно разбираться при рестарте.
DEFAULT_LEASE_SECONDS = 120.0

# Ключ эффекта для подтверждений: у них ход — само подтверждение, и эффект в
# нём ровно один. Имя постоянное, чтобы повтор claim'а попал в ту же строку.
APPROVAL_TOOL_USE_ID = "approval"


class EffectInFlight(RuntimeError):
    """Эффект уже исполняется под живой арендой. Второй раз — нельзя."""

    def __init__(self, effect: Effect) -> None:
        super().__init__(f"эффект {effect.id} ({effect.kind}) уже исполняется")
        self.effect = effect


@dataclass(frozen=True)
class Effect:
    id: str
    run_id: str
    tool_use_id: str
    kind: str
    approval_id: str | None
    payload_sha: str | None
    status: str
    result: str | None
    error: str | None
    attempts: int
    lease_until: float | None
    created_at: float
    updated_at: float

    @property
    def settled(self) -> bool:
        """Исход известен — повторять нечего ни при каком раскладе."""
        return self.status in {STATUS_DONE, STATUS_FAILED, STATUS_OUTCOME_UNKNOWN}

    @classmethod
    def from_row(cls, row: Any) -> Effect:
        return cls(
            id=row["id"],
            run_id=row["run_id"],
            tool_use_id=row["tool_use_id"],
            kind=row["kind"],
            approval_id=row["approval_id"],
            payload_sha=row["payload_sha"],
            status=row["status"],
            result=row["result"],
            error=row["error"],
            attempts=row["attempts"],
            lease_until=row["lease_until"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def record_attempt(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    tool_use_id: str,
    kind: str,
    approval_id: str | None = None,
    payload_sha: str | None = None,
    now: float,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
) -> str:
    """Записать намерение исполнить — **внутри чужой транзакции**.

    Так `claim` подтверждения и запись попытки становятся одним атомарным
    фактом: не бывает подтверждения без записи о нём и записи без подтверждения.
    """
    effect_id = new_id()
    connection.execute(
        "INSERT INTO effects (id, run_id, tool_use_id, kind, approval_id, payload_sha,"
        " status, attempts, lease_until, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
        (
            effect_id, run_id, tool_use_id, kind, approval_id, payload_sha,
            STATUS_PENDING, now + lease_seconds, now, now,
        ),
    )
    return effect_id


class EffectJournal:
    def __init__(self, database: Database, *, clock=time.time) -> None:
        self.db = database
        self.clock = clock

    # --- начало эффекта -----------------------------------------------------

    def begin(
        self,
        *,
        run_id: str,
        tool_use_id: str,
        kind: str,
        approval_id: str | None = None,
        payload_sha: str | None = None,
        now: float | None = None,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> tuple[Effect, bool]:
        """Взять эффект в исполнение.

        Возвращает `(эффект, свежий)`. `свежий=False` — эффект уже был: либо
        завершён (исполнитель звать не нужно, результат готов), либо повисший
        после падения, и тогда его судьбу решает стратегия вида, а не этот код.
        """
        now = self.clock() if now is None else now
        with self.db.write() as connection:
            row = connection.execute(
                "SELECT * FROM effects WHERE run_id = ? AND tool_use_id = ?",
                (run_id, tool_use_id),
            ).fetchone()
            if row is not None:
                existing = Effect.from_row(row)
                if existing.settled:
                    return existing, False
                if (existing.lease_until or 0) > now:
                    raise EffectInFlight(existing)
                # Аренда истекла: строку возвращаем как есть — перезапускать
                # наружный вызов здесь мы не вправе.
                return existing, False
            effect_id = record_attempt(
                connection,
                run_id=run_id, tool_use_id=tool_use_id, kind=kind,
                approval_id=approval_id, payload_sha=payload_sha,
                now=now, lease_seconds=lease_seconds,
            )
        effect = self.get(effect_id)
        assert effect is not None
        return effect, True

    # --- исход --------------------------------------------------------------

    def complete(self, effect_id: str, result: str, *, now: float | None = None) -> None:
        self._settle(effect_id, STATUS_DONE, result=result, now=now)

    def fail(self, effect_id: str, error: str, *, now: float | None = None) -> None:
        """Эффект точно не произошёл: канал отказал явно."""
        self._settle(effect_id, STATUS_FAILED, error=error, now=now)

    def outcome_unknown(self, effect_id: str, error: str, *, now: float | None = None) -> None:
        """Связь оборвалась. Могло произойти. Повторять запрещено."""
        self._settle(effect_id, STATUS_OUTCOME_UNKNOWN, error=error, now=now)

    def _settle(
        self, effect_id: str, status: str, *,
        result: str | None = None, error: str | None = None, now: float | None = None,
    ) -> None:
        now = self.clock() if now is None else now
        with self.db.write() as connection:
            connection.execute(
                "UPDATE effects SET status = ?, result = ?, error = ?, lease_until = NULL,"
                " updated_at = ? WHERE id = ?",
                (status, result, error, now, effect_id),
            )

    def extend_lease(
        self, effect_id: str, *, seconds: float = DEFAULT_LEASE_SECONDS,
        now: float | None = None,
    ) -> None:
        now = self.clock() if now is None else now
        with self.db.write() as connection:
            connection.execute(
                "UPDATE effects SET lease_until = ?, attempts = attempts + 1, updated_at = ?"
                " WHERE id = ?",
                (now + seconds, now, effect_id),
            )

    # --- чтение -------------------------------------------------------------

    def get(self, effect_id: str) -> Effect | None:
        row = self.db.query_one("SELECT * FROM effects WHERE id = ?", (effect_id,))
        return Effect.from_row(row) if row else None

    def find(self, *, run_id: str, tool_use_id: str) -> Effect | None:
        row = self.db.query_one(
            "SELECT * FROM effects WHERE run_id = ? AND tool_use_id = ?",
            (run_id, tool_use_id),
        )
        return Effect.from_row(row) if row else None

    def for_run(self, run_id: str) -> list[Effect]:
        rows = self.db.query(
            "SELECT * FROM effects WHERE run_id = ? ORDER BY created_at, id", (run_id,)
        )
        return [Effect.from_row(row) for row in rows]

    def has_effects(self, run_id: str) -> bool:
        """Были ли в этом ходе эффекты. От этого зависит право на retry хода."""
        return bool(
            self.db.query_one("SELECT id FROM effects WHERE run_id = ? LIMIT 1", (run_id,))
        )

    def stale(self, *, now: float | None = None, limit: int = 50) -> list[Effect]:
        """Повисшие эффекты: аренда истекла, исход неизвестен."""
        now = self.clock() if now is None else now
        rows = self.db.query(
            "SELECT * FROM effects WHERE status = ? AND (lease_until IS NULL OR lease_until <= ?)"
            " ORDER BY created_at LIMIT ?",
            (STATUS_PENDING, now, limit),
        )
        return [Effect.from_row(row) for row in rows]

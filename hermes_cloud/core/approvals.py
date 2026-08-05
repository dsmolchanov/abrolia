"""Staged-подтверждения: ничего наружу без явного «да» от человека.

Порт донорского фасада `claim_pending(code, chat, thread, actor)` на
транзакционный слой. Инварианты донора сохранены дословно, потому что каждый
из них закрывает конкретную атаку (`docs/SECURITY.md`, T2/T5):

* **Код одноразовый и хранится хэшем.** Утечка базы не даёт подтвердить чужое
  действие; повторное использование кода не проходит.
* **Точная привязка chat + thread + actor.** Подтверждение действует только в
  том же чате и треде, где предложение появилось, и только от того, кому это
  разрешено. Код, подсмотренный в соседнем чате, бесполезен.
* **Rate-limit неверных кодов per (actor, chat).** 64-битный код всё равно
  перебирается, если попытки бесплатны.
* **`payload_sha` проверяется при подтверждении.** Человек подтверждает
  конкретный payload; подменённый после показа карточки — не исполняется.
* **TTL.** Забытое предложение истекает, а не ждёт вечно.

К донорским добавлен один инвариант Фазы 2: **claim и запись попытки в журнал
эффектов — одна транзакция**. Подтверждение без записи о нём означало бы, что
после падения нельзя понять, начинали ли исполнять.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any

from hermes_cloud.core.db import Database, new_id
from hermes_cloud.core.effects import APPROVAL_TOOL_USE_ID, record_attempt

CODE_BYTES = 8  # 16 hex-символов = 64 бита
CODE_RE = re.compile(r"^[0-9a-f]{16}$")
TTL_SECONDS = 15 * 60

# Окно и порог из донора: 5 неверных кодов за 10 минут закрывают попытки.
FAILED_WINDOW_SECONDS = 600
FAILED_MAX = 5

STATUS_STAGED = "staged"
STATUS_CLAIMED = "claimed"
STATUS_EXECUTING = "executing"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_EXPIRED = "expired"


class ApprovalError(RuntimeError):
    """Базовая ошибка контура подтверждений."""


class RateLimited(ApprovalError):
    """Слишком много неверных кодов от этого актора в этом чате."""


class PayloadTampered(ApprovalError):
    """Payload изменился после того, как человек увидел карточку."""


@dataclass(frozen=True)
class Staged:
    """Результат постановки предложения: id и код, показываемый один раз."""

    id: str
    code: str
    expires_at: float


@dataclass(frozen=True)
class Approval:
    id: str
    event_id: str | None
    kind: str
    payload: dict[str, Any]
    chat: str
    thread: int | None
    context_key: str | None
    actor: str
    claimed_by: str | None
    status: str
    created_at: float
    expires_at: float
    last_error: str | None = None
    receipt: str | None = None

    @classmethod
    def from_row(cls, row: Any) -> Approval:
        return cls(
            id=row["id"],
            event_id=row["event_id"],
            kind=row["kind"],
            payload=json.loads(row["payload"]),
            chat=row["chat"],
            thread=row["thread"],
            context_key=row["context_key"],
            actor=row["actor"],
            claimed_by=row["claimed_by"],
            status=row["status"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            last_error=row["last_error"],
            receipt=row["receipt"],
        )


def payload_sha(payload: dict[str, Any]) -> str:
    """Хэш payload'а: детерминированная сериализация, иначе хэш «плавает»."""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ApprovalStore:
    def __init__(self, database: Database, *, clock=time.time) -> None:
        self.db = database
        self.clock = clock

    # --- постановка ---------------------------------------------------------

    def stage(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        chat: str,
        actor: str,
        thread: int | None = None,
        context_key: str | None = None,
        event_id: str | None = None,
        ttl_seconds: float = TTL_SECONDS,
        now: float | None = None,
    ) -> Staged:
        """Поставить предложение и вернуть одноразовый код (показывается один раз)."""
        now = self.clock() if now is None else now
        code = secrets.token_hex(CODE_BYTES)
        approval_id = new_id()
        expires_at = now + ttl_seconds
        with self.db.write() as connection:
            connection.execute(
                "INSERT INTO approvals (id, event_id, kind, payload, payload_sha,"
                " code_sha, chat, thread, context_key, actor, status, created_at,"
                " expires_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    approval_id, event_id, kind,
                    json.dumps(payload, ensure_ascii=False), payload_sha(payload),
                    _sha(code), str(chat), thread, context_key, str(actor),
                    STATUS_STAGED, now, expires_at, now,
                ),
            )
        return Staged(id=approval_id, code=code, expires_at=expires_at)

    # --- подтверждение ------------------------------------------------------

    def claim(
        self,
        *,
        code: str,
        chat: str,
        thread: int | None,
        actor: str,
        now: float | None = None,
    ) -> Approval | None:
        """Подтвердить предложение кодом. None — код не подошёл.

        Возврат None намеренно не различает «нет такого кода», «чужой чат» и
        «истёк»: разница подсказывала бы перебирающему, насколько он близко.
        """
        now = self.clock() if now is None else now
        code = code.strip().lower()
        self._guard_rate_limit(actor=actor, chat=chat, now=now)
        if not CODE_RE.match(code):
            self._record_failure(actor=actor, chat=chat, now=now)
            return None
        return self._claim_where(
            "code_sha = ?", (_sha(code),), chat=chat, thread=thread, actor=actor, now=now
        )

    def claim_by_id(
        self,
        *,
        approval_id: str,
        chat: str,
        thread: int | None,
        actor: str,
        now: float | None = None,
    ) -> Approval | None:
        """Подтверждение кнопкой: callback несёт id, а не код.

        Кнопка приходит из того же чата, что и карточка, поэтому привязка
        chat+thread+actor проверяется так же строго, как для кода.
        """
        now = self.clock() if now is None else now
        self._guard_rate_limit(actor=actor, chat=chat, now=now)
        return self._claim_where(
            "id = ?", (approval_id,), chat=chat, thread=thread, actor=actor, now=now
        )

    def _claim_where(
        self,
        predicate: str,
        params: tuple[Any, ...],
        *,
        chat: str,
        thread: int | None,
        actor: str,
        now: float,
    ) -> Approval | None:
        with self.db.write() as connection:
            row = connection.execute(
                f"SELECT * FROM approvals WHERE {predicate} AND status = ?"
                "   AND chat = ? AND (thread IS ? OR thread = ?)",
                (*params, STATUS_STAGED, str(chat), thread, thread),
            ).fetchone()
            if row is None:
                self._record_failure(actor=actor, chat=chat, now=now, connection=connection)
                return None
            if row["expires_at"] <= now:
                connection.execute(
                    "UPDATE approvals SET status = ?, updated_at = ? WHERE id = ?",
                    (STATUS_EXPIRED, now, row["id"]),
                )
                return None
            # Payload мог быть изменён в базе после показа карточки — тогда
            # человек подтверждал не то, что исполнится.
            if payload_sha(json.loads(row["payload"])) != row["payload_sha"]:
                connection.execute(
                    "UPDATE approvals SET status = ?, last_error = ?, updated_at = ?"
                    " WHERE id = ?",
                    (STATUS_FAILED, "payload изменился после подтверждения", now, row["id"]),
                )
                raise PayloadTampered(row["id"])
            connection.execute(
                "UPDATE approvals SET status = ?, claimed_by = ?, updated_at = ?"
                " WHERE id = ?",
                (STATUS_CLAIMED, str(actor), now, row["id"]),
            )
            # Попытка записывается здесь же, а не перед вызовом исполнителя:
            # подтверждение и запись о нём — один атомарный факт. Падение сразу
            # после коммита оставит `pending`, который разберёт реконсиляция.
            record_attempt(
                connection,
                run_id=row["id"],
                tool_use_id=APPROVAL_TOOL_USE_ID,
                kind=row["kind"],
                approval_id=row["id"],
                payload_sha=row["payload_sha"],
                now=now,
            )
        claimed = self.get(row["id"])
        assert claimed is not None
        return claimed

    # --- жизненный цикл -----------------------------------------------------

    def mark(
        self,
        approval_id: str,
        status: str,
        *,
        receipt: str | None = None,
        error: str | None = None,
        now: float | None = None,
    ) -> None:
        now = self.clock() if now is None else now
        with self.db.write() as connection:
            connection.execute(
                "UPDATE approvals SET status = ?, receipt = COALESCE(?, receipt),"
                " last_error = ?, updated_at = ? WHERE id = ?",
                (status, receipt, error, now, approval_id),
            )

    def cancel(self, approval_id: str, *, now: float | None = None) -> bool:
        """Отмена (кнопка ✏️/❌): код инвалидируется, новое предложение = новый код."""
        now = self.clock() if now is None else now
        with self.db.write() as connection:
            changed = connection.execute(
                "UPDATE approvals SET status = ?, updated_at = ?"
                " WHERE id = ? AND status = ?",
                (STATUS_CANCELLED, now, approval_id, STATUS_STAGED),
            ).rowcount
        return bool(changed)

    def pending_for(
        self, chat: str, thread: int | None = None, *, now: float | None = None
    ) -> list[Approval]:
        now = self.clock() if now is None else now
        rows = self.db.query(
            "SELECT * FROM approvals WHERE chat = ? AND (thread IS ? OR thread = ?)"
            "  AND status = ? AND expires_at > ? ORDER BY created_at",
            (str(chat), thread, thread, STATUS_STAGED, now),
        )
        return [Approval.from_row(row) for row in rows]

    def get(self, approval_id: str) -> Approval | None:
        row = self.db.query_one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        return Approval.from_row(row) if row else None

    def expire_stale(self, *, now: float | None = None) -> int:
        now = self.clock() if now is None else now
        with self.db.write() as connection:
            return connection.execute(
                "UPDATE approvals SET status = ?, updated_at = ?"
                " WHERE status = ? AND expires_at <= ?",
                (STATUS_EXPIRED, now, STATUS_STAGED, now),
            ).rowcount

    # --- rate limit ---------------------------------------------------------

    def failed_attempts(self, *, actor: str, chat: str, now: float) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM approval_attempts"
            " WHERE actor = ? AND chat = ? AND attempted_at > ?",
            (str(actor), str(chat), now - FAILED_WINDOW_SECONDS),
        )
        return int(row["n"]) if row else 0

    def _guard_rate_limit(self, *, actor: str, chat: str, now: float) -> None:
        if self.failed_attempts(actor=actor, chat=chat, now=now) >= FAILED_MAX:
            raise RateLimited(
                f"слишком много неверных кодов от {actor} в чате {chat};"
                f" попытки закрыты до конца окна ({FAILED_WINDOW_SECONDS} с)"
            )

    def _record_failure(
        self, *, actor: str, chat: str, now: float, connection: Any | None = None
    ) -> None:
        statement = (
            "INSERT INTO approval_attempts (id, actor, chat, attempted_at)"
            " VALUES (?, ?, ?, ?)"
        )
        params = (new_id(), str(actor), str(chat), now)
        if connection is not None:
            connection.execute(statement, params)
            return
        with self.db.write() as own:
            own.execute(statement, params)

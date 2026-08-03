"""Экспорт и удаление данных household'а — права субъекта, исполнимые кодом.

Обе операции необратимы по-разному: экспорт выносит наружу всё, что у нас есть,
удаление уничтожает это же безвозвратно. Поэтому обе доступны только владельцу
и обе проходят через подтверждение кодом — тот же контур, что и у действий
(`docs/privacy/data-map.md`, р. 4).

**Полнота экспорта проверяется тестом, а не обещанием.** Каждый класс данных с
пометкой E в data map обязан появиться в дампе; новая таблица без строки в
экспорте роняет `tests/test_dsar.py`. Забыть добавить сюда таблицу нельзя —
можно только осознанно объявить её неэкспортируемой.

**Удаление в приложении — не всё удаление.** Внешние поверхности (Nerve, Resend,
Google, Telegram, Evolution, бэкапы, Anthropic) закрываются по
`docs/privacy/delete-runbook.md`, и дамп об этом честно говорит: то, что уже
ушло в чат семьи, мы удалить не можем.
"""

from __future__ import annotations

import json
import time
from typing import Any

from hermes_cloud.core.db import Database

# Таблицы, попадающие в экспорт, и колонки, которые из них берутся.
# `raw` письма выгружается как есть — это данные семьи; ключи и хэши кодов не
# выгружаются никогда (их нет ни в одном списке ниже).
EXPORTED: dict[str, tuple[str, ...]] = {
    "events": ("id", "source", "external_id", "context_key", "received_at", "status", "raw"),
    "approvals": (
        "id", "event_id", "kind", "payload", "chat", "thread", "actor", "claimed_by",
        "status", "created_at", "expires_at", "receipt", "last_error",
    ),
    "effects": (
        "id", "run_id", "tool_use_id", "kind", "approval_id", "status", "result",
        "error", "attempts", "created_at", "updated_at",
    ),
    "reminders": (
        "id", "approval_id", "chat", "thread", "text", "due_at", "status",
        "attempts", "created_at", "updated_at",
    ),
    "extraction_runs": (
        "id", "event_id", "model", "prompt_sha", "input_tokens", "output_tokens", "created_at",
    ),
    "evidence_refs": (
        "id", "extraction_run_id", "event_id", "sender_domain", "message_date",
        "content_sha", "span_start", "span_end", "created_at",
    ),
    "commitments": (
        "id", "extraction_run_id", "kind", "payload", "status", "supersedes",
        "confidence", "observed_at", "recorded_at", "approval_id", "reminder_id",
        "calendar_event_id", "updated_at",
    ),
    "memory_statements": (
        "id", "kind", "subject", "text", "status", "supersedes", "extraction_run_id",
        "actor", "approval_id", "observed_at", "recorded_at", "valid_from",
        "valid_to", "updated_at",
    ),
    "jobs": ("id", "event_id", "kind", "payload", "run_at", "status", "created_at"),
}

# Таблицы, которых в экспорте нет — и почему. Список существует, чтобы
# «забыли» отличалось от «решили не выгружать».
NOT_EXPORTED = {
    "schema_migrations": "служебная: версии схемы, не данные семьи",
    "channel_state": "служебная: курсор апдейтов канала",
    "approval_attempts": "счётчик неверных кодов; содержимого не несёт",
}

# То, что нельзя удалить у себя, потому что оно не у нас.
EXTERNAL_SURFACES = (
    "Telegram: копии карточек и сообщений в чате семьи и у её собеседников",
    "Google Calendar: события, созданные ассистентом (удаляются в календаре семьи)",
    "Nerve/Resend: письма и метаданные доставки — по срокам провайдера",
    "Anthropic: промпты и ответы — по коммерческим условиям, без обучения",
    "Бэкапы: исчезают в пределах окна 30 дней (см. delete-runbook)",
)


def export_household(database: Database, *, now: float | None = None) -> dict[str, Any]:
    """Собрать полный дамп данных household'а."""
    now = time.time() if now is None else now
    tables: dict[str, list[dict[str, Any]]] = {}
    for table, columns in EXPORTED.items():
        rows = database.query(f"SELECT {', '.join(columns)} FROM {table}")
        tables[table] = [
            {
                column: (
                    value.decode("utf-8", "replace") if isinstance(value, bytes) else value
                )
                for column, value in zip(columns, row, strict=True)
            }
            for row in rows
        ]
    return {
        "exported_at": now,
        "tables": tables,
        "not_exported": NOT_EXPORTED,
        "outside_our_control": list(EXTERNAL_SURFACES),
    }


def export_bytes(database: Database, *, now: float | None = None) -> bytes:
    """Дамп в виде файла: его отдают человеку, а не показывают в чате."""
    return json.dumps(
        export_household(database, now=now), ensure_ascii=False, indent=2, default=str
    ).encode("utf-8")


def wipe_household(database: Database, *, now: float | None = None) -> dict[str, int]:
    """Стереть данные household'а в приложении. Необратимо.

    Порядок — от ссылающихся к тем, на кого ссылаются: не ради FK (они
    `SET NULL`), а чтобы промежуточное состояние не выглядело осмысленным, если
    процесс упадёт посреди удаления.
    """
    now = time.time() if now is None else now
    order = (
        "evidence_refs", "extraction_runs", "effects", "commitments",
        "memory_statements", "reminders", "approval_attempts", "approvals",
        "jobs", "events", "channel_state",
    )
    removed: dict[str, int] = {}
    with database.write() as connection:
        for table in order:
            removed[table] = connection.execute(f"DELETE FROM {table}").rowcount
        # Надгробие: household удалён, и отложенный вебхук не должен его
        # воскресить молчаливой вставкой события.
        connection.execute(
            "INSERT INTO channel_state (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at",
            ("household_deleted_at", str(now), now),
        )
    return removed


def is_deleted(database: Database) -> bool:
    """Стёрт ли household. Проверяется до приёма нового события."""
    row = database.query_one(
        "SELECT value FROM channel_state WHERE key = ?", ("household_deleted_at",)
    )
    return row is not None

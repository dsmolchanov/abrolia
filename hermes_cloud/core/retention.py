"""Retention-джоба: сроки хранения из data map, исполняемые кодом.

Матрица живёт в `docs/privacy/data-map.md`, и расхождение фактического TTL с
таблицей считается дефектом, а не «настройкой». Поэтому сроки здесь — именованные
константы с указанием строки матрицы, а не числа внутри запросов.

Два решения, которые стоит объяснить.

**Содержимое и метаданные стираются раздельно.** У неудавшегося события payload
живёт 30 дней (столько же, сколько у обычного сырья: неудача обработки не
продлевает срок хранения чужого письма), а метаданные отказа — 90, потому что по
ним разбирают хронические поломки. Без раздельного хранения эти сроки нельзя
было бы развести — это требование к схеме, а не к джобе.

**Память не удаляется по сроку, а выносится на пересмотр.** Удалять то, что
семья просила запомнить, по таймеру — значит тихо терять важное. Раз в 90 дней
джоба показывает, что залежалось, и решает человек. Исключение — кандидаты,
которые никто не подтвердил: несостоявшееся предложение не память.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from hermes_cloud.core.commitments import STATUS_CANDIDATE
from hermes_cloud.core.db import Database

DAY = 86_400.0

# Сроки — из retention-матрицы data map (сводка Gate −1, п. 5).
RAW_EVENT_DAYS = 30          # сырое входящее: .eml, webhook-payload
DLQ_METADATA_DAYS = 90       # метаданные отказа — без содержимого
# Транскрипты диалога пока не хранятся вовсе (ход модели не персистится), так
# что стирать нечего. Срок объявлен здесь, чтобы при появлении таблицы он не
# изобретался заново.
TRANSCRIPT_DAYS = 180
ACTIONS_DAYS = 365           # журнал действий: эффекты, подтверждения, прогоны
REMINDER_DONE_DAYS = 90      # выполненные напоминания
MEMORY_REVIEW_DAYS = 90      # не удаление, а пересмотр
CANDIDATE_DAYS = 30          # неподтверждённые гипотезы и записи памяти

TERMINAL_REMINDERS = ("done", "cancelled")


@dataclass
class RetentionReport:
    """Что джоба сделала. Числа идут в лог и в отчёт оператора."""

    raw_blanked: int = 0
    events_deleted: int = 0
    reminders_deleted: int = 0
    approvals_deleted: int = 0
    effects_deleted: int = 0
    extraction_runs_deleted: int = 0
    candidates_deleted: int = 0
    memory_candidates_deleted: int = 0
    memory_due_for_review: list[str] = field(default_factory=list)

    @property
    def total_deleted(self) -> int:
        return (
            self.events_deleted + self.reminders_deleted + self.approvals_deleted
            + self.effects_deleted + self.extraction_runs_deleted
            + self.candidates_deleted + self.memory_candidates_deleted
        )


class RetentionJob:
    """Ежедневная уборка по матрице. Идемпотентна: повтор ничего не ломает."""

    def __init__(self, database: Database, *, clock=time.time) -> None:
        self.db = database
        self.clock = clock

    def run(self, *, now: float | None = None) -> RetentionReport:
        now = self.clock() if now is None else now
        report = RetentionReport()
        with self.db.write() as connection:
            # 1. Содержимое писем. Строка события остаётся — на неё ссылается
            # провенанс, который обязан пережить удаление контента.
            report.raw_blanked = connection.execute(
                "UPDATE events SET raw = ?, updated_at = ?"
                " WHERE received_at <= ? AND length(raw) > 0",
                (b"", now, now - RAW_EVENT_DAYS * DAY),
            ).rowcount

            # 2. Метаданные события — часть журнала действий.
            report.events_deleted = connection.execute(
                "DELETE FROM events WHERE received_at <= ?",
                (now - ACTIONS_DAYS * DAY,),
            ).rowcount

            # 3. Выполненные напоминания: done + 90 дней.
            placeholders = ", ".join("?" * len(TERMINAL_REMINDERS))
            report.reminders_deleted = connection.execute(
                f"DELETE FROM reminders WHERE status IN ({placeholders}) AND updated_at <= ?",
                (*TERMINAL_REMINDERS, now - REMINDER_DONE_DAYS * DAY),
            ).rowcount

            # 4. Журнал действий: сначала эффекты, потом подтверждения —
            # обратный порядок оставил бы эффекты без связи.
            report.effects_deleted = connection.execute(
                "DELETE FROM effects WHERE created_at <= ?",
                (now - ACTIONS_DAYS * DAY,),
            ).rowcount
            # Статус подтверждения здесь не спрашивается намеренно: TTL кода —
            # 15 минут, поэтому годовалое подтверждение не бывает «ещё живым»,
            # в каком бы состоянии оно ни застряло. Фильтр по статусу оставил бы
            # зависшие `claimed` навсегда.
            report.approvals_deleted = connection.execute(
                "DELETE FROM approvals WHERE updated_at <= ?",
                (now - ACTIONS_DAYS * DAY,),
            ).rowcount
            connection.execute(
                "DELETE FROM approval_attempts WHERE attempted_at <= ?",
                (now - DLQ_METADATA_DAYS * DAY,),
            )

            # 5. Прогоны извлечения; ссылки на источник уезжают каскадом.
            report.extraction_runs_deleted = connection.execute(
                "DELETE FROM extraction_runs WHERE created_at <= ?",
                (now - ACTIONS_DAYS * DAY,),
            ).rowcount

            # 6. Несостоявшиеся предложения: гипотеза, которую никто не
            # подтвердил за месяц, — не факт и не память.
            report.candidates_deleted = connection.execute(
                "DELETE FROM commitments WHERE status = ? AND recorded_at <= ?",
                (STATUS_CANDIDATE, now - CANDIDATE_DAYS * DAY),
            ).rowcount
            report.memory_candidates_deleted = connection.execute(
                "DELETE FROM memory_statements WHERE status = ? AND recorded_at <= ?",
                (STATUS_CANDIDATE, now - CANDIDATE_DAYS * DAY),
            ).rowcount

        report.memory_due_for_review = self.memory_due_for_review(now=now)
        return report

    def memory_due_for_review(self, *, now: float | None = None, limit: int = 50) -> list[str]:
        """Записи памяти, которые пора пересмотреть. Удаляет человек, не джоба."""
        now = self.clock() if now is None else now
        rows = self.db.query(
            "SELECT id FROM memory_statements WHERE status = 'confirmed'"
            " AND updated_at <= ? ORDER BY updated_at LIMIT ?",
            (now - MEMORY_REVIEW_DAYS * DAY, limit),
        )
        return [row["id"] for row in rows]

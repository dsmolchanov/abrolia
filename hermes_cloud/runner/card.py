"""Карточка-предложение: единственное, что человек видит перед подтверждением.

Правила рендера заданы планом (Фаза 1, п. 5) и одним соображением: человек
подтверждает не «действие вообще», а конкретные дату, сумму и получателя —
значит, они должны быть видны буквально, а не пересказаны.

`confidence` влияет **только** на рендер: низкая уверенность добавляет строку
«проверьте данные», но никогда не отменяет подтверждение и не запускает
исполнение (Locked Decisions → «Модели»).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from hermes_cloud.runner.extraction import ExtractionResult

# Ниже этого порога карточка просит перепроверить извлечённое.
LOW_CONFIDENCE = 0.7

ACTION_CONFIRM = "confirm"
ACTION_EDIT = "edit"
ACTION_REJECT = "reject"

KIND_REMINDER = "reminder"
KIND_ICS = "ics"
# Запись в память — тоже предложение с кнопками: попасть в память иначе как
# через человека нельзя (`core/memory.py`).
KIND_MEMORY = "memory"


@dataclass(frozen=True)
class Button:
    label: str
    action: str
    approval_id: str

    @property
    def callback_data(self) -> str:
        """Данные кнопки: действие и id, но никогда не код подтверждения."""
        return f"{self.action}:{self.approval_id}"


@dataclass(frozen=True)
class Card:
    text: str
    buttons: tuple[Button, ...] = ()
    proposal: dict[str, Any] | None = None

    @property
    def actionable(self) -> bool:
        return bool(self.buttons)


def _format_money(amount_cents: int, currency: str) -> str:
    return f"{amount_cents / 100:.2f} {currency.upper()}".replace(".", ",")


def _format_date(value: date | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y %H:%M")
    return value.strftime("%d.%m.%Y")


def proposal_for(result: ExtractionResult) -> dict[str, Any] | None:
    """Что именно предлагается сделать. None — предлагать нечего.

    В Фазе 1 доступны два исполнителя: напоминание и файл-приглашение (ICS).
    Событие с известным началом → ICS; всё остальное, что требует действия, →
    напоминание к дедлайну.
    """
    if result.kind in {"info", "spam"} or not result.action_required:
        return None
    if result.kind == "event" and result.event_start is not None:
        return {
            "kind": KIND_ICS,
            "title": result.title,
            "start": result.event_start.isoformat(),
            "end": result.event_end.isoformat() if result.event_end else None,
            "location": result.location,
            "description": result.summary,
        }
    due = result.due_date or (result.event_start.date() if result.event_start else None)
    if due is None:
        return None
    return {
        "kind": KIND_REMINDER,
        "text": result.title,
        "due_date": due.isoformat(),
        "amount_cents": result.amount.amount_cents if result.amount else None,
        "currency": result.amount.currency if result.amount else None,
        # Ответственный едет в payload не для карточки, а для вопросов вида
        # «какие платежи на этой неделе и кто их закрывает» (`core/queries.py`).
        "responsible": result.responsible,
    }


def render_card(
    result: ExtractionResult,
    *,
    approval_id: str | None = None,
    code: str | None = None,
    source: str | None = None,
) -> Card:
    """Собрать карточку. Без `approval_id` карточка получается без кнопок.

    `source` — строка происхождения из `core/evidence.py`: цитата из живого
    письма, а после его удаления по сроку хранения — честный след без контента.
    """
    lines: list[str] = [result.title]
    if result.summary:
        lines.append("")
        lines.append(result.summary)

    facts: list[str] = []
    if result.amount is not None:
        facts.append(f"Сумма: {_format_money(result.amount.amount_cents, result.amount.currency)}")
    when = _format_date(result.event_start)
    if when:
        facts.append(f"Когда: {when}")
    deadline = _format_date(result.due_date)
    if deadline:
        facts.append(f"Срок: {deadline}")
    if result.location:
        facts.append(f"Где: {result.location}")
    if result.responsible:
        facts.append(f"Кто: {result.responsible}")
    if result.original_sender is not None:
        # Отправитель показывается отдельной строкой намеренно: в Фазе 4
        # именно он станет получателем исходящего письма.
        sender = result.original_sender.email
        if result.original_sender.name:
            sender = f"{result.original_sender.name} <{sender}>"
        facts.append(f"Отправитель: {sender}")
    if source:
        facts.append(source)
    if facts:
        lines.append("")
        lines.extend(facts)

    proposal = proposal_for(result)
    if proposal is None:
        if result.kind == "spam":
            lines.append("")
            lines.append("Похоже на рекламу — ничего делать не нужно.")
        elif not result.action_required:
            lines.append("")
            lines.append("Действий не требуется — это к сведению.")
        return Card(text="\n".join(lines))

    if result.confidence < LOW_CONFIDENCE:
        lines.append("")
        lines.append("⚠️ Проверьте дату и сумму: письмо распознано не уверенно.")

    lines.append("")
    lines.append(
        "Создать напоминание?" if proposal["kind"] == KIND_REMINDER
        else "Добавить в календарь?"
    )
    if code:
        lines.append(f"Код подтверждения: {code}")

    buttons: tuple[Button, ...] = ()
    if approval_id:
        buttons = (
            Button("✅ Да", ACTION_CONFIRM, approval_id),
            Button("✏️ Исправить", ACTION_EDIT, approval_id),
            Button("❌ Нет", ACTION_REJECT, approval_id),
        )
    return Card(text="\n".join(lines), buttons=buttons, proposal=proposal)

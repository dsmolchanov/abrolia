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
# Переключение пункта в связке. Несёт номер пункта третьим полем callback'а.
ACTION_TOGGLE = "toggle"

KIND_REMINDER = "reminder"
# Событие уезжает в календарь семьи, если он подключён, и файлом — если нет.
# Для человека это одна и та же карточка: разница в том, куда мы можем писать.
KIND_CALENDAR = "calendar"
KIND_ICS = "ics"
# Запись в память — тоже предложение с кнопками: попасть в память иначе как
# через человека нельзя (`core/memory.py`).
KIND_MEMORY = "memory"
# Права субъекта: выгрузка и стирание. Тот же контур подтверждения — потому что
# и то и другое необратимо (`core/dsar.py`).
KIND_EXPORT = "export"
KIND_DELETE = "delete"
# Связка: одно письмо породило несколько действий, подтверждение одно.
KIND_BUNDLE = "bundle"
# Исходящее письмо — единственное действие, которое нельзя отозвать.
KIND_EMAIL = "email"
# WhatsApp is irreversible too and therefore uses the same staged bundle path.
KIND_WHATSAPP = "whatsapp"


@dataclass(frozen=True)
class Button:
    label: str
    action: str
    approval_id: str
    argument: str | None = None

    @property
    def callback_data(self) -> str:
        """Данные кнопки: действие, id и (для связки) номер пункта.

        Кода подтверждения здесь нет и быть не может: callback виден всем, кто
        может прочитать разметку сообщения.
        """
        data = f"{self.action}:{self.approval_id}"
        return f"{data}:{self.argument}" if self.argument is not None else data


@dataclass(frozen=True)
class Card:
    text: str
    buttons: tuple[Button, ...] = ()
    proposal: dict[str, Any] | None = None

    @property
    def actionable(self) -> bool:
        return bool(self.buttons)


def format_money(amount_cents: int, currency: str) -> str:
    return f"{amount_cents / 100:.2f} {currency.upper()}".replace(".", ",")


def format_date(value: date | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y %H:%M")
    return value.strftime("%d.%m.%Y")


def header_lines(result: ExtractionResult, *, source: str | None = None) -> list[str]:
    """Шапка карточки: заголовок, пересказ и факты, которые человек проверяет.

    Вынесена отдельно, потому что связка из нескольких действий (`runner/bundle.py`)
    показывает ту же шапку — письмо-то одно.
    """
    lines: list[str] = [result.title]
    if result.summary:
        lines.append("")
        lines.append(result.summary)

    facts: list[str] = []
    if result.amount is not None:
        facts.append(f"Сумма: {format_money(result.amount.amount_cents, result.amount.currency)}")
    when = format_date(result.event_start)
    if when:
        facts.append(f"Когда: {when}")
    deadline = format_date(result.due_date)
    if deadline:
        facts.append(f"Срок: {deadline}")
    if result.location:
        facts.append(f"Где: {result.location}")
    if result.responsible:
        facts.append(f"Кто: {result.responsible}")
    if result.original_sender is not None:
        # Отправитель показывается отдельной строкой намеренно: именно он
        # станет получателем исходящего письма.
        sender = result.original_sender.email
        if result.original_sender.name:
            sender = f"{result.original_sender.name} <{sender}>"
        facts.append(f"Отправитель: {sender}")
    if source:
        facts.append(source)
    if facts:
        lines.append("")
        lines.extend(facts)
    return lines


def render_info(result: ExtractionResult, *, source: str | None = None) -> Card:
    """Карточка письма, не требующего действий: без кнопок и без предложения.

    Всё, что действия требует, рендерит `runner/bundle.py`: даже одно действие —
    вырожденная связка, и отдельного пути для него нет специально, иначе два
    пути начнут расходиться в поведении.
    """
    lines = header_lines(result, source=source)
    if result.kind == "spam":
        lines.append("")
        lines.append("Похоже на рекламу — ничего делать не нужно.")
    elif not result.action_required:
        lines.append("")
        lines.append("Действий не требуется — это к сведению.")
    return Card(text="\n".join(lines))

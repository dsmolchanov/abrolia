"""Связка: одно письмо — несколько действий, одно подтверждение.

Школьное письмо обычно означает не одно дело, а два: прийти в назначенный день
**и** заплатить взнос до другого. Две отдельные карточки для одного письма —
это два раза одно и то же чтение и два кода; связка показывает письмо один раз,
а действия — списком.

Три правила.

**Одно подтверждение на всю связку.** Человек читает письмо один раз и решает
один раз. Каждый пункт при этом исполняется своим эффектом со своим
`tool_use_id` в общем ходе — поэтому упавший пункт не отменяет удавшиеся и
виден по-пунктно.

**Пункт можно выключить.** По умолчанию включены все; нажатие на пункт
пересобирает предложение — со **новым кодом**, потому что подтверждать теперь
предлагается другое (та же логика, что у ✏️).

**Один пункт — это тоже связка.** Вырожденный случай не выделяется в отдельный
путь: иначе два пути начнут расходиться в поведении.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from hermes_cloud.runner.card import (
    ACTION_CONFIRM,
    ACTION_EDIT,
    ACTION_REJECT,
    ACTION_TOGGLE,
    KIND_BUNDLE,
    KIND_CALENDAR,
    KIND_EMAIL,
    KIND_ICS,
    KIND_REMINDER,
    LOW_CONFIDENCE,
    Button,
    Card,
    format_date,
    format_money,
    header_lines,
)
from hermes_cloud.runner.extraction import ExtractionResult

CHECKED = "☑"
UNCHECKED = "☐"


@dataclass(frozen=True)
class Item:
    """Один пункт связки: что сделать и включён ли он."""

    payload: dict[str, Any]
    enabled: bool = True

    @property
    def kind(self) -> str:
        return str(self.payload.get("kind", ""))

    def to_json(self) -> dict[str, Any]:
        return {**self.payload, "enabled": self.enabled}

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Item:
        payload = {key: value for key, value in raw.items() if key != "enabled"}
        return cls(payload=payload, enabled=bool(raw.get("enabled", True)))


def items_for(result: ExtractionResult, *, calendar: bool = False) -> list[Item]:
    """Разложить письмо на действия. Пусто — делать нечего.

    Событие и оплата — разные дела с разными сроками, поэтому и пункта два:
    прийти 12-го и заплатить до 8-го. Слепить их в одно означало бы потерять
    один из двух сроков.
    """
    if result.kind in {"info", "spam"} or not result.action_required:
        return []

    items: list[Item] = []
    if result.event_start is not None:
        items.append(
            Item(
                payload={
                    "kind": KIND_CALENDAR if calendar else KIND_ICS,
                    "title": result.title,
                    "start": result.event_start.isoformat(),
                    "end": result.event_end.isoformat() if result.event_end else None,
                    "location": result.location,
                    "description": result.summary,
                }
            )
        )

    due = result.due_date or (result.event_start.date() if result.event_start else None)
    needs_reminder = result.amount is not None or result.kind in {"payment", "task"}
    if due is not None and (needs_reminder or not items):
        items.append(
            Item(
                payload={
                    "kind": KIND_REMINDER,
                    "text": _reminder_text(result),
                    "due_date": due.isoformat(),
                    "amount_cents": result.amount.amount_cents if result.amount else None,
                    "currency": result.amount.currency if result.amount else None,
                    "responsible": result.responsible,
                }
            )
        )
    return items


def _reminder_text(result: ExtractionResult) -> str:
    """Текст напоминания: про деньги — «оплатить», про остальное — заголовок."""
    if result.amount is None:
        return result.title
    money = format_money(result.amount.amount_cents, result.amount.currency)
    return f"Оплатить {money} — {result.title}"


def bundle_payload(items: list[Item], *, header: str) -> dict[str, Any]:
    """Payload связки. `header` хранится, чтобы пересобрать карточку при правке."""
    return {
        "kind": KIND_BUNDLE,
        "header": header,
        "items": [item.to_json() for item in items],
    }


def items_of(payload: dict[str, Any]) -> list[Item]:
    return [Item.from_json(raw) for raw in payload.get("items", [])]


def enabled_items(payload: dict[str, Any]) -> list[Item]:
    return [item for item in items_of(payload) if item.enabled]


def toggled(payload: dict[str, Any], index: int) -> dict[str, Any]:
    """Переключить пункт. Последний включённый выключить нельзя.

    Связка без единого пункта — это отказ, и для отказа есть кнопка ❌: пустое
    подтверждение не должно выглядеть как подтверждение.
    """
    items = items_of(payload)
    if not 0 <= index < len(items):
        return payload
    switching_off = items[index].enabled
    if switching_off and sum(1 for item in items if item.enabled) <= 1:
        return payload
    items[index] = Item(payload=items[index].payload, enabled=not items[index].enabled)
    return {**payload, "items": [item.to_json() for item in items]}


def reconcile_items(items: list[Item], previous: Any) -> list[Item]:
    """Превратить связку в reconcile: не «сделать заново», а «поправить».

    Календарное событие правится по тому же id (его корень — первая версия
    факта), а напоминание прежней версии отменяется при создании новой. Иначе
    у семьи останутся два напоминания об одном взносе с разными датами.
    """
    updated: list[Item] = []
    for item in items:
        payload = dict(item.payload)
        if item.kind in {KIND_CALENDAR, KIND_ICS}:
            payload["update"] = True
        elif item.kind == KIND_REMINDER and previous.reminder_id:
            payload["supersedes_reminder_id"] = previous.reminder_id
        updated.append(Item(payload=payload, enabled=item.enabled))
    return updated


def update_lines(previous: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Строки «что изменилось» — то, ради чего человек читает второе письмо."""
    from hermes_cloud.core.matching import LABELS, changes

    delta = changes(previous, current)
    if not delta:
        return ["Это уточнение к тому, что уже подтверждено — по сути ничего не меняется."]
    lines = ["Это обновление того, что уже подтверждено. Что изменилось:"]
    for field, (was, now) in delta.items():
        lines.append(f"— {LABELS.get(field, field)}: {_readable(field, was)} → {_readable(field, now)}")
    return lines


def _readable(field: str, value: Any) -> str:
    if value is None:
        return "—"
    if field == "amount_cents":
        return format_money(int(value), "")
    if field in {"start", "end"}:
        return format_date(datetime.fromisoformat(str(value))) or str(value)
    if field == "due_date":
        return format_date(date.fromisoformat(str(value))) or str(value)
    return str(value)


def item_line(item: Item) -> str:
    """Строка пункта в карточке: что именно произойдёт после ✅."""
    payload = item.payload
    kind = item.kind
    if kind in {KIND_CALENDAR, KIND_ICS}:
        when = format_date(datetime.fromisoformat(payload["start"]))
        where = f", {payload['location']}" if payload.get("location") else ""
        if payload.get("update"):
            verb = "Обновить в календаре" if kind == KIND_CALENDAR else "Новый файл события"
        else:
            verb = "В календарь" if kind == KIND_CALENDAR else "Файл события"
        return f"{verb}: {payload['title']} — {when}{where}"
    if kind == KIND_REMINDER:
        when = format_date(date.fromisoformat(payload["due_date"]))
        verb = "Перенести напоминание на" if payload.get("supersedes_reminder_id") else "Напомнить"
        return f"{verb} {when}: {payload['text']}"
    if kind == KIND_EMAIL:
        # Получатель — отдельной строкой и всегда: подтверждают не «письмо
        # вообще», а письмо этому адресу.
        sender = payload.get("from_address") or "не настроен"
        return (
            f"Письмо\n   От: {sender}\n   Кому: {payload['to']}\n"
            f"   Тема: {payload['subject']}"
        )
    return f"{kind}: {payload.get('title') or payload.get('text') or ''}"


def render_bundle(
    result: ExtractionResult,
    items: list[Item],
    *,
    approval_id: str | None = None,
    code: str | None = None,
    source: str | None = None,
    header: str | None = None,
) -> Card:
    """Карточка связки: письмо сверху, действия списком, одно подтверждение."""
    lines = header.splitlines() if header is not None else header_lines(result, source=source)
    lines = list(lines)

    if result is not None and result.confidence < LOW_CONFIDENCE:
        lines.append("")
        lines.append("⚠️ Проверьте дату и сумму: письмо распознано не уверенно.")

    lines.append("")
    lines.append("Что сделать:" if len(items) > 1 else "Что сделать?")
    for index, item in enumerate(items):
        mark = CHECKED if item.enabled else UNCHECKED
        # Номер здесь не для красоты: кнопки-переключатели подписаны номерами,
        # и без них непонятно, какая кнопка какой пункт выключает.
        number = f"{index + 1}. " if len(items) > 1 else ""
        lines.append(f"{mark} {number}{item_line(item)}")

    lines.append("")
    lines.append("Подтверждаете?")
    if code:
        lines.append(f"Код подтверждения: {code}")

    buttons: tuple[Button, ...] = ()
    if approval_id:
        toggles = tuple(
            Button(
                f"{CHECKED if item.enabled else UNCHECKED} {index + 1}",
                ACTION_TOGGLE,
                approval_id,
                argument=str(index),
            )
            for index, item in enumerate(items)
        ) if len(items) > 1 else ()
        buttons = (
            *toggles,
            Button("✅ Да", ACTION_CONFIRM, approval_id),
            Button("✏️ Исправить", ACTION_EDIT, approval_id),
            Button("❌ Нет", ACTION_REJECT, approval_id),
        )
    return Card(
        text="\n".join(lines),
        buttons=buttons,
        proposal=bundle_payload(items, header="\n".join(
            header.splitlines() if header is not None else header_lines(result, source=source)
        )),
    )

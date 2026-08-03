"""Реестр tools: что модель вообще может попросить сделать.

Права проверяются **дважды и независимо**. Реестр не показывает модели tool,
на который у актора нет прав, — модель не может попросить того, чего не видит.
И сам обработчик первым делом вызывает `context.require(...)` — потому что
однажды кто-нибудь вызовет обработчик мимо реестра (из CLI, из теста, из
нового места в конвейере), и эта вторая проверка окажется единственной.

Ни один tool здесь ничего не отправляет наружу и ничего не создаёт напрямую:
мутирующий tool ставит **предложение**, а исполняет его человек нажатием ✅
(`docs/SECURITY.md`, инвариант подтверждения). Поэтому caps мутации — это
право *предложить*, а не право *сделать*.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from hermes_cloud.core.approvals import ApprovalStore
from hermes_cloud.core.runcontext import (
    ALL_WRITE,
    READ_TASKS,
    WRITE_REMINDER,
    RunContext,
)
from hermes_cloud.execute.reminder import ReminderStore, due_timestamp


class UnknownTool(LookupError):
    """Модель попросила tool, которого нет. Не ошибка прав — ошибка имени."""


class ToolInputError(ValueError):
    """Аргументы tool'а не разобрались. Возвращается модели, не роняет ход."""


@dataclass(frozen=True)
class Services:
    """Всё, к чему обработчики имеют доступ. Больше им ничего не дано."""

    approvals: ApprovalStore
    reminders: ReminderStore


Handler = Callable[[RunContext, "Services", dict[str, Any]], Any]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    capability: str
    input_schema: dict[str, Any]
    handler: Handler

    @property
    def mutates(self) -> bool:
        return self.capability in ALL_WRITE

    def spec(self) -> dict[str, Any]:
        """Описание для Messages API."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    def __call__(
        self, context: RunContext, services: Services, arguments: dict[str, Any]
    ) -> Any:
        return self.handler(context, services, arguments)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} уже зарегистрирован")
        self._tools[tool.name] = tool
        return tool

    def tool(
        self, *, name: str, capability: str, description: str, input_schema: dict[str, Any]
    ) -> Callable[[Handler], Handler]:
        def decorate(handler: Handler) -> Handler:
            self.register(
                Tool(
                    name=name,
                    description=description,
                    capability=capability,
                    input_schema=input_schema,
                    handler=handler,
                )
            )
            return handler

        return decorate

    def __iter__(self):
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise UnknownTool(name) from None

    def available(self, context: RunContext) -> list[Tool]:
        """Tools, которые видит этот актор. Неизвестному — пустой список."""
        return [tool for tool in self._tools.values() if context.can(tool.capability)]

    def specs(self, context: RunContext) -> list[dict[str, Any]]:
        return [tool.spec() for tool in self.available(context)]

    def invoke(
        self, context: RunContext, name: str, arguments: dict[str, Any], *, services: Services
    ) -> Any:
        """Первая из двух проверок прав; вторая — внутри самого обработчика."""
        tool = self.get(name)
        context.require(tool.capability)
        return tool(context, services, arguments)


REGISTRY = ToolRegistry()


# --- чтение -------------------------------------------------------------------


@REGISTRY.tool(
    name="list_reminders",
    capability=READ_TASKS,
    description=(
        "Показать напоминания семьи, которые ещё не сработали: "
        "текст и дату. Отвечает на «что у нас на неделе», «когда платить»."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Сколько напоминаний вернуть, максимум 50.",
                "minimum": 1,
                "maximum": 50,
            }
        },
        "required": [],
    },
)
def list_reminders(
    context: RunContext, services: Services, arguments: dict[str, Any]
) -> dict[str, Any]:
    context.require(READ_TASKS)
    limit = _int(arguments.get("limit", 20), "limit", low=1, high=50)
    reminders = [
        reminder
        for reminder in services.reminders.pending(limit=limit * 4)
        # Чужой чат — чужие дела: даже у своего актора нет права читать
        # напоминания другого чата этого household'а.
        if reminder.chat == context.chat_id
    ][:limit]
    return {
        "reminders": [
            {
                "id": reminder.id,
                "text": reminder.text,
                "due_at": reminder.due_at,
            }
            for reminder in reminders
        ]
    }


@REGISTRY.tool(
    name="list_pending_proposals",
    capability=READ_TASKS,
    description=(
        "Показать предложения, ожидающие подтверждения: что именно "
        "ассистент предложил и чего ждёт. Ничего не подтверждает."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
)
def list_pending_proposals(
    context: RunContext, services: Services, arguments: dict[str, Any]
) -> dict[str, Any]:
    context.require(READ_TASKS)
    pending = services.approvals.pending_for(chat=context.chat_id, thread=context.thread_id)
    return {
        "proposals": [
            {"id": approval.id, "kind": approval.kind, "expires_at": approval.expires_at}
            for approval in pending
        ]
    }


# --- предложения --------------------------------------------------------------


@REGISTRY.tool(
    name="propose_reminder",
    capability=WRITE_REMINDER,
    description=(
        "Предложить напоминание. Ничего не создаёт: семья увидит карточку "
        "и подтвердит её сама. Дата — в формате ГГГГ-ММ-ДД."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "О чём напомнить, одной строкой."},
            "due_date": {"type": "string", "description": "Дата напоминания, ГГГГ-ММ-ДД."},
        },
        "required": ["text", "due_date"],
    },
)
def propose_reminder(
    context: RunContext, services: Services, arguments: dict[str, Any]
) -> dict[str, Any]:
    context.require(WRITE_REMINDER)
    text = _text(arguments.get("text"), "text", limit=500)
    try:
        due = date.fromisoformat(str(arguments.get("due_date", "")))
    except ValueError:
        raise ToolInputError("due_date должен быть датой в формате ГГГГ-ММ-ДД") from None
    staged = services.approvals.stage(
        kind="reminder",
        payload={
            "kind": "reminder",
            "text": text,
            "due_date": due.isoformat(),
            "due_at": due_timestamp(due),
        },
        chat=context.chat_id,
        thread=context.thread_id,
        actor=context.actor_id,
        context_key=f"chat:{context.chat_id}",
    )
    # Код одноразовый и показывается человеку в карточке — модели он не нужен
    # и в её контекст не попадает.
    return {"proposal_id": staged.id, "status": "ожидает подтверждения"}


# --- разбор аргументов --------------------------------------------------------


def _text(value: Any, field: str, *, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolInputError(f"{field} обязателен и должен быть непустой строкой")
    text = value.strip()
    if len(text) > limit:
        raise ToolInputError(f"{field} длиннее {limit} символов")
    return text


def _int(value: Any, field: str, *, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ToolInputError(f"{field} должен быть числом") from None
    return max(low, min(high, number))

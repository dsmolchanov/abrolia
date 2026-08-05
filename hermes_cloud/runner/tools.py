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
from hermes_cloud.core.commitments import CommitmentStore
from hermes_cloud.core.memory import KIND_FACT, KINDS, MemoryStore
from hermes_cloud.core.runcontext import (
    ALL_WRITE,
    READ_CALENDAR,
    READ_MEMORY,
    READ_TASKS,
    WRITE_EMAIL,
    WRITE_MEMORY,
    WRITE_REMINDER,
    RunContext,
)
from hermes_cloud.execute.gcal import Calendar
from hermes_cloud.execute.reminder import ReminderStore, due_timestamp
from hermes_cloud.runner.bundle import Item, bundle_payload
from hermes_cloud.runner.card import KIND_BUNDLE, KIND_EMAIL, KIND_MEMORY


class UnknownTool(LookupError):
    """Модель попросила tool, которого нет. Не ошибка прав — ошибка имени."""


class ToolInputError(ValueError):
    """Аргументы tool'а не разобрались. Возвращается модели, не роняет ход."""


@dataclass(frozen=True)
class Services:
    """Всё, к чему обработчики имеют доступ. Больше им ничего не дано."""

    approvals: ApprovalStore
    reminders: ReminderStore
    memory: MemoryStore | None = None
    commitments: CommitmentStore | None = None
    calendar: Calendar | None = None

    @classmethod
    def on(cls, database, *, calendar: Calendar | None = None) -> Services:
        """Собрать полный набор сторов над одной базой."""
        return cls(
            approvals=ApprovalStore(database),
            reminders=ReminderStore(database),
            memory=MemoryStore(database),
            commitments=CommitmentStore(database),
            calendar=calendar,
        )


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


@REGISTRY.tool(
    name="calendar_list_events",
    capability=READ_CALENDAR,
    description=(
        "Показать ближайшие события семейного календаря: что и когда. "
        "Ничего не меняет."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "description": "На сколько дней вперёд смотреть, максимум 60.",
                "minimum": 1,
                "maximum": 60,
            }
        },
        "required": [],
    },
)
def calendar_list_events(
    context: RunContext, services: Services, arguments: dict[str, Any]
) -> dict[str, Any]:
    context.require(READ_CALENDAR)
    if services.calendar is None:
        # Честный ответ лучше пустого списка: «событий нет» и «календарь не
        # подключён» — разные вещи, и модель обязана их различать.
        raise ToolInputError("календарь household'а не подключён")
    days = _int(arguments.get("days", 14), "days", low=1, high=60)
    return {
        "events": [
            {
                "summary": event.get("summary"),
                "start": (event.get("start") or {}).get("dateTime")
                or (event.get("start") or {}).get("date"),
                "location": event.get("location"),
            }
            for event in services.calendar.upcoming(days=days)
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


@REGISTRY.tool(
    name="memory_search",
    capability=READ_MEMORY,
    description=(
        "Найти в памяти семьи подтверждённые записи: факты, распорядок, "
        "предпочтения. Показывает только действующее — заменённое и "
        "просроченное не возвращается."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Что искать, одним-двумя словами."},
            "kind": {
                "type": "string",
                "description": "Вид записи: fact, routine или preference.",
                "enum": sorted(KINDS),
            },
        },
        "required": [],
    },
)
def memory_search(
    context: RunContext, services: Services, arguments: dict[str, Any]
) -> dict[str, Any]:
    context.require(READ_MEMORY)
    if services.memory is None:
        raise ToolInputError("память не подключена")
    kind = arguments.get("kind")
    if kind is not None and kind not in KINDS:
        raise ToolInputError(f"неизвестный вид записи: {kind!r}")
    found = services.memory.recall(query=arguments.get("query"), kind=kind)
    return {
        "statements": [
            {"id": item.id, "kind": item.kind, "subject": item.subject, "text": item.text}
            for item in found
        ]
    }


@REGISTRY.tool(
    name="memory_append",
    capability=WRITE_MEMORY,
    description=(
        "Предложить запомнить что-то о семье надолго. Ничего не запоминает "
        "сразу: семья увидит карточку и подтвердит. Указывай `supersedes`, "
        "если новая запись заменяет прежнюю."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Что запомнить, одной фразой."},
            "kind": {
                "type": "string",
                "description": "fact — факт, routine — распорядок, preference — предпочтение.",
                "enum": sorted(KINDS),
            },
            "subject": {"type": "string", "description": "О ком или о чём запись."},
            "supersedes": {
                "type": "string",
                "description": "id записи, которую эта заменяет, если такая есть.",
            },
        },
        "required": ["text"],
    },
)
def memory_append(
    context: RunContext, services: Services, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Запись в память — всегда предложение.

    Память переживает и письмо, и разговор: попадание в неё напрямую означало
    бы, что письмо, притворившееся инструкцией, влияет на все будущие ходы
    (`docs/SECURITY.md`, T1). Поэтому здесь только кандидат и карточка.
    """
    context.require(WRITE_MEMORY)
    if services.memory is None:
        raise ToolInputError("память не подключена")
    text = _text(arguments.get("text"), "text", limit=500)
    kind = arguments.get("kind") or KIND_FACT
    if kind not in KINDS:
        raise ToolInputError(f"неизвестный вид записи: {kind!r}")
    try:
        statement = services.memory.propose(
            text=text,
            kind=kind,
            subject=arguments.get("subject"),
            actor=context.actor_id,
            supersedes=arguments.get("supersedes") or None,
        )
    except Exception as error:  # ошибки инвариантов памяти — ошибки аргументов
        raise ToolInputError(str(error)) from error
    staged = services.approvals.stage(
        kind=KIND_MEMORY,
        payload={
            "kind": KIND_MEMORY,
            "statement_id": statement.id,
            "text": statement.text,
            "memory_kind": statement.kind,
        },
        chat=context.chat_id,
        thread=context.thread_id,
        actor=context.actor_id,
        context_key=f"chat:{context.chat_id}",
    )
    return {"proposal_id": staged.id, "statement_id": statement.id,
            "status": "ожидает подтверждения"}


@REGISTRY.tool(
    name="propose_email",
    capability=WRITE_EMAIL,
    description=(
        "Предложить отправить письмо. Ничего не отправляет: семья увидит "
        "карточку с получателем и текстом и подтвердит сама. Для ответа в "
        "существующую переписку укажи `in_reply_to` — Message-ID письма, "
        "на которое отвечаем."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "Адрес получателя."},
            "subject": {"type": "string", "description": "Тема письма."},
            "body": {"type": "string", "description": "Текст письма целиком."},
            "in_reply_to": {
                "type": "string",
                "description": "Message-ID письма, на которое это ответ, если это ответ.",
            },
        },
        "required": ["to", "subject", "body"],
    },
)
def propose_email(
    context: RunContext, services: Services, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Письмо — предложение, и получатель в нём подтверждается наравне с текстом.

    Проверка адреса и заголовков делается уже здесь, а не только перед
    отправкой: показать семье карточку с негодным адресом — значит попросить
    подтвердить то, что всё равно не уйдёт.
    """
    context.require(WRITE_EMAIL)
    from hermes_cloud.execute.email_send import EmailRejected, Outgoing, validate

    letter = Outgoing(
        to=str(arguments.get("to") or ""),
        subject=str(arguments.get("subject") or ""),
        body=str(arguments.get("body") or ""),
        in_reply_to=arguments.get("in_reply_to") or None,
    )
    try:
        validate(letter)
    except EmailRejected as error:
        raise ToolInputError(str(error)) from error

    payload = bundle_payload(
        [Item(payload={
            "kind": KIND_EMAIL,
            "to": letter.to,
            "subject": letter.subject,
            "body": letter.body,
            "in_reply_to": letter.in_reply_to,
        })],
        header=f"Письмо для {letter.to}",
    )
    staged = services.approvals.stage(
        kind=KIND_BUNDLE,
        payload=payload,
        chat=context.chat_id,
        thread=context.thread_id,
        actor=context.actor_id,
        context_key=f"chat:{context.chat_id}",
    )
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

"""Ручной tool-loop: права, идемпотентность, пределы и запрет слепого повтора.

Модель здесь — сценарий: ответы заданы заранее, потому что проверяется не она,
а то, что цикл делает с её просьбами.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from hermes_cloud.core.approvals import ApprovalStore
from hermes_cloud.core.db import open_database
from hermes_cloud.core.effects import EffectJournal
from hermes_cloud.core.runcontext import Household, build_run_context
from hermes_cloud.execute.reminder import ReminderStore
from hermes_cloud.runner.model import (
    STOP_BUDGET,
    STOP_COMPLETED,
    STOP_INTERRUPTED,
    STOP_ITERATIONS,
    STOP_NO_TOOLS,
    STOP_REFUSED,
    STOP_TIMEOUT,
    TEXT_INTERRUPTED,
    TEXT_NO_TOOLS,
    TEXT_NOTHING_HAPPENED,
    ToolLoop,
)
from hermes_cloud.runner.tools import Services

CHAT = "-100990000101"
PARENT = "990000002"
NANNY = "990000003"
STRANGER = "990000009"

HOUSEHOLD = Household(
    owner="990000001",
    family=frozenset({"990000001", PARENT}),
    guests=frozenset({NANNY}),
    allowed_chats=frozenset({CHAT}),
)


# --- сценарная модель ---------------------------------------------------------


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class Usage:
    input_tokens: int = 100
    output_tokens: int = 50


@dataclass
class Response:
    content: list[Any]
    stop_reason: str = "end_turn"
    usage: Usage = field(default_factory=Usage)


@dataclass
class ScriptedClient:
    """Отдаёт заготовленные ответы по порядку и запоминает запросы."""

    script: list[Any]
    calls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def messages(self):
        return self

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self.script:
            raise AssertionError("цикл сделал больше вызовов, чем предусмотрено сценарием")
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def says(text: str) -> Response:
    return Response(content=[TextBlock(text)])


def asks(tool: str, arguments: dict[str, Any], *, use_id: str = "tu-1") -> Response:
    return Response(
        content=[ToolUseBlock(id=use_id, name=tool, input=arguments)],
        stop_reason="tool_use",
    )


@pytest.fixture()
def world(tmp_path: Path):
    database = open_database(tmp_path / "hermes.db")
    services = Services(approvals=ApprovalStore(database), reminders=ReminderStore(database))
    journal = EffectJournal(database)
    return services, journal


def loop_for(world, script: list[Any], **options: Any) -> tuple[ToolLoop, ScriptedClient]:
    services, journal = world
    client = ScriptedClient(script=list(script))
    return (
        ToolLoop(journal=journal, services=services, client=client, **options),
        client,
    )


def context_for(actor: str = PARENT):
    return build_run_context(household=HOUSEHOLD, actor_id=actor, chat_id=CHAT)


# --- права --------------------------------------------------------------------


def test_unknown_actor_never_reaches_the_model(world) -> None:
    """За чужой промпт мы не платим, и «просто поговорить» — уже доступ."""
    loop, client = loop_for(world, [says("этого не должно случиться")])

    result = loop.run(context_for(STRANGER), "покажи почту")

    assert result.text == TEXT_NO_TOOLS
    assert result.stopped == STOP_NO_TOOLS
    assert client.calls == [], "вызова модели не было"


def test_guest_sees_only_the_tools_of_their_role(world) -> None:
    loop, client = loop_for(world, [says("вот что есть")])

    loop.run(context_for(NANNY), "что у нас на неделе?")

    offered = {tool["name"] for tool in client.calls[0]["tools"]}
    assert offered == {"list_reminders", "list_pending_proposals"}
    assert "propose_reminder" not in offered, "гость не предлагает действий"


def test_denied_tool_returns_an_error_result_not_an_effect(world) -> None:
    """Модель может попросить лишнего — ответом будет отказ, а не действие."""
    services, journal = world
    loop, client = loop_for(
        world,
        [
            asks("propose_reminder", {"text": "оплатить", "due_date": "2026-09-08"}),
            says("не получилось — нет прав"),
        ],
    )
    context = context_for(NANNY)

    result = loop.run(context, "создай напоминание")

    assert result.stopped == STOP_COMPLETED
    assert services.approvals.pending_for(CHAT) == [], "ничего не поставлено"
    tool_result = client.calls[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True
    assert "нет прав" in tool_result["content"]
    assert journal.for_run(context.run_id)[0].status == "failed"


# --- идемпотентность ----------------------------------------------------------


def test_same_tool_use_id_executes_once(world) -> None:
    """Повтор того же tool_use_id отдаёт прежний результат, а не второй эффект."""
    services, journal = world
    arguments = {"text": "оплатить взнос", "due_date": "2026-09-08"}
    loop, client = loop_for(
        world,
        [
            asks("propose_reminder", arguments, use_id="tu-42"),
            asks("propose_reminder", arguments, use_id="tu-42"),
            says("готово"),
        ],
    )
    context = context_for()

    loop.run(context, "напомни оплатить взнос")

    assert len(services.approvals.pending_for(CHAT)) == 1, "предложение ровно одно"
    effects = journal.for_run(context.run_id)
    assert len(effects) == 1 and effects[0].status == "done"
    second = client.calls[2]["messages"][-1]["content"][0]
    assert second["content"] == effects[0].result, "второй раз — результат из журнала"


def test_tool_effects_are_journalled_before_they_happen(world) -> None:
    services, journal = world
    loop, _ = loop_for(
        world,
        [asks("propose_reminder", {"text": "х", "due_date": "2026-09-08"}), says("готово")],
    )
    context = context_for()

    loop.run(context, "напомни")

    effect = journal.for_run(context.run_id)[0]
    assert effect.kind == "propose_reminder"
    assert json.loads(effect.result)["proposal_id"]


# --- запрет слепого повтора ---------------------------------------------------


def test_api_failure_is_retried_while_the_turn_is_still_empty(world) -> None:
    """Пока ничего не произошло, повтор безопасен — и потому разрешён."""
    loop, client = loop_for(world, [RuntimeError("сеть моргнула"), says("всё хорошо")])

    result = loop.run(context_for(), "привет")

    assert result.text == "всё хорошо"
    assert result.stopped == STOP_COMPLETED
    assert len(client.calls) == 2


def test_api_failure_after_an_effect_is_never_retried(world) -> None:
    """После первого эффекта повтор запрещён: он может сделать второй."""
    services, journal = world
    loop, client = loop_for(
        world,
        [
            asks("propose_reminder", {"text": "взнос", "due_date": "2026-09-08"}),
            RuntimeError("сеть упала"),
            says("этого шага быть не должно"),
        ],
    )
    context = context_for()

    result = loop.run(context, "напомни про взнос")

    assert result.stopped == STOP_INTERRUPTED
    assert result.text.startswith(TEXT_INTERRUPTED)
    assert "propose_reminder" in result.text
    assert len(client.calls) == 2, "повтора не было"
    assert len(services.approvals.pending_for(CHAT)) == 1, "и второго предложения тоже"


def test_interrupted_empty_turn_says_so_plainly(world) -> None:
    loop, _ = loop_for(world, [RuntimeError("раз"), RuntimeError("два")])

    result = loop.run(context_for(), "привет")

    assert result.stopped == STOP_INTERRUPTED
    assert result.text == TEXT_NOTHING_HAPPENED


# --- пределы ------------------------------------------------------------------


def test_a_looping_model_hits_the_iteration_limit(world) -> None:
    """Зациклившаяся модель упирается в предел, а не в счёт за месяц."""
    arguments = {"limit": 5}
    script = [
        asks("list_reminders", arguments, use_id=f"tu-{index}") for index in range(10)
    ]
    loop, client = loop_for(world, script, max_iterations=3)

    result = loop.run(context_for(), "покажи напоминания")

    assert result.stopped == STOP_ITERATIONS
    assert result.iterations == 3 and len(client.calls) == 3


def test_the_deadline_stops_the_turn(world) -> None:
    ticks = iter([0.0, 0.0, 10_000.0, 20_000.0])
    loop, client = loop_for(
        world,
        [asks("list_reminders", {}, use_id="tu-1"), says("не дойдёт")],
        clock=lambda: next(ticks),
        max_seconds=60,
    )

    result = loop.run(context_for(), "покажи напоминания")

    assert result.stopped == STOP_TIMEOUT
    assert len(client.calls) == 1


def test_the_token_budget_stops_the_turn(world) -> None:
    loop, client = loop_for(
        world,
        [asks("list_reminders", {}, use_id="tu-1"), says("не дойдёт")],
        token_budget=10,
    )

    result = loop.run(context_for(), "покажи напоминания")

    assert result.stopped == STOP_BUDGET
    assert result.tokens > 10


# --- ответы модели ------------------------------------------------------------


def test_refusal_is_not_a_crash(world) -> None:
    loop, _ = loop_for(world, [Response(content=[], stop_reason="refusal")])

    result = loop.run(context_for(), "сделай что-нибудь запрещённое")

    assert result.stopped == STOP_REFUSED
    assert result.text


def test_plain_answer_needs_no_tools(world) -> None:
    services, journal = world
    loop, client = loop_for(world, [says("Ближайший взнос — 8 сентября.")])
    context = context_for()

    result = loop.run(context, "когда платить?")

    assert result.text == "Ближайший взнос — 8 сентября."
    assert journal.for_run(context.run_id) == []
    assert client.calls[0]["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_crash_mid_loop_leaves_exactly_one_proposal(tmp_path: Path) -> None:
    """kill -9 между вызовами модели: эффект один, и повтор хода его не удвоит."""
    import sys as _sys

    from test_effects import CHILD, crash_in  # тот же дочерний процесс

    _sys.path.insert(0, str(CHILD.parent))
    from chaos_child import RUN_ID, TOOL_ARGUMENTS, TOOL_USE_ID

    database_path = tmp_path / "hermes.db"
    crash_in("mid_loop", database_path)

    database = open_database(database_path)
    services = Services(approvals=ApprovalStore(database), reminders=ReminderStore(database))
    journal = EffectJournal(database)
    effects = journal.for_run(RUN_ID)
    assert [effect.status for effect in effects] == ["done"], "эффект пережил падение"
    assert len(services.approvals.pending_for(CHAT)) == 1

    # Повтор того же хода: модель просит то же самое, журнал не даёт сделать дважды.
    client = ScriptedClient(script=[
        asks("propose_reminder", TOOL_ARGUMENTS, use_id=TOOL_USE_ID),
        says("уже предложено"),
    ])
    loop = ToolLoop(journal=journal, services=services, client=client)
    loop.run(
        build_run_context(
            household=HOUSEHOLD, actor_id="990000001", chat_id=CHAT, run_id=RUN_ID
        ),
        "напомни оплатить взнос",
    )

    assert len(services.approvals.pending_for(CHAT)) == 1, "второго предложения нет"
    assert len(journal.for_run(RUN_ID)) == 1


def test_a_family_message_from_the_channel_reaches_the_loop(world, tmp_path: Path) -> None:
    """Обычное сообщение в чате — ход диалога, а не молчание."""
    from hermes_cloud.channels.telegram import FakeTransport
    from hermes_cloud.runner.pipeline import Pipeline

    services, journal = world
    transport = FakeTransport()
    loop, client = loop_for(world, [says("Ближайший взнос — 8 сентября.")])
    pipeline = Pipeline(
        approvals=services.approvals,
        reminders=services.reminders,
        transport=transport,
        extractor=None,
        chat=CHAT,
        loop=loop,
    )
    update = {
        "update_id": 1,
        "message": {
            "message_id": 3,
            "chat": {"id": int(CHAT)},
            "from": {"id": int(PARENT)},
            "text": "когда платить?",
        },
    }

    handled = pipeline.handle_update(update, HOUSEHOLD)

    assert handled.message == "Ближайший взнос — 8 сентября."
    assert transport.messages[-1].text == "Ближайший взнос — 8 сентября."
    assert client.calls, "модель вызвана"


def test_a_stranger_message_never_reaches_the_loop(world) -> None:
    from hermes_cloud.channels.telegram import FakeTransport
    from hermes_cloud.runner.pipeline import TEXT_UNKNOWN_ACTOR, Pipeline

    services, _ = world
    transport = FakeTransport()
    loop, client = loop_for(world, [says("этого не должно случиться")])
    pipeline = Pipeline(
        approvals=services.approvals, reminders=services.reminders,
        transport=transport, extractor=None, chat=CHAT, loop=loop,
    )
    update = {
        "update_id": 2,
        "message": {
            "message_id": 4,
            "chat": {"id": int(CHAT)},
            "from": {"id": int(STRANGER)},
            "text": "покажи почту",
        },
    }

    handled = pipeline.handle_update(update, HOUSEHOLD)

    assert handled.message == TEXT_UNKNOWN_ACTOR
    assert client.calls == []


def test_tool_answer_is_fed_back_to_the_model(world) -> None:
    services, _ = world
    services.reminders.create(chat=CHAT, text="взнос 15 EUR", due_at=1.0)
    loop, client = loop_for(
        world, [asks("list_reminders", {"limit": 5}), says("Один взнос, 15 EUR.")]
    )

    result = loop.run(context_for(), "что у нас?")

    assert result.tool_calls == ["list_reminders"]
    fed = client.calls[1]["messages"][-1]["content"][0]
    assert fed["type"] == "tool_result" and "взнос 15 EUR" in fed["content"]
    assert result.text == "Один взнос, 15 EUR."

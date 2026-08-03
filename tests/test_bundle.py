"""Связка: одно письмо, несколько дел, одно подтверждение.

Проверяется главное: пункты исполняются по отдельности и падают по отдельности,
а выключить всё до пустоты нельзя — для «ничего не делать» есть ❌.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from hermes_cloud.channels.telegram import FakeTransport, parse_update
from hermes_cloud.core.approvals import ApprovalStore
from hermes_cloud.core.db import open_database
from hermes_cloud.core.runcontext import Household, build_run_context
from hermes_cloud.execute.gcal import Calendar, CalendarError, FakeCalendar
from hermes_cloud.execute.reminder import ReminderStore
from hermes_cloud.runner.bundle import (
    CHECKED,
    UNCHECKED,
    Item,
    bundle_payload,
    enabled_items,
    items_for,
    items_of,
    render_bundle,
    toggled,
)
from hermes_cloud.runner.card import (
    ACTION_CONFIRM,
    ACTION_TOGGLE,
    KIND_BUNDLE,
    KIND_CALENDAR,
    KIND_REMINDER,
)
from hermes_cloud.runner.extraction import ExtractionResult, Money
from hermes_cloud.runner.pipeline import Pipeline

CHAT = "-100990000101"
PARENT = "990000001"
NANNY = "990000003"

HOUSEHOLD = Household(
    owner=PARENT, family=frozenset({PARENT}), guests=frozenset({NANNY}),
    allowed_chats=frozenset({CHAT}),
)

SCHOOL_TRIP = ExtractionResult(
    kind="event",
    title="Экскурсия 3b",
    summary="Экскурсия 12 сентября, взнос 15 € до 8 сентября.",
    source_language="de",
    action_required=True,
    event_start=datetime(2026, 9, 12, 7, 45, tzinfo=UTC),
    due_date=date(2026, 9, 8),
    amount=Money(amount_cents=1500, currency="EUR"),
    location="Haupteingang",
    confidence=0.9,
)


def context(actor: str = PARENT):
    return build_run_context(household=HOUSEHOLD, actor_id=actor, chat_id=CHAT)


@pytest.fixture()
def world(tmp_path: Path):
    database = open_database(tmp_path / "hermes.db")
    backend = FakeCalendar()
    transport = FakeTransport()
    pipeline = Pipeline(
        approvals=ApprovalStore(database),
        reminders=ReminderStore(database),
        transport=transport,
        extractor=None,
        chat=CHAT,
        calendar=Calendar(backend, calendar_id="family@group.calendar.example"),
    )
    return pipeline, transport, backend


def stage_bundle(pipeline: Pipeline, items=None):
    items = items or items_for(SCHOOL_TRIP, calendar=True)
    payload = bundle_payload(items, header="Экскурсия 3b")
    return pipeline.approvals.stage(
        kind=KIND_BUNDLE, payload=payload, chat=CHAT, actor=PARENT
    ), items


# --- разбор письма ------------------------------------------------------------


def test_one_letter_two_deadlines_two_items() -> None:
    """Прийти 12-го и заплатить до 8-го — разные сроки, значит разные пункты."""
    items = items_for(SCHOOL_TRIP, calendar=True)

    assert [item.kind for item in items] == [KIND_CALENDAR, KIND_REMINDER]
    assert items[1].payload["due_date"] == "2026-09-08"
    assert "15,00 EUR" in items[1].payload["text"]
    assert all(item.enabled for item in items), "по умолчанию включены все"


def test_a_plain_payment_is_a_single_item_bundle() -> None:
    """Вырожденный случай не выделяется в отдельный путь."""
    payment = SCHOOL_TRIP.model_copy(update={"kind": "payment", "event_start": None})

    items = items_for(payment, calendar=True)

    assert [item.kind for item in items] == [KIND_REMINDER]


# --- карточка -----------------------------------------------------------------


def test_the_card_lists_every_item_with_its_own_date() -> None:
    card = render_bundle(
        SCHOOL_TRIP, items_for(SCHOOL_TRIP, calendar=True),
        approval_id="a1", code="0123456789abcdef",
    )

    assert "12.09.2026" in card.text and "08.09.2026" in card.text
    assert card.text.count(CHECKED) >= 2
    labels = [button.label for button in card.buttons]
    assert labels[:2] == [f"{CHECKED} 1", f"{CHECKED} 2"], "переключатели пунктов"
    assert labels[-3:] == ["✅ Да", "✏️ Исправить", "❌ Нет"]


def test_toggle_buttons_carry_the_item_number_and_never_the_code() -> None:
    card = render_bundle(
        SCHOOL_TRIP, items_for(SCHOOL_TRIP, calendar=True),
        approval_id="a1", code="0123456789abcdef",
    )

    toggles = [b for b in card.buttons if b.action == ACTION_TOGGLE]
    assert [b.callback_data for b in toggles] == ["toggle:a1:0", "toggle:a1:1"]
    assert all("0123456789abcdef" not in b.callback_data for b in card.buttons)


def test_a_single_item_bundle_has_no_toggles() -> None:
    """Переключать нечего: один пункт либо подтверждают, либо отклоняют."""
    payment = SCHOOL_TRIP.model_copy(update={"kind": "payment", "event_start": None})

    card = render_bundle(payment, items_for(payment), approval_id="a1")

    assert [button.action for button in card.buttons] == ["confirm", "edit", "reject"]


# --- переключение -------------------------------------------------------------


def test_toggling_switches_one_item_off() -> None:
    payload = bundle_payload(items_for(SCHOOL_TRIP, calendar=True), header="x")

    switched = toggled(payload, 0)

    assert [item.enabled for item in items_of(switched)] == [False, True]
    assert [item.kind for item in enabled_items(switched)] == [KIND_REMINDER]


def test_the_last_item_cannot_be_switched_off() -> None:
    """Пустое подтверждение не должно выглядеть как подтверждение."""
    payload = bundle_payload([Item(payload={"kind": KIND_REMINDER})], header="x")

    assert toggled(payload, 0) == payload


def test_toggling_restages_with_a_new_code(world) -> None:
    """Подтверждать предлагается другое — значит и код должен быть другим."""
    pipeline, transport, backend = world
    staged, _ = stage_bundle(pipeline)

    handled = pipeline.handle_callback(
        action=ACTION_TOGGLE, approval_id=staged.id, argument="0", context=context()
    )

    assert handled.approval_id != staged.id
    assert pipeline.approvals.get(staged.id).status == "cancelled"
    fresh = pipeline.approvals.get(handled.approval_id)
    assert [item.enabled for item in items_of(fresh.payload)] == [False, True]
    assert UNCHECKED in transport.messages[-1].text
    assert "Код подтверждения" in transport.messages[-1].text


def test_a_switched_off_item_is_not_executed(world) -> None:
    pipeline, transport, backend = world
    staged, _ = stage_bundle(pipeline)
    toggled_handled = pipeline.handle_callback(
        action=ACTION_TOGGLE, approval_id=staged.id, argument="0", context=context()
    )

    pipeline.handle_callback(
        action=ACTION_CONFIRM, approval_id=toggled_handled.approval_id, context=context()
    )

    assert backend.inserts == 0, "календарный пункт выключили — событие не создано"
    assert len(pipeline.reminders.pending()) == 1


def test_the_toggle_callback_is_parsed_with_its_index() -> None:
    update = {
        "callback_query": {
            "id": "cb1",
            "from": {"id": int(PARENT)},
            "data": "toggle:approval-42:1",
            "message": {"message_id": 5, "chat": {"id": int(CHAT)}},
        }
    }

    parsed = parse_update(update, HOUSEHOLD)

    assert parsed.action == ACTION_TOGGLE
    assert parsed.approval_id == "approval-42"
    assert parsed.argument == "1"


# --- исполнение ---------------------------------------------------------------


def test_every_item_gets_its_own_effect(world) -> None:
    pipeline, transport, backend = world
    staged, _ = stage_bundle(pipeline)

    handled = pipeline.handle_callback(
        action=ACTION_CONFIRM, approval_id=staged.id, context=context()
    )

    assert handled.executed == KIND_BUNDLE
    effects = pipeline.effects.for_run(staged.id)
    kinds = sorted(effect.kind for effect in effects)
    assert kinds == [KIND_BUNDLE, KIND_CALENDAR, KIND_REMINDER], (
        "эффект самого подтверждения плюс по одному на пункт"
    )
    assert all(effect.status == "done" for effect in effects)
    assert backend.inserts == 1 and len(pipeline.reminders.pending()) == 1


def test_a_failing_item_does_not_undo_the_others(world) -> None:
    """Две трети сделанного лучше, чем ничего, — если сказать, какая треть нет."""
    pipeline, transport, backend = world
    staged, _ = stage_bundle(pipeline)

    def explode(*_args, **_kwargs):
        raise CalendarError("insert: HTTP 403 нет доступа к календарю")

    backend.insert_event = explode  # type: ignore[method-assign]

    handled = pipeline.handle_callback(
        action=ACTION_CONFIRM, approval_id=staged.id, context=context()
    )

    assert handled.executed is None
    assert len(pipeline.reminders.pending()) == 1, "напоминание сделано"
    statuses = {
        effect.kind: effect.status for effect in pipeline.effects.for_run(staged.id)
    }
    assert statuses[KIND_REMINDER] == "done"
    assert statuses[KIND_CALENDAR] == "failed"
    assert "✓" in handled.message and "✗" in handled.message
    assert pipeline.approvals.get(staged.id).status == "failed"


def test_replaying_a_bundle_does_not_redo_finished_items(world) -> None:
    """Доделка после падения трогает только то, что не закончено."""
    pipeline, transport, backend = world
    staged, _ = stage_bundle(pipeline)
    approval = pipeline.approvals.claim_by_id(
        approval_id=staged.id, chat=CHAT, thread=None, actor=PARENT
    )

    pipeline._execute_bundle(approval, approval.payload, now=None)
    pipeline._execute_bundle(approval, approval.payload, now=None)

    assert len(pipeline.reminders.pending()) == 1, "второго напоминания нет"
    assert backend.inserts == 1, "событие создано ровно один раз"
    assert len(pipeline.effects.for_run(staged.id)) == 3


def test_a_guest_may_not_confirm_a_bundle_of_things_they_cannot_do(world) -> None:
    pipeline, transport, backend = world
    staged, _ = stage_bundle(pipeline)

    handled = pipeline.handle_callback(
        action=ACTION_CONFIRM, approval_id=staged.id, context=context(actor=NANNY)
    )

    assert handled.executed is None
    assert "нет такого права" in transport.messages[-1].text
    assert backend.inserts == 0 and pipeline.reminders.pending() == []

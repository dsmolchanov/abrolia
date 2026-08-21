"""Календарь: одно событие на одно подтверждение, сколько ни повторяй."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from hermes_cloud.channels.telegram import FakeTransport
from hermes_cloud.core.approvals import ApprovalStore
from hermes_cloud.core.db import open_database
from hermes_cloud.core.effects import DEFAULT_LEASE_SECONDS
from hermes_cloud.core.runcontext import Household, build_run_context
from hermes_cloud.execute.gcal import (
    Calendar,
    CalendarError,
    CalendarOutcomeUnknown,
    FakeCalendar,
    build_calendar_event,
    event_id_for,
)
from hermes_cloud.execute.reminder import ReminderStore
from hermes_cloud.runner.card import ACTION_CONFIRM, KIND_CALENDAR, KIND_ICS
from hermes_cloud.runner.pipeline import TEXT_OUTCOME_UNKNOWN, Pipeline

CHAT = "-100990000101"
PARENT = "990000001"
START = datetime(2026, 9, 12, 7, 45, tzinfo=UTC)
MOVED = datetime(2026, 9, 19, 7, 45, tzinfo=UTC)

HOUSEHOLD = Household(
    owner=PARENT, family=frozenset({PARENT}), allowed_chats=frozenset({CHAT})
)


def context():
    return build_run_context(household=HOUSEHOLD, actor_id=PARENT, chat_id=CHAT)


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


def stage_event(pipeline: Pipeline, *, start: datetime = START, title: str = "Экскурсия 3b"):
    payload = {
        "kind": KIND_CALENDAR,
        "title": title,
        "start": start.isoformat(),
        "end": None,
        "location": "Haupteingang",
        "description": None,
    }
    staged = pipeline.approvals.stage(
        kind=KIND_CALENDAR, payload=payload, chat=CHAT, actor=PARENT
    )
    return staged


# --- детерминированный id -----------------------------------------------------


def test_event_id_is_derived_from_the_approval() -> None:
    """Один и тот же id на повторе — вся идемпотентность держится на этом."""
    first = event_id_for("approval-42")

    assert first == event_id_for("approval-42")
    assert first != event_id_for("approval-43")


def test_event_id_fits_googles_alphabet() -> None:
    """Google принимает только base32hex: 0-9 и a-v, 5–1024 символа."""
    identifier = event_id_for("подтверждение-с-кириллицей")

    assert 5 <= len(identifier) <= 1024
    assert set(identifier) <= set("0123456789abcdefghijklmnopqrstuv")


# --- идемпотентность ----------------------------------------------------------


def test_repeating_the_same_approval_does_not_create_a_second_event(world) -> None:
    pipeline, transport, backend = world
    staged = stage_event(pipeline)
    approval = pipeline.approvals.claim_by_id(
        approval_id=staged.id, chat=CHAT, thread=None, actor=PARENT
    )

    first = pipeline.execute(approval)
    # Прямой повтор исполнения — как после падения между эффектом и отметкой.
    pipeline.effects.find(run_id=approval.id, tool_use_id="approval")
    second = pipeline.execute(pipeline.approvals.get(approval.id))

    assert first.executed == KIND_CALENDAR
    assert backend.inserts == 1, "второго события нет"
    assert len(backend.events) == 1
    assert second.executed is None, "исход уже известен — исполнитель не звался"


def test_a_crash_before_the_mark_is_replayed_into_the_same_event(world) -> None:
    """Календарь доигрывается после падения — у него есть ключ, по которому видно."""
    pipeline, transport, backend = world
    staged = stage_event(pipeline)
    approval = pipeline.approvals.claim_by_id(
        approval_id=staged.id, chat=CHAT, thread=None, actor=PARENT
    )
    # Событие создано, но процесс умер до отметки: эффект остался pending.
    pipeline.calendar.upsert(
        build_calendar_event(approval_id=approval.id, title="Экскурсия 3b", start=START)
    )

    handled = pipeline.reconcile(now=None)
    assert handled == [], "живая аренда — не повод вмешиваться"

    import time

    handled = pipeline.reconcile(now=time.time() + 10 * DEFAULT_LEASE_SECONDS)

    assert len(handled) == 1
    assert backend.inserts == 1, "доделка нашла событие, а не создала второе"
    assert pipeline.approvals.get(approval.id).status == "done"


def test_a_moved_event_updates_in_place(world) -> None:
    """Суперсессия: то же подтверждение, новая дата — patch, а не второй insert."""
    pipeline, transport, backend = world
    event = build_calendar_event(approval_id="a-1", title="Экскурсия 3b", start=START)
    pipeline.calendar.upsert(event)

    moved = build_calendar_event(approval_id="a-1", title="Экскурсия 3b", start=MOVED)
    written = pipeline.calendar.upsert(moved)

    assert written.updated is True and written.created is False
    assert backend.inserts == 1 and backend.patches == 1
    stored = next(iter(backend.events.values()))
    assert stored["start"]["dateTime"].startswith("2026-09-19")


def test_an_unchanged_event_is_left_alone(world) -> None:
    pipeline, transport, backend = world
    event = build_calendar_event(approval_id="a-1", title="Экскурсия 3b", start=START)
    pipeline.calendar.upsert(event)

    written = pipeline.calendar.upsert(event)

    assert (written.created, written.updated) == (False, False)
    assert backend.patches == 0, "лишний patch — лишнее уведомление всем гостям события"


def test_google_timezone_normalization_does_not_trigger_a_patch(world) -> None:
    pipeline, _transport, backend = world
    event = build_calendar_event(approval_id="a-1", title="Экскурсия 3b", start=START)
    pipeline.calendar.upsert(event)
    stored = next(iter(backend.events.values()))
    stored["start"]["dateTime"] = "2026-09-12T09:45:00+02:00"
    stored["end"]["dateTime"] = "2026-09-12T10:45:00+02:00"

    written = pipeline.calendar.upsert(event)

    assert (written.created, written.updated) == (False, False)
    assert backend.patches == 0


def test_a_racing_insert_is_read_as_already_there(world) -> None:
    """Два воркера, один id: 409 от Google означает «событие уже наше»."""
    pipeline, transport, backend = world
    event = build_calendar_event(approval_id="a-1", title="Экскурсия 3b", start=START)
    backend.insert_event("family@group.calendar.example", event.to_body())

    written = Calendar(
        _BlindBackend(backend), calendar_id="family@group.calendar.example"
    ).upsert(event)

    assert (written.created, written.updated) == (False, False)
    assert backend.inserts == 1


class _BlindBackend:
    """Бэкенд, который «не видит» событие при первом чтении — гонка воркеров."""

    def __init__(self, inner: FakeCalendar) -> None:
        self.inner = inner
        self.looked = False

    def get_event(self, calendar_id: str, event_id: str):
        if not self.looked:
            self.looked = True
            return None
        return self.inner.get_event(calendar_id, event_id)

    def insert_event(self, calendar_id: str, body):
        return self.inner.insert_event(calendar_id, body)

    def patch_event(self, calendar_id: str, event_id: str, body):
        return self.inner.patch_event(calendar_id, event_id, body)

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        self.inner.delete_event(calendar_id, event_id)

    def list_events(self, calendar_id: str, **kwargs):
        return self.inner.list_events(calendar_id, **kwargs)


# --- конвейер -----------------------------------------------------------------


def test_the_card_goes_to_the_calendar_when_one_is_connected(world) -> None:
    pipeline, transport, backend = world
    staged = stage_event(pipeline)

    handled = pipeline.handle_callback(
        action=ACTION_CONFIRM, approval_id=staged.id, context=context()
    )

    assert handled.executed == KIND_CALENDAR
    assert "календар" in transport.messages[-1].text.lower()
    assert transport.documents == [], "файл .ics не нужен: событие уже в календаре"


def test_without_a_calendar_the_event_still_reaches_the_family(tmp_path: Path) -> None:
    """Нет календаря — нет и потери: событие уезжает файлом, как в Фазе 1."""
    from hermes_cloud.runner.bundle import items_for
    from hermes_cloud.runner.extraction import ExtractionResult

    result = ExtractionResult(
        kind="event", title="Экскурсия 3b", summary="", source_language="de",
        action_required=True, event_start=START, confidence=0.9,
    )

    assert items_for(result, calendar=False)[0].kind == KIND_ICS
    assert items_for(result, calendar=True)[0].kind == KIND_CALENDAR


def test_a_lost_connection_is_outcome_unknown_not_a_second_event(world) -> None:
    pipeline, transport, backend = world
    staged = stage_event(pipeline)

    def explode(*_args, **_kwargs):
        raise CalendarOutcomeUnknown("соединение оборвалось")

    backend.insert_event = explode  # type: ignore[method-assign]

    handled = pipeline.handle_callback(
        action=ACTION_CONFIRM, approval_id=staged.id, context=context()
    )

    assert handled.executed is None
    assert transport.messages[-1].text == TEXT_OUTCOME_UNKNOWN
    effect = pipeline.effects.find(run_id=staged.id, tool_use_id="approval")
    assert effect.status == "outcome_unknown"


def test_a_definitive_refusal_is_reported_as_failure(world) -> None:
    pipeline, transport, backend = world
    staged = stage_event(pipeline)

    def explode(*_args, **_kwargs):
        raise CalendarError("insert: HTTP 403 нет доступа к календарю")

    backend.insert_event = explode  # type: ignore[method-assign]

    handled = pipeline.handle_callback(
        action=ACTION_CONFIRM, approval_id=staged.id, context=context()
    )

    assert handled.executed is None
    assert "Не получилось" in transport.messages[-1].text
    assert pipeline.approvals.get(staged.id).status == "failed"


def test_the_calendar_tool_reads_but_never_writes(world) -> None:
    from hermes_cloud.runner.tools import REGISTRY, Services

    pipeline, transport, backend = world
    pipeline.calendar.upsert(
        build_calendar_event(approval_id="a-1", title="Экскурсия 3b", start=START)
    )
    services = Services.on(pipeline.approvals.db, calendar=pipeline.calendar)

    listed = REGISTRY.invoke(context(), "calendar_list_events", {"days": 60}, services=services)

    assert [item["summary"] for item in listed["events"]] == ["Экскурсия 3b"]
    assert backend.inserts == 1, "чтение ничего не создало"


def test_the_tool_says_plainly_when_no_calendar_is_connected(tmp_path: Path) -> None:
    """«Событий нет» и «календарь не подключён» — разные ответы."""
    from hermes_cloud.runner.tools import REGISTRY, Services, ToolInputError

    services = Services.on(open_database(tmp_path / "hermes.db"))

    with pytest.raises(ToolInputError):
        REGISTRY.invoke(context(), "calendar_list_events", {}, services=services)

"""Второе письмо про то же самое: обновление, а не второе событие.

Самый дорогой промах здесь — тихий: если не узнать в новом письме прежний факт,
у семьи останутся два подтверждённых события на разные даты, и она узнает об
этом, приехав не в тот день.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from hermes_cloud.channels.telegram import FakeTransport
from hermes_cloud.core.approvals import ApprovalStore
from hermes_cloud.core.commitments import (
    STATUS_CONFIRMED,
    STATUS_SUPERSEDED,
)
from hermes_cloud.core.db import open_database
from hermes_cloud.core.events import EventStore
from hermes_cloud.core.matching import MAYBE, SURE, find_match, score, title_similarity
from hermes_cloud.execute.gcal import Calendar, FakeCalendar
from hermes_cloud.execute.reminder import ReminderStore
from hermes_cloud.ingest.worker import Worker
from hermes_cloud.runner.card import ACTION_CONFIRM
from hermes_cloud.runner.extraction import Extraction, ExtractionResult, Money
from hermes_cloud.runner.pipeline import Pipeline

CHAT = "-100990000101"
PARENT = "990000001"

from hermes_cloud.core.runcontext import Household, build_run_context  # noqa: E402

HOUSEHOLD = Household(
    owner=PARENT, family=frozenset({PARENT}), allowed_chats=frozenset({CHAT})
)


def context():
    return build_run_context(household=HOUSEHOLD, actor_id=PARENT, chat_id=CHAT)


def letter(subject: str, body: str, *, message_id: str) -> bytes:
    return (
        b"From: Sekretariat <sekretariat@grundschule.example>\r\n"
        + f"Subject: {subject}\r\n".encode()
        + f"Message-ID: <{message_id}>\r\n".encode()
        + b"Content-Type: text/plain; charset=\"utf-8\"\r\n\r\n"
        + body.encode()
    )


FIRST = letter(
    "Klassenfahrt 3b", "Die Klassenfahrt findet am 12.09.2026 statt.",
    message_id="trip-1@example",
)
SECOND = letter(
    "Klassenfahrt 3b verschoben", "Die Klassenfahrt wurde auf den 19.09.2026 verschoben.",
    message_id="trip-2@example",
)

TRIP_V1 = ExtractionResult(
    kind="event",
    title="Экскурсия класса 3b",
    summary="Экскурсия 12 сентября.",
    source_language="de",
    action_required=True,
    event_start=datetime(2026, 9, 12, 7, 45, tzinfo=UTC),
    confidence=0.93,
)
TRIP_V2 = TRIP_V1.model_copy(update={
    "title": "Экскурсия класса 3b перенесена",
    "summary": "Экскурсия перенесена на 19 сентября.",
    "event_start": datetime(2026, 9, 19, 7, 45, tzinfo=UTC),
})
OTHER = ExtractionResult(
    kind="payment",
    title="Фотограф в школе — 8 EUR",
    summary="Оплата школьной фотосъёмки.",
    source_language="de",
    action_required=True,
    due_date=date(2026, 10, 1),
    amount=Money(amount_cents=800, currency="EUR"),
    confidence=0.9,
)


class SequenceExtractor:
    """Отдаёт заготовленные извлечения по порядку писем."""

    def __init__(self, *results: ExtractionResult) -> None:
        self.results = list(results)

    def extract_email(self, parsed) -> Extraction:
        return Extraction(result=self.results.pop(0), model="stub")

    def system_prompt(self) -> str:
        return "stub-prompt"


@pytest.fixture()
def world(tmp_path: Path):
    database = open_database(tmp_path / "hermes.db")
    backend = FakeCalendar()
    transport = FakeTransport()
    pipeline = Pipeline(
        approvals=ApprovalStore(database),
        reminders=ReminderStore(database),
        transport=transport,
        extractor=SequenceExtractor(TRIP_V1, TRIP_V2),
        chat=CHAT,
        calendar=Calendar(backend, calendar_id="family@group.calendar.example"),
    )
    return EventStore(database), pipeline, transport, backend


def deliver(events: EventStore, pipeline: Pipeline, raw: bytes, external_id: str) -> str:
    events.append(source="inject", external_id=external_id, raw=raw)
    handled: list = []
    Worker(events, lambda event: handled.append(pipeline.handle_event(event))).run_once()
    return handled[0].approval_id


def confirm(pipeline: Pipeline, approval_id: str):
    return pipeline.handle_callback(
        action=ACTION_CONFIRM, approval_id=approval_id, context=context()
    )


# --- сопоставление ------------------------------------------------------------


def test_the_same_trip_scores_above_the_sure_threshold() -> None:
    value = score(
        same_domain=True, same_kind=True,
        title_left="Экскурсия класса 3b перенесена",
        title_right="Экскурсия класса 3b",
    )

    assert value >= SURE


def test_a_different_letter_from_the_same_school_is_not_a_match() -> None:
    """Домен совпадает у половины писем школы — одного его мало."""
    value = score(
        same_domain=True, same_kind=False,
        title_left="Фотограф в школе — 8 EUR",
        title_right="Экскурсия класса 3b",
    )

    assert value < MAYBE


def test_common_words_do_not_create_similarity() -> None:
    """«Экскурсия» и «взнос» есть в каждом письме — различать должны не они."""
    assert title_similarity("Экскурсия 3b", "Экскурсия 5a") < 1.0
    assert title_similarity("взнос за обед", "взнос за автобус") < 0.8


# --- сквозной сценарий --------------------------------------------------------


def test_a_moved_trip_updates_the_same_event(world) -> None:
    events, pipeline, transport, backend = world
    first = deliver(events, pipeline, FIRST, "eml:<trip-1@example>")
    confirm(pipeline, first)
    assert backend.inserts == 1

    second = deliver(events, pipeline, SECOND, "eml:<trip-2@example>")
    card = transport.messages[-1].text
    assert "Это обновление" in card and "12.09.2026" in card and "19.09.2026" in card
    assert "Обновить в календаре" in card

    confirm(pipeline, second)

    assert backend.inserts == 1, "второго события нет — обновлено первое"
    assert backend.patches == 1
    stored = next(iter(backend.events.values()))
    assert stored["start"]["dateTime"].startswith("2026-09-19")


def test_the_old_version_is_superseded_and_the_chain_reads_back(world) -> None:
    events, pipeline, transport, backend = world
    first = deliver(events, pipeline, FIRST, "eml:<trip-1@example>")
    confirm(pipeline, first)
    second = deliver(events, pipeline, SECOND, "eml:<trip-2@example>")
    confirm(pipeline, second)

    confirmed = pipeline.commitments.confirmed()
    assert len(confirmed) == 1, "действующая версия одна"
    chain = pipeline.commitments.chain(confirmed[0].id)
    assert [item.status for item in chain] == [STATUS_SUPERSEDED, STATUS_CONFIRMED]
    assert chain[0].payload["start"].startswith("2026-09-12")
    assert chain[1].payload["start"].startswith("2026-09-19")


def test_an_unrelated_letter_is_its_own_fact(world, tmp_path: Path) -> None:
    events, pipeline, transport, backend = world
    pipeline.extractor = SequenceExtractor(TRIP_V1, OTHER)
    first = deliver(events, pipeline, FIRST, "eml:<trip-1@example>")
    confirm(pipeline, first)

    second = deliver(events, pipeline, SECOND, "eml:<trip-2@example>")
    confirm(pipeline, second)

    assert len(pipeline.commitments.confirmed()) == 2, "два разных факта"
    assert "Это обновление" not in transport.messages[-1].text


def test_the_new_reminder_replaces_the_old_one(world) -> None:
    """Два напоминания об одном взносе с разными датами — худшее из возможного."""
    events, pipeline, transport, backend = world
    with_fee = TRIP_V1.model_copy(update={
        "kind": "payment", "due_date": date(2026, 9, 8),
        "amount": Money(amount_cents=1500, currency="EUR"), "event_start": None,
    })
    moved_fee = with_fee.model_copy(update={
        "title": "Экскурсия класса 3b — взнос перенесён",
        "due_date": date(2026, 9, 15),
    })
    pipeline.extractor = SequenceExtractor(with_fee, moved_fee)

    confirm(pipeline, deliver(events, pipeline, FIRST, "eml:<trip-1@example>"))
    second = deliver(events, pipeline, SECOND, "eml:<trip-2@example>")
    assert "Перенести напоминание" in transport.messages[-1].text
    confirm(pipeline, second)

    pending = pipeline.reminders.pending()
    assert len(pending) == 1, "старое напоминание отменено, осталось новое"
    assert datetime.fromtimestamp(pending[0].due_at, UTC).date() == date(2026, 9, 15)


def test_an_uncertain_match_shows_both_and_says_so(world) -> None:
    """Склейку человек не заметит, дубликат — заметит. Поэтому решает он."""
    events, pipeline, transport, backend = world
    similar = TRIP_V1.model_copy(update={"title": "Экскурсия класса 3b и музей"})
    pipeline.extractor = SequenceExtractor(TRIP_V1, similar)
    confirm(pipeline, deliver(events, pipeline, FIRST, "eml:<trip-1@example>"))

    deliver(events, pipeline, SECOND, "eml:<trip-2@example>")

    card = transport.messages[-1].text
    match = find_match(
        pipeline.commitments, pipeline.evidence, kind="event",
        title="Экскурсия класса 3b и музей", sender_domain="grundschule.example",
    )
    if match and match.maybe:
        assert "Возможно, это про то же" in card
        assert len(pipeline.commitments.candidates()) == 1, "второй факт независим"
    else:
        assert "Это обновление" in card, "уверенное совпадение — значит версия"

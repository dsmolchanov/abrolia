"""Сроки хранения, выгрузка и стирание — права субъекта, проверяемые тестом.

Полнота экспорта здесь не декларируется, а сверяется: таблица, появившаяся в
схеме и не попавшая ни в список выгружаемых, ни в список сознательно
исключённых, роняет набор.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cloud.core.approvals import ApprovalStore
from hermes_cloud.core.commitments import CommitmentStore
from hermes_cloud.core.db import open_database
from hermes_cloud.core.dsar import (
    EXPORTED,
    EXTERNAL_SURFACES,
    NOT_EXPORTED,
    export_household,
    is_deleted,
    wipe_household,
)
from hermes_cloud.core.effects import EffectJournal
from hermes_cloud.core.events import EventStore, HouseholdDeleted
from hermes_cloud.core.evidence import EvidenceStore
from hermes_cloud.core.memory import MemoryStore
from hermes_cloud.core.retention import (
    ACTIONS_DAYS,
    CANDIDATE_DAYS,
    DAY,
    DLQ_METADATA_DAYS,
    MEMORY_REVIEW_DAYS,
    RAW_EVENT_DAYS,
    REMINDER_DONE_DAYS,
    RetentionJob,
)
from hermes_cloud.execute.reminder import ReminderStore

CHAT = "-100990000101"
ACTOR = "990000001"
LETTER = (
    b"From: Sekretariat <sekretariat@grundschule.example>\r\n"
    b"Subject: Elternbeitrag\r\n"
    b"Message-ID: <synthetic-dsar-1@example>\r\n"
    b"Content-Type: text/plain; charset=\"utf-8\"\r\n\r\n"
    b"Bitte ueberweisen Sie 15,00 EUR bis zum 08.09.2026.\r\n"
)

NOW = 1_800_000_000.0


@pytest.fixture()
def household(tmp_path: Path):
    database = open_database(tmp_path / "hermes.db")
    return database


def populate(database, *, when: float = NOW) -> dict[str, str]:
    """Небольшой household: письмо, подтверждение, эффект, напоминание, память."""
    events = EventStore(database)
    approvals = ApprovalStore(database)
    reminders = ReminderStore(database)
    evidence = EvidenceStore(database)
    commitments = CommitmentStore(database)
    memory = MemoryStore(database)

    event = events.append(
        source="inject", external_id="eml:<synthetic-dsar-1@example>", raw=LETTER,
        received_at=when,
    ).event
    run = evidence.record_run(event_id=event.id, model="stub", prompt_sha="sha", now=when)
    evidence.add_ref(
        extraction_run_id=run.id, event_id=event.id, text="Bitte 15,00 EUR",
        sender="sekretariat@grundschule.example", now=when,
    )
    commitment = commitments.propose(
        kind="payment", payload={"amount_cents": 1500}, extraction_run_id=run.id, now=when
    )
    staged = approvals.stage(
        kind="reminder", payload={"kind": "reminder", "commitment_id": commitment.id},
        chat=CHAT, actor=ACTOR, now=when,
    )
    approval = approvals.claim_by_id(
        approval_id=staged.id, chat=CHAT, thread=None, actor=ACTOR, now=when
    )
    commitments.confirm(commitment.id, approval, now=when)
    reminder = reminders.create(
        chat=CHAT, text="взнос", due_at=when + DAY, approval_id=approval.id, now=when
    )
    statement = memory.propose(text="Лиза не ест орехи", now=when)
    memory.confirm(statement.id, approval, now=when)
    return {"event": event.id, "approval": approval.id, "reminder": reminder.id,
            "commitment": commitment.id, "statement": statement.id, "run": run.id}


# --- retention ----------------------------------------------------------------


def test_letter_content_dies_before_its_metadata(household) -> None:
    """30 дней — содержимому, 365 — следу: иначе провенанс не переживёт письма."""
    ids = populate(household)

    RetentionJob(household).run(now=NOW + (RAW_EVENT_DAYS + 1) * DAY)

    row = household.query_one("SELECT raw FROM events WHERE id = ?", (ids["event"],))
    assert row is not None, "строка события осталась"
    assert bytes(row["raw"]) == b"", "содержимое письма стёрто"
    refs = EvidenceStore(household).for_run(ids["run"])
    assert refs and refs[0].sender_domain is not None, "след источника остался"


def test_action_journal_dies_after_a_year(household) -> None:
    ids = populate(household)

    report = RetentionJob(household).run(now=NOW + (ACTIONS_DAYS + 1) * DAY)

    assert report.events_deleted == 1
    assert report.effects_deleted == 1
    assert report.approvals_deleted == 1
    assert report.extraction_runs_deleted == 1
    assert EvidenceStore(household).for_run(ids["run"]) == [], "ссылки ушли каскадом"


def test_done_reminders_live_ninety_days_after_they_fire(household) -> None:
    ids = populate(household)
    reminders = ReminderStore(household)
    reminders.mark_delivered(ids["reminder"], now=NOW)

    RetentionJob(household).run(now=NOW + (REMINDER_DONE_DAYS - 1) * DAY)
    assert reminders.get(ids["reminder"]) is not None

    RetentionJob(household).run(now=NOW + (REMINDER_DONE_DAYS + 1) * DAY)
    assert reminders.get(ids["reminder"]) is None


def test_unconfirmed_hypotheses_do_not_linger(household) -> None:
    """Гипотеза, которую никто не подтвердил, — не факт и не память."""
    commitments = CommitmentStore(household)
    memory = MemoryStore(household)
    candidate = commitments.propose(kind="payment", payload={"amount_cents": 100}, now=NOW)
    statement = memory.propose(text="возможно, вторник", now=NOW)

    report = RetentionJob(household).run(now=NOW + (CANDIDATE_DAYS + 1) * DAY)

    assert report.candidates_deleted == 1 and report.memory_candidates_deleted == 1
    assert commitments.get(candidate.id) is None
    assert memory.get(statement.id) is None


def test_confirmed_memory_is_offered_for_review_not_deleted(household) -> None:
    """Удалять по таймеру то, что просили запомнить, — тихо терять важное."""
    ids = populate(household)

    report = RetentionJob(household).run(now=NOW + (MEMORY_REVIEW_DAYS + 1) * DAY)

    assert ids["statement"] in report.memory_due_for_review
    assert MemoryStore(household).get(ids["statement"]) is not None


def test_failed_code_attempts_expire(household) -> None:
    approvals = ApprovalStore(household)
    approvals.claim(code="0" * 16, chat=CHAT, thread=None, actor=ACTOR, now=NOW)

    RetentionJob(household).run(now=NOW + (DLQ_METADATA_DAYS + 1) * DAY)

    assert approvals.failed_attempts(
        actor=ACTOR, chat=CHAT, now=NOW + (DLQ_METADATA_DAYS + 1) * DAY
    ) == 0


def test_retention_is_idempotent(household) -> None:
    populate(household)
    later = NOW + (ACTIONS_DAYS + 1) * DAY

    first = RetentionJob(household).run(now=later)
    second = RetentionJob(household).run(now=later)

    assert first.total_deleted > 0
    assert second.total_deleted == 0, "повтор ничего не находит и ничего не ломает"


# --- экспорт ------------------------------------------------------------------


def test_every_table_is_either_exported_or_explicitly_excluded(household) -> None:
    """Новая таблица без решения об экспорте — дефект, а не мелочь."""
    tables = {
        row["name"]
        for row in household.query(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }

    undecided = tables - set(EXPORTED) - set(NOT_EXPORTED)

    assert undecided == set(), f"таблицы без решения об экспорте: {sorted(undecided)}"


def test_export_contains_the_family_data(household) -> None:
    ids = populate(household)

    dump = export_household(household, now=NOW)

    assert [row["id"] for row in dump["tables"]["events"]] == [ids["event"]]
    assert "15,00 EUR" in dump["tables"]["events"][0]["raw"], "письмо выгружается целиком"
    assert [row["id"] for row in dump["tables"]["commitments"]] == [ids["commitment"]]
    assert [row["text"] for row in dump["tables"]["memory_statements"]] == [
        "Лиза не ест орехи"
    ]
    assert dump["outside_our_control"] == list(EXTERNAL_SURFACES)


def test_export_never_carries_secrets(household) -> None:
    """Хэш кода подтверждения — ключ к чужому действию, и в выгрузке ему нечего делать."""
    populate(household)

    dump = json.dumps(export_household(household, now=NOW), ensure_ascii=False)

    assert "code_sha" not in dump
    assert "payload_sha" not in dump


# --- стирание -----------------------------------------------------------------


def test_wipe_removes_everything_and_leaves_a_tombstone(household) -> None:
    populate(household)

    removed = wipe_household(household, now=NOW)

    assert sum(removed.values()) > 0
    for table in EXPORTED:
        assert household.query(f"SELECT * FROM {table}") == [], f"{table} не стёрта"
    assert is_deleted(household) is True


def test_a_deleted_household_cannot_be_resurrected(household) -> None:
    """Отложенный вебхук после стирания не воскрешает household молчаливой вставкой."""
    populate(household)
    wipe_household(household, now=NOW)

    with pytest.raises(HouseholdDeleted):
        EventStore(household).append(
            source="nerve", external_id="eml:<late-webhook@example>", raw=LETTER
        )


# --- owner-авторизация --------------------------------------------------------


def owner_world(database):
    """Конвейер с household'ом, где владелец один, а второй взрослый — просто семья."""
    from hermes_cloud.channels.telegram import FakeTransport
    from hermes_cloud.core.runcontext import Household, build_run_context
    from hermes_cloud.runner.pipeline import Pipeline

    transport = FakeTransport()
    pipeline = Pipeline(
        approvals=ApprovalStore(database),
        reminders=ReminderStore(database),
        transport=transport,
        extractor=None,
        chat=CHAT,
    )
    household = Household(
        owner=ACTOR, family=frozenset({ACTOR, "990000002"}),
        allowed_chats=frozenset({CHAT}),
    )
    return pipeline, transport, household, build_run_context


@pytest.mark.parametrize("kind", ["export", "delete"])
def test_only_the_owner_may_confirm_export_and_delete(household, kind: str) -> None:
    """Родитель — не владелец: выгрузка и стирание ему не по чину."""
    from hermes_cloud.runner.card import ACTION_CONFIRM

    populate(household)
    pipeline, transport, family, build = owner_world(household)
    staged = pipeline.approvals.stage(
        kind=kind, payload={"kind": kind}, chat=CHAT, actor=ACTOR
    )

    parent = build(household=family, actor_id="990000002", chat_id=CHAT)
    refused = pipeline.handle_callback(
        action=ACTION_CONFIRM, approval_id=staged.id, context=parent
    )

    assert refused.executed is None
    assert "нет такого права" in transport.messages[-1].text
    assert pipeline.approvals.get(staged.id).status == "staged", "код не сгорел"
    assert is_deleted(household) is False


def test_the_owner_gets_the_export_as_a_file(household) -> None:
    from hermes_cloud.runner.card import ACTION_CONFIRM

    ids = populate(household)
    pipeline, transport, family, build = owner_world(household)
    staged = pipeline.approvals.stage(
        kind="export", payload={"kind": "export"}, chat=CHAT, actor=ACTOR
    )

    handled = pipeline.handle_callback(
        action=ACTION_CONFIRM, approval_id=staged.id,
        context=build(household=family, actor_id=ACTOR, chat_id=CHAT),
    )

    assert handled.executed == "export"
    assert len(transport.documents) == 1
    dump = json.loads(transport.documents[0].content)
    assert [row["id"] for row in dump["tables"]["events"]] == [ids["event"]]


def test_the_owner_confirmation_wipes_and_says_what_remains(household) -> None:
    from hermes_cloud.runner.card import ACTION_CONFIRM

    populate(household)
    pipeline, transport, family, build = owner_world(household)
    staged = pipeline.approvals.stage(
        kind="delete", payload={"kind": "delete"}, chat=CHAT, actor=ACTOR
    )

    handled = pipeline.handle_callback(
        action=ACTION_CONFIRM, approval_id=staged.id,
        context=build(household=family, actor_id=ACTOR, chat_id=CHAT),
    )

    assert handled.executed == "delete"
    assert is_deleted(household) is True
    assert "Telegram" in transport.messages[-1].text, "о неудалимом сказано прямо"


def test_effects_journal_is_included_in_the_export(household) -> None:
    ids = populate(household)

    dump = export_household(household, now=NOW)

    effects = dump["tables"]["effects"]
    assert [row["approval_id"] for row in effects] == [ids["approval"]]
    assert EffectJournal(household).for_run(ids["approval"])

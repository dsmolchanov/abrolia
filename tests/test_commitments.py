"""Обязательства, память и провенанс: статусная модель и её инварианты."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cloud.core.approvals import ApprovalStore
from hermes_cloud.core.commitments import (
    KIND_PAYMENT,
    STATUS_CANDIDATE,
    STATUS_CONFIRMED,
    STATUS_REJECTED,
    STATUS_SUPERSEDED,
    CommitmentError,
    CommitmentStore,
)
from hermes_cloud.core.db import open_database
from hermes_cloud.core.evidence import (
    TEXT_SOURCE_GONE,
    EvidenceStore,
    content_sha,
    find_span,
    sender_domain,
)
from hermes_cloud.core.memory import KIND_FACT, KIND_ROUTINE, MemoryStore

CHAT = "-100990000101"
ACTOR = "990000001"
PAYLOAD = {"kind": "reminder", "text": "взнос за экскурсию", "due_date": "2026-09-08",
           "amount_cents": 1500, "currency": "EUR"}


@pytest.fixture()
def world(tmp_path: Path):
    database = open_database(tmp_path / "hermes.db")
    return (
        CommitmentStore(database),
        MemoryStore(database),
        EvidenceStore(database),
        ApprovalStore(database),
    )


def claimed_approval(approvals: ApprovalStore, kind: str = "reminder"):
    staged = approvals.stage(kind=kind, payload=PAYLOAD, chat=CHAT, actor=ACTOR)
    approval = approvals.claim_by_id(
        approval_id=staged.id, chat=CHAT, thread=None, actor=ACTOR
    )
    assert approval is not None
    return approval


# --- статусная модель ---------------------------------------------------------


def test_the_model_can_only_produce_candidates(world) -> None:
    commitments, _, _, _ = world

    commitment = commitments.propose(kind=KIND_PAYMENT, payload=PAYLOAD, confidence=0.95)

    assert commitment.status == STATUS_CANDIDATE
    assert commitment.is_belief is False, "гипотеза — ещё не факт семьи"
    assert commitments.confirmed() == [], "кандидат не отдаётся как подтверждённое"


def test_confirmation_requires_a_claimed_approval(world) -> None:
    """«Подтверждено» не бывает без человека — даже изнутри кода."""
    commitments, _, _, approvals = world
    commitment = commitments.propose(kind=KIND_PAYMENT, payload=PAYLOAD)
    staged_only = approvals.stage(kind="reminder", payload=PAYLOAD, chat=CHAT, actor=ACTOR)

    class NotAnApproval:
        id = "подделка"
        status = "staged"

    with pytest.raises(CommitmentError):
        commitments.confirm(commitment.id, NotAnApproval())
    with pytest.raises(CommitmentError):
        commitments.confirm(commitment.id, approvals.get(staged_only.id))

    assert commitments.get(commitment.id).status == STATUS_CANDIDATE


def test_confirmed_commitment_is_a_family_fact(world) -> None:
    commitments, _, _, approvals = world
    commitment = commitments.propose(kind=KIND_PAYMENT, payload=PAYLOAD)

    confirmed = commitments.confirm(commitment.id, claimed_approval(approvals))

    assert confirmed.status == STATUS_CONFIRMED and confirmed.is_belief
    assert [item.id for item in commitments.confirmed()] == [commitment.id]


def test_double_confirmation_is_refused(world) -> None:
    commitments, _, _, approvals = world
    commitment = commitments.propose(kind=KIND_PAYMENT, payload=PAYLOAD)
    commitments.confirm(commitment.id, claimed_approval(approvals))

    with pytest.raises(CommitmentError):
        commitments.confirm(commitment.id, claimed_approval(approvals))


def test_rejection_closes_only_candidates(world) -> None:
    commitments, _, _, approvals = world
    commitment = commitments.propose(kind=KIND_PAYMENT, payload=PAYLOAD)

    assert commitments.reject(commitment.id) is True
    assert commitments.get(commitment.id).status == STATUS_REJECTED
    assert commitments.reject(commitment.id) is False, "повторное «нет» ничего не меняет"


# --- версии -------------------------------------------------------------------


def test_superseding_keeps_the_whole_chain(world) -> None:
    """«Экскурсия перенесена» не стирает прежнюю дату, а становится v2."""
    commitments, _, _, approvals = world
    first = commitments.propose(kind="event", payload={"start": "2026-09-12T07:45:00"})
    commitments.confirm(first.id, claimed_approval(approvals))

    second = commitments.propose(
        kind="event", payload={"start": "2026-09-19T07:45:00"}, supersedes=first.id
    )

    assert commitments.get(first.id).status == STATUS_SUPERSEDED
    assert commitments.get(first.id).payload["start"] == "2026-09-12T07:45:00"
    assert [item.id for item in commitments.chain(second.id)] == [first.id, second.id]
    assert commitments.latest_version_of(first.id).id == second.id


def test_superseding_a_missing_version_is_an_error(world) -> None:
    commitments, _, _, _ = world

    with pytest.raises(CommitmentError):
        commitments.propose(kind="event", payload={}, supersedes="нет такого")


# --- память -------------------------------------------------------------------


def test_memory_never_takes_effect_without_a_human(world) -> None:
    _, memory, _, approvals = world

    statement = memory.propose(text="Лиза не ест орехи", kind=KIND_FACT, actor=ACTOR)

    assert statement.status == STATUS_CANDIDATE
    assert memory.recall() == [], "кандидат не вспоминается"

    memory.confirm(statement.id, claimed_approval(approvals, kind="memory"))

    assert [item.text for item in memory.recall()] == ["Лиза не ест орехи"]


def test_memory_supersedes_on_confirmation_not_before(world) -> None:
    """Пока новая версия — кандидат, действует прежняя. Иначе «нет» стирает память."""
    _, memory, _, approvals = world
    first = memory.propose(text="плавание по понедельникам", kind=KIND_ROUTINE)
    memory.confirm(first.id, claimed_approval(approvals, kind="memory"))
    second = memory.propose(
        text="плавание по вторникам", kind=KIND_ROUTINE, supersedes=first.id
    )

    assert [item.text for item in memory.recall()] == ["плавание по понедельникам"]

    memory.confirm(second.id, claimed_approval(approvals, kind="memory"))

    assert [item.text for item in memory.recall()] == ["плавание по вторникам"]
    assert memory.get(first.id).status == STATUS_SUPERSEDED
    assert [item.id for item in memory.chain(second.id)] == [first.id, second.id]


def test_a_fact_has_no_validity_window(world) -> None:
    _, memory, _, _ = world

    with pytest.raises(CommitmentError):
        memory.propose(text="Лиза не ест орехи", kind=KIND_FACT, valid_to=1.0)


def test_expired_routine_is_not_recalled(world) -> None:
    _, memory, _, approvals = world
    statement = memory.propose(
        text="бассейн до конца мая", kind=KIND_ROUTINE, valid_to=1000.0
    )
    memory.confirm(statement.id, claimed_approval(approvals, kind="memory"), now=500.0)

    assert memory.recall(now=900.0)
    assert memory.recall(now=1100.0) == []


# --- провенанс ----------------------------------------------------------------


def test_sender_domain_keeps_the_source_not_the_person() -> None:
    assert sender_domain("sekretariat@grundschule.example") == "grundschule.example"
    assert sender_domain("не адрес") is None


def test_find_span_returns_the_sentence_with_the_number() -> None:
    text = "Liebe Eltern.\nBitte ueberweisen Sie 15,00 EUR bis zum 08.09.2026.\nGruesse"

    span = find_span(text, ["15,00"])

    assert span is not None
    assert "15,00 EUR" in text[span[0]:span[1]]


def test_find_span_gives_up_rather_than_guessing() -> None:
    assert find_span("короткий текст", ["999,00"]) is None
    assert find_span("короткий текст", ["15"]) is None, "две цифры — не опора"


def test_evidence_survives_the_letter_it_came_from(world, tmp_path: Path) -> None:
    """После удаления письма ссылка остаётся разрешимой — без содержимого."""
    from hermes_cloud.core.events import EventStore

    commitments, _, evidence, _ = world
    events = EventStore(evidence.db)
    raw = (
        b"From: Sekretariat <sekretariat@grundschule.example>\r\n"
        b"Subject: Elternbeitrag\r\n"
        b"Message-ID: <synthetic-1@example>\r\n"
        b"Content-Type: text/plain; charset=\"utf-8\"\r\n\r\n"
        b"Bitte ueberweisen Sie 15,00 EUR bis zum 08.09.2026.\r\n"
    )
    event = events.append(
        source="inject", external_id="eml:<synthetic-1@example>", raw=raw
    ).event
    run = evidence.record_run(event_id=event.id, model="stub", prompt_sha="sha")
    from hermes_cloud.ingest.eml import parse_eml

    ref = evidence.add_ref(
        extraction_run_id=run.id,
        event_id=event.id,
        text=parse_eml(raw).text,
        sender="sekretariat@grundschule.example",
        message_date="Tue, 1 Sep 2026 16:03:00 +0200",
        needles=["15,00"],
    )

    assert "15,00 EUR" in evidence.render_source(ref)
    assert ref.content_sha == content_sha(parse_eml(raw).text)

    # Retention: содержимое письма удалено, метаданные ссылки остались.
    with evidence.db.write() as connection:
        connection.execute("UPDATE events SET raw = ? WHERE id = ?", (b"", event.id))

    rendered = evidence.render_source(ref)
    assert TEXT_SOURCE_GONE in rendered
    assert "grundschule.example" in rendered, "след источника остаётся проверяемым"
    assert "15,00" not in rendered, "цитата не переживает удаление письма"

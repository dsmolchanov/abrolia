"""Competency questions: вопросы, на которые схема обязана отвечать структурно.

Набор регрессионный и растёт вместе с пилотом: каждый вопрос, на который семья
не получила ответа, становится здесь тестом. Смысл набора — не «покрытие», а
единственный оговорённый триггер: **провал recall тут** — единственный повод
заводить embeddings-индекс. Пока эти запросы отвечают из SQL, повода нет.

Первый набор — три вопроса из плана:

1. Какие платежи с дедлайном на этой неделе и кто отвечает?
2. Из какого письма взялась эта сумма?
3. Что изменилось между первым письмом и вторым?

И одно требование ко всем ответам сразу: `candidate` не звучит как факт.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from hermes_cloud.core.approvals import ApprovalStore
from hermes_cloud.core.commitments import KIND_EVENT, KIND_PAYMENT, CommitmentStore
from hermes_cloud.core.db import open_database
from hermes_cloud.core.events import EventStore
from hermes_cloud.core.evidence import EvidenceStore
from hermes_cloud.core.queries import payments_due, source_of, version_delta
from hermes_cloud.ingest.eml import parse_eml

CHAT = "-100990000101"
ACTOR = "990000001"

WEEK_START = date(2026, 9, 7)
WEEK_END = date(2026, 9, 13)

SCHOOL_LETTER = (
    b"From: Sekretariat <sekretariat@grundschule.example>\r\n"
    b"Subject: Elternbeitrag Klassenfahrt\r\n"
    b"Message-ID: <synthetic-cq-1@example>\r\n"
    b"Content-Type: text/plain; charset=\"utf-8\"\r\n\r\n"
    b"Liebe Eltern.\r\n"
    b"Bitte ueberweisen Sie den Elternbeitrag von 15,00 EUR bis zum 08.09.2026.\r\n"
)


@pytest.fixture()
def household(tmp_path: Path):
    database = open_database(tmp_path / "hermes.db")
    return (
        CommitmentStore(database),
        EvidenceStore(database),
        EventStore(database),
        ApprovalStore(database),
    )


def confirmed(commitments: CommitmentStore, approvals: ApprovalStore, commitment_id: str):
    """Подтвердить обязательство так же, как это делает человек кнопкой."""
    staged = approvals.stage(kind="reminder", payload={"x": 1}, chat=CHAT, actor=ACTOR)
    approval = approvals.claim_by_id(
        approval_id=staged.id, chat=CHAT, thread=None, actor=ACTOR
    )
    return commitments.confirm(commitment_id, approval)


# --- CQ 1: платежи недели ------------------------------------------------------


def test_payments_due_this_week_with_the_responsible(household) -> None:
    commitments, _, _, approvals = household
    this_week = commitments.propose(
        kind=KIND_PAYMENT,
        payload={"text": "взнос за экскурсию", "due_date": "2026-09-08",
                 "amount_cents": 1500, "currency": "EUR", "responsible": "мама"},
    )
    next_month = commitments.propose(
        kind=KIND_PAYMENT,
        payload={"text": "продлёнка", "due_date": "2026-10-01", "amount_cents": 8000,
                 "currency": "EUR", "responsible": "папа"},
    )
    confirmed(commitments, approvals, this_week.id)
    confirmed(commitments, approvals, next_month.id)

    due = payments_due(commitments, start=WEEK_START, end=WEEK_END)

    assert [item.text for item in due] == ["взнос за экскурсию"]
    assert due[0].responsible == "мама"
    assert (due[0].amount_cents, due[0].currency) == (1500, "EUR")


def test_a_candidate_payment_is_never_reported_as_due(household) -> None:
    """Статусная модель на практике: гипотеза не звучит как обязательство."""
    commitments, _, _, _ = household
    commitments.propose(
        kind=KIND_PAYMENT,
        payload={"text": "возможно взнос", "due_date": "2026-09-08", "amount_cents": 1500},
    )

    assert payments_due(commitments, start=WEEK_START, end=WEEK_END) == []


def test_a_superseded_payment_drops_out_of_the_week(household) -> None:
    commitments, _, _, approvals = household
    first = commitments.propose(
        kind=KIND_PAYMENT,
        payload={"text": "взнос 15 EUR", "due_date": "2026-09-08", "amount_cents": 1500},
    )
    confirmed(commitments, approvals, first.id)
    second = commitments.propose(
        kind=KIND_PAYMENT,
        payload={"text": "взнос 18 EUR", "due_date": "2026-09-08", "amount_cents": 1800},
        supersedes=first.id,
    )
    confirmed(commitments, approvals, second.id)

    due = payments_due(commitments, start=WEEK_START, end=WEEK_END)

    assert [item.amount_cents for item in due] == [1800], "в неделе только действующая версия"


# --- CQ 2: откуда взялась сумма -----------------------------------------------


def test_where_did_this_amount_come_from(household) -> None:
    commitments, evidence, events, approvals = household
    event = events.append(
        source="inject", external_id="eml:<synthetic-cq-1@example>", raw=SCHOOL_LETTER
    ).event
    run = evidence.record_run(event_id=event.id, model="stub", prompt_sha="sha")
    evidence.add_ref(
        extraction_run_id=run.id,
        event_id=event.id,
        text=parse_eml(SCHOOL_LETTER).text,
        sender="sekretariat@grundschule.example",
        message_date="Tue, 1 Sep 2026 16:03:00 +0200",
        needles=["15,00"],
    )
    commitment = commitments.propose(
        kind=KIND_PAYMENT,
        payload={"text": "взнос", "due_date": "2026-09-08", "amount_cents": 1500},
        extraction_run_id=run.id,
    )
    confirmed(commitments, approvals, commitment.id)

    source = source_of(evidence, commitments.get(commitment.id))

    assert "grundschule.example" in source
    assert "15,00 EUR" in source, "цитата собрана из живого письма"


def test_the_source_answer_degrades_honestly(household) -> None:
    """Письмо удалено по сроку хранения — ответ остаётся, цитата исчезает."""
    from hermes_cloud.core.evidence import TEXT_SOURCE_GONE

    commitments, evidence, events, _ = household
    event = events.append(
        source="inject", external_id="eml:<synthetic-cq-1@example>", raw=SCHOOL_LETTER
    ).event
    run = evidence.record_run(event_id=event.id, model="stub", prompt_sha="sha")
    evidence.add_ref(
        extraction_run_id=run.id, event_id=event.id, text=parse_eml(SCHOOL_LETTER).text,
        sender="sekretariat@grundschule.example", needles=["15,00"],
    )
    commitment = commitments.propose(
        kind=KIND_PAYMENT, payload={"amount_cents": 1500}, extraction_run_id=run.id
    )
    with evidence.db.write() as connection:
        connection.execute("UPDATE events SET raw = ? WHERE id = ?", (b"", event.id))

    source = source_of(evidence, commitments.get(commitment.id))

    assert TEXT_SOURCE_GONE in source and "grundschule.example" in source


# --- CQ 3: что изменилось между письмами --------------------------------------


def test_what_changed_between_the_two_letters(household) -> None:
    commitments, _, _, approvals = household
    first = commitments.propose(
        kind=KIND_EVENT,
        payload={"title": "Экскурсия 3b", "start": "2026-09-12T07:45:00",
                 "location": "Haupteingang"},
    )
    confirmed(commitments, approvals, first.id)
    second = commitments.propose(
        kind=KIND_EVENT,
        payload={"title": "Экскурсия 3b", "start": "2026-09-19T07:45:00",
                 "location": "Haupteingang"},
        supersedes=first.id,
    )

    delta = version_delta(commitments, second.id)

    assert delta == {"start": ("2026-09-12T07:45:00", "2026-09-19T07:45:00")}
    assert version_delta(commitments, first.id) == {}, "у первой версии дельты нет"


def test_the_chain_reads_back_to_the_first_version(household) -> None:
    commitments, _, _, _ = household
    first = commitments.propose(kind=KIND_EVENT, payload={"start": "2026-09-12"})
    second = commitments.propose(
        kind=KIND_EVENT, payload={"start": "2026-09-19"}, supersedes=first.id
    )
    third = commitments.propose(
        kind=KIND_EVENT, payload={"start": "2026-09-26"}, supersedes=second.id
    )

    chain = commitments.chain(third.id)

    assert [item.payload["start"] for item in chain] == [
        "2026-09-12", "2026-09-19", "2026-09-26"
    ]
    assert commitments.latest_version_of(first.id).id == third.id

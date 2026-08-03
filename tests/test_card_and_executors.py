"""Карточка и исполнители Фазы 1: что человек видит и что происходит после ✅."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from hermes_cloud.core.db import open_database
from hermes_cloud.execute.ics import (
    build_event,
    escape_text,
    filename_for,
    render_ics,
)
from hermes_cloud.execute.reminder import ReminderStore, due_timestamp
from hermes_cloud.runner.card import (
    ACTION_CONFIRM,
    ACTION_EDIT,
    ACTION_REJECT,
    KIND_ICS,
    KIND_REMINDER,
    render_card,
)
from hermes_cloud.runner.extraction import (
    ExtractionResult,
    Money,
    OriginalSenderHint,
)


def result(**overrides) -> ExtractionResult:
    params = {
        "kind": "payment",
        "title": "Экскурсия 12.09 — взнос 15 €",
        "summary": "Класс 3b едет на экскурсию. Взнос 15 € до 8 сентября.",
        "source_language": "de",
        "action_required": True,
        "due_date": date(2026, 9, 8),
        "amount": Money(amount_cents=1500, currency="EUR"),
        "confidence": 0.92,
    }
    params.update(overrides)
    return ExtractionResult(**params)


# --- карточка ---------------------------------------------------------------


def test_payment_card_shows_amount_and_deadline_verbatim() -> None:
    card = render_card(result(), approval_id="a1", code="0123456789abcdef")

    assert "15,00 EUR" in card.text
    assert "08.09.2026" in card.text
    assert "0123456789abcdef" in card.text
    assert card.proposal["kind"] == KIND_REMINDER
    assert card.proposal["amount_cents"] == 1500


def test_event_with_start_becomes_a_calendar_proposal() -> None:
    card = render_card(
        result(
            kind="event",
            event_start=datetime(2026, 9, 12, 7, 45, tzinfo=UTC),
            location="Haupteingang",
        ),
        approval_id="a2",
    )

    assert card.proposal["kind"] == KIND_ICS
    assert "12.09.2026 07:45" in card.text
    assert "Haupteingang" in card.text


def test_info_card_has_no_buttons() -> None:
    card = render_card(result(kind="info", action_required=False), approval_id="a3")

    assert card.actionable is False
    assert card.proposal is None
    assert "Действий не требуется" in card.text


def test_spam_card_says_nothing_to_do() -> None:
    card = render_card(result(kind="spam", action_required=False), approval_id="a4")

    assert card.actionable is False
    assert "рекламу" in card.text


def test_buttons_carry_the_id_and_never_the_code() -> None:
    card = render_card(result(), approval_id="a5", code="0123456789abcdef")

    assert [button.action for button in card.buttons] == [
        ACTION_CONFIRM, ACTION_EDIT, ACTION_REJECT
    ]
    for button in card.buttons:
        assert button.callback_data.endswith(":a5")
        assert "0123456789abcdef" not in button.callback_data


def test_low_confidence_only_changes_the_rendering() -> None:
    low = render_card(result(confidence=0.4), approval_id="a6")
    high = render_card(result(confidence=0.95), approval_id="a6")

    assert "Проверьте дату и сумму" in low.text
    assert "Проверьте дату и сумму" not in high.text
    # Предложение и кнопки не зависят от уверенности: автоисполнения нет.
    assert low.proposal == high.proposal
    assert len(low.buttons) == len(high.buttons) == 3


def test_original_sender_is_a_separate_line() -> None:
    card = render_card(
        result(original_sender=OriginalSenderHint(
            email="sekretariat@grundschule.example", name="Sekretariat"
        )),
        approval_id="a7",
    )

    assert "Отправитель: Sekretariat <sekretariat@grundschule.example>" in card.text


def test_actionable_letter_without_any_date_gets_no_proposal() -> None:
    card = render_card(result(kind="task", due_date=None, amount=None), approval_id="a8")

    assert card.proposal is None and card.actionable is False


# --- напоминания ------------------------------------------------------------


@pytest.fixture()
def reminders(tmp_path: Path) -> ReminderStore:
    return ReminderStore(open_database(tmp_path / "hermes.db"))


def test_reminder_is_delivered_once(reminders: ReminderStore) -> None:
    created = reminders.create(
        chat="990000001", text="оплатить 15 EUR", due_at=2000.0, now=1000.0
    )

    assert reminders.claim_due(now=1999.0) is None, "раньше срока не доставляем"
    claimed = reminders.claim_due(now=2001.0)
    assert claimed is not None and claimed.id == created.id
    assert reminders.claim_due(now=2002.0) is None, "аренда держит напоминание"

    reminders.mark_delivered(created.id, now=2003.0)
    assert reminders.claim_due(now=2100.0) is None
    assert reminders.get(created.id).status == "done"


def test_crashed_delivery_is_retried_after_the_lease(reminders: ReminderStore) -> None:
    created = reminders.create(chat="990000001", text="x", due_at=2000.0, now=1000.0)

    reminders.claim_due(now=2001.0, lease_seconds=60)
    assert reminders.claim_due(now=2030.0) is None
    retried = reminders.claim_due(now=2062.0)
    assert retried is not None and retried.id == created.id
    assert retried.attempts == 2


def test_cancelled_reminder_is_never_delivered(reminders: ReminderStore) -> None:
    created = reminders.create(chat="990000001", text="x", due_at=2000.0, now=1000.0)

    assert reminders.cancel(created.id, now=1500.0) is True
    assert reminders.claim_due(now=2001.0) is None


def test_date_without_time_fires_in_the_morning() -> None:
    stamp = due_timestamp(date(2026, 9, 8))
    assert datetime.fromtimestamp(stamp, UTC).hour == 9


# --- ICS --------------------------------------------------------------------


def test_ics_is_well_formed() -> None:
    event = build_event(
        approval_id="a9",
        title="Klassenfahrt 3b",
        start=datetime(2026, 9, 12, 7, 45, tzinfo=UTC),
        description="Treffpunkt Haupteingang",
        location="Musterweg 1",
    )
    text = render_ics(event, now=datetime(2026, 9, 1, 12, 0, tzinfo=UTC))

    assert text.startswith("BEGIN:VCALENDAR\r\n")
    assert text.endswith("END:VCALENDAR\r\n")
    assert "\r\n" in text and "\n\n" not in text
    assert "UID:a9@hermes-cloud.invalid" in text
    assert "DTSTART:20260912T074500Z" in text
    assert "DTEND:20260912T084500Z" in text, "по умолчанию событие длится час"
    assert "SUMMARY:Klassenfahrt 3b" in text


def test_ics_uid_is_deterministic_per_approval() -> None:
    """Повторная генерация обновляет событие, а не создаёт второе."""
    first = build_event(approval_id="a10", title="X",
                        start=datetime(2026, 9, 12, tzinfo=UTC))
    second = build_event(approval_id="a10", title="X (исправлено)",
                         start=datetime(2026, 9, 13, tzinfo=UTC))

    assert first.uid == second.uid


def test_ics_escapes_special_characters() -> None:
    assert escape_text("Klasse 3b; Treffpunkt, 07:45\nBitte") == (
        "Klasse 3b\\; Treffpunkt\\, 07:45\\nBitte"
    )


def test_long_lines_are_folded() -> None:
    event = build_event(
        approval_id="a11", title="X" * 200,
        start=datetime(2026, 9, 12, tzinfo=UTC),
    )
    lines = render_ics(event).split("\r\n")

    assert all(len(line) <= 75 for line in lines)
    assert any(line.startswith(" ") for line in lines), "перенос строки по RFC 5545"


def test_filename_is_safe() -> None:
    assert filename_for("Klassenfahrt 3b / 12.09") == "Klassenfahrt_3b___12_09.ics"
    unsafe = filename_for("../../etc/passwd")
    assert "/" not in unsafe and ".." not in unsafe
    assert unsafe.endswith(".ics")
    assert filename_for("") == "event.ics"

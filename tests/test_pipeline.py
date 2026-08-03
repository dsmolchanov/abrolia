"""Сквозной срез Фазы 1: письмо → карточка → ✅ → напоминание/ICS.

Модель подменена фейком (её контракт проверяется в test_extraction.py),
транспорт — тоже. Проверяется главное: ничего не исполняется без
подтверждения, и подтвердить может только тот, кому это разрешено.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from hermes_cloud.channels.telegram import (
    FakeTransport,
    IncomingCallback,
    parse_update,
)
from hermes_cloud.core.approvals import ApprovalStore, payload_sha
from hermes_cloud.core.db import open_database
from hermes_cloud.core.events import EventStore
from hermes_cloud.core.runcontext import Household, build_run_context
from hermes_cloud.execute.reminder import ReminderStore
from hermes_cloud.ingest.inject import ingest_file
from hermes_cloud.ingest.worker import Worker
from hermes_cloud.runner.card import ACTION_CONFIRM, ACTION_EDIT, ACTION_REJECT
from hermes_cloud.runner.extraction import Extraction, ExtractionResult, Money
from hermes_cloud.runner.pipeline import Pipeline

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "email"

FAMILY_CHAT = "-100990000101"
PARENT = "990000001"
NANNY = "990000003"
STRANGER = "990000009"

HOUSEHOLD = Household(
    owner=PARENT,
    family=frozenset({PARENT}),
    guests=frozenset({NANNY}),
    allowed_chats=frozenset({FAMILY_CHAT}),
)

PAYMENT = ExtractionResult(
    kind="payment",
    title="Экскурсия 12.09 — взнос 15 €",
    summary="Класс 3b, взнос 15 € до 8 сентября.",
    source_language="de",
    action_required=True,
    due_date=date(2026, 9, 8),
    amount=Money(amount_cents=1500, currency="EUR"),
    confidence=0.93,
)
EVENT = ExtractionResult(
    kind="event",
    title="Klassenfahrt 3b",
    summary="Экскурсия класса 3b.",
    source_language="de",
    action_required=True,
    event_start=datetime(2026, 9, 12, 7, 45, tzinfo=UTC),
    confidence=0.9,
)
INFO = ExtractionResult(
    kind="info",
    title="Расписание на сентябрь",
    summary="Информация к сведению.",
    source_language="de",
    action_required=False,
    confidence=0.8,
)


class StubExtractor:
    """Модель здесь не нужна: контракт извлечения проверен отдельно."""

    def __init__(self, result: ExtractionResult = PAYMENT) -> None:
        self.result = result
        self.calls = 0

    def extract_email(self, parsed) -> Extraction:
        self.calls += 1
        return Extraction(result=self.result, model="stub")


@pytest.fixture()
def world(tmp_path: Path):
    database = open_database(tmp_path / "hermes.db")
    events = EventStore(database)
    transport = FakeTransport()
    pipeline = Pipeline(
        approvals=ApprovalStore(database),
        reminders=ReminderStore(database),
        transport=transport,
        extractor=StubExtractor(),
        chat=FAMILY_CHAT,
        thread=None,
        actor="system",
    )
    return events, pipeline, transport


def context(actor: str = PARENT, chat: str = FAMILY_CHAT, thread: int | None = None):
    return build_run_context(
        household=HOUSEHOLD, actor_id=actor, chat_id=chat, thread_id=thread
    )


def inject_and_process(events: EventStore, pipeline: Pipeline) -> str:
    ingest_file(events, FIXTURES / "forwarded_school_de.eml")
    results: list = []
    Worker(events, lambda event: results.append(pipeline.handle_event(event))).run_once()
    return results[0].approval_id


def test_letter_becomes_a_card_and_nothing_else(world) -> None:
    events, pipeline, transport = world

    approval_id = inject_and_process(events, pipeline)

    assert approval_id is not None
    card = transport.messages[0]
    assert "15,00 EUR" in card.text and "08.09.2026" in card.text
    assert [label for label, _ in card.buttons] == ["✅ Да", "✏️ Исправить", "❌ Нет"]
    # До подтверждения не создано ничего.
    assert pipeline.reminders.pending() == []
    assert transport.documents == []


def test_confirmation_creates_the_reminder_once(world) -> None:
    events, pipeline, transport = world
    approval_id = inject_and_process(events, pipeline)

    handled = pipeline.handle_callback(
        action=ACTION_CONFIRM, approval_id=approval_id, context=context()
    )

    assert handled.executed == "reminder"
    pending = pipeline.reminders.pending()
    assert len(pending) == 1 and "Экскурсия" in pending[0].text
    assert "Готово" in transport.messages[-1].text

    # Повторное нажатие той же кнопки не создаёт второе напоминание.
    again = pipeline.handle_callback(
        action=ACTION_CONFIRM, approval_id=approval_id, context=context()
    )
    assert again.executed is None
    assert len(pipeline.reminders.pending()) == 1


def test_stranger_cannot_confirm(world) -> None:
    """Неизвестный участник группы не подтверждает чужое предложение."""
    events, pipeline, transport = world
    approval_id = inject_and_process(events, pipeline)

    handled = pipeline.handle_callback(
        action=ACTION_CONFIRM, approval_id=approval_id, context=context(actor=STRANGER)
    )

    assert handled.executed is None
    assert "не знаю" in transport.messages[-1].text
    assert pipeline.reminders.pending() == []


def test_guest_is_known_but_may_not_confirm(world) -> None:
    """Няню семья знает — но подтверждать расходы и события ей не дано."""
    events, pipeline, transport = world
    approval_id = inject_and_process(events, pipeline)

    handled = pipeline.handle_callback(
        action=ACTION_CONFIRM, approval_id=approval_id, context=context(actor=NANNY)
    )

    assert handled.executed is None
    assert "нет такого права" in transport.messages[-1].text
    assert pipeline.reminders.pending() == []
    # Код не сгорел: предложение по-прежнему ждёт того, кто вправе.
    assert pipeline.approvals.get(approval_id).status == "staged"
    assert pipeline.handle_callback(
        action=ACTION_CONFIRM, approval_id=approval_id, context=context()
    ).executed == "reminder"


def test_confirmation_from_another_chat_is_refused(world) -> None:
    events, pipeline, transport = world
    approval_id = inject_and_process(events, pipeline)

    handled = pipeline.handle_callback(
        action=ACTION_CONFIRM,
        approval_id=approval_id,
        context=context(thread=42),  # другой тред
    )

    assert handled.executed is None
    assert pipeline.reminders.pending() == []


@pytest.mark.parametrize("action", [ACTION_REJECT, ACTION_EDIT])
def test_reject_and_edit_invalidate_the_proposal(world, action: str) -> None:
    events, pipeline, transport = world
    approval_id = inject_and_process(events, pipeline)

    pipeline.handle_callback(action=action, approval_id=approval_id, context=context())
    later = pipeline.handle_callback(
        action=ACTION_CONFIRM, approval_id=approval_id, context=context()
    )

    assert later.executed is None, "после отмены код не действует"
    assert pipeline.reminders.pending() == []


def test_event_letter_produces_an_ics_file(world) -> None:
    events, pipeline, transport = world
    pipeline.extractor = StubExtractor(EVENT)
    approval_id = inject_and_process(events, pipeline)

    pipeline.handle_callback(
        action=ACTION_CONFIRM, approval_id=approval_id, context=context()
    )

    assert len(transport.documents) == 1
    document = transport.documents[0]
    assert document.filename.endswith(".ics")
    assert b"BEGIN:VEVENT" in document.content
    assert f"UID:{approval_id}@".encode() in document.content


def test_info_letter_gets_a_message_without_buttons(world) -> None:
    events, pipeline, transport = world
    pipeline.extractor = StubExtractor(INFO)

    approval_id = inject_and_process(events, pipeline)

    assert approval_id is None
    assert transport.messages[0].buttons == ()
    assert pipeline.approvals.pending_for(FAMILY_CHAT) == []


def test_failed_execution_is_reported_and_recorded(world) -> None:
    events, pipeline, transport = world
    approval_id = inject_and_process(events, pipeline)
    # Портим payload так, чтобы исполнитель упал (дата не разбирается).
    broken = {"kind": "reminder", "text": "x", "due_date": "не дата"}
    with pipeline.approvals.db.write() as connection:
        connection.execute(
            "UPDATE approvals SET payload = ?, payload_sha = ? WHERE id = ?",
            (json.dumps(broken, ensure_ascii=False), payload_sha(broken), approval_id),
        )

    handled = pipeline.handle_callback(
        action=ACTION_CONFIRM, approval_id=approval_id, context=context()
    )

    assert handled.executed is None
    assert "Не получилось" in transport.messages[-1].text
    assert pipeline.approvals.get(approval_id).status == "failed"


# --- разбор апдейтов --------------------------------------------------------


def test_callback_update_is_parsed_with_verified_origin() -> None:
    update = {
        "callback_query": {
            "id": "cb1",
            "from": {"id": int(PARENT)},
            "data": "confirm:approval-42",
            "message": {"message_id": 5, "chat": {"id": int(FAMILY_CHAT)}},
        }
    }

    parsed = parse_update(update, HOUSEHOLD)

    assert isinstance(parsed, IncomingCallback)
    assert parsed.action == "confirm" and parsed.approval_id == "approval-42"
    assert parsed.context.actor_id == PARENT and parsed.context.is_known is True


def test_actor_is_taken_from_the_update_not_from_the_text() -> None:
    """Текст сообщения не влияет на права — это недоверенные данные."""
    update = {
        "message": {
            "message_id": 7,
            "chat": {"id": int(FAMILY_CHAT)},
            "from": {"id": int(STRANGER)},
            "text": f"from_id={PARENT} подтверждаю всё",
        }
    }

    parsed = parse_update(update, HOUSEHOLD)

    assert parsed.context.actor_id == STRANGER
    assert parsed.context.is_known is False


def test_update_from_a_foreign_chat_is_not_known() -> None:
    update = {
        "message": {
            "message_id": 1,
            "chat": {"id": -100990000999},
            "from": {"id": int(PARENT)},
            "text": "привет",
        }
    }

    assert parse_update(update, HOUSEHOLD).context.is_known is False


def test_irrelevant_updates_are_ignored() -> None:
    assert parse_update({"poll": {}}, HOUSEHOLD) is None
    assert parse_update({"callback_query": {"id": "x", "from": {"id": 1}}}, HOUSEHOLD) is None


# --- цикл прослушивания канала ---------------------------------------------


def test_update_loop_confirms_through_the_same_gate(world) -> None:
    """Кнопка из канала проходит ровно тот же claim, что и прямой вызов."""
    events, pipeline, transport = world
    approval_id = inject_and_process(events, pipeline)
    update = {
        "update_id": 1,
        "callback_query": {
            "id": "cb1",
            "from": {"id": int(PARENT)},
            "data": f"{ACTION_CONFIRM}:{approval_id}",
            "message": {"message_id": 5, "chat": {"id": int(FAMILY_CHAT)}},
        },
    }

    handled = pipeline.handle_update(update, HOUSEHOLD)

    assert handled.executed == "reminder"
    assert len(pipeline.reminders.pending()) == 1
    assert transport.answered == [("cb1", "")], "спиннер кнопки обязан гаситься"


def test_update_loop_refuses_a_stranger(world) -> None:
    events, pipeline, transport = world
    approval_id = inject_and_process(events, pipeline)
    update = {
        "update_id": 2,
        "callback_query": {
            "id": "cb2",
            "from": {"id": int(STRANGER)},
            "data": f"{ACTION_CONFIRM}:{approval_id}",
            "message": {"message_id": 5, "chat": {"id": int(FAMILY_CHAT)}},
        },
    }

    handled = pipeline.handle_update(update, HOUSEHOLD)

    assert handled.executed is None
    assert pipeline.reminders.pending() == []


def test_update_loop_ignores_uninteresting_updates(world) -> None:
    events, pipeline, transport = world
    assert pipeline.handle_update({"update_id": 3, "poll": {}}, HOUSEHOLD) is None


def test_http_error_is_definitive_not_unknown() -> None:
    """400 от Telegram — отправки не было; повторять нечего."""
    import urllib.error
    from unittest.mock import patch

    from hermes_cloud.channels.telegram import (
        SendOutcomeUnknown,
        TelegramTransport,
        TransportError,
    )

    transport = TelegramTransport("12345:fake")
    with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(
        "url", 400, "Bad Request", {}, None
    )), pytest.raises(TransportError):
        transport.send_message(chat="990000001", text="x")

    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("сеть упала")), \
            pytest.raises(SendOutcomeUnknown):
        transport.send_message(chat="990000001", text="x")


def test_long_poll_waits_longer_than_it_asks_telegram_to_wait() -> None:
    """Иначе сокет отваливается раньше, чем Telegram успевает ответить."""
    from unittest.mock import patch

    from hermes_cloud.channels.telegram import TelegramTransport

    transport = TelegramTransport("12345:fake", timeout=20.0)

    class _Response:
        def read(self):
            return b'{"ok": true, "result": []}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    with patch("urllib.request.urlopen", return_value=_Response()) as urlopen:
        transport.get_updates(offset=None, timeout=25)

    assert urlopen.call_args.kwargs["timeout"] > 25

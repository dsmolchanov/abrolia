"""Опрос ящика семьи: только ярлык, только новое, без тихих перезапусков.

FakeIMAP повторяет ответы сервера настолько, насколько от них зависит логика:
поиск по ярлыку, выдача письма целиком, UIDVALIDITY. Живой Gmail тут не нужен —
проверяется не он, а то, что мы делаем с его ответами.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cloud.core.db import open_database
from hermes_cloud.core.events import EventStore
from hermes_cloud.ingest.gmail_poll import (
    SEEN_LIMIT,
    Cursor,
    GmailPoller,
    MailError,
    UidValidityChanged,
    load_cursor,
    message_id_of,
)

LABEL = "Hermes"


def letter(message_id: str, subject: str = "Elternbeitrag") -> bytes:
    return (
        b"From: Sekretariat <sekretariat@grundschule.example>\r\n"
        + f"Subject: {subject}\r\n".encode()
        + f"Message-ID: <{message_id}>\r\n".encode()
        + b"Content-Type: text/plain; charset=\"utf-8\"\r\n\r\n"
        + b"Bitte ueberweisen Sie 15,00 EUR bis zum 08.09.2026.\r\n"
    )


class FakeIMAP:
    """Ящик, в котором у писем есть ярлыки, а у папки — UIDVALIDITY."""

    def __init__(self, messages=None, *, validity: str = "42") -> None:
        # message: (uid, message_id, labels, raw)
        self.messages = list(messages or [])
        self.validity = validity
        self.logged_out = False
        self.searches: list[str] = []
        self.fetched: list[str] = []

    # --- то, что вызывает поллер ---
    def list(self):
        return "OK", [b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"']

    def status(self, folder, what):
        return "OK", [f'"{folder}" (UIDVALIDITY {self.validity})'.encode()]

    def select(self, folder, readonly=True):
        return "OK", [b"1"]

    def uid(self, command, *args):
        if command.upper() == "SEARCH":
            query = args[-1]
            self.searches.append(query)
            wanted = query.strip('"').removeprefix("label:")
            found = [
                uid for uid, _mid, labels, _raw in self.messages if wanted in labels
            ]
            return "OK", [b" ".join(uid.encode() for uid in found)]
        if command.lower() == "fetch":
            uid = args[0].decode() if isinstance(args[0], bytes) else str(args[0])
            self.fetched.append(uid)
            for stored_uid, _mid, _labels, raw in self.messages:
                if stored_uid == uid:
                    return "OK", [(b"1 (BODY[] {%d}" % len(raw), raw)]
            return "NO", []
        raise AssertionError(f"неожиданная команда {command}")

    def logout(self):
        self.logged_out = True


@pytest.fixture()
def world(tmp_path: Path):
    events = EventStore(open_database(tmp_path / "hermes.db"))
    return events


def poller(events: EventStore, mailbox: FakeIMAP) -> GmailPoller:
    return GmailPoller(
        events, address="family@example.com", app_password="secret",
        label=LABEL, connector=lambda: mailbox,
    )


# --- ярлык как граница --------------------------------------------------------


def test_only_labelled_mail_is_ever_fetched(world) -> None:
    """Письмо без ярлыка не покидает Gmail: мы его даже не запрашиваем."""
    mailbox = FakeIMAP([
        ("1", "school@example", ["Hermes"], letter("school@example")),
        ("2", "private@example", ["Personal"], letter("private@example", "Личное")),
    ])

    result = poller(world, mailbox).poll()

    assert result.accepted == 1
    assert mailbox.fetched == ["1"], "второе письмо не скачивалось"
    assert world.by_external_id("eml:<school@example>") is not None
    assert world.by_external_id("eml:<private@example>") is None


def test_the_search_asks_gmail_for_the_label_itself(world) -> None:
    mailbox = FakeIMAP([("1", "a@example", ["Hermes"], letter("a@example"))])

    poller(world, mailbox).poll()

    assert mailbox.searches == ['"label:Hermes"']


# --- курсор -------------------------------------------------------------------


def test_the_same_letter_is_ingested_once(world) -> None:
    mailbox = FakeIMAP([("1", "a@example", ["Hermes"], letter("a@example"))])
    poll = poller(world, mailbox)

    first = poll.poll()
    second = poll.poll()

    assert (first.accepted, second.accepted) == (1, 0)
    assert second.skipped == 1
    assert world.counts() == {"received": 1}


def test_a_new_letter_after_a_poll_is_picked_up(world) -> None:
    mailbox = FakeIMAP([("1", "a@example", ["Hermes"], letter("a@example"))])
    poll = poller(world, mailbox)
    poll.poll()

    mailbox.messages.append(("2", "b@example", ["Hermes"], letter("b@example")))
    result = poll.poll()

    assert result.accepted == 1
    assert world.by_external_id("eml:<b@example>") is not None


def test_the_cursor_is_bounded(world) -> None:
    """Список виденных не растёт вечно — иначе он однажды станет базой данных."""
    mailbox = FakeIMAP([
        (str(index), f"m{index}@example", ["Hermes"], letter(f"m{index}@example"))
        for index in range(1, 60)
    ])
    poll = poller(world, mailbox)
    poll.poll()

    cursor = load_cursor(world.db)

    assert 0 < len(cursor.seen) <= SEEN_LIMIT


def test_the_cursor_survives_a_restart(world, tmp_path: Path) -> None:
    mailbox = FakeIMAP([("1", "a@example", ["Hermes"], letter("a@example"))])
    poller(world, mailbox).poll()

    # Новый процесс, та же база.
    restarted = EventStore(open_database(tmp_path / "hermes.db"))
    result = poller(restarted, mailbox).poll()

    assert result.accepted == 0, "перезапуск не переигрывает почту"


# --- UIDVALIDITY --------------------------------------------------------------


def test_a_renumbered_mailbox_is_an_explicit_error(world) -> None:
    """Тихий сброс курсора завалил бы семью карточками из прошлого года."""
    mailbox = FakeIMAP([("1", "a@example", ["Hermes"], letter("a@example"))])
    poll = poller(world, mailbox)
    poll.poll()

    mailbox.validity = "77"

    with pytest.raises(UidValidityChanged):
        poll.poll()
    assert world.counts() == {"received": 1}, "почта не переиграна"


def test_rebaselining_is_deliberate_and_ingests_nothing(world) -> None:
    mailbox = FakeIMAP([("1", "a@example", ["Hermes"], letter("a@example"))])
    poll = poller(world, mailbox)
    poll.poll()
    mailbox.validity = "77"

    result = poll.rebaseline()

    assert result.accepted == 0
    assert load_cursor(world.db).uidvalidity == "77"
    assert poll.poll().accepted == 0, "после перебазирования старое не всплывает"


def test_an_unknown_uidvalidity_is_not_a_change(world) -> None:
    """Сервер промолчал — это не повод считать, что ящик перенумерован."""
    mailbox = FakeIMAP([("1", "a@example", ["Hermes"], letter("a@example"))])
    mailbox.status = lambda *_: ("NO", [])  # type: ignore[assignment]

    assert poller(world, mailbox).poll().accepted == 1


# --- первый запуск ------------------------------------------------------------


def test_the_first_run_marks_history_as_seen(world) -> None:
    """Подключение к живому ящику не должно превратиться в сотню карточек."""
    mailbox = FakeIMAP([
        (str(index), f"old{index}@example", ["Hermes"], letter(f"old{index}@example"))
        for index in range(1, 20)
    ])
    poll = poller(world, mailbox)

    baseline = poll.baseline()

    assert baseline.accepted == 0
    assert world.counts() == {}
    mailbox.messages.append(("99", "new@example", ["Hermes"], letter("new@example")))
    assert poll.poll().accepted == 1, "новое после базиса — уже новости"


# --- отказы -------------------------------------------------------------------


def test_a_failed_search_is_not_read_as_an_empty_mailbox(world) -> None:
    mailbox = FakeIMAP([("1", "a@example", ["Hermes"], letter("a@example"))])
    mailbox.uid = lambda *_args: ("NO", [])  # type: ignore[assignment]

    with pytest.raises(MailError):
        poller(world, mailbox).poll()


def test_missing_credentials_are_an_explicit_error(world) -> None:
    from hermes_cloud.ingest.gmail_poll import connect

    with pytest.raises(MailError):
        connect("", "")


def test_a_letter_without_a_message_id_is_skipped(world) -> None:
    """Без Message-ID нет идентичности — повтор было бы не отличить от нового."""
    raw = b"From: x@example\r\nSubject: no id\r\n\r\ntext\r\n"
    mailbox = FakeIMAP([("1", "", ["Hermes"], raw)])

    result = poller(world, mailbox).poll()

    assert result.accepted == 0
    assert message_id_of(raw) == ""


def test_the_connection_is_closed_even_when_the_poll_fails(world) -> None:
    mailbox = FakeIMAP([("1", "a@example", ["Hermes"], letter("a@example"))])
    mailbox.uid = lambda *_args: ("NO", [])  # type: ignore[assignment]

    with pytest.raises(MailError):
        poller(world, mailbox).poll()

    assert mailbox.logged_out is True


def test_the_same_letter_gives_the_same_result_whichever_door_it_came_through(
    tmp_path: Path,
) -> None:
    """Вход не влияет на результат: письмо одно — карточка одна и та же."""
    from hermes_cloud.channels.telegram import FakeTransport
    from hermes_cloud.core.approvals import ApprovalStore
    from hermes_cloud.execute.reminder import ReminderStore
    from hermes_cloud.ingest.inject import ingest_file
    from hermes_cloud.ingest.worker import Worker
    from hermes_cloud.runner.pipeline import Pipeline

    fixture = Path(__file__).resolve().parent / "fixtures" / "email" / "direct_invoice_it.eml"
    raw = fixture.read_bytes()

    def run(door: str) -> tuple[str, dict]:
        from test_pipeline import StubExtractor

        database = open_database(tmp_path / f"{door}.db")
        events = EventStore(database)
        transport = FakeTransport()
        pipeline = Pipeline(
            approvals=ApprovalStore(database), reminders=ReminderStore(database),
            transport=transport, extractor=StubExtractor(), chat="-100990000101",
        )
        if door == "inject":
            ingest_file(events, fixture)
        else:
            mailbox = FakeIMAP([("1", "x@example", ["Hermes"], raw)])
            GmailPoller(
                events, address="family@example.com", app_password="secret",
                label=LABEL, connector=lambda: mailbox,
            ).poll()
        handled: list = []
        Worker(events, lambda event: handled.append(pipeline.handle_event(event))).run_once()
        approval = pipeline.approvals.get(handled[0].approval_id)
        payload = {key: value for key, value in approval.payload.items()
                   if key != "commitment_id"}
        return transport.messages[0].text, payload

    injected_text, injected_payload = run("inject")
    polled_text, polled_payload = run("gmail")

    assert injected_payload == polled_payload
    # Отличается только одноразовый код — он на то и одноразовый.
    assert _without_code(injected_text) == _without_code(polled_text)


def _without_code(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.startswith("Код подтверждения")
    )


def test_cursor_json_round_trip() -> None:
    cursor = Cursor(uidvalidity="42", seen=["<a@example>", "<b@example>"])

    assert Cursor.from_json(cursor.to_json()) == cursor
    assert Cursor.from_json(None).seen == []

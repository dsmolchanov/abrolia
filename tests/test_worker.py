"""Воркер и CLI: долговечность в присутствии падающего обработчика."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cloud import cli
from hermes_cloud.core.db import open_database
from hermes_cloud.core.events import (
    STATUS_DLQ,
    STATUS_DONE,
    STATUS_RECEIVED,
    Event,
    EventStore,
)
from hermes_cloud.ingest.worker import Worker

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "email"


@pytest.fixture()
def store(tmp_path: Path) -> EventStore:
    return EventStore(open_database(tmp_path / "hermes.db"))


def test_worker_processes_and_marks_done(store: EventStore) -> None:
    seen: list[Event] = []
    store.append(source="inject", external_id="eml:w1", raw=b"letter")

    processed = Worker(store, seen.append).run_once()

    assert processed is not None and processed.ok is True
    assert processed.event.status == STATUS_DONE
    assert [event.raw for event in seen] == [b"letter"]


def test_empty_queue_returns_none(store: EventStore) -> None:
    assert Worker(store, lambda event: None).run_once() is None


def test_handler_failure_retries_then_goes_to_dlq(store: EventStore) -> None:
    accepted = store.append(source="inject", external_id="eml:w2", raw=b"x")

    def always_fails(event: Event) -> None:
        raise ValueError("модель недоступна")

    worker = Worker(store, always_fails, max_attempts=3)
    statuses = [worker.run_once().event.status for _ in range(3)]

    assert statuses == [STATUS_RECEIVED, STATUS_RECEIVED, STATUS_DLQ]
    assert worker.run_once() is None, "из DLQ событие само в работу не уходит"
    event = store.get(accepted.event.id)
    assert "модель недоступна" in event.last_error


def test_transient_failure_is_retried_and_then_succeeds(store: EventStore) -> None:
    store.append(source="inject", external_id="eml:w3", raw=b"x")
    attempts = {"n": 0}

    def flaky(event: Event) -> None:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise TimeoutError("сеть моргнула")

    worker = Worker(store, flaky, max_attempts=3)
    assert worker.run_once().ok is False
    second = worker.run_once()
    assert second.ok is True
    assert second.event.status == STATUS_DONE
    assert second.event.attempts == 1, "счётчик попыток остаётся историей события"


def test_drain_processes_everything_available(store: EventStore) -> None:
    for index in range(3):
        store.append(
            source="inject", external_id=f"eml:d{index}", raw=b"x",
            context_key=f"thread-{index}",
        )

    results = Worker(store, lambda event: None).drain()

    assert [r.ok for r in results] == [True, True, True]
    assert store.counts() == {STATUS_DONE: 3}


def test_handler_logs_do_not_contain_message_content(store: EventStore, caplog) -> None:
    """Логи без содержимого письма — требование data map (S12)."""
    store.append(source="inject", external_id="eml:w4", raw=b"SEHR GEHEIM 15 EUR")

    def fails(event: Event) -> None:
        raise ValueError("boom")

    with caplog.at_level("WARNING"):
        Worker(store, fails).run_once()

    assert "SEHR GEHEIM" not in caplog.text


# --- CLI --------------------------------------------------------------------


def test_cli_inject_replay_and_status(tmp_path: Path, capsys) -> None:
    database = tmp_path / "hermes.db"
    fixture = str(FIXTURES / "forwarded_school_de.eml")

    assert cli.main(["--db", str(database), "inject-eml", fixture]) == 0
    first = capsys.readouterr().out
    assert "принято" in first
    assert "sekretariat@grundschule.example" in first, "карточка оператора без оригинала"

    assert cli.main(["--db", str(database), "inject-eml", fixture]) == 0
    assert "дубль" in capsys.readouterr().out

    store = EventStore(open_database(database))
    event_id = store.by_external_id(
        "eml:caf1synthetic0001@mail.example.com"
    ).id
    store.mark_failed(event_id, "boom", max_attempts=1)

    assert cli.main(["--db", str(database), "dlq"]) == 0
    assert event_id in capsys.readouterr().out

    assert cli.main(["--db", str(database), "replay", event_id]) == 0
    assert "возвращено в очередь" in capsys.readouterr().out

    assert cli.main(["--db", str(database), "status"]) == 0
    assert STATUS_RECEIVED in capsys.readouterr().out


def test_cli_replay_of_unknown_event_fails(tmp_path: Path, capsys) -> None:
    assert cli.main(["--db", str(tmp_path / "h.db"), "replay", "nope"]) == 1
    assert "не найдено" in capsys.readouterr().err

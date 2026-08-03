"""Долговечность ingress: событие не теряется, не двоится и не застревает.

Проверяется то, ради чего вообще заведён SQLite вместо файлов-журналов:
пережитый kill, дедупликация по `external_id`, аренда вместо «забрал
насовсем», FIFO внутри одной цепочки, DLQ после N неудач и ручной replay.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from hermes_cloud.core.db import open_database
from hermes_cloud.core.events import (
    STATUS_DLQ,
    STATUS_PROCESSING,
    STATUS_RECEIVED,
    EventStore,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def store(tmp_path: Path) -> EventStore:
    return EventStore(open_database(tmp_path / "hermes.db"))


def test_append_is_idempotent_by_external_id(store: EventStore) -> None:
    first = store.append(source="inject", external_id="eml:1", raw=b"letter")
    second = store.append(source="inject", external_id="eml:1", raw=b"letter (retry)")

    assert first.created is True
    assert second.created is False
    assert second.event.id == first.event.id
    assert store.counts() == {STATUS_RECEIVED: 1}
    # Повтор не перезаписывает исходник: первое принятое письмо — истина.
    assert store.get(first.event.id).raw == b"letter"


def test_event_survives_a_killed_process(tmp_path: Path) -> None:
    """kill -9 сразу после приёма: письмо на диске, обработка продолжится."""
    database_path = tmp_path / "hermes.db"
    script = textwrap.dedent(
        f"""
        import os, signal, sys
        sys.path.insert(0, {str(REPO_ROOT)!r})
        from hermes_cloud.core.db import open_database
        from hermes_cloud.core.events import EventStore

        store = EventStore(open_database({str(database_path)!r}))
        store.append(source="inject", external_id="eml:killed", raw=b"school letter")
        # Приём вернул управление => данные должны быть на диске. Убиваем
        # процесс до того, как он успеет что-либо обработать.
        os.kill(os.getpid(), signal.SIGKILL)
        """
    )
    completed = subprocess.run([sys.executable, "-c", script], capture_output=True)
    assert completed.returncode == -signal.SIGKILL, completed.stderr.decode()

    store = EventStore(open_database(database_path))
    event = store.by_external_id("eml:killed")
    assert event is not None, "принятое событие исчезло вместе с процессом"
    assert event.status == STATUS_RECEIVED
    assert event.raw == b"school letter"
    assert store.lease("worker-2").id == event.id


def test_abandoned_lease_is_reclaimed_only_after_expiry(store: EventStore) -> None:
    accepted = store.append(source="inject", external_id="eml:2", raw=b"x")

    leased = store.lease("worker-1", lease_seconds=60, now=1_000.0)
    assert leased.id == accepted.event.id
    assert leased.status == STATUS_PROCESSING

    # Воркер «завис»: пока аренда жива, событие никому не отдаётся.
    assert store.lease("worker-2", now=1_030.0) is None
    # Аренда истекла — событие подбирает следующий воркер.
    reclaimed = store.lease("worker-2", now=1_061.0)
    assert reclaimed is not None
    assert reclaimed.id == accepted.event.id
    assert reclaimed.leased_by == "worker-2"


def test_fifo_within_a_context_and_parallel_across_contexts(store: EventStore) -> None:
    first = store.append(
        source="inject", external_id="eml:a1", raw=b"1",
        context_key="thread-a", received_at=100.0,
    )
    second = store.append(
        source="inject", external_id="eml:a2", raw=b"2",
        context_key="thread-a", received_at=101.0,
    )
    other = store.append(
        source="inject", external_id="eml:b1", raw=b"3",
        context_key="thread-b", received_at=102.0,
    )

    assert store.lease("w1", now=200.0).id == first.event.id
    # Второе письмо той же цепочки ждёт: порядок внутри треда сохраняется.
    assert store.lease("w2", now=200.0).id == other.event.id
    assert store.lease("w3", now=200.0) is None

    store.mark_done(first.event.id)
    assert store.lease("w3", now=201.0).id == second.event.id


def test_failures_go_to_dlq_and_replay_returns_them(store: EventStore) -> None:
    accepted = store.append(source="inject", external_id="eml:3", raw=b"x")
    event_id = accepted.event.id

    for attempt in range(1, 3):
        assert store.lease(f"w{attempt}") is not None
        event = store.mark_failed(event_id, "extraction failed", max_attempts=3)
        assert event.status == STATUS_RECEIVED
        assert event.attempts == attempt

    assert store.lease("w3") is not None
    event = store.mark_failed(event_id, "extraction failed", max_attempts=3)
    assert event.status == STATUS_DLQ
    assert store.lease("w4") is None, "событие из DLQ не должно уходить в работу само"
    assert [e.id for e in store.dead_letters()] == [event_id]

    replayed = store.replay(event_id)
    assert replayed.status == STATUS_RECEIVED
    assert replayed.attempts == 0
    assert replayed.last_error is None
    assert store.lease("w5").id == event_id


def test_oldest_event_is_leased_first(store: EventStore) -> None:
    later = store.append(
        source="inject", external_id="eml:late", raw=b"b", received_at=200.0
    )
    earlier = store.append(
        source="inject", external_id="eml:early", raw=b"a", received_at=100.0
    )

    assert store.lease("w1", now=300.0).id == earlier.event.id
    assert store.lease("w2", now=300.0).id == later.event.id

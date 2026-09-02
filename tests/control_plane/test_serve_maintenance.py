"""The periodic maintenance schedule inside `serve`.

These tasks shared one `try` block and the archive was last, so a task that
failed persistently jumped to the outer handler before the archive branch was
ever reached. The service stayed up, nothing looked wrong, and 26 hours later
the backup was stale again — the exact defect O0a describes, arriving through
a different door.
"""

from __future__ import annotations

import sqlite3
from io import StringIO
from types import SimpleNamespace

import pytest

from control_plane.cli import _run_maintenance
from control_plane.observability import StructuredLogger

NOW = 1800000000.0


class _Boom:
    """A maintenance task that fails every single time it is asked."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    def __call__(self, *_args, **_kwargs):
        self.calls += 1
        raise self.error


def _active(monkeypatch, *, retention_error: BaseException | None = None):
    archive_calls: list[float] = []
    retention = _Boom(retention_error) if retention_error else None

    active = SimpleNamespace(
        config=SimpleNamespace(runtime_provider="dry-run-runtime", backup_key=b"k" * 32),
        retention=SimpleNamespace(
            run=retention if retention else (lambda **_kw: None)
        ),
        database=SimpleNamespace(path="/tmp/does-not-matter.db"),
    )
    monkeypatch.setattr(
        "control_plane.cli._take_periodic_archive",
        lambda _a, _l, *, now: archive_calls.append(now),
    )
    monkeypatch.setattr(
        "control_plane.cli._resume_deletions_if_running",
        lambda *_a, **_kw: None,
    )
    return active, archive_calls, retention


def test_a_persistently_failing_task_cannot_starve_the_archive(monkeypatch) -> None:
    """The regression the review asked for, and the reason for the change.

    Retention raising on every tick used to mean the archive branch was never
    reached at all. The service kept serving, so nothing surfaced it until the
    backup went stale a day later.
    """
    active, archive_calls, retention = _active(
        monkeypatch, retention_error=RuntimeError("retention is broken")
    )
    logger = StructuredLogger(StringIO())
    schedule: dict[str, float] = {}

    # Many ticks, each far enough apart that every task is due.
    for tick in range(5):
        _run_maintenance(active, logger, schedule, now=NOW + tick * 100_000)

    assert retention.calls == 5, "the failing task should still be attempted"
    assert len(archive_calls) == 5, (
        "the archive was starved by a task that fails before it — the defect "
        "this isolation exists to prevent"
    )


def test_a_failing_task_does_not_retry_on_every_tick(monkeypatch) -> None:
    """Its schedule advances BEFORE the call.

    Leaving it unadvanced is what made one broken task consume every 0.5s
    iteration, which is how it crowded out everything after it.
    """
    active, _archive, retention = _active(
        monkeypatch, retention_error=RuntimeError("retention is broken")
    )
    logger = StructuredLogger(StringIO())
    schedule: dict[str, float] = {}

    _run_maintenance(active, logger, schedule, now=NOW)
    for tick in range(1, 20):
        _run_maintenance(active, logger, schedule, now=NOW + tick)

    assert retention.calls == 1, (
        "a failing task retried on every tick instead of waiting for its "
        "interval"
    )


def test_every_task_failure_is_named_in_the_log(monkeypatch) -> None:
    active, _archive, _retention = _active(
        monkeypatch, retention_error=RuntimeError("retention is broken")
    )
    stream = StringIO()
    _run_maintenance(active, StructuredLogger(stream), {}, now=NOW)
    written = stream.getvalue()
    assert "maintenance_task_failed" in written
    assert "retention:RuntimeError" in written, (
        "the log must say WHICH task failed, not merely that one did"
    )


@pytest.mark.parametrize(
    "error",
    [
        sqlite3.OperationalError("database is locked"),
        sqlite3.DatabaseError("malformed"),
        OSError("No space left on device"),
        RuntimeError("something unforeseen"),
    ],
    ids=["sqlite-operational", "sqlite-database", "oserror", "unforeseen"],
)
def test_an_archive_failure_of_any_kind_is_recorded_as_failed(
    monkeypatch, error: BaseException
) -> None:
    """The narrow `except (BackupError, OSError)` was the bug.

    `take_periodic_archive` reaches `PRAGMA integrity_check`, a second
    `sqlite3.connect` and `connection.backup()` — all of which raise
    `sqlite3.Error`, which is neither. Those escaped without recording an
    attempt, so `/readyz` kept reporting the previous `written` while nothing
    was being written: exactly the invisibility `backup_writer` exists to end.
    """
    from control_plane import cli

    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "control_plane.backup.take_periodic_archive",
        lambda *_a, **_kw: (_ for _ in ()).throw(error),
    )
    monkeypatch.setattr(
        "control_plane.backup.record_boot_archive_attempt",
        lambda _db, *, outcome, detail, now: recorded.append((outcome, detail)),
    )
    active = SimpleNamespace(
        config=SimpleNamespace(backup_key=b"k" * 32),
        database=SimpleNamespace(path="/tmp/does-not-matter.db"),
    )

    # Must not propagate: a durability failure never takes the service down.
    cli._take_periodic_archive(active, StructuredLogger(StringIO()), now=NOW)

    assert recorded, f"{type(error).__name__} was not recorded as an attempt"
    assert recorded[0][0] == "failed"

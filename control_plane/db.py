"""Separate durable SQLite storage for control-plane metadata."""

from __future__ import annotations

import fcntl
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
BUSY_TIMEOUT_SECONDS = 30.0


class ProcessAlreadyRunning(RuntimeError):
    """A second control-plane writer attempted to start on the pilot DB."""


def new_id() -> str:
    return str(uuid.uuid4())


class ControlPlaneDatabase:
    """One serialized in-process writer over a dedicated control-plane DB."""

    def __init__(
        self,
        path: Path | str,
        *,
        timeout: float = BUSY_TIMEOUT_SECONDS,
        preserve_journal_mode: bool = False,
    ) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self.preserve_journal_mode = preserve_journal_mode
        self._connection: sqlite3.Connection | None = None
        self._mutex = threading.RLock()
        self._process_lock_file = None

    @property
    def connection(self) -> sqlite3.Connection:
        with self._mutex:
            if self._connection is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                connection = sqlite3.connect(
                    self.path,
                    timeout=self.timeout,
                    isolation_level=None,
                    check_same_thread=False,
                )
                connection.row_factory = sqlite3.Row
                if not self.preserve_journal_mode:
                    # PERSISTENT: this rewrites the database header when the
                    # mode differs and creates -wal/-shm sidecars, so it is a
                    # write. A caller that promises to mutate nothing has to be
                    # able to decline it. `synchronous`, `foreign_keys` and
                    # `busy_timeout` below are per-connection and change nothing
                    # on disk.
                    connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)}")
                self._connection = connection
            return self._connection

    def close(self) -> None:
        with self._mutex:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            self.release_process_lock()

    def __enter__(self) -> ControlPlaneDatabase:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def acquire_process_lock(self) -> None:
        """Explicit startup guard for the one-Machine/one-writer pilot topology."""
        with self._mutex:
            if self._process_lock_file is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self.path.with_name(f"{self.path.name}.writer.lock")
            lock_file = lock_path.open("a+b")
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                lock_file.close()
                raise ProcessAlreadyRunning(
                    f"control-plane writer already owns {lock_path}"
                ) from error
            self._process_lock_file = lock_file

    def release_process_lock(self) -> None:
        with self._mutex:
            if self._process_lock_file is None:
                return
            fcntl.flock(self._process_lock_file.fileno(), fcntl.LOCK_UN)
            self._process_lock_file.close()
            self._process_lock_file = None

    @property
    def worker_pause_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.workers-paused")

    @property
    def workers_paused(self) -> bool:
        return self.worker_pause_path.exists()

    def pause_workers(self, reason: str = "restore requires explicit reconciliation") -> None:
        self.worker_pause_path.write_text(reason + "\n", encoding="utf-8")
        os.chmod(self.worker_pause_path, 0o600)

    def resume_workers(self) -> None:
        with suppress(FileNotFoundError):
            self.worker_pause_path.unlink()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        with self._mutex:
            connection = self.connection
            if connection.in_transaction:
                raise RuntimeError("nested control-plane write transaction")
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")

    def query(self, sql: str, params: dict | tuple = ()) -> list[sqlite3.Row]:
        with self._mutex:
            return list(self.connection.execute(sql, params))

    def query_one(self, sql: str, params: dict | tuple = ()) -> sqlite3.Row | None:
        with self._mutex:
            return self.connection.execute(sql, params).fetchone()

    def migrate(self, directory: Path | None = None) -> list[str]:
        directory = directory or MIGRATIONS_DIR
        connection = self.connection
        with self._mutex:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "name TEXT PRIMARY KEY, applied_at REAL NOT NULL)"
            )
            applied = {
                row["name"]
                for row in connection.execute("SELECT name FROM schema_migrations")
            }
            freshly: list[str] = []
            for script in sorted(directory.glob("*.sql")):
                if script.name in applied:
                    continue
                name_literal = script.name.replace("'", "''")
                body = "\n".join((
                    "BEGIN IMMEDIATE;",
                    script.read_text(encoding="utf-8"),
                    "INSERT INTO schema_migrations (name, applied_at)"
                    f" VALUES ('{name_literal}', {time.time()!r});",
                    "COMMIT;",
                ))
                try:
                    connection.executescript(body)
                except BaseException:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                    raise
                freshly.append(script.name)
            return freshly

    def pragma(self) -> dict[str, object]:
        return {
            "journal_mode": self.query_one("PRAGMA journal_mode")[0],
            "synchronous": self.query_one("PRAGMA synchronous")[0],
            "foreign_keys": self.query_one("PRAGMA foreign_keys")[0],
        }


def open_control_plane_database(path: Path | str) -> ControlPlaneDatabase:
    database = ControlPlaneDatabase(path)
    database.migrate()
    return database

"""Separate durable SQLite storage for control-plane metadata."""

from __future__ import annotations

import fcntl
import os
import sqlite3
import sys
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
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

    def pending_migrations(self, directory: Path | None = None) -> list[str]:
        directory = directory or MIGRATIONS_DIR
        if not self.path.exists():
            return [script.name for script in sorted(directory.glob("*.sql"))]
        applied = {row["name"] for row in self._applied_migrations()}
        return [
            script.name
            for script in sorted(directory.glob("*.sql"))
            if script.name not in applied
        ]

    def applied_revision(self) -> str:
        """Numeric prefix of the last applied migration, `0000` when empty."""
        applied = sorted(row["name"] for row in self._applied_migrations())
        if not applied:
            return "0000"
        return applied[-1].split("_", 1)[0]

    def _applied_migrations(self) -> list[sqlite3.Row]:
        with self._mutex:
            table = self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name = 'schema_migrations'"
            ).fetchone()
            if table is None:
                return []
            return list(self.connection.execute("SELECT name FROM schema_migrations"))

    def migrate(self, directory: Path | None = None) -> list[str]:
        directory = directory or MIGRATIONS_DIR
        connection = self.connection
        with self._mutex:
            # Read the ledger WITHOUT creating it. Creating it here autocommits
            # before the batch transaction opens, so on a fresh or legacy
            # database a failed migration left the file different from the
            # snapshot taken moments earlier — the next boot then compared
            # content, found a difference, rejected the archive and wrote
            # another. The creation moves into the batch below, where it rolls
            # back with everything else.
            try:
                applied = {
                    row["name"]
                    for row in connection.execute(
                        "SELECT name FROM schema_migrations"
                    )
                }
            except sqlite3.OperationalError as error:
                # ONLY "no such table". A missing ledger genuinely means nothing
                # has been applied — that is a fresh database. Any other
                # `DatabaseError` was also being read as "nothing applied", so a
                # malformed or unreadable `schema_migrations` made the boot
                # attempt every migration from 0001: on a production schema that
                # fails on objects that already exist, and with an idempotent
                # script it would redo work whose ledger entry it could not
                # read. An unreadable ledger is not an empty one.
                if "no such table" not in str(error).lower():
                    raise
                applied = set()
            pending = [
                script
                for script in sorted(directory.glob("*.sql"))
                if script.name not in applied
            ]
            if not pending:
                return []
            # ONE transaction for the WHOLE pending batch, not one per file.
            #
            # Per-file transactions meant a failure in the third of four scripts
            # left the first two committed: a partially upgraded schema that no
            # migration file describes. The pre-migrate backup is taken before
            # the batch, so the operator's restore point is correct — but a
            # RESTART would snapshot the partial state and record THAT as the
            # new restore point, quietly replacing the good one. Step E9
            # promises "a failed file leaves no partial schema"; this is what
            # makes that true across files as well as within one.
            #
            # Safe because every migration here is transactional DDL: no PRAGMA,
            # no VACUUM. The `BEGIN`s inside the .sql files are trigger bodies,
            # not transaction control. A future migration needing either must
            # not join this batch.
            statements = [
                "BEGIN IMMEDIATE;",
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "name TEXT PRIMARY KEY, applied_at REAL NOT NULL);",
            ]
            for script in pending:
                name_literal = script.name.replace("'", "''")
                statements.append(script.read_text(encoding="utf-8"))
                statements.append(
                    "INSERT INTO schema_migrations (name, applied_at)"
                    f" VALUES ('{name_literal}', {time.time()!r});"
                )
            statements.append("COMMIT;")
            try:
                connection.executescript("\n".join(statements))
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            return [script.name for script in pending]

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


def main(argv: Sequence[str] | None = None) -> int:
    """Container entrypoint step: back up before applying any new migration."""

    # Imported here so the module stays importable from control_plane.backup.
    import argparse
    import binascii
    import json

    from control_plane.backup import BackupError, create_pre_migrate_backup
    from control_plane.config import decode_key_material

    parser = argparse.ArgumentParser(prog="python -m control_plane.db")
    commands = parser.add_subparsers(dest="command", required=True)
    migrate = commands.add_parser("migrate", help="apply pending migrations")
    migrate.add_argument(
        "--backup-first",
        action="store_true",
        help="take an authenticated snapshot before the first pending migration",
    )
    args = parser.parse_args(argv)

    # This step runs before the application config is loaded, so it reads only
    # the two values it needs: the volume database and the dedicated backup key.
    database = ControlPlaneDatabase(
        os.environ.get("ABROLIA_CONTROL_PLANE_DB", "data/control-plane.db")
    )
    # Same normalisation as ControlPlaneConfig, deliberately shared: this step
    # reads the key before the application config exists, and decoding it
    # differently is how a valid unpadded key became `b""` here and stopped the
    # container from starting at all.
    try:
        backup_key = decode_key_material(
            os.environ.get("ABROLIA_CONTROL_PLANE_BACKUP_KEY", "")
        )
    except (binascii.Error, ValueError, TypeError):
        backup_key = b""
    # Exclusive ownership for the whole step, taken BEFORE the pending check so
    # nothing is decided — let alone archived — while another writer is live.
    #
    # Overlapping a running `serve` breaks the snapshot's only promise: an
    # application write can commit AFTER the archive is taken and BEFORE the
    # migration, so the restore point silently omits committed data, and the old
    # process then carries on against the upgraded schema. The legacy CLI has
    # always taken this lock; the entrypoint that actually runs in the container
    # did not.
    try:
        database.acquire_process_lock()
    except ProcessAlreadyRunning as error:
        print(f"pre-migrate backup refused: {error}", file=sys.stderr)
        database.close()
        return 1
    try:
        backup: Path | None = None
        if args.backup_first:
            try:
                backup = create_pre_migrate_backup(database, backup_key=backup_key)
            except (BackupError, OSError) as error:
                # Fail closed: never migrate a database we could not snapshot.
                print(f"pre-migrate backup failed: {error}", file=sys.stderr)
                return 1
        try:
            applied = database.migrate()
        except Exception as error:
            print(f"migration failed: {error}", file=sys.stderr)
            if backup is not None:
                print(f"restore from {backup}", file=sys.stderr)
            return 1
        print(json.dumps(
            {"applied": applied, "backup": str(backup) if backup else None},
            sort_keys=True,
        ))
        return 0
    finally:
        database.release_process_lock()
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())

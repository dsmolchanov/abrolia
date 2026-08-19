from __future__ import annotations

import base64
import json
import os
import sqlite3
import tracemalloc
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from control_plane.backup import (
    MAGIC,
    NONCE_BYTES,
    BackupError,
    _key,
    create_pre_migrate_backup,
    restore_backup,
)
from control_plane.db import ControlPlaneDatabase, main

BACKUP_KEY_BYTES = b"pre-migrate-backup-key-32-bytes!"
BACKUP_KEY = base64.urlsafe_b64encode(BACKUP_KEY_BYTES).decode()


def _database(tmp_path: Path) -> ControlPlaneDatabase:
    return ControlPlaneDatabase(tmp_path / "control-plane.db")


def _pending_migration_directory(tmp_path: Path) -> Path:
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "9999_pilot_probe.sql").write_text(
        "CREATE TABLE pilot_probe (id TEXT PRIMARY KEY);\n", encoding="utf-8"
    )
    return directory


def test_pre_migrate_backup_is_skipped_when_no_migration_is_pending(tmp_path) -> None:
    database = _database(tmp_path)
    database.migrate()
    assert database.pending_migrations() == []

    assert create_pre_migrate_backup(database, backup_key=BACKUP_KEY_BYTES) is None
    assert list(tmp_path.glob("*.bak")) == []
    database.close()


def test_pre_migrate_backup_captures_the_schema_before_the_migration(tmp_path) -> None:
    database = _database(tmp_path)
    database.migrate()
    directory = _pending_migration_directory(tmp_path)
    revision = database.applied_revision()

    backup = create_pre_migrate_backup(
        database,
        backup_key=BACKUP_KEY_BYTES,
        now=1_800_000_000,
        directory=directory,
    )
    assert database.migrate(directory) == ["9999_pilot_probe.sql"]

    assert backup is not None
    assert backup.name == f"control-plane.db.pre-migrate-{revision}-1800000000.bak"
    assert backup.stat().st_mode & 0o777 == 0o600
    restored = restore_backup(
        backup, tmp_path / "restored.db", backup_key=BACKUP_KEY_BYTES
    )
    # The snapshot predates the migration, so the new table is absent from it.
    assert restored.query_one(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'pilot_probe'"
    ) is None
    restored.close()
    database.close()


def test_pre_migrate_backup_fails_closed_without_a_backup_key(tmp_path) -> None:
    database = _database(tmp_path)

    with pytest.raises(BackupError):
        create_pre_migrate_backup(database, backup_key=b"")
    database.close()


def test_migrate_command_backs_up_before_applying(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_DB", str(tmp_path / "control-plane.db"))
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_BACKUP_KEY", BACKUP_KEY)

    assert main(["migrate", "--backup-first"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["applied"] == [
        script.name
        for script in sorted(Path("control_plane/migrations").glob("*.sql"))
    ]
    backup = Path(report["backup"])
    assert backup.exists()
    assert backup.name.startswith("control-plane.db.pre-migrate-0000-")
    assert _database(tmp_path).pending_migrations() == []


def test_migrate_command_refuses_to_migrate_without_a_backup_key(
    tmp_path, capsys, monkeypatch
) -> None:
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_DB", str(tmp_path / "control-plane.db"))
    monkeypatch.delenv("ABROLIA_CONTROL_PLANE_BACKUP_KEY", raising=False)

    assert main(["migrate", "--backup-first"]) == 1

    assert "pre-migrate backup failed" in capsys.readouterr().err
    # Fail closed: no schema was applied to the volume database.
    assert _database(tmp_path).query_one(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
        " AND name = 'schema_migrations'"
    ) is None
    assert list(tmp_path.glob("*.bak")) == []


def test_rollback_restore_keeps_the_archived_schema(tmp_path) -> None:
    database = _database(tmp_path)
    database.migrate()
    directory = _pending_migration_directory(tmp_path)
    backup = create_pre_migrate_backup(
        database, backup_key=BACKUP_KEY_BYTES, directory=directory
    )
    assert backup is not None
    assert database.migrate(directory) == ["9999_pilot_probe.sql"]
    database.close()

    rolled_back = restore_backup(
        backup,
        tmp_path / "rollback.db",
        backup_key=BACKUP_KEY_BYTES,
        apply_migrations=False,
    )

    # The rollback keeps the pre-migration schema instead of reapplying it, and
    # stays paused until an operator resumes it on the pre-upgrade image.
    assert rolled_back.pending_migrations(directory) == ["9999_pilot_probe.sql"]
    assert rolled_back.workers_paused
    rolled_back.close()


def test_an_unpadded_backup_key_still_starts_the_container(
    tmp_path, capsys, monkeypatch
) -> None:
    """The application accepted this key; the startup step must too.

    `ControlPlaneConfig._decode_key` restores absent base64 padding, and this
    step decoded the SAME environment variable without doing so — an unpadded
    but perfectly valid key became `b""` here. With a migration pending the
    pre-migrate backup then failed closed, `serve` never ran, and the only
    symptom was a container that would not start.
    """
    unpadded = BACKUP_KEY.rstrip("=")
    assert unpadded != BACKUP_KEY, "pick a key whose encoding actually needs padding"
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_DB", str(tmp_path / "control-plane.db"))
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_BACKUP_KEY", unpadded)

    assert main(["migrate", "--backup-first"]) == 0

    report = json.loads(capsys.readouterr().out)
    backup = Path(report["backup"])
    assert backup.exists()
    # Written under the real key, not a truncated one.
    restored = tmp_path / "restored.db"
    restore_backup(backup, restored, backup_key=BACKUP_KEY_BYTES, apply_migrations=False)
    assert restored.exists()


def test_both_key_readers_agree_on_padding() -> None:
    """One spelling of the rule, so the two readers cannot diverge again."""
    from control_plane.config import decode_key_material

    padded = base64.urlsafe_b64encode(BACKUP_KEY_BYTES).decode()
    assert decode_key_material(padded) == BACKUP_KEY_BYTES
    assert decode_key_material(padded.rstrip("=")) == BACKUP_KEY_BYTES


def test_the_rollback_procedure_matches_the_paths_it_depends_on() -> None:
    """The rollback restored to a path the rolled-back image never opens.

    `fly.toml` pins the database path, and the worker pause is a sibling file
    named after it. The documented procedure has to name both, or a rollback
    reopens the migrated database and — if the marker is left behind — resumes
    workers on a database nobody reconciled. This test fails when either
    convention moves, which is when the prose goes stale.
    """
    root = Path(__file__).resolve().parents[2]
    fly = (root / "deploy/control-plane/fly.toml").read_text(encoding="utf-8")
    configured = next(
        line.split("=", 1)[1].strip().strip('"')
        for line in fly.splitlines()
        if line.strip().startswith("ABROLIA_CONTROL_PLANE_DB")
    )
    doc = (root / "docs/control-plane-restore.md").read_text(encoding="utf-8")

    assert configured == "/data/control-plane.db"
    # Whitespace-insensitive: the page aligns these commands in a column, and
    # the procedure is what matters, not its formatting.
    moves = {
        tuple(line.split()[1:3])
        for line in doc.splitlines()
        if line.strip().startswith("mv ") and len(line.split()) >= 3
    }

    # The restore has to land where the rolled-back image will actually look.
    assert ("/data/control-plane-rollback.db", configured) in moves
    # The pause must travel with it, under the name the code gives it — merely
    # mentioning the marker elsewhere on the page is not the procedure.
    suffix = ControlPlaneDatabase(Path(configured)).worker_pause_path.name
    assert suffix == "control-plane.db.workers-paused"
    assert (
        "/data/control-plane-rollback.db.workers-paused",
        f"{configured}.workers-paused",
    ) in moves
    # And so must the WAL sidecars, in BOTH directions: one left behind at the
    # canonical path is replayed into the restore, and one left behind by the
    # superseded database separates it from its own committed pages.
    for sidecar in ("-wal", "-shm"):
        assert (
            f"/data/control-plane-rollback.db{sidecar}",
            f"{configured}{sidecar}",
        ) in moves, sidecar
        assert any(
            source == f"{configured}{sidecar}" for source, _ in moves
        ), f"superseded {sidecar} is never moved aside"


def test_a_failing_script_leaves_no_partially_upgraded_schema(tmp_path) -> None:
    """A batch is all or nothing, across files as well as within one.

    Per-file transactions meant a failure in the third of four scripts left the
    first two committed — a schema no migration file describes. The pre-migrate
    backup is still correct, but a RESTART would snapshot the partial state and
    record that as the new restore point, quietly replacing the good one.
    """
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "0001_first.sql").write_text(
        "CREATE TABLE first_table (id TEXT PRIMARY KEY);\n", encoding="utf-8"
    )
    (directory / "0002_second.sql").write_text(
        "CREATE TABLE second_table (id TEXT PRIMARY KEY);\n", encoding="utf-8"
    )
    (directory / "0003_broken.sql").write_text(
        "CREATE TABLE third_table (id TEXT PRIMARY KEY);\n"
        "THIS IS NOT VALID SQL;\n",
        encoding="utf-8",
    )
    database = _database(tmp_path)

    with pytest.raises(sqlite3.DatabaseError):
        database.migrate(directory)

    # Neither the earlier scripts' tables nor their bookkeeping rows survive.
    tables = {
        row["name"]
        for row in database.query(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert "first_table" not in tables
    assert "second_table" not in tables
    assert "third_table" not in tables
    assert database.pending_migrations(directory) == [
        "0001_first.sql",
        "0002_second.sql",
        "0003_broken.sql",
    ]
    database.close()


def test_a_healthy_batch_still_applies_every_script(tmp_path) -> None:
    """The counterpart: batching must not stop it working."""
    directory = tmp_path / "migrations"
    directory.mkdir()
    for index in (1, 2, 3):
        (directory / f"000{index}_step.sql").write_text(
            f"CREATE TABLE step_{index} (id TEXT PRIMARY KEY);\n", encoding="utf-8"
        )
    database = _database(tmp_path)

    applied = database.migrate(directory)

    assert applied == ["0001_step.sql", "0002_step.sql", "0003_step.sql"]
    assert database.pending_migrations(directory) == []
    assert database.migrate(directory) == []
    database.close()


def test_a_restart_loop_does_not_accumulate_snapshots(tmp_path) -> None:
    """The real loop: a migration that keeps failing, and a container restart.

    The previous version of this test repeated only the backup call, so it never
    opened the failing transaction or closed the connection — and therefore
    never moved the mtimes that a timestamp-based check was relying on. It
    passed while the accumulation it was written to prevent still happened.
    """
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "9999_broken.sql").write_text(
        "CREATE TABLE half_applied (id TEXT PRIMARY KEY);\nTHIS IS NOT SQL;\n",
        encoding="utf-8",
    )

    for boot in range(4):
        database = _database(tmp_path)
        database.migrate()  # the real migrations; the broken one is in `directory`
        archive = create_pre_migrate_backup(
            database, backup_key=BACKUP_KEY_BYTES, directory=directory
        )
        assert archive is not None, f"boot {boot} took no snapshot"
        with pytest.raises(sqlite3.DatabaseError):
            database.migrate(directory)
        # As the entrypoint does: the process exits and the connection closes,
        # which checkpoints and moves the file timestamps.
        database.close()

    assert len(list(tmp_path.glob("*.bak"))) == 1


def test_a_snapshot_is_not_reused_once_the_database_has_moved_on(tmp_path) -> None:
    """Same revision is not sufficient — the data can still have changed.

    A migration can fail, be dropped from the next image so the container serves
    at the unchanged revision, take writes, and only then meet a new migration.
    The old archive matches by revision and no longer matches the data; reusing
    it would hand the operator a restore point that silently loses those writes.
    """
    directory = _pending_migration_directory(tmp_path)
    database = _database(tmp_path)
    database.migrate()
    first = create_pre_migrate_backup(
        database, backup_key=BACKUP_KEY_BYTES, directory=directory
    )
    assert first is not None

    # Serving writes to the database after the archive was taken.
    with database.write() as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS post_backup_write (id TEXT PRIMARY KEY)"
        )
    for companion in (database.path, database.path.with_name(f"{database.path.name}-wal")):
        if companion.exists():
            os.utime(companion, (first.stat().st_mtime + 10,) * 2)

    second = create_pre_migrate_backup(
        database, backup_key=BACKUP_KEY_BYTES, directory=directory
    )

    assert second is not None and second != first
    assert len(list(tmp_path.glob("*.bak"))) == 2
    database.close()


def test_a_rotated_key_does_not_reuse_the_old_archive(tmp_path) -> None:
    """Reuse skipped authentication entirely.

    An archive left by an earlier boot satisfied the backup-first gate even when
    the key had since been rotated — so the migration proceeded believing it had
    a restore point that could not be opened. That belief is the only thing this
    gate exists to establish.
    """
    directory = _pending_migration_directory(tmp_path)
    database = _database(tmp_path)
    database.migrate()
    first = create_pre_migrate_backup(
        database, backup_key=BACKUP_KEY_BYTES, directory=directory
    )
    assert first is not None

    # Derived so the length is right by construction, not by counting.
    rotated = b"rotated!" + BACKUP_KEY_BYTES[8:]
    assert len(rotated) == len(BACKUP_KEY_BYTES) and rotated != BACKUP_KEY_BYTES
    second = create_pre_migrate_backup(
        database, backup_key=rotated, directory=directory
    )

    assert second is not None and second != first
    # And the new one opens with the key actually in use.
    restore_backup(
        second, tmp_path / "restored.db", backup_key=rotated, apply_migrations=False
    )
    database.close()


def test_a_truncated_archive_is_not_reused(tmp_path) -> None:
    """A disk that filled mid-write leaves exactly this."""
    directory = _pending_migration_directory(tmp_path)
    database = _database(tmp_path)
    database.migrate()
    first = create_pre_migrate_backup(
        database, backup_key=BACKUP_KEY_BYTES, directory=directory
    )
    assert first is not None
    first.write_bytes(first.read_bytes()[: len(first.read_bytes()) // 2])

    second = create_pre_migrate_backup(
        database, backup_key=BACKUP_KEY_BYTES, directory=directory
    )

    assert second is not None and second != first
    restore_backup(
        second,
        tmp_path / "restored.db",
        backup_key=BACKUP_KEY_BYTES,
        apply_migrations=False,
    )
    database.close()


def _grow(database, megabytes: int) -> None:
    with database.write() as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS bulk (id INTEGER, blob BLOB)")
        payload = b"x" * 64_000
        for row in range(megabytes * 16):
            connection.execute("INSERT INTO bulk VALUES (?, ?)", (row, payload))


def test_the_reuse_decision_does_not_hold_the_database_in_memory(tmp_path) -> None:
    """512 MiB of RAM against a 1 GiB volume.

    Deciding whether a snapshot is reusable used to allocate a decrypted archive
    AND an in-memory SQLite copy AND a serialised duplicate — several times the
    database size, on the retry path of a failed migration, which is exactly
    when a container can least afford to die. A database of a few hundred MiB
    could leave it permanently unable to migrate or serve.

    `tracemalloc` measures Python-level allocation, which is precisely where
    those buffers lived.
    """
    directory = _pending_migration_directory(tmp_path)
    database = _database(tmp_path)
    database.migrate()
    _grow(database, megabytes=8)
    first = create_pre_migrate_backup(
        database, backup_key=BACKUP_KEY_BYTES, directory=directory
    )
    assert first is not None
    assert first.stat().st_size > 4_000_000, "the fixture database is too small to test"

    tracemalloc.start()
    try:
        again = create_pre_migrate_backup(
            database, backup_key=BACKUP_KEY_BYTES, directory=directory
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert again == first, "the reuse decision itself must still be correct"
    # Bounded by the chunk size and bookkeeping, not by the database.
    assert peak < 1_000_000, f"reuse decision peaked at {peak} bytes"
    database.close()


def test_an_archive_written_by_the_one_shot_path_still_restores(tmp_path) -> None:
    """Streaming changed how archives are written; it must not orphan old ones.

    An operator's existing `.bak` predates this change, and it is the file they
    reach for on the worst day.
    """
    database = _database(tmp_path)
    database.migrate()
    legacy = tmp_path / "legacy.bak"
    with sqlite3.connect(":memory:") as image:
        database.connection.backup(image)
        plaintext = image.serialize()
    nonce = os.urandom(NONCE_BYTES)
    legacy.write_bytes(
        MAGIC + nonce + AESGCM(_key(BACKUP_KEY_BYTES)).encrypt(nonce, plaintext, MAGIC)
    )

    restored = restore_backup(
        legacy,
        tmp_path / "from-legacy.db",
        backup_key=BACKUP_KEY_BYTES,
        apply_migrations=False,
    )

    assert restored.query_one("SELECT COUNT(*) AS n FROM schema_migrations")["n"] > 0
    restored.close()
    database.close()

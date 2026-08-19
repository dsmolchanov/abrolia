from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path

import pytest

from control_plane.backup import BackupError, create_pre_migrate_backup, restore_backup
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
    # The move has to put the restore where the image will actually look.
    assert f"mv /data/control-plane-rollback.db {configured}" in doc
    # And the pause must be MOVED with it, under the name the code gives it —
    # merely mentioning the marker elsewhere in the page is not the procedure.
    suffix = ControlPlaneDatabase(Path(configured)).worker_pause_path.name
    assert suffix == "control-plane.db.workers-paused"
    assert (
        f"mv /data/control-plane-rollback.db.workers-paused {configured}.workers-paused"
        in doc
    )


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

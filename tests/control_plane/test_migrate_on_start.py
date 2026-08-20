from __future__ import annotations

import base64
import errno
import json
import os
import sqlite3
import stat
import tracemalloc
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from control_plane import backup as backup_module
from control_plane.backup import (
    MAGIC,
    NONCE_BYTES,
    SCRATCH_DIR_ENV,
    BackupError,
    RollbackError,
    _key,
    create_backup,
    create_pre_migrate_backup,
    install_rollback,
    restore_backup,
)
from control_plane.cli import main as cli_main
from control_plane.config import ConfigurationError
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
    """The page must name the path and the command the rollback actually uses.

    This used to parse the page's column of `mv` commands and assert each pair
    was present. That was the best available check while the procedure WAS the
    prose — and it was still only a check that the right strings appeared, which
    is why it passed for a procedure that ended in `ENOSPC`. The moves are now
    `install_rollback`, exercised end to end against a constrained volume by the
    tests below; what is left for the page is that it points operators at that
    command, for the path the deployment actually pins.
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
    # The restore has to land where the rolled-back image will actually look,
    # and the page has to say so with the path `fly.toml` pins.
    assert f"--target {configured}" in doc

    # Off-volume staging, and the command that installs from it. A restore left
    # in the staging directory is never read.
    assert "--target /tmp/control-plane-rollback.db" in doc
    for flag in ("install-rollback", "--restored"):
        assert flag in doc, flag
    # The command refuses rather than freeing space, so the page has to carry
    # the manual procedure — and carry it in the safe order, archive verified
    # before anything is deleted.
    archive_at = doc.index("abrolia-control-plane backup /tmp/control-plane-superseded.cpb")
    verify_at = doc.index("--target /tmp/verify.db --no-migrate")
    delete_at = doc.index("rm -f /data/control-plane.db /data/control-plane.db-wal")
    assert archive_at < verify_at < delete_at, (
        "the documented order lets an operator delete before verifying"
    )

    # Check the space BEFORE restoring: at that point /data holds the live
    # database and the archive, and a third full-size file is what fails.
    assert "df -h /data" in doc
    assert "ENOSPC" in doc

    # The pause marker's name is a convention shared between the code and the
    # page. This fails when it moves, which is when the prose goes stale.
    assert ControlPlaneDatabase(Path(configured)).worker_pause_path.name == (
        "control-plane.db.workers-paused"
    )
    assert "<db path>.workers-paused" in doc


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


def test_the_snapshot_image_is_not_written_beside_the_database(
    tmp_path, monkeypatch
) -> None:
    """The RAM problem must not simply become a disk problem.

    The image and the encrypted archive are each about the size of the database.
    Putting both on the 1 GiB data volume beside the live file means a database
    near a third of the volume exhausts it while taking a backup — and every
    deployment with a pending migration then fails closed and never serves.
    """
    scratch = tmp_path / "ephemeral"
    scratch.mkdir()
    monkeypatch.setenv(SCRATCH_DIR_ENV, str(scratch))
    directory = _pending_migration_directory(tmp_path)
    database = _database(tmp_path)
    database.migrate()

    destinations: list[Path] = []
    original = backup_module._materialise

    def recording(db, target):
        destinations.append(Path(target))
        return original(db, target)

    monkeypatch.setattr(backup_module, "_materialise", recording)
    archive = create_pre_migrate_backup(
        database, backup_key=BACKUP_KEY_BYTES, directory=directory
    )

    assert archive is not None
    assert destinations, "no image was materialised"
    for target in destinations:
        assert target.parent == scratch, f"{target} landed on the data volume"
    # The archive itself must still be installed beside the database.
    assert archive.parent == database.path.parent
    database.close()


def test_restoring_does_not_buffer_the_whole_archive(tmp_path) -> None:
    """The rollback path is the one an operator needs on the worst day.

    It held the payload, a ciphertext slice, the decrypted bytes AND a copy of
    them — so a ~125 MiB archive could OOM the 512 MiB Machine, taking the
    documented recovery procedure down for exactly the larger databases whose
    migrations are most likely to need it.
    """
    database = _database(tmp_path)
    database.migrate()
    _grow(database, megabytes=8)
    archive = tmp_path / "rollback.bak"
    create_backup(database, archive, backup_key=BACKUP_KEY_BYTES)
    assert archive.stat().st_size > 4_000_000, "the fixture archive is too small"

    tracemalloc.start()
    try:
        restored = restore_backup(
            archive,
            tmp_path / "restored.db",
            backup_key=BACKUP_KEY_BYTES,
            apply_migrations=False,
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert restored.query_one("SELECT COUNT(*) AS n FROM bulk")["n"] > 0
    restored.close()
    assert peak < 1_000_000, f"restore peaked at {peak} bytes"
    database.close()


def _durability_trace(monkeypatch, published) -> list[str]:
    """Record fsync/rename/migrate in order, distinguishing dir from file.

    An fd is not self-describing, so `fstat` is what separates a directory sync
    from a file sync. Asserting on call counts alone would pass with two file
    syncs and no directory sync at all — which is precisely the defect.

    `published` decides which rename is the publication, so the scratch image's
    own temporary churn cannot be mistaken for it.
    """
    trace: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    real_migrate = ControlPlaneDatabase.migrate

    def fsync(fd):
        trace.append("fsync:dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "fsync:file")
        return real_fsync(fd)

    def replace(source, target, **kwargs):
        if published(Path(target)):
            trace.append("rename")
        return real_replace(source, target, **kwargs)

    def migrate(self, directory=None):
        trace.append("migrate")
        return real_migrate(self, directory)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(ControlPlaneDatabase, "migrate", migrate)
    return trace


def test_the_archive_is_linked_durably_before_the_migration_runs(
    tmp_path, capsys, monkeypatch
) -> None:
    """file fsync → rename → DIRECTORY fsync, all of it before `migrate`.

    Syncing the archive's contents and then renaming leaves the new name in the
    parent directory's unsynced metadata. Lose power between the snapshot
    returning and the migration committing and that entry can be gone while the
    upgraded database survives: the archive's blocks are still on the disk with
    nothing pointing at them, so the one rollback path is unreachable at the one
    moment it is needed. The directory sync has to be inside the gate, not
    merely somewhere in the function.

    What this does NOT prove: that the filesystem honours any of it, or that the
    ordering survives a real power cut. It asserts the order of the syscalls we
    issue, which is the part we control.
    """
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_DB", str(tmp_path / "control-plane.db"))
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_BACKUP_KEY", BACKUP_KEY)

    trace = _durability_trace(monkeypatch, lambda target: target.suffix == ".bak")
    assert main(["migrate", "--backup-first"]) == 0

    report = json.loads(capsys.readouterr().out)
    archive = Path(report["backup"])
    assert archive.exists() and archive.parent == tmp_path
    assert report["applied"], "nothing was pending, so nothing was proved"

    assert "migrate" in trace, trace
    published = trace[: trace.index("migrate") + 1]
    assert published[-4:] == ["fsync:file", "rename", "fsync:dir", "migrate"], trace


def test_a_directory_that_will_not_sync_stops_the_migration(
    tmp_path, capsys, monkeypatch
) -> None:
    """Fail closed on the durability step, exactly as on the snapshot itself.

    A directory sync that raises means the archive's name is not durable, which
    for rollback purposes is the same as having no archive. `db.main` already
    treats an `OSError` from the snapshot as fatal, so the requirement is that
    `_publish` raise rather than swallow — this fails if anyone wraps it in a
    `try`.

    It also pins the cleanup. Leaving the renamed file behind would be worse
    than never writing it: the next boot's reuse check finds that archive, skips
    the snapshot and migrates, so a fail-closed boot becomes a fail-open one.
    """
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_DB", str(tmp_path / "control-plane.db"))
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_BACKUP_KEY", BACKUP_KEY)

    real_fsync = os.fsync

    def refuse_directories(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "directory sync failed")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", refuse_directories)
    monkeypatch.setattr(
        ControlPlaneDatabase,
        "migrate",
        lambda *a, **k: pytest.fail("migrated without a durably linked archive"),
    )

    assert main(["migrate", "--backup-first"]) == 1
    assert "pre-migrate backup failed" in capsys.readouterr().err
    assert list(tmp_path.glob("*.bak")) == [], "a half-published archive was left behind"


def test_the_rollback_restore_is_published_durably_too(tmp_path, monkeypatch) -> None:
    """The sibling publication site, and the marker that has to precede it.

    `restore_backup` renames the operator's rollback into place, and it is the
    worse of the two publications: a restore lost to an unsynced directory entry
    is discovered with the service already stopped and the superseded database
    already moved aside.

    The worker pause has to land FIRST and durably. `pause_workers` only wrote
    and chmod'ed, so a power loss between publishing the database and writing
    the marker — or simply a failed write — left a published database with no
    `<db>.workers-paused`. The retry then refuses because the target exists, and
    starting it instead makes `workers_paused` false and lets unreconciled jobs
    run against a freshly restored database. A database that is visible before
    its pause is a database that can be started without one.
    """
    database = _database(tmp_path)
    database.migrate()
    archive = tmp_path / "control-plane.cpb"
    create_backup(database, archive, backup_key=BACKUP_KEY_BYTES)
    database.close()

    target = tmp_path / "restored" / "control-plane.db"
    marker = _pause_marker_path(target)
    trace: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        trace.append("fsync:dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "fsync:file")
        return real_fsync(fd)

    def replace(source, destination, **kwargs):
        landed = Path(destination)
        if landed == target:
            trace.append("publish:database")
        elif landed == marker:
            trace.append("publish:marker")
        return real_replace(source, destination, **kwargs)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    restored = restore_backup(
        archive, target, backup_key=BACKUP_KEY_BYTES, apply_migrations=False
    )
    restored.close()

    assert "publish:marker" in trace and "publish:database" in trace, trace
    assert trace.index("publish:marker") < trace.index("publish:database"), (
        "the database became visible before its worker pause"
    )
    # Each publication is followed by a DIRECTORY fsync, so the name that finds
    # the bytes is durable and not merely the bytes. The file syncs come earlier
    # — the payload is fsynced as it is written, before either rename — so this
    # asserts the ordering that the rename depends on rather than adjacency.
    for event in ("publish:marker", "publish:database"):
        at = trace.index(event)
        assert trace[at + 1] == "fsync:dir", (event, trace)
        assert "fsync:file" in trace[:at], (event, trace)
    assert marker.exists()


def test_a_restore_that_cannot_pause_publishes_nothing(tmp_path, monkeypatch) -> None:
    """Fail closed: no database rather than an unpausable one.

    A marker write or fsync that fails used to leave the restore published and
    running-capable. There is no safe way to hand an operator a restored
    database that cannot be held paused, so the whole restore is refused and the
    target stays absent — which is also what makes the obvious retry work.
    """
    database = _database(tmp_path)
    database.migrate()
    archive = tmp_path / "control-plane.cpb"
    create_backup(database, archive, backup_key=BACKUP_KEY_BYTES)
    database.close()

    destination = tmp_path / "restored"
    destination.mkdir()
    target = destination / "control-plane.db"
    real_fsync = os.fsync

    def refuse_the_marker(fd):
        name = getattr(fd, "name", "")
        del name
        return real_fsync(fd)

    original_write_text = Path.write_text

    def write_text(self, *args, **kwargs):
        if self.name.endswith(".workers-paused"):
            raise OSError(errno.EIO, "marker write failed")
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(os, "fsync", refuse_the_marker)
    monkeypatch.setattr(Path, "write_text", write_text)

    with pytest.raises(BackupError, match="durably pause workers"):
        restore_backup(
            archive, target, backup_key=BACKUP_KEY_BYTES, apply_migrations=False
        )

    assert not target.exists(), "an unpausable restore was published anyway"
    assert not _pause_marker_path(target).exists()
    # The writer lock is the restore TAKING the lock, not a published artifact.
    assert [
        path.name
        for path in destination.iterdir()
        if not path.name.endswith(".writer.lock")
    ] == []


def _constrain(monkeypatch, volume: Path, capacity: int) -> None:
    """Give `volume` a real capacity, and make renames onto it cross-device.

    Free space is DERIVED from the files actually on the volume, not returned as
    a fixed number, so releasing blocks genuinely changes the reading and a test
    cannot pass by freeing nothing. The files, their sizes and every operation
    on them are real.

    Two things are simulated, and both are simulated as the kernel produces
    them: `EXDEV` is literally what `rename(2)` returns across filesystems, and
    the capacity is applied at the one `statvfs` seam. Everything downstream is
    real — a real encrypted archive of the real superseded database, a real
    authenticated read-back, real deletions, a real chunked copy, real sidecars
    and a real pause marker.

    We cannot do better without privileges: a genuinely small filesystem needs a
    loopback mount or a tmpfs, so such a test would skip on macOS and on any
    runner without them, which is a guard that is not one.
    """
    real_replace = os.replace

    def replace(source, target, **kwargs):
        if Path(target).parent == volume and Path(source).parent != volume:
            raise OSError(errno.EXDEV, "Cross-device link")
        return real_replace(source, target, **kwargs)

    real_free = backup_module._free_bytes

    def free(directory) -> int:
        if Path(directory) != volume:
            return real_free(directory)
        used = sum(path.stat().st_size for path in volume.iterdir() if path.is_file())
        return max(0, capacity - used)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(backup_module, "_free_bytes", free)


def _rollback_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A migrated volume database, and a staged rollback restored beside it.

    The volume database is the SUPERSEDED one: it carries a table the archive
    does not, which is what lets a test tell the two apart after the install
    rather than trusting a report field.
    """
    volume = tmp_path / "data"
    staging = tmp_path / "staging"
    volume.mkdir()
    staging.mkdir()

    target = volume / "control-plane.db"
    database = ControlPlaneDatabase(target)
    database.migrate()
    archive = staging / "control-plane.db.pre-migrate-0008-1800000000.bak"
    create_backup(database, archive, backup_key=BACKUP_KEY_BYTES)
    # Only the superseded database has this, so it must not survive the install.
    with database.write() as connection:
        connection.execute("CREATE TABLE migrated_only (id TEXT PRIMARY KEY)")
    database.close()

    restored = restore_backup(
        archive,
        staging / "control-plane-rollback.db",
        backup_key=BACKUP_KEY_BYTES,
        apply_migrations=False,
    )
    restored.close()
    return volume, target, staging / "control-plane-rollback.db"


def _tables(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        connection.close()


def test_a_low_space_install_is_refused_with_the_volume_untouched(
    tmp_path, monkeypatch
) -> None:
    """The low-space branch, executed rather than asserted about.

    The documented `/tmp` staging only moved WHERE the restore was written. The
    install still copied a third database-sized file onto a volume already
    holding the superseded database and the archive, so the recovery ended in
    `ENOSPC` exactly as before, with a good backup sitting right there. Renaming
    the superseded file aside frees nothing: same filesystem, same blocks.

    An earlier version of this command resolved that itself — archiving the
    superseded database off-volume and deleting it to make room. That made a
    command an operator runs to MOVE A FILE capable of destroying the only copy
    of every write taken after the migration, and it needed a backup key, a
    staging directory, capacity proofs and collision-free archive naming to do
    it. Freeing space is a decision with data-loss consequences and it belongs
    to whoever knows what else is on the machine.

    So it refuses, says what is needed and what is free, and leaves `/data`
    exactly as it found it — which is also what makes the retry work.
    """
    volume, target, restored = _rollback_fixture(tmp_path)
    # Room for one database, which is not enough while the superseded one is
    # still on the volume — the situation the runbook could not survive.
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 3 // 2)
    before = _fingerprint(volume)

    with pytest.raises(RollbackError, match="bytes and .* are free") as refusal:
        install_rollback(restored, target, now=1800000042)

    # The operator is told the two numbers, why it is a copy, and what to do.
    message = str(refusal.value)
    assert "copy, not a rename" in message
    assert "Nothing has been changed" in message
    assert _fingerprint(volume) == before, "the refusal moved something anyway"
    assert "migrated_only" in _tables(target), "the live database was disturbed"
    assert restored.exists(), "the candidate was consumed by a refusal"


def test_the_install_keeps_the_superseded_database_when_there_is_room(
    tmp_path, monkeypatch
) -> None:
    """Room means no archiving: the superseded database is set aside intact.

    Converting it to an archive costs a full-size write and makes recovery a
    restore rather than a rename. That price is worth paying only when the
    volume leaves no choice, so the cheap path has to stay cheap.
    """
    volume, target, restored = _rollback_fixture(tmp_path)
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 10)

    report = install_rollback(restored, target, now=1800000042)

    assert "freed_bytes" not in report
    kept = Path(str(report["superseded_kept_as"]))
    assert kept.parent == volume and kept.suffix != ".cpb"
    assert "migrated_only" in _tables(kept), "the superseded database was not preserved"
    assert "migrated_only" not in _tables(target)


def test_no_superseded_sidecar_is_left_at_the_canonical_path(
    tmp_path, monkeypatch
) -> None:
    """The silent failure: a stale `-wal` is REPLAYED into the restore.

    A service that was killed rather than stopped leaves committed frames in
    `control-plane.db-wal`. Left beside the installed rollback, SQLite replays
    them on the next open and reapplies the migrated pages the rollback exists
    to undo — with no error, and nothing in the report to suggest it happened.
    """
    volume, target, restored = _rollback_fixture(tmp_path)
    stale = volume / "control-plane.db-wal"
    stale.write_bytes(b"\x00" * 4096)
    (volume / "control-plane.db-shm").write_bytes(b"\x00" * 4096)
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 10)

    install_rollback(
        restored,
        target,
        now=1800000042,
    )

    assert not stale.exists(), "a superseded WAL survived at the canonical path"
    assert not (volume / "control-plane.db-shm").exists()


def test_the_worker_pause_travels_with_the_restore(tmp_path, monkeypatch) -> None:
    """Moving the database alone resumes workers on unreconciled data.

    `restore_backup` pauses the database it writes, and the marker is a sibling
    file named after it — so it does not follow the database unless something
    moves it. The install must refuse to finish without it rather than leave a
    running service to discover the difference.
    """
    volume, target, restored = _rollback_fixture(tmp_path)
    marker = restored.with_name(f"{restored.name}.workers-paused")
    assert marker.exists(), "the fixture is wrong: the restore was never paused"
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 10)

    report = install_rollback(restored, target, now=1800000042)

    assert report["workers"] == "paused"
    installed = ControlPlaneDatabase(target)
    try:
        assert installed.workers_paused
    finally:
        installed.close()


def test_a_restore_with_no_pause_marker_is_refused_before_anything_moves(
    tmp_path, monkeypatch
) -> None:
    """Refused in the preflight, not discovered halfway through.

    This check used to run after the install, so the live database had already
    been renamed aside and the invalid candidate was already at the canonical
    path by the time it fired — leaving an operator mid-rollback with neither a
    working database nor an obvious way back. The marker is written by
    `restore`, so its absence means the candidate did not come from one, which
    is knowable before anything is touched.
    """
    volume, target, restored = _rollback_fixture(tmp_path)
    restored.with_name(f"{restored.name}.workers-paused").unlink()
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 10)
    before = _fingerprint(volume)

    with pytest.raises(RollbackError, match="has no worker pause"):
        install_rollback(restored, target, now=1800000042)

    assert _fingerprint(volume) == before, "the volume was modified by a refused install"


def _fingerprint(directory: Path) -> dict[str, bytes]:
    """Every file under `directory`, by name and content.

    The writer lock is excluded. It is a zero-byte file the install creates to
    TAKE the lock — the same one `serve` uses — so its appearance is the guard
    working, not the volume being modified.
    """
    return {
        path.name: path.read_bytes()
        for path in sorted(directory.iterdir())
        if path.is_file() and not path.name.endswith(".writer.lock")
    }


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        pytest.param(
            lambda volume, target, restored: restored.write_bytes(b"not a database"),
            "not a valid SQLite|does not open as SQLite|integrity check",
            id="not-sqlite",
        ),
        pytest.param(
            lambda volume, target, restored: restored.write_bytes(
                restored.read_bytes()[: len(restored.read_bytes()) // 2]
            ),
            "not a valid SQLite|does not open as SQLite|integrity check",
            id="truncated",
        ),
        pytest.param(
            lambda volume, target, restored: (
                target.unlink(),
                target.mkdir(),
            ),
            "not a regular file",
            id="directory-target",
        ),
    ],
)
def test_an_invalid_rollback_bundle_leaves_the_volume_untouched(
    tmp_path, monkeypatch, damage, message
) -> None:
    """Everything decidable in advance is decided before the live data moves.

    The first version checked that `--restored` was a regular file and that the
    target existed, then started renaming. A truncated file, a non-database, or
    a directory target therefore reached the rename loop, and the operator found
    out only once the live database was already aside.
    """
    volume, target, restored = _rollback_fixture(tmp_path)
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 10)
    damage(volume, target, restored)
    before = _fingerprint(volume)

    with pytest.raises(RollbackError, match=message):
        install_rollback(restored, target, now=1800000042)

    assert _fingerprint(volume) == before, "a refused install modified the volume"


def test_installing_a_database_over_itself_is_refused(tmp_path, monkeypatch) -> None:
    """`--restored` equal to `--target` would supersede the file being installed."""
    volume, target, _restored = _rollback_fixture(tmp_path)
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 10)
    before = _fingerprint(volume)

    with pytest.raises(RollbackError, match="same file"):
        install_rollback(target, target, now=1800000042)

    assert _fingerprint(volume) == before


def test_the_preflight_leaves_no_sidecars_beside_the_restore(
    tmp_path, monkeypatch
) -> None:
    """Read-only is not side-effect free, and the check must not forge evidence.

    Opening a WAL database with `mode=ro` creates BOTH `-wal` and `-shm` and
    leaves them after close. The integrity check therefore manufactured sidecars
    beside the restored database, which the install loop then copied to the
    canonical path — a validation step fabricating the exact artifact the
    install exists to keep straight.
    """
    volume, target, restored = _rollback_fixture(tmp_path)
    staging = restored.parent
    assert not list(staging.glob("*-wal")), "the fixture already has a WAL"
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 10)

    install_rollback(restored, target, now=1800000042)

    assert not (volume / "control-plane.db-wal").exists()
    assert not (volume / "control-plane.db-shm").exists()


def test_a_superseded_name_collision_never_overwrites_the_earlier_copy(
    tmp_path, monkeypatch
) -> None:
    """`Path.rename` replaces an existing file on POSIX, silently.

    The superseded name is second-resolution, so a retried install inside the
    same second — or a caller passing a fixed `now`, or a clock that steps back —
    renamed the live database straight over the previous attempt's recovery
    copy. The whole bundle moves as one generation, so a name is only taken when
    every member of it is free.
    """
    volume, target, restored = _rollback_fixture(tmp_path)
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 10)
    earlier = {
        volume / "control-plane.db.superseded-1800000042": b"the-earlier-database",
        volume / "control-plane.db.superseded-1800000042-wal": b"the-earlier-wal",
        volume / "control-plane.db.superseded-1800000042.workers-paused": b"paused\n",
    }
    for path, sentinel in earlier.items():
        path.write_bytes(sentinel)

    report = install_rollback(
        restored, target, now=1800000042
    )

    for path, sentinel in earlier.items():
        assert path.read_bytes() == sentinel, f"{path.name} was overwritten"
    kept = Path(str(report["superseded_kept_as"]))
    assert kept not in earlier
    assert "migrated_only" in _tables(kept), "this generation was not preserved either"


def test_a_running_writer_stops_the_install(tmp_path, monkeypatch) -> None:
    """Renaming a database out from under a live connection fails silently.

    The process keeps its descriptors on the superseded inode, so its writes
    land in a file nothing will read again, and it can recreate stale sidecars
    at the canonical path beside the installed rollback. The command documented
    "with the service stopped" and then trusted it.

    The lock is taken through `ControlPlaneDatabase.acquire_process_lock`, the
    same call `serve` uses, so the two cannot disagree about which file it is.
    """
    volume, target, restored = _rollback_fixture(tmp_path)
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 10)
    before = _fingerprint(volume)

    writer = ControlPlaneDatabase(target)
    writer.acquire_process_lock()
    try:
        with pytest.raises(RollbackError, match="writer still owns"):
            install_rollback(
                restored, target, now=1800000042
            )
    finally:
        writer.release_process_lock()
        writer.close()

    assert _fingerprint(volume) == before

    # And it succeeds once the writer lets go, so the guard is a gate rather
    # than a wall.
    install_rollback(restored, target, now=1800000042)
    assert "migrated_only" not in _tables(target)


def test_restoring_leaves_no_orphaned_sidecars_behind(tmp_path) -> None:
    """`with sqlite3.connect(...)` commits; it does not close.

    The integrity check's connection stayed open, so its `-wal` and `-shm`
    remained under the TEMPORARY name that publication then renamed away from.
    Every restore left two orphans on the volume — on the 1 GiB volume whose
    exhaustion is the reason this rollback path exists at all.
    """
    database = _database(tmp_path)
    database.migrate()
    archive = tmp_path / "control-plane.cpb"
    create_backup(database, archive, backup_key=BACKUP_KEY_BYTES)
    database.close()

    destination = tmp_path / "restored"
    destination.mkdir()
    restored = restore_backup(
        archive,
        destination / "control-plane.db",
        backup_key=BACKUP_KEY_BYTES,
        apply_migrations=False,
    )
    restored.close()

    leftovers = sorted(
        path.name
        for path in destination.iterdir()
        if path.name.startswith(".") or path.name.endswith(("-wal", "-shm"))
    )
    assert leftovers == [], f"restore left {leftovers} on the volume"


def test_recovery_needs_only_the_backup_key(tmp_path, monkeypatch, capsys) -> None:
    """Rollback must not require the deployment it is rolling back to be intact.

    Both commands read the key through `ControlPlaneConfig.from_env`, which
    validates the WHOLE application — field encryption, both HMAC keys, and
    under the checked-in `fly-runtime` configuration the Fly token,
    organization, image digest and bootstrap host. A single missing or invalid
    unrelated secret therefore withdrew the documented two-command rollback,
    with a perfectly good archive and a perfectly good key in hand, in exactly
    the broken state the procedure exists for.

    So: nothing in the environment but the backup key and the file arguments.
    """
    for name in list(os.environ):
        if name.startswith("ABROLIA_") or name.startswith("FLY_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_BACKUP_KEY", BACKUP_KEY)

    volume = tmp_path / "data"
    volume.mkdir()
    target = volume / "control-plane.db"
    database = ControlPlaneDatabase(target)
    database.migrate()
    archive = tmp_path / "control-plane.db.pre-migrate-0008-1800000000.bak"
    create_backup(database, archive, backup_key=BACKUP_KEY_BYTES)
    database.close()

    staged = tmp_path / "control-plane-rollback.db"
    assert cli_main(["restore", str(archive), "--target", str(staged), "--no-migrate"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "restored"

    assert cli_main([
        "install-rollback",
        "--restored", str(staged),
        "--target", str(target),
    ]) == 0
    assert json.loads(capsys.readouterr().out)["workers"] == "paused"
    assert target.exists()


def test_a_recovery_command_still_refuses_a_missing_backup_key(
    tmp_path, monkeypatch
) -> None:
    """Fewer requirements, not none: the key itself is still mandatory."""
    monkeypatch.delenv("ABROLIA_CONTROL_PLANE_BACKUP_KEY", raising=False)
    with pytest.raises(ConfigurationError, match="BACKUP_KEY is required"):
        cli_main([
            "restore", str(tmp_path / "nothing.cpb"), "--target", str(tmp_path / "t.db")
        ])


def test_migrate_on_start_refuses_to_run_beside_a_live_writer(
    tmp_path, capsys, monkeypatch
) -> None:
    """The snapshot's promise depends on nothing else writing.

    Overlapping a running `serve` lets an application write commit AFTER the
    archive is taken and BEFORE the migration, so the advertised rollback point
    silently omits committed data, and the old process then carries on against
    the upgraded schema. The legacy CLI always took this lock; the entrypoint
    that actually runs in the container did not.

    Taken before the pending check, so nothing is decided — let alone archived —
    while another writer is live.
    """
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_DB", str(tmp_path / "control-plane.db"))
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_BACKUP_KEY", BACKUP_KEY)

    writer = ControlPlaneDatabase(tmp_path / "control-plane.db")
    writer.acquire_process_lock()
    try:
        assert main(["migrate", "--backup-first"]) == 1
    finally:
        writer.release_process_lock()
        writer.close()

    assert "refused" in capsys.readouterr().err
    assert list(tmp_path.glob("*.bak")) == [], "an archive was written anyway"
    # And no schema was applied: the whole point of failing before the check.
    probe = ControlPlaneDatabase(tmp_path / "control-plane.db")
    try:
        assert probe.query_one(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name = 'schema_migrations'"
        ) is None
    finally:
        probe.close()


def test_a_hard_linked_candidate_is_refused(tmp_path, monkeypatch) -> None:
    """Two names, one inode. `resolve()` cannot see it; `samefile` can.

    A candidate hard-linked from the target passes a path comparison, and the
    install then leaves the superseded path and the canonical target pointing at
    the same migrated database while reporting a successful rollback — the
    operator believes they rolled back and did not.
    """
    volume, target, restored = _rollback_fixture(tmp_path)
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 10)
    alias = volume / "control-plane-alias.db"
    os.link(target, alias)
    # Give it a pause marker so it reaches the alias check rather than failing
    # earlier for a different reason.
    alias.with_name(f"{alias.name}.workers-paused").write_text("paused\n")
    before = _fingerprint(volume)

    with pytest.raises(RollbackError, match="same file"):
        install_rollback(alias, target, now=1800000042)

    assert _fingerprint(volume) == before
    assert "migrated_only" in _tables(target), "the live database was replaced"
    del restored


def test_the_space_requirement_covers_every_bundle_member(tmp_path) -> None:
    """A gate that passes must guarantee the install ends PAUSED.

    `required` counted the database and its SQLite sidecars by logical
    `st_size`, leaving out the pause marker it later declares mandatory and
    ignoring block rounding. With free space at or just above that
    underestimate, the database copies fine and the marker then fails with
    `ENOSPC` — a restored database at the canonical path with no
    `workers-paused` beside it, so starting the service resumes workers against
    unreconciled rollback data.

    Sizes are constructed rather than taken from a fixture, because a realistic
    database is large enough that a block of padding hides an omitted marker.
    One byte per file makes each omission visible on its own.

    This asserts the requirement itself rather than staging a real `ENOSPC`,
    which needs a filesystem the test cannot create without privileges. It is
    the half that can be checked exactly: the number the gate compares against
    has to account for every file the install will write.
    """
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    database = bundle / "control-plane.db"
    for path in (
        database,
        bundle / "control-plane.db-wal",
        bundle / "control-plane.db-shm",
        bundle / "control-plane.db.workers-paused",
    ):
        path.write_bytes(b"x")

    assert _pause_marker_path(database) in backup_module._bundle(database), (
        "the pause marker is not part of the bundle the gate reserves for"
    )
    required = backup_module._occupied_bytes(database, block_size=4096)
    # Four one-byte files still occupy four blocks, and the publication writes a
    # temporary before renaming. Anything at or near the four logical bytes is
    # the underestimate that let the marker be the file that ran out of room.
    assert required >= 4 * 4096, f"{required} does not cover four allocated blocks"


def _pause_marker_path(database: Path) -> Path:
    return database.with_name(f"{database.name}.workers-paused")


def test_a_corrupt_future_dated_snapshot_does_not_hide_the_valid_ones(tmp_path) -> None:
    """The reuse check must consider EVERY candidate, not the highest-named one.

    The name is not authoritative. An archive left by a clock that later stepped
    back sorts above everything forever, so one corrupt or old-key file in that
    position hid every valid archive beneath it: each restart failed the reuse
    check, wrote another lower-named backup that would likewise never be
    considered, and filled `/data` — the unbounded growth this check exists to
    prevent, caused by the way it picked.
    """
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "9999_broken.sql").write_text(
        "CREATE TABLE half_applied (id TEXT PRIMARY KEY);\nTHIS IS NOT SQL;\n",
        encoding="utf-8",
    )

    seed = _database(tmp_path)
    seed.migrate()
    revision = seed.applied_revision()
    seed.close()

    # Future-dated, so it sorts above every archive the boots below will write,
    # and unopenable, so it can never satisfy the check itself.
    poison = tmp_path / f"control-plane.db.pre-migrate-{revision}-9999999999.bak"
    poison.write_bytes(b"not an archive")

    for boot in range(4):
        database = _database(tmp_path)
        database.migrate()
        archive = create_pre_migrate_backup(
            database, backup_key=BACKUP_KEY_BYTES, directory=directory
        )
        assert archive is not None, f"boot {boot} took no snapshot"
        assert archive != poison
        with pytest.raises(sqlite3.DatabaseError):
            database.migrate(directory)
        database.close()

    written = sorted(path.name for path in tmp_path.glob("*.bak"))
    assert len(written) == 2, f"one snapshot per boot accumulated: {written}"
    assert poison.name in written, "the unreadable archive was deleted to shape the report"
    # And the archive that IS reused is the valid one, readable with the key.
    valid = next(path for path in tmp_path.glob("*.bak") if path != poison)
    assert backup_module._authenticated_digest(valid, BACKUP_KEY_BYTES) is not None


@pytest.mark.parametrize("position", ["database", "-wal", ".workers-paused"])
@pytest.mark.parametrize("shape", ["directory", "fifo", "symlink"])
def test_a_bundle_member_that_is_not_a_regular_file_is_refused(
    tmp_path, monkeypatch, position, shape
) -> None:
    """`is_file()` follows symlinks and says nothing about the other shapes.

    Each failure lands after the live database has already moved, and each is
    different: a directory raises on the rename, a FIFO blocks the copy
    indefinitely with the target already gone, and a symlink can leave the
    canonical path pointing outside the volume a Machine replacement preserves.
    `lstat` asks about the entry the installer will actually move.
    """
    volume, target, restored = _rollback_fixture(tmp_path)
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 10)
    member = (
        restored
        if position == "database"
        else restored.with_name(f"{restored.name}{position}")
    )
    member.unlink(missing_ok=True)
    if shape == "directory":
        member.mkdir()
    elif shape == "fifo":
        os.mkfifo(member)
    else:
        elsewhere = tmp_path / f"elsewhere{position}"
        elsewhere.write_bytes(target.read_bytes() if position == "database" else b"x")
        member.symlink_to(elsewhere)
    before = _fingerprint(volume)

    with pytest.raises(RollbackError, match="not a regular file|no worker pause"):
        install_rollback(restored, target, now=1800000042)

    assert _fingerprint(volume) == before, "a refused install moved the live bundle"
    assert "migrated_only" in _tables(target)


def test_a_name_too_long_for_the_filesystem_is_refused_before_the_move(
    tmp_path, monkeypatch
) -> None:
    """`exists()` answers False for a name the filesystem cannot hold.

    So the reservation accepted a bundle whose sidecar names were over the
    limit: on an ext4-like `NAME_MAX=255`, a 242-character basename yields a
    255-character aside database name and a 259-character aside WAL name. The
    database rename succeeded and the WAL rename raised `ENAMETOOLONG`, leaving
    no canonical database — and the obvious retry refuses, because the target it
    would supersede is missing. The name is only taken when every member of the
    bundle is both free and expressible.
    """
    volume = tmp_path / "data"
    staging = tmp_path / "staging"
    volume.mkdir()
    staging.mkdir()
    limit = backup_module._name_max(volume)
    # Long enough that ".superseded-1800000042-wal" pushes the sidecar over.
    name = "c" * (limit - len(".superseded-1800000042-wal"))
    target = volume / name
    database = ControlPlaneDatabase(target)
    database.migrate()
    database.close()
    (volume / f"{name}-wal").write_bytes(b"\x00" * 32)

    source = ControlPlaneDatabase(staging / "rollback.db")
    source.migrate()
    source.pause_workers()
    source.close()
    restored = staging / "rollback.db"
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 10)
    before = _fingerprint(volume)

    with pytest.raises(RollbackError, match="character limit"):
        install_rollback(restored, target, now=1800000042)

    assert _fingerprint(volume) == before
    assert target.exists(), "the canonical database is gone"


def test_a_running_writer_stops_a_restore(tmp_path) -> None:
    """Publishing under a live writer is the same hazard as installing under one.

    `install-rollback` took the writer lock; `restore` published a database and
    its pause marker at a path another process could be writing at the same
    time. Same lock, taken the same way, for the same reason.
    """
    seed = _database(tmp_path)
    seed.migrate()
    archive = tmp_path / "control-plane.cpb"
    create_backup(seed, archive, backup_key=BACKUP_KEY_BYTES)
    seed.close()

    destination = tmp_path / "restored"
    destination.mkdir()
    target = destination / "control-plane.db"
    writer = ControlPlaneDatabase(target)
    writer.acquire_process_lock()
    try:
        with pytest.raises(BackupError, match="already owns"):
            restore_backup(
                archive, target, backup_key=BACKUP_KEY_BYTES, apply_migrations=False
            )
    finally:
        writer.release_process_lock()
        writer.close()

    assert not target.exists()

    # And it succeeds once the writer lets go, so the guard is a gate not a wall.
    restored = restore_backup(
        archive, target, backup_key=BACKUP_KEY_BYTES, apply_migrations=False
    )
    try:
        assert restored.workers_paused
    finally:
        restored.close()


def test_the_volume_probe_cannot_collide_with_a_concurrent_one(tmp_path) -> None:
    """A PID is reused, and two containers on one volume share the namespace.

    The probe was named from `os.getpid()`, so two rollbacks racing on the same
    volume could write, rename and unlink each other's file — and the loser
    reads the wrong answer to "is this a copy", which decides whether the
    install is refused for space.
    """
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    seen: set[str] = set()
    real_rename = backup_module._rename_or_exdev

    def record(probe, landed):
        seen.add(probe.name)
        return real_rename(probe, landed)

    original = backup_module._rename_or_exdev
    backup_module._rename_or_exdev = record
    try:
        for _ in range(5):
            backup_module._is_a_copy(source, destination)
    finally:
        backup_module._rename_or_exdev = original

    assert len(seen) == 5, f"probe names repeat across calls: {seen}"
    assert all(name.startswith(".volume-probe-") for name in seen)
    # And nothing is left behind on either side.
    assert list(source.iterdir()) == []
    assert list(destination.iterdir()) == []


def test_the_documented_low_space_recovery_runs_end_to_end(tmp_path, monkeypatch) -> None:
    """The runbook's manual reclamation, executed exactly as written.

    It previously ended in an outage: it told the operator to delete
    `/data/control-plane.db` to release blocks, and the next `install-rollback`
    refused with "nothing to supersede". Following the supported procedure left
    the service database absent and the recovery command unusable.

    So the whole sequence runs here — archive off-volume, verify it restores,
    delete, install — and the volume has to end with a paused canonical
    database.
    """
    volume, target, restored = _rollback_fixture(tmp_path)
    off_volume = tmp_path / "off-volume"
    off_volume.mkdir()
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 3 // 2)

    # Refused first, with the volume untouched — which is what sends an operator
    # to the manual procedure.
    before = _fingerprint(volume)
    with pytest.raises(RollbackError, match="bytes and .* are free"):
        install_rollback(restored, target, now=1800000042)
    assert _fingerprint(volume) == before

    # Step 1: archive the superseded database off-volume, and verify it opens.
    superseded = ControlPlaneDatabase(target)
    archive = off_volume / "control-plane-superseded.cpb"
    try:
        create_backup(superseded, archive, backup_key=BACKUP_KEY_BYTES)
    finally:
        superseded.close()
    verify = restore_backup(
        archive,
        off_volume / "verify.db",
        backup_key=BACKUP_KEY_BYTES,
        apply_migrations=False,
    )
    verify.close()
    assert "migrated_only" in _tables(off_volume / "verify.db")

    # Step 2: only now release the blocks.
    for path in _bundle_paths(target):
        path.unlink(missing_ok=True)

    # Step 3: install, saying the target is gone on purpose.
    report = install_rollback(
        restored, target, now=1800000043, target_already_freed=True
    )

    assert report["superseded_kept_as"] is None
    assert report["workers"] == "paused"
    assert target.exists(), "the documented recovery ended without a database"
    assert "migrated_only" not in _tables(target), "the rollback was not installed"
    installed = ControlPlaneDatabase(target)
    try:
        assert installed.workers_paused
    finally:
        installed.close()


def _bundle_paths(database: Path) -> tuple[Path, ...]:
    return backup_module._bundle(database)


def test_the_freed_target_mode_refuses_a_target_that_was_not_freed(
    tmp_path, monkeypatch
) -> None:
    """A leftover member means the target is still there under another name.

    The flag says "I archived and deleted it"; if any bundle member is still
    present that is not true, and installing over it would separate a database
    from its own sidecars.
    """
    volume, target, restored = _rollback_fixture(tmp_path)
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 10)
    target.unlink()
    (volume / "control-plane.db-wal").write_bytes(b"\x00" * 4096)
    before = _fingerprint(volume)

    with pytest.raises(RollbackError, match="was not freed"):
        install_rollback(
            restored, target, now=1800000042, target_already_freed=True
        )

    assert _fingerprint(volume) == before


def test_a_missing_target_without_the_flag_says_what_to_do(tmp_path, monkeypatch) -> None:
    volume, target, restored = _rollback_fixture(tmp_path)
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 10)
    for path in _bundle_paths(target):
        path.unlink(missing_ok=True)

    with pytest.raises(RollbackError, match="--target-already-freed"):
        install_rollback(restored, target, now=1800000042)


def test_the_pause_marker_is_installed_before_the_database(tmp_path, monkeypatch) -> None:
    """A database visible before its marker is one that can be started unpaused.

    Installing in bundle order put the database at the canonical path first, so
    a crash or a failed copy before the marker arrived left a startable,
    unreconciled rollback.
    """
    volume, target, restored = _rollback_fixture(tmp_path)
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 10)
    order: list[str] = []
    real_replace = os.replace

    def replace(source, destination, **kwargs):
        landed = Path(destination)
        if landed == target:
            order.append("database")
        elif landed == _pause_marker_path(target):
            order.append("marker")
        return real_replace(source, destination, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    install_rollback(restored, target, now=1800000042)

    # Two entries per file: `_rename_or_exdev` attempts the rename, and on
    # `EXDEV` `_copy_install` publishes the temporary. What matters is that
    # EVERY marker event precedes every database event — the marker is durably
    # in place before the database it guards becomes visible.
    assert set(order) == {"marker", "database"}, order
    assert max(i for i, v in enumerate(order) if v == "marker") < min(
        i for i, v in enumerate(order) if v == "database"
    ), order


def test_a_writer_on_the_candidate_stops_the_install(tmp_path, monkeypatch) -> None:
    """The candidate is a control-plane database too.

    A `restore` still finishing at that path, or a second install reading it,
    would have its bundle moved out from under it.
    """
    volume, target, restored = _rollback_fixture(tmp_path)
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 10)
    before = _fingerprint(volume)

    holder = ControlPlaneDatabase(restored)
    holder.acquire_process_lock()
    try:
        with pytest.raises(RollbackError, match="still owns"):
            install_rollback(restored, target, now=1800000042)
    finally:
        holder.release_process_lock()
        holder.close()

    assert _fingerprint(volume) == before
    assert "migrated_only" in _tables(target)


def test_a_restore_target_created_after_the_check_is_not_clobbered(
    tmp_path, monkeypatch
) -> None:
    """The existence check has to be inside the lock the refusal depends on."""
    seed = _database(tmp_path)
    seed.migrate()
    archive = tmp_path / "control-plane.cpb"
    create_backup(seed, archive, backup_key=BACKUP_KEY_BYTES)
    seed.close()

    destination = tmp_path / "restored"
    destination.mkdir()
    target = destination / "control-plane.db"
    real_acquire = ControlPlaneDatabase.acquire_process_lock

    def acquire_then_race(self):
        real_acquire(self)
        # Another process wins the path between the outer check and the lock.
        if not target.exists():
            target.write_bytes(b"someone else got here first")

    monkeypatch.setattr(ControlPlaneDatabase, "acquire_process_lock", acquire_then_race)

    with pytest.raises(FileExistsError):
        restore_backup(
            archive, target, backup_key=BACKUP_KEY_BYTES, apply_migrations=False
        )

    assert target.read_bytes() == b"someone else got here first"

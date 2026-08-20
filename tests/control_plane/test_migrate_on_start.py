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
    verify_at = doc.index('--target "$verify/control-plane.db" --no-migrate')
    offsite_at = doc.index("upload /tmp/control-plane-superseded.cpb")
    delete_at = doc.index("rm -f /data/control-plane.db /data/control-plane.db-wal")
    # Archive, verify, get it OFF the machine, and only then delete. After the
    # delete this archive is the only copy of every write made since the
    # pre-migrate snapshot, so it must not still be sitting on ephemeral
    # storage the next Machine replacement discards.
    assert archive_at < verify_at < offsite_at < delete_at, (
        "the documented order deletes the source before the archive is durable"
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
    real_fsync, real_link = os.fsync, os.link
    real_migrate = ControlPlaneDatabase.migrate

    def fsync(fd):
        trace.append("fsync:dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "fsync:file")
        return real_fsync(fd)

    def link(source, target, **kwargs):
        # Publication is an atomic CLAIM now — `os.link`, which refuses to
        # replace — not `os.replace`. The trace follows the code.
        if published(Path(target)):
            trace.append("rename")
        return real_link(source, target, **kwargs)

    def migrate(self, directory=None):
        trace.append("migrate")
        return real_migrate(self, directory)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "link", link)
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
    real_fsync, real_link = os.fsync, os.link

    def fsync(fd):
        trace.append("fsync:dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "fsync:file")
        return real_fsync(fd)

    def link(source, destination, **kwargs):
        landed = Path(destination)
        if landed == target:
            trace.append("publish:database")
        elif landed == marker:
            trace.append("publish:marker")
        return real_link(source, destination, **kwargs)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "link", link)
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
    real_link = os.link

    def link(source, target, **kwargs):
        if Path(target).parent == volume and Path(source).parent != volume:
            raise OSError(errno.EXDEV, "Cross-device link")
        return real_link(source, target, **kwargs)

    real_free = backup_module._free_bytes

    def free(directory) -> int:
        if Path(directory) != volume:
            return real_free(directory)
        used = sum(path.stat().st_size for path in volume.iterdir() if path.is_file())
        return max(0, capacity - used)

    monkeypatch.setattr(os, "link", link)
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


def _sidecars_of(database: Path) -> tuple[Path, ...]:
    return tuple(Path(f"{database}{suffix}") for suffix in ("-wal", "-shm"))


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


def _documented_reclamation_paths(volume: Path) -> list[Path]:
    """The files `docs/control-plane-restore.md` tells the operator to remove.

    Parsed from the page rather than restated here, so the test cannot drift
    away from the procedure it is meant to prove.
    """
    doc = (
        Path(__file__).resolve().parents[2] / "docs/control-plane-restore.md"
    ).read_text(encoding="utf-8")
    block = doc[doc.index("# 2. Only now release the blocks") :]
    block = block[: block.index("# 3.")]
    named = [
        token
        for token in block.replace("\\", " ").split()
        if token.startswith("/data/")
    ]
    assert named, "the runbook no longer documents a reclamation step"
    return [volume / Path(name).name for name in named]


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
    # The superseded database is PAUSED, which is the realistic state and the
    # one that exposes an incomplete reclamation list: a database that was
    # itself restored carries its marker until `resume-jobs`, so rolling back a
    # rollback — or rolling back twice — starts here. Without this the marker
    # does not exist, omitting it from the runbook changes nothing, and the test
    # passes over the discrepancy it exists to catch.
    _pause_marker_path(target).write_text("paused\n", encoding="utf-8")
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

    # Step 2: only now release the blocks — running the paths the RUNBOOK
    # lists, not the bundle the code knows about. The previous version of this
    # test unlinked `_bundle(target)` and so tested the implementation against
    # itself: the page listed three files and the code required four, and the
    # documented procedure failed at step 3 with the destructive step already
    # done. A test of a documented procedure has to execute the document.
    for path in _documented_reclamation_paths(volume):
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
    """The database is published LAST, after its marker and its sidecars.

    A database visible before its marker can be started unpaused. A database
    visible before its sidecars is worse and quieter: a staged candidate
    inspected by a process that was then killed still has committed frames in
    its `-wal`, and a crash between the two publications leaves a canonical
    database whose own latest commits are back at the staging path, which the
    next open reads straight past.
    """
    volume, target, restored = _rollback_fixture(tmp_path)
    # Give the candidate a WAL, so there is a sidecar to order against.
    restored.with_name(f"{restored.name}-wal").write_bytes(b"\x00" * 4096)
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 10)
    order: list[str] = []
    real_link = os.link

    def link(source, destination, **kwargs):
        landed = Path(destination)
        if landed == target:
            order.append("database")
        elif landed == _pause_marker_path(target):
            order.append("marker")
        elif landed.name.endswith(("-wal", "-shm")):
            order.append("sidecar")
        return real_link(source, destination, **kwargs)

    monkeypatch.setattr(os, "link", link)
    install_rollback(restored, target, now=1800000042)

    # Two entries per file: `_rename_or_exdev` attempts the rename, and on
    # `EXDEV` `_copy_install` publishes the temporary. What matters is that
    # EVERY marker and sidecar event precedes every database event — the
    # database becoming visible is what makes the rollback real, so everything
    # belonging to that generation is in place first.
    assert "database" in order and "marker" in order, order
    first_database = min(i for i, v in enumerate(order) if v == "database")
    assert all(
        i < first_database for i, v in enumerate(order) if v != "database"
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


def test_backup_does_not_migrate_the_database_it_archives(tmp_path, monkeypatch) -> None:
    """The command you reach for when a deployment is broken must not need it.

    `backup` ran through `_container()`, and `ControlPlaneContainer.build`
    migrates. So on the principal call path — a persistently failing pending
    migration, which is exactly when a superseded database must be archived
    before its blocks are released — it repeated that migration and exited
    before writing anything. The operator could not free space safely, and the
    documented low-space recovery was unreachable.
    """
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_DB", str(tmp_path / "control-plane.db"))
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_BACKUP_KEY", BACKUP_KEY)
    seed = _database(tmp_path)
    seed.migrate()
    seed.close()
    monkeypatch.setattr(
        ControlPlaneDatabase,
        "migrate",
        lambda *a, **k: pytest.fail("backup migrated the database it was archiving"),
    )

    archive = tmp_path / "superseded.cpb"
    assert cli_main(["backup", str(archive)]) == 0
    assert archive.exists()
    assert backup_module._authenticated_digest(archive, BACKUP_KEY_BYTES) is not None


def test_backup_refuses_while_a_writer_holds_the_lock(tmp_path, monkeypatch) -> None:
    """Archiving a database another process is writing captures a moment that
    never existed. The manual reclamation brackets its `rm` with this command
    and the install, so both ends must refuse rather than proceed."""
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_DB", str(tmp_path / "control-plane.db"))
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_BACKUP_KEY", BACKUP_KEY)
    seed = _database(tmp_path)
    seed.migrate()
    seed.close()

    writer = ControlPlaneDatabase(tmp_path / "control-plane.db")
    writer.acquire_process_lock()
    try:
        with pytest.raises(SystemExit, match="backup refused"):
            cli_main(["backup", str(tmp_path / "superseded.cpb")])
    finally:
        writer.release_process_lock()
        writer.close()

    assert not (tmp_path / "superseded.cpb").exists()


@pytest.mark.parametrize("shape", ["directory", "file", "symlink"])
def test_an_occupied_copy_temporary_cannot_break_the_install(
    tmp_path, monkeypatch, shape
) -> None:
    """The temporary name came from the destination, so anything could hold it.

    A directory or unwritable entry at `<destination>.installing` raised only
    AFTER the live database had been renamed aside; a regular file or symlink
    was followed and truncated. Either an outage with no canonical database, or
    an unrelated file destroyed. `mkstemp` takes the name from the kernel with
    `O_EXCL`, so the install owns what it publishes from.
    """
    volume, target, restored = _rollback_fixture(tmp_path)
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 10)
    squatter = volume / f".{target.name}.installing"
    bystander = tmp_path / "bystander"
    bystander.write_bytes(b"not part of this rollback")
    if shape == "directory":
        squatter.mkdir()
    elif shape == "file":
        squatter.write_bytes(b"someone else's file")
    else:
        squatter.symlink_to(bystander)

    report = install_rollback(restored, target, now=1800000042)

    assert report["workers"] == "paused"
    assert "migrated_only" not in _tables(target), "the rollback was not installed"
    assert bystander.read_bytes() == b"not part of this rollback"
    if shape == "file":
        assert squatter.read_bytes() == b"someone else's file"


def test_a_dangling_symlink_in_the_bundle_is_refused(tmp_path, monkeypatch) -> None:
    """`exists()` follows symlinks, so a dangling one answered False.

    It therefore skipped every validation, survived the superseded move, and sat
    beside the installed rollback — where the next SQLite write follows it out
    of the volume a Machine replacement preserves.
    """
    volume, target, restored = _rollback_fixture(tmp_path)
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 10)
    dangling = volume / f"{target.name}-wal"
    dangling.symlink_to(tmp_path / "nothing-is-here")
    assert not dangling.exists(), "the fixture link is not dangling"
    assert os.path.lexists(dangling), "the fixture link does not exist as an entry"
    before = _fingerprint(volume)

    with pytest.raises(RollbackError, match="not a regular file"):
        install_rollback(restored, target, now=1800000042)

    assert _fingerprint(volume) == before
    assert os.path.lexists(dangling), "the refusal removed the entry it objected to"


def test_a_symlinked_snapshot_is_not_accepted_as_the_restore_point(tmp_path) -> None:
    """The archive has to be ON the volume, not point off it.

    A symlink matching the glob — to an archive in `/tmp` or any other ephemeral
    filesystem — authenticated perfectly and was returned as the restore point,
    so the migration proceeded believing a durable snapshot sat beside the
    database while removing the link's target left no rollback artifact at all.
    Proving something durable exists on the volume is the entire job of this
    check.
    """
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "9999_pilot_probe.sql").write_text(
        "CREATE TABLE pilot_probe (id TEXT PRIMARY KEY);\n", encoding="utf-8"
    )
    elsewhere = tmp_path / "ephemeral"
    elsewhere.mkdir()

    seed = _database(tmp_path)
    seed.migrate()
    revision = seed.applied_revision()
    # A genuine, authenticating archive — taken from this very database, so
    # content matching cannot be what rejects it.
    real = elsewhere / "offsite.bak"
    create_backup(seed, real, backup_key=BACKUP_KEY_BYTES)
    seed.close()

    link = tmp_path / f"control-plane.db.pre-migrate-{revision}-1800000000.bak"
    link.symlink_to(real)
    assert backup_module._authenticated_digest(link, BACKUP_KEY_BYTES) is not None, (
        "the fixture link does not authenticate, so this proves nothing"
    )

    database = _database(tmp_path)
    try:
        archive = create_pre_migrate_backup(
            database, backup_key=BACKUP_KEY_BYTES, directory=directory
        )
    finally:
        database.close()

    assert archive is not None
    assert archive != link, "a symlink off the volume was reused as the restore point"
    assert not archive.is_symlink()
    assert archive.parent == tmp_path


def test_a_restore_refuses_a_destination_with_stale_sidecars(tmp_path) -> None:
    """A `-wal` from another database replays into the authenticated restore.

    An interrupted cleanup or a killed process leaves `<target>-wal` behind with
    the database itself gone. The existence check looked only at the database,
    so publication put the restore beside those frames — and opening it replays
    them, silently replacing restored rows while `integrity_check` still reports
    `ok`. Nothing downstream notices, which is what makes it worth refusing.
    """
    seed = _database(tmp_path)
    seed.migrate()
    archive = tmp_path / "control-plane.cpb"
    create_backup(seed, archive, backup_key=BACKUP_KEY_BYTES)
    seed.close()

    destination = tmp_path / "restored"
    destination.mkdir()
    target = destination / "control-plane.db"
    stale = destination / "control-plane.db-wal"
    stale.write_bytes(b"\x00" * 4096)

    with pytest.raises(BackupError, match="leftover SQLite state"):
        restore_backup(
            archive, target, backup_key=BACKUP_KEY_BYTES, apply_migrations=False
        )

    assert not target.exists(), "the restore was published beside foreign state"
    assert stale.read_bytes() == b"\x00" * 4096, "the refusal deleted what it objected to"


def test_backup_refuses_a_missing_database_rather_than_archiving_nothing(
    tmp_path, monkeypatch
) -> None:
    """An empty database passes every check the verification step runs.

    `sqlite3.connect` creates a missing file, so an unset, mistyped or unmounted
    `ABROLIA_CONTROL_PLANE_DB` produced a new empty database, a valid
    authenticated archive of nothing, and a verification restore that passed —
    an empty database satisfies `integrity_check` and `foreign_key_check`. The
    operator then deletes the real `/data` bundle believing it is archived.
    """
    absent = tmp_path / "not-mounted" / "control-plane.db"
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_DB", str(absent))
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_BACKUP_KEY", BACKUP_KEY)

    # "found no database", not "refused": the existence check runs BEFORE the
    # lock, because acquiring the lock creates the parent directory and the lock
    # file — doing that for an unmounted path is itself a mutation this command
    # must not make.
    with pytest.raises(SystemExit, match="found no database"):
        cli_main(["backup", str(tmp_path / "superseded.cpb")])

    assert not (tmp_path / "superseded.cpb").exists()
    assert not absent.exists(), "the refusal created the database it refused to find"
    assert not absent.parent.exists()


def test_an_unreadable_migration_ledger_is_not_treated_as_empty(tmp_path) -> None:
    """"Nothing applied" is a claim, and only a missing table supports it.

    Any `DatabaseError` reading `schema_migrations` was read as "nothing has been
    applied", so a malformed or unreadable ledger made the boot attempt every
    migration from 0001 — which on a production schema fails on objects that
    already exist, and with an idempotent script would redo work whose ledger
    entry it simply could not read.
    """
    database = _database(tmp_path)
    database.migrate()
    applied = database.query("SELECT name FROM schema_migrations")
    assert applied, "the fixture applied no migrations"
    with database.write() as connection:
        # A ledger that exists and cannot be read as one.
        connection.execute("DROP TABLE schema_migrations")
        connection.execute("CREATE TABLE schema_migrations (wrong_column TEXT)")
    database.close()

    probe = _database(tmp_path)
    try:
        # The LEDGER's own error, raised where it happened. Asserting only
        # `DatabaseError` proves nothing: treating the unreadable ledger as
        # empty also raises one, from a `CREATE TABLE` for an object that
        # already exists, several migrations later and with a message that
        # sends an operator to the wrong place entirely.
        with pytest.raises(sqlite3.DatabaseError, match="no such column"):
            probe.migrate()
    finally:
        probe.close()

    # And nothing was applied on top of the existing schema.
    after = _database(tmp_path)
    try:
        assert "pilot_probe" not in _table_names(after)
    finally:
        after.close()


def _table_names(database) -> set[str]:
    return {
        row["name"]
        for row in database.query(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


@pytest.mark.parametrize(
    ("make", "message"),
    [
        # A zero-byte file IS a valid empty SQLite database, so the ledger
        # check is what catches it — which is exactly the case the previous
        # `is_file()` guard let through and the operator then deleted `/data`
        # believing it was archived.
        pytest.param(lambda p: p.write_bytes(b""), "no migration ledger", id="empty-file"),
        pytest.param(lambda p: p.write_bytes(b"not sqlite at all"), "not a valid SQLite|does not open", id="not-sqlite"),
        pytest.param(
            lambda p: _foreign_sqlite(p), "no migration ledger", id="another-database"
        ),
        pytest.param(lambda p: p.symlink_to("/dev/null"), "not a regular file", id="symlink"),
    ],
)
def test_backup_refuses_anything_that_is_not_the_control_plane_database(
    tmp_path, monkeypatch, make, message
) -> None:
    """Archiving the wrong file is worse than refusing.

    The operator verifies the archive, it passes, and they delete the real
    bundle. `is_file()` established nothing useful: a zero-byte file, an
    unrelated SQLite database and a symlink to either all satisfy it. What has
    to be true is that this IS the control-plane database.
    """
    database_path = tmp_path / "control-plane.db"
    make(database_path)
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_DB", str(database_path))
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_BACKUP_KEY", BACKUP_KEY)

    with pytest.raises(SystemExit, match=message):
        cli_main(["backup", str(tmp_path / "superseded.cpb")])

    assert not (tmp_path / "superseded.cpb").exists()


def _foreign_sqlite(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE somebody_elses (id TEXT)")
        connection.commit()
    finally:
        connection.close()


def test_a_dangling_symlink_at_a_publication_path_is_refused(tmp_path) -> None:
    """`Path.exists()` reports a dangling link absent, and `os.replace` eats it.

    The archive would publish over an entry the operation does not own,
    destroying a link that meant something else and taking its name.
    """
    seed = _database(tmp_path)
    seed.migrate()
    try:
        occupied = tmp_path / "taken.cpb"
        occupied.symlink_to(tmp_path / "nothing-here")
        assert not occupied.exists() and os.path.lexists(occupied)

        with pytest.raises(FileExistsError):
            create_backup(seed, occupied, backup_key=BACKUP_KEY_BYTES)

        assert os.path.lexists(occupied), "the refusal consumed the entry"
        assert occupied.is_symlink()
    finally:
        seed.close()


def test_an_entry_created_during_the_backup_is_not_replaced(tmp_path, monkeypatch) -> None:
    """The check at the top cannot see what appears while the work runs.

    Materialising an image and encrypting it takes as long as the database is
    big, and an entry created at the destination in that window was invisible to
    it — `os.replace` then destroyed the file without a word and took its name.
    """
    seed = _database(tmp_path)
    seed.migrate()
    destination = tmp_path / "racing.cpb"
    real_materialise = backup_module._materialise

    def materialise_then_race(database, target):
        real_materialise(database, target)
        # Somebody else claims the name while this backup is still working.
        destination.write_bytes(b"not this backup's file")

    monkeypatch.setattr(backup_module, "_materialise", materialise_then_race)
    try:
        with pytest.raises(FileExistsError):
            create_backup(seed, destination, backup_key=BACKUP_KEY_BYTES)
    finally:
        seed.close()

    assert destination.read_bytes() == b"not this backup's file", (
        "the backup replaced a file that appeared while it was running"
    )
    assert not list(tmp_path.glob(".racing.cpb.*")), "a temporary was left behind"


def test_a_failure_midway_leaves_the_original_bundle_standing(tmp_path, monkeypatch) -> None:
    """Either the original bundle stands or the new one is complete.

    A rename that raises after an earlier member moved — an `EIO` on the WAL, a
    cross-filesystem copy running out of space after the marker landed — escaped
    with the bundle half-apart: no canonical database, no complete candidate,
    and a retry that refuses because the target is missing. There is no third
    acceptable state.
    """
    volume, target, restored = _rollback_fixture(tmp_path)
    (volume / f"{target.name}-wal").write_bytes(b"\x00" * 4096)
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 10)
    before = _fingerprint(volume)
    real_link = os.link
    seen = {"count": 0}

    def fail_on_the_second_move(source, destination, **kwargs):
        seen["count"] += 1
        if seen["count"] == 2:
            raise OSError(errno.EIO, "the volume gave up mid-bundle")
        return real_link(source, destination, **kwargs)

    monkeypatch.setattr(os, "link", fail_on_the_second_move)

    with pytest.raises(OSError, match="gave up mid-bundle"):
        install_rollback(restored, target, now=1800000042)

    monkeypatch.undo()
    assert _fingerprint(volume) == before, (
        "a failure left the canonical bundle partly moved aside"
    )
    assert "migrated_only" in _tables(target), "the live database did not come back"


def test_a_snapshot_of_a_damaged_database_does_not_open_the_gate(tmp_path) -> None:
    """A snapshot of a corrupt database is not a restore point.

    `backup()` materialises whatever state the file is in and the archive
    authenticates perfectly, and a reusable archive is accepted on its digest
    alone — which proves the archive matches the database, not that the database
    can be restored. The gate this snapshot guards is "may the migration
    proceed".
    """
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "9999_pilot_probe.sql").write_text(
        "CREATE TABLE pilot_probe (id TEXT PRIMARY KEY);\n", encoding="utf-8"
    )
    database = _database(tmp_path)
    database.migrate()
    database.close()
    # A foreign-key violation the integrity check tolerates but the restore
    # would carry forward. Written through a raw connection: SQLite ignores
    # `PRAGMA foreign_keys` inside a transaction, so the repository's own
    # `write()` cannot create this state.
    raw = sqlite3.connect(tmp_path / "control-plane.db")
    try:
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute(
            "CREATE TABLE dangling (id TEXT PRIMARY KEY,"
            " missing TEXT REFERENCES households (id))"
        )
        raw.execute("INSERT INTO dangling (id, missing) VALUES ('a', 'nobody')")
        raw.commit()
    finally:
        raw.close()
    database = _database(tmp_path)

    try:
        with pytest.raises(BackupError, match="foreign-key check"):
            create_pre_migrate_backup(
                database, backup_key=BACKUP_KEY_BYTES, directory=directory
            )
    finally:
        database.close()

    assert list(tmp_path.glob("*.bak")) == [], "a snapshot of damaged state was kept"


def test_an_unsyncable_removal_keeps_the_worker_pause(tmp_path, monkeypatch) -> None:
    """A database that may survive must keep its pause.

    `_publish` unlinks the database when the directory sync fails — and if that
    removal cannot itself be synced, whether the entry survives a crash is
    unknown. Removing the marker on that path would leave a database present and
    unpaused, reached through the CLEANUP of a different failure.
    """
    seed = _database(tmp_path)
    seed.migrate()
    archive = tmp_path / "control-plane.cpb"
    create_backup(seed, archive, backup_key=BACKUP_KEY_BYTES)
    seed.close()

    destination = tmp_path / "restored"
    destination.mkdir()
    target = destination / "control-plane.db"
    real_fsync = os.fsync
    directories = {"seen": 0}

    def refuse_the_second_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directories["seen"] += 1
            # The FIRST directory sync publishes the pause marker and must
            # succeed — the marker has to be durably in place for its removal to
            # be the thing under test. The second is the database's.
            if directories["seen"] > 1:
                raise OSError(errno.EIO, "directory sync failed")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", refuse_the_second_directory)

    with pytest.raises(BackupError, match="may or may not exist"):
        restore_backup(
            archive, target, backup_key=BACKUP_KEY_BYTES, apply_migrations=False
        )

    monkeypatch.undo()
    assert _pause_marker_path(target).exists(), (
        "the pause was removed while the database it guards may have survived"
    )


def test_publication_claims_a_name_and_never_replaces_one(tmp_path) -> None:
    """`_claim` refuses an occupied name; it does not check and then rename.

    What this can and cannot show is worth being exact about. The race being
    closed — another process taking the final name between a check and a rename
    — is not reproducible in-process: any seam that would let a test occupy that
    instant is a seam the non-atomic version does not have, so a test built on
    one proves only that the seam was called. That is exactly why the code must
    not depend on a check at all.

    So this asserts the primitive's contract directly: an occupied destination
    is refused, its content is untouched, and the source survives for the caller
    to clean up. `os.link` gives that with no window; a check followed by
    `os.replace` gives it only when nobody intervenes.
    """
    source = tmp_path / "incoming"
    source.write_bytes(b"the new archive")
    occupied = tmp_path / "already-here"
    occupied.write_bytes(b"somebody else's file")

    with pytest.raises(FileExistsError):
        backup_module._claim(source, occupied)

    assert occupied.read_bytes() == b"somebody else's file"
    assert source.read_bytes() == b"the new archive"

    # And it does move onto a free name, so the refusal is not the only
    # behaviour it has.
    free = tmp_path / "free-name"
    backup_module._claim(source, free)
    assert free.read_bytes() == b"the new archive"
    assert not source.exists()


def test_the_ledger_check_looks_past_the_table_name(tmp_path, monkeypatch) -> None:
    """An unrelated database can have a table called `schema_migrations`.

    `CREATE TABLE schema_migrations(name TEXT)` in somebody else's database
    passed integrity, foreign keys and the existence check, and was archived
    successfully — after which the operator verifies a perfectly good archive of
    the wrong file and deletes the real one. The ledger's columns and its
    contents are what make it the control-plane one.
    """
    impostor = tmp_path / "control-plane.db"
    raw = sqlite3.connect(impostor)
    try:
        raw.execute("CREATE TABLE schema_migrations (name TEXT)")
        raw.execute("INSERT INTO schema_migrations VALUES ('not a migration')")
        raw.commit()
    finally:
        raw.close()
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_DB", str(impostor))
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_BACKUP_KEY", BACKUP_KEY)

    with pytest.raises(SystemExit, match="not the control-plane ledger"):
        cli_main(["backup", str(tmp_path / "superseded.cpb")])

    assert not (tmp_path / "superseded.cpb").exists()


def test_backup_takes_the_lock_before_it_validates(tmp_path, monkeypatch) -> None:
    """Exclusive ownership is a precondition of the check, not a companion.

    Validating and then locking leaves a window in which another process
    replaces the database, so the command authenticates one file and archives
    another — and `_readable_sqlite` can remove sidecars a writer starting in
    that window has just created.
    """
    seed = _database(tmp_path)
    seed.migrate()
    seed.close()
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_DB", str(tmp_path / "control-plane.db"))
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_BACKUP_KEY", BACKUP_KEY)

    order: list[str] = []
    real_acquire = ControlPlaneDatabase.acquire_process_lock
    real_require = backup_module.require_control_plane_database

    def acquire(self):
        order.append("lock")
        return real_acquire(self)

    def require(path):
        order.append("validate")
        return real_require(path)

    monkeypatch.setattr(ControlPlaneDatabase, "acquire_process_lock", acquire)
    monkeypatch.setattr(backup_module, "require_control_plane_database", require)
    monkeypatch.setattr(
        "control_plane.cli.require_control_plane_database", require, raising=False
    )

    assert cli_main(["backup", str(tmp_path / "archive.cpb")]) == 0

    assert order[:2] == ["lock", "validate"], order


def test_the_volume_probe_deletes_only_what_it_created(tmp_path, monkeypatch) -> None:
    """A NAME can be reused between creating a temporary and removing it.

    The cleanup unlinked both probe names unconditionally: after a successful
    move the source name is vacant, and after `EXDEV` the destination is — so
    another process creating either in that window had its file deleted by a
    preflight that never owned it.
    """
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    real_rename = backup_module._rename_or_exdev
    interlopers: list[Path] = []

    def rename_then_squat(probe, landed, journal=None):
        moved = real_rename(probe, landed, journal)
        # Somebody else takes the name this call has just vacated.
        vacated = probe if moved else landed
        vacated.write_bytes(b"somebody else's file")
        interlopers.append(vacated)
        return moved

    monkeypatch.setattr(backup_module, "_rename_or_exdev", rename_then_squat)
    backup_module._is_a_copy(source, destination)

    assert interlopers, "the fixture never created an interloper"
    for path in interlopers:
        assert path.exists(), f"the probe deleted {path}, which it did not create"
        assert path.read_bytes() == b"somebody else's file"


def test_a_move_is_journalled_before_its_source_unlink(tmp_path, monkeypatch) -> None:
    """The link IS the move; the unlink is tidying after it.

    `_claim` unlinked the source before recording that the destination now
    existed, so an unlink failing on its own — `EIO`, `EPERM`, a read-only
    remount — left the caller's journal unaware of a move that had already
    happened, and `_undo` could not reverse it.

    Tested directly on `_claim`. Driving it through a whole install kept
    intercepting one of the several other `os.link` sites — the volume probe
    among them — and a race test aimed at the wrong call proves nothing, which
    is a mistake this session has already made more than once.
    """
    source = tmp_path / "moving"
    source.write_bytes(b"the bundle member")
    destination = tmp_path / "landed"
    journal: list[backup_module._Move] = []
    real_unlink = Path.unlink

    def refuse(self, **kwargs):
        if self == source:
            raise OSError(errno.EIO, "the unlink failed after the link succeeded")
        return real_unlink(self, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse)

    with pytest.raises(OSError, match="unlink failed"):
        backup_module._claim(source, destination, journal)

    monkeypatch.undo()
    assert len(journal) == 1, (
        "a move that had already happened went unrecorded, so a reversal would"
        " not know to undo it"
    )
    recorded = journal[0]
    assert (recorded.source, recorded.destination) == (source, destination)
    assert destination.exists(), "the link that succeeded is not there"
    # The half that did NOT happen. A journal saying only "this move happened"
    # cannot describe both names holding the generation, and `_undo` reading it
    # that way put the source back — over a source that was still there.
    assert not recorded.source_removed, (
        "the unlink failed, so the source has not left; recording the move as"
        " complete makes the reversal try to restore a name already occupied"
    )
    assert recorded.published_inode == (
        destination.lstat().st_dev,
        destination.lstat().st_ino,
    )


def test_a_failed_source_unlink_is_reversed_by_removing_the_destination(
    tmp_path,
) -> None:
    """Published, source still standing: the reversal is a removal.

    `_undo` asked one question — is the destination there and the source
    gone? — and a move whose unlink failed answers no, because the source IS
    still there. It therefore skipped the reversal entirely and left the
    destination published: for an install, a rollback database standing beside
    the superseded generation's sidecars; for a restore, the pause withdrawn
    while the database it guards remains.
    """
    source = tmp_path / "moving"
    source.write_bytes(b"the bundle member")
    destination = tmp_path / "landed"
    journal: list[backup_module._Move] = []
    os.link(source, destination)
    journal.append(
        backup_module._Move(
            source,
            destination,
            published_inode=(destination.lstat().st_dev, destination.lstat().st_ino),
        )
    )

    backup_module._undo(journal)

    assert not destination.exists(), (
        "the destination was published and never withdrawn, so the bundle is"
        " left half-apart"
    )
    assert source.read_bytes() == b"the bundle member", "the source was disturbed"


def test_undo_leaves_a_destination_another_process_has_replaced(tmp_path) -> None:
    """Reversal by name destroys whoever holds the name now."""
    source = tmp_path / "moving"
    destination = tmp_path / "landed"
    journal = [backup_module._Move(source, destination, published_inode=(1, 2))]
    destination.write_bytes(b"somebody else's file")

    backup_module._undo(journal)

    assert destination.read_bytes() == b"somebody else's file"


def test_an_unrelated_application_ledger_is_not_this_database(
    tmp_path, monkeypatch
) -> None:
    """`schema_migrations(name, applied_at)` is a shared convention, not an identity.

    The previous check accepted any SQLite database whose ledger had those two
    columns and whose rows looked like `NNNN_something.sql` — a shape most
    migration frameworks produce. A mistyped `ABROLIA_CONTROL_PLANE_DB` could
    therefore point at a completely unrelated service's database, archive it,
    verify the archive, and license the operator to delete the real bundle.
    """
    impostor = tmp_path / "control-plane.db"
    raw = sqlite3.connect(impostor)
    try:
        raw.execute("CREATE TABLE schema_migrations (name TEXT, applied_at TEXT)")
        for name in ("0001_create_users.sql", "0002_add_billing.sql"):
            raw.execute(
                "INSERT INTO schema_migrations VALUES (?, '2026-01-01T00:00:00Z')",
                (name,),
            )
        raw.commit()
    finally:
        raw.close()
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_DB", str(impostor))
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_BACKUP_KEY", BACKUP_KEY)

    with pytest.raises(SystemExit, match="some other application's database"):
        cli_main(["backup", str(tmp_path / "superseded.cpb")])

    assert not (tmp_path / "superseded.cpb").exists()
    assert impostor.exists(), "the refusal mutated the source it refused"


def test_a_database_stopped_at_a_failed_migration_is_still_this_database(
    tmp_path,
) -> None:
    """Identity has to hold at every revision, including a partial one.

    The low-space recovery procedure runs `backup` against a database whose
    pending migration failed — that is precisely why an operator is there — so
    a check demanding the complete ledger would refuse exactly the bundle worth
    archiving. Every prefix of the shipped sequence is this database.
    """
    seed = _database(tmp_path)
    seed.migrate()
    seed.close()
    shipped = backup_module._shipped_migrations()
    assert len(shipped) > 1, "the fixture cannot truncate a one-migration ledger"

    for keep in range(1, len(shipped) + 1):
        raw = sqlite3.connect(tmp_path / "control-plane.db")
        try:
            raw.execute(
                "DELETE FROM schema_migrations WHERE name NOT IN"
                f" ({','.join('?' * keep)})",
                shipped[:keep],
            )
            raw.commit()
        finally:
            raw.close()
        backup_module.require_control_plane_database(tmp_path / "control-plane.db")


@pytest.mark.parametrize(
    "forward_migration",
    [
        "ALTER TABLE accounts ADD COLUMN added_by_0009 TEXT",
        "CREATE INDEX accounts_added_by_0009 ON accounts (id)",
        "CREATE TABLE entirely_new_in_0009 (id TEXT PRIMARY KEY)",
    ],
)
def test_a_newer_database_than_this_image_is_still_this_database(
    tmp_path, forward_migration
) -> None:
    """An image rolled back sees migrations it does not ship.

    Refusing there denies the operator the backup command at the moment a bad
    deploy is being undone — and a forward migration that ALTERS a known object
    changes that object's stored definition, so a byte-equal schema comparison
    rejected exactly the database the rollback procedure has to archive before
    freeing the volume. The recovery stopped at its own data-preservation gate.

    The shared prefix establishes identity. Past it, the requirement is that
    every object the prefix declares is still present and its tables still have
    at least the columns it gave them: migrations add, they do not remove.
    """
    seed = _database(tmp_path)
    seed.migrate()
    seed.close()
    raw = sqlite3.connect(tmp_path / "control-plane.db")
    try:
        raw.execute(forward_migration)
        raw.execute(
            "INSERT INTO schema_migrations VALUES"
            " ('9999_from_a_newer_image.sql', '2026-09-01T00:00:00Z')"
        )
        raw.commit()
    finally:
        raw.close()

    backup_module.require_control_plane_database(tmp_path / "control-plane.db")


def test_a_newer_ledger_does_not_excuse_a_missing_schema(tmp_path) -> None:
    """The looser comparison is looser, not absent.

    A copied ledger with an unknown tail must not become a way past the schema
    check — otherwise the impostor test is defeated by adding one row.
    """
    impostor = tmp_path / "control-plane.db"
    shipped = backup_module._shipped_migrations()
    raw = sqlite3.connect(impostor)
    try:
        raw.execute("CREATE TABLE schema_migrations (name TEXT, applied_at TEXT)")
        for migration in (*shipped, "9999_from_a_newer_image.sql"):
            raw.execute(
                "INSERT INTO schema_migrations VALUES (?, '2026-01-01T00:00:00Z')",
                (migration,),
            )
        raw.execute("CREATE TABLE accounts (x TEXT)")
        raw.commit()
    finally:
        raw.close()

    with pytest.raises(backup_module.BackupError, match="schema is missing"):
        backup_module.require_control_plane_database(impostor)


def test_validation_does_not_delete_a_dangling_sidecar_symlink(
    tmp_path, monkeypatch
) -> None:
    """A nominally read-only check deleted an entry it never created.

    The "which sidecars were here before" snapshot used `Path.exists()`, which
    FOLLOWS symlinks — so a dangling `-wal` symlink answered False, was absent
    from the snapshot, present after the read-only open, and unlinked by the
    cleanup as though SQLite had just made it. Backup would silently destroy
    filesystem state while validating a source it was told to leave alone.
    """
    seed = _database(tmp_path)
    seed.migrate()
    seed.close()
    database = tmp_path / "control-plane.db"
    for sidecar in ("-wal", "-shm"):
        Path(f"{database}{sidecar}").unlink(missing_ok=True)
    dangling = Path(f"{database}-wal")
    dangling.symlink_to(tmp_path / "nothing-is-here")
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_DB", str(database))
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_BACKUP_KEY", BACKUP_KEY)

    with pytest.raises(SystemExit, match="not a regular file"):
        cli_main(["backup", str(tmp_path / "superseded.cpb")])

    assert os.path.lexists(dangling), (
        "validation deleted a sidecar entry the operator put there"
    )
    assert not (tmp_path / "superseded.cpb").exists()


def test_a_sidecar_appearing_under_the_lock_stops_the_restore(
    tmp_path, monkeypatch
) -> None:
    """The locked recheck asked about the database and nothing else.

    Publication claims the database and marker names atomically, but claims
    nothing for `-wal`/`-shm`. A generation that released its writer lock
    between the outer check and this one leaves committed frames behind, which
    the restored database then replays — rows silently replaced, with
    `integrity_check` still reporting `ok`.
    """
    seed = _database(tmp_path)
    seed.migrate()
    archive = tmp_path / "restore-point.cpb"
    backup_module.create_backup(seed, archive, backup_key=BACKUP_KEY_BYTES)
    seed.close()
    destination = tmp_path / "restored" / "control-plane.db"
    destination.parent.mkdir()

    stale = Path(f"{destination}-wal")
    real_acquire = ControlPlaneDatabase.acquire_process_lock
    entered: list[str] = []

    def acquire(self):
        result = real_acquire(self)
        if self.path == destination and not stale.exists():
            stale.write_bytes(b"frames from another generation")
        return result

    def restore_locked(*arguments, **keywords):
        entered.append("materialise")
        raise AssertionError("the locked recheck let a foreign sidecar through")

    monkeypatch.setattr(ControlPlaneDatabase, "acquire_process_lock", acquire)
    monkeypatch.setattr(backup_module, "_restore_locked", restore_locked)

    with pytest.raises(backup_module.BackupError, match="leftover SQLite state"):
        backup_module.restore_backup(
            archive, destination, backup_key=BACKUP_KEY_BYTES
        )

    # Refused UNDER THE LOCK, before a database-sized image is decrypted and
    # fsynced onto the volume this command exists to keep room on.
    assert entered == [], entered
    assert not destination.exists(), "a restore published beside foreign WAL frames"
    assert not backup_module._pause_marker(destination).exists()
    assert stale.read_bytes() == b"frames from another generation"


def test_a_sidecar_appearing_during_decryption_stops_the_publication(
    tmp_path, monkeypatch
) -> None:
    """Everything between the locked check and publication is a window.

    Decrypting, fsyncing and integrity-checking a database-sized image is the
    longest stretch of the restore, and publication claims only the database
    and marker names — a `-wal` that arrives in that stretch is still there
    afterwards, and the restored database replays its frames.
    """
    seed = _database(tmp_path)
    seed.migrate()
    archive = tmp_path / "restore-point.cpb"
    backup_module.create_backup(seed, archive, backup_key=BACKUP_KEY_BYTES)
    seed.close()
    destination = tmp_path / "restored" / "control-plane.db"
    destination.parent.mkdir()

    stale = Path(f"{destination}-shm")
    real_connect = sqlite3.connect

    def connect(*arguments, **keywords):
        # The integrity check on the materialised temporary: past the locked
        # recheck, before anything has been published.
        if not stale.exists():
            stale.write_bytes(b"shared memory from another generation")
        return real_connect(*arguments, **keywords)

    monkeypatch.setattr(backup_module.sqlite3, "connect", connect)

    with pytest.raises(backup_module.BackupError, match="leftover SQLite state"):
        backup_module.restore_backup(
            archive, destination, backup_key=BACKUP_KEY_BYTES
        )

    assert not destination.exists(), "a restore published beside foreign SQLite state"
    assert not backup_module._pause_marker(destination).exists()
    assert stale.read_bytes() == b"shared memory from another generation"


def test_sidecar_cleanup_removes_only_the_entry_it_created(
    tmp_path, monkeypatch
) -> None:
    """A NAME can be reused between closing the connection and cleaning up.

    The cleanup unlinked every sidecar that had not been there before, by name.
    A writer starting in that window creates its own `-wal` at the same path,
    and a validation that was supposed to leave the bundle untouched deletes
    it — taking that writer's committed frames with it.
    """
    seed = _database(tmp_path)
    seed.migrate()
    seed.close()
    database = tmp_path / "control-plane.db"
    for suffix in ("-wal", "-shm"):
        Path(f"{database}{suffix}").unlink(missing_ok=True)
    successor = Path(f"{database}-wal")
    real_connect = sqlite3.connect

    class SwapOnClose:
        def __init__(self, connection):
            self._connection = connection

        def execute(self, *arguments):
            return self._connection.execute(*arguments)

        def close(self):
            self._connection.close()
            # Somebody else's `-wal`, at the same name, after this open let go.
            #
            # Written elsewhere and renamed in, so the replacement's inode is
            # allocated while the original is still linked. Unlinking first and
            # writing after passed here and failed on CI, where the filesystem
            # handed the freed inode straight back and the two entries were
            # indistinguishable — a test whose subject was inode identity,
            # deciding it by luck.
            replacement = successor.with_name("a-successor")
            replacement.write_bytes(b"a live writer's frames")
            os.replace(replacement, successor)

    def connect(*arguments, **keywords):
        return SwapOnClose(real_connect(*arguments, **keywords))

    monkeypatch.setattr(backup_module.sqlite3, "connect", connect)
    with backup_module._read_only_sqlite(database) as connection:
        connection.execute("PRAGMA integrity_check").fetchone()
        assert successor.exists(), "the read-only open created no sidecar to own"
        owned = backup_module._inode(successor)

    assert backup_module._inode(successor) != owned, (
        "the fixture did not actually replace the entry, so this proves nothing"
    )
    assert successor.read_bytes() == b"a live writer's frames", (
        "cleanup unlinked by name and destroyed an entry it never created"
    )


def test_an_unjournalled_publication_withdraws_itself(tmp_path, monkeypatch) -> None:
    """`_publish` passes no journal, so nothing else can reverse it.

    `_claim` links and then unlinks, and the unlink can fail on its own — `EIO`,
    `EPERM`, a read-only remount. A journalled caller reverses that in `_undo`.
    `_publish` is not one: the destination was published, the exception escaped
    before the directory was even synced, and the caller's cleanup unlinked a
    temporary that had already moved. Restore then withdrew its pause marker
    over a database that was standing there.
    """
    source = tmp_path / "temporary"
    source.write_bytes(b"the published generation")
    destination = tmp_path / "canonical.db"
    real_unlink = Path.unlink

    def refuse(self, **keywords):
        if self == source:
            raise OSError(errno.EIO, "the unlink failed after the link succeeded")
        return real_unlink(self, **keywords)

    monkeypatch.setattr(Path, "unlink", refuse)

    with pytest.raises(OSError, match="unlink failed"):
        backup_module._publish(source, destination)

    monkeypatch.undo()
    assert not destination.exists(), (
        "a publication nobody journalled left its destination standing"
    )
    assert source.read_bytes() == b"the published generation"


def test_a_restore_whose_publication_half_fails_keeps_its_pause(
    tmp_path, monkeypatch
) -> None:
    """The invariant, end to end: no database ever stands without its marker."""
    seed = _database(tmp_path)
    seed.migrate()
    archive = tmp_path / "restore-point.cpb"
    backup_module.create_backup(seed, archive, backup_key=BACKUP_KEY_BYTES)
    seed.close()
    destination = tmp_path / "restored" / "control-plane.db"
    destination.parent.mkdir()
    real_unlink = Path.unlink

    # The DATABASE's own temporary, named EXACTLY, not by prefix. Everything in
    # that directory shares the prefix — the marker's temporary, and the
    # temporary's own `-wal` and `-shm`, which the integrity check unlinks well
    # before publication — so a prefix match fired on the wrong step every time
    # and the test proved nothing about the publication it names.
    published: dict[str, Path] = {}
    real_publish = backup_module._publish

    def publish(temporary, target):
        if Path(target) == destination:
            published["temporary"] = Path(temporary)
        return real_publish(temporary, target)

    def refuse(self, **keywords):
        if self == published.get("temporary"):
            raise OSError(errno.EIO, "the unlink failed after the link succeeded")
        return real_unlink(self, **keywords)

    monkeypatch.setattr(backup_module, "_publish", publish)
    monkeypatch.setattr(Path, "unlink", refuse)

    with pytest.raises(OSError, match="unlink failed"):
        backup_module.restore_backup(
            archive, destination, backup_key=BACKUP_KEY_BYTES
        )

    monkeypatch.undo()
    # The publication failing takes the marker with it — that branch is already
    # there and correct. What it cannot do is take back a database it does not
    # know became visible, and a marker withdrawn over a standing database is
    # the exact state the marker exists to prevent.
    assert not destination.exists(), (
        "the database was published and never withdrawn"
    )
    assert not (
        destination.exists()
        and not backup_module._pause_marker(destination).exists()
    ), "a restored database is startable with no worker pause beside it"


def test_a_reversal_across_directories_syncs_the_one_it_emptied(
    tmp_path, monkeypatch
) -> None:
    """Both helpers sync where they LAND, and neither syncs where they left.

    Within one directory that is the same entry and the gap does not show. The
    rollback install moves between the staging directory and the volume, and in
    `--target-already-freed` there is no superseded bundle to restore
    afterwards — so nothing else ever syncs the volume, and a crash could
    resurrect an arbitrary subset of the canonical members `_undo` had just
    reported gone.
    """
    staging = tmp_path / "staging"
    volume = tmp_path / "volume"
    staging.mkdir()
    volume.mkdir()
    source = staging / "control-plane.db"
    destination = volume / "control-plane.db"
    destination.write_bytes(b"the installed generation")
    journal = [
        backup_module._Move(
            source,
            destination,
            published_inode=(destination.lstat().st_dev, destination.lstat().st_ino),
            source_removed=True,
        )
    ]
    synced: list[Path] = []
    real_fsync_directory = backup_module._fsync_directory

    def record(directory):
        synced.append(Path(directory))
        return real_fsync_directory(directory)

    monkeypatch.setattr(backup_module, "_fsync_directory", record)

    backup_module._undo(journal)

    assert source.read_bytes() == b"the installed generation"
    assert not destination.exists()
    assert volume in synced, (
        "the directory the reversal emptied was never made durable, so the"
        f" canonical entry can come back after a crash: {synced}"
    )
    assert staging in synced, "the restored entry was never made durable either"


def test_a_ledger_without_its_schema_is_not_this_database(tmp_path, monkeypatch) -> None:
    """A copied ledger is the easiest thing in the world to produce by accident.

    A file holding nothing but `schema_migrations` and the row
    `0001_control_plane.sql` passed every earlier check — including integrity
    and foreign-key checks, which it passes precisely BECAUSE it has no rows and
    no references. The ledger names a schema; the schema has to be there.
    """
    impostor = tmp_path / "control-plane.db"
    shipped = backup_module._shipped_migrations()
    raw = sqlite3.connect(impostor)
    try:
        raw.execute("CREATE TABLE schema_migrations (name TEXT, applied_at TEXT)")
        raw.execute(
            "INSERT INTO schema_migrations VALUES (?, '2026-01-01T00:00:00Z')",
            (shipped[0],),
        )
        raw.commit()
    finally:
        raw.close()
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_DB", str(impostor))
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_BACKUP_KEY", BACKUP_KEY)

    with pytest.raises(SystemExit, match="schema is not the one they declare"):
        cli_main(["backup", str(tmp_path / "superseded.cpb")])

    assert not (tmp_path / "superseded.cpb").exists()


def test_the_reference_schema_is_the_one_the_migrator_produces(tmp_path) -> None:
    """Derived by RUNNING the scripts, so it cannot drift from them.

    A hand-kept catalogue of tables — or of columns, keys and indexes — falls
    behind the migrations that create them, and every historical revision needs
    its own entry. Executing the recorded prefix answers all of that from the
    same source of truth the migrator uses.
    """
    shipped = backup_module._shipped_migrations()
    reference, reference_columns = backup_module._reference_schema(shipped)

    seed = _database(tmp_path)
    seed.migrate()
    actual = backup_module._schema_objects(seed.connection)
    actual_columns = backup_module._table_columns(
        seed.connection, backup_module._table_names(actual)
    )
    seed.close()

    assert reference, "no schema was parsed out of the shipped migrations"
    assert reference_columns == actual_columns
    assert reference == actual, (
        "the schema this check compares against is not the schema `migrate()`"
        " builds: "
        + str(
            sorted(
                name
                for name in set(reference) | set(actual)
                if reference.get(name) != actual.get(name)
            )
        )
    )


def test_a_look_alike_schema_is_not_this_database(tmp_path, monkeypatch) -> None:
    """Every expected table NAME, and none of the definitions.

    Checking that the names exist accepted `accounts(x)` beside equally hollow
    placeholders for the rest — and such a file passes the integrity and
    foreign-key checks precisely BECAUSE it is empty. A mistyped path or a
    schema-damaged database could then be archived, verified, and used to
    authorise deleting the real one.
    """
    impostor = tmp_path / "control-plane.db"
    shipped = backup_module._shipped_migrations()
    expected, _columns = backup_module._reference_schema(shipped)
    raw = sqlite3.connect(impostor)
    try:
        raw.execute("CREATE TABLE schema_migrations (name TEXT, applied_at TEXT)")
        for migration in shipped:
            raw.execute(
                "INSERT INTO schema_migrations VALUES (?, '2026-01-01T00:00:00Z')",
                (migration,),
            )
        # Every object the reference declares, by NAME, and none of them right.
        # Hollow tables cannot carry the real indexes and triggers — those index
        # columns that are not there — so they are created against the one
        # column that is. The names all match; nothing else does.
        for name in expected:
            kind, object_name = name.split(":", 1)
            if kind == "table":
                raw.execute(f"CREATE TABLE {object_name} (x TEXT)")
        for name in expected:
            kind, object_name = name.split(":", 1)
            if kind == "index":
                raw.execute(f"CREATE INDEX {object_name} ON accounts (x)")
            elif kind == "trigger":
                raw.execute(
                    f"CREATE TRIGGER {object_name} AFTER INSERT ON accounts"
                    " BEGIN SELECT 1; END"
                )
        raw.commit()
    finally:
        raw.close()
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_DB", str(impostor))
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_BACKUP_KEY", BACKUP_KEY)

    with pytest.raises(
        SystemExit, match="schema is not the one they declare"
    ) as exit_code:
        cli_main(["backup", str(tmp_path / "superseded.cpb")])

    # A TABLE is named, not merely a missing index. Every object the reference
    # declares exists here by name, so a check that only asks "does this name
    # exist" reports nothing at all about the hollow definitions — which is
    # precisely the version this replaced.
    assert "table:" in str(exit_code.value), str(exit_code.value)
    assert not (tmp_path / "superseded.cpb").exists()


@pytest.mark.parametrize(
    "alias", ["", "-wal", "-shm", ".workers-paused", ".writer.lock"]
)
def test_backup_refuses_a_target_inside_the_live_bundle(
    tmp_path, monkeypatch, alias
) -> None:
    """The dangerous aliases are the ones that are ABSENT.

    `<db>.workers-paused` is normally not there, so the occupancy check said the
    name was free and the authenticated archive was published as the live pause
    marker. The backup reported success; every worker afterwards read the
    database as an unreconciled restore and stopped doing durable work. A
    nominal backup silently disabled production.
    """
    seed = _database(tmp_path)
    seed.migrate()
    seed.close()
    database = tmp_path / "control-plane.db"
    for sidecar in _sidecars_of(database):
        sidecar.unlink(missing_ok=True)
    before = _fingerprint(tmp_path)
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_DB", str(database))
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_BACKUP_KEY", BACKUP_KEY)

    with pytest.raises(SystemExit, match="belongs to the database being archived"):
        cli_main(["backup", f"{database}{alias}"])

    assert _fingerprint(tmp_path) == before, "the refusal mutated the volume"
    assert not backup_module._pause_marker(database).exists(), (
        "the backup paused the workers it was meant to leave alone"
    )


def test_backup_refuses_an_alias_reached_through_a_symlinked_directory(
    tmp_path, monkeypatch
) -> None:
    """String comparison does not see this one; resolving the parent does."""
    volume = tmp_path / "data"
    volume.mkdir()
    seed = ControlPlaneDatabase(volume / "control-plane.db")
    seed.migrate()
    seed.close()
    database = volume / "control-plane.db"
    for sidecar in _sidecars_of(database):
        sidecar.unlink(missing_ok=True)
    link = tmp_path / "by-another-name"
    link.symlink_to(volume, target_is_directory=True)
    before = _fingerprint(volume)
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_DB", str(database))
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_BACKUP_KEY", BACKUP_KEY)

    with pytest.raises(SystemExit, match="belongs to the database being archived"):
        cli_main(["backup", str(link / "control-plane.db.workers-paused")])

    assert _fingerprint(volume) == before
    assert not backup_module._pause_marker(database).exists()


@pytest.mark.parametrize("alias", ["-wal", "-shm", ".workers-paused", ".writer.lock"])
def test_install_rollback_refuses_a_target_inside_the_candidate(
    tmp_path, monkeypatch, alias
) -> None:
    """Only the two DATABASES were compared, and everything else passed.

    `--target <restored>.workers-paused` is a different regular file, so the
    superseded loop moved the candidate's own marker aside, renamed the
    candidate database onto that marker's pathname, and the missing-pause check
    raised from OUTSIDE the undo block — leaving the candidate split across two
    names, no database at the intended canonical path, and nothing to retry
    from.
    """
    volume, target, restored = _rollback_fixture(tmp_path)
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 10)
    volume_before = _fingerprint(volume)
    staging_before = _fingerprint(restored.parent)

    with pytest.raises(RollbackError, match="belongs to"):
        install_rollback(restored, Path(f"{restored}{alias}"), now=1800000042)

    assert _fingerprint(volume) == volume_before
    assert _fingerprint(restored.parent) == staging_before, (
        "the refusal moved part of the rollback candidate"
    )


@pytest.mark.parametrize("alias", ["-wal", "-shm", ".workers-paused", ".writer.lock"])
def test_install_rollback_refuses_a_candidate_inside_the_target(
    tmp_path, monkeypatch, alias
) -> None:
    """The rule is symmetric: neither namespace may name part of the other."""
    volume, target, restored = _rollback_fixture(tmp_path)
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 10)
    volume_before = _fingerprint(volume)
    staging_before = _fingerprint(restored.parent)

    with pytest.raises(RollbackError, match="belongs to"):
        install_rollback(Path(f"{target}{alias}"), target, now=1800000042)

    assert _fingerprint(volume) == volume_before
    assert _fingerprint(restored.parent) == staging_before


def test_install_rollback_refuses_a_hard_linked_bundle_member(
    tmp_path, monkeypatch
) -> None:
    """A different name for the same entry, which no path comparison can see.

    `--target /data/aliased.db` looks unrelated to the candidate until you ask
    what it refers to: the candidate's own pause marker. Normalising paths
    answers "two different files"; comparing entries answers correctly.
    """
    volume, target, restored = _rollback_fixture(tmp_path)
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 10)
    marker = backup_module._pause_marker(restored)
    assert marker.exists(), "the fixture candidate has no pause marker to alias"
    # Inside the staging directory: `_constrain` simulates EXDEV between these
    # two directories, and a hard link cannot cross a device anyway. The alias
    # this guards against is a name in the SAME directory as its referent.
    aliased = restored.parent / "aliased.db"
    os.link(marker, aliased)
    volume_before = _fingerprint(volume)
    staging_before = _fingerprint(restored.parent)

    with pytest.raises(RollbackError, match="belongs to"):
        install_rollback(restored, aliased, now=1800000042)

    assert _fingerprint(volume) == volume_before
    assert _fingerprint(restored.parent) == staging_before


def test_a_restore_does_not_delete_another_operation_s_pause_marker(
    tmp_path, monkeypatch
) -> None:
    """`_publish` fails with EEXIST precisely when somebody else got there first.

    The cleanup then unlinked that marker BY NAME, destroying the pause
    belonging to another restore while reporting only this one's error. The
    other restore's database is then startable against unreconciled data.
    """
    seed = _database(tmp_path)
    seed.migrate()
    archive = tmp_path / "restore-point.cpb"
    backup_module.create_backup(seed, archive, backup_key=BACKUP_KEY_BYTES)
    seed.close()
    destination = tmp_path / "restored" / "control-plane.db"
    destination.parent.mkdir()
    interloper = backup_module._pause_marker(destination)
    real_publish = backup_module._publish

    def publish(temporary, target):
        # Somebody else's restore claims the marker name in the instant between
        # the last occupancy check and this publication.
        if Path(target) == interloper and not interloper.exists():
            interloper.write_bytes(b"another restore's pause")
        return real_publish(temporary, target)

    monkeypatch.setattr(backup_module, "_publish", publish)

    with pytest.raises(backup_module.BackupError, match="could not durably pause"):
        backup_module.restore_backup(
            archive, destination, backup_key=BACKUP_KEY_BYTES
        )

    monkeypatch.undo()
    assert interloper.read_bytes() == b"another restore's pause", (
        "the cleanup deleted a pause marker this restore never created"
    )
    assert not destination.exists()


def test_a_database_arriving_at_the_target_keeps_the_pause(
    tmp_path, monkeypatch
) -> None:
    """Removing the pause is the one thing that must not happen here.

    The marker was published, this restore's own database publication then
    failed, and the cleanup unlinked the marker by name — but by then something
    else had arrived at the target path. A database with no `workers-paused`
    beside it is a database that starts unpaused, which is the whole reason the
    marker exists.
    """
    seed = _database(tmp_path)
    seed.migrate()
    archive = tmp_path / "restore-point.cpb"
    backup_module.create_backup(seed, archive, backup_key=BACKUP_KEY_BYTES)
    seed.close()
    destination = tmp_path / "restored" / "control-plane.db"
    destination.parent.mkdir()
    marker = backup_module._pause_marker(destination)
    real_publish = backup_module._publish

    def publish(temporary, target):
        if Path(target) == destination and not destination.exists():
            # Another operation lands its database first, so this publication
            # fails `EEXIST` with a database now sitting at the target.
            destination.write_bytes(b"another operation's database")
        return real_publish(temporary, target)

    monkeypatch.setattr(backup_module, "_publish", publish)

    with pytest.raises(FileExistsError):
        backup_module.restore_backup(
            archive, destination, backup_key=BACKUP_KEY_BYTES
        )

    monkeypatch.undo()
    assert destination.read_bytes() == b"another operation's database"
    assert marker.exists(), (
        "a database stands at the target with its worker pause removed"
    )


def test_a_pause_marker_replaced_after_publication_is_left_alone(
    tmp_path, monkeypatch
) -> None:
    """Publishing it is not the same as still owning it.

    Between this restore publishing its marker and its database publication
    failing, another operation can replace that marker with its own. Removing it
    then takes away a pause this invocation no longer owns — the same
    delete-by-name defect as the `EEXIST` case, one step further along.
    """
    seed = _database(tmp_path)
    seed.migrate()
    archive = tmp_path / "restore-point.cpb"
    backup_module.create_backup(seed, archive, backup_key=BACKUP_KEY_BYTES)
    seed.close()
    destination = tmp_path / "restored" / "control-plane.db"
    destination.parent.mkdir()
    marker = backup_module._pause_marker(destination)
    real_publish = backup_module._publish

    def publish(temporary, target):
        if Path(target) == destination:
            # Written elsewhere and renamed in, so the replacement's inode is
            # allocated while the original is still linked and the two cannot
            # collide by recycling.
            replacement = marker.with_name("a-successor")
            replacement.write_bytes(b"another restore's pause")
            os.replace(replacement, marker)
            raise OSError(errno.EIO, "the database publication failed")
        return real_publish(temporary, target)

    monkeypatch.setattr(backup_module, "_publish", publish)

    with pytest.raises(OSError, match="database publication failed"):
        backup_module.restore_backup(
            archive, destination, backup_key=BACKUP_KEY_BYTES
        )

    monkeypatch.undo()
    assert not destination.exists(), "the fixture published a database after all"
    assert marker.read_bytes() == b"another restore's pause", (
        "the cleanup removed a pause marker this restore no longer owned"
    )


def test_backup_refuses_a_database_replaced_after_validation(
    tmp_path, monkeypatch
) -> None:
    """The proof and the archive must be about the same file.

    `require_control_plane_database` opens and closes its own read-only
    connection; `database.connection` is not opened until `_materialise`; and
    the writer lock is ADVISORY, so nothing stops a rename in between. The
    command then authenticated a file nobody had validated and reported a valid
    archive of the wrong generation — after which the runbook permits deleting
    the real bundle.
    """
    seed = _database(tmp_path)
    seed.migrate()
    seed.close()
    database = tmp_path / "control-plane.db"
    for sidecar in _sidecars_of(database):
        sidecar.unlink(missing_ok=True)
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_DB", str(database))
    monkeypatch.setenv("ABROLIA_CONTROL_PLANE_BACKUP_KEY", BACKUP_KEY)

    imposter = tmp_path / "somebody-elses.db"
    other = ControlPlaneDatabase(imposter)
    other.migrate()
    with other.write() as connection:
        connection.execute("CREATE TABLE the_wrong_generation (id TEXT PRIMARY KEY)")
    other.close()
    for sidecar in _sidecars_of(imposter):
        sidecar.unlink(missing_ok=True)

    real_require = backup_module.require_control_plane_database

    real_materialise = backup_module._materialise

    def materialise(handle, image):
        # The swap lands INSIDE the snapshot: `_materialise` is where
        # `database.connection` first opens, by path, so a check made before it
        # proves nothing about the file SQLite went on to read. Only the check
        # made afterwards can see this.
        os.replace(imposter, database)
        return real_materialise(handle, image)

    assert real_require is not None
    monkeypatch.setattr(backup_module, "_materialise", materialise)

    with pytest.raises(SystemExit, match="not the database that was validated"):
        cli_main(["backup", str(tmp_path / "archive.cpb")])

    assert not (tmp_path / "archive.cpb").exists(), (
        "an archive was written of a database that was never validated"
    )


def test_a_canonical_entry_appearing_after_the_move_is_reversed(
    tmp_path, monkeypatch
) -> None:
    """The post-move assertions sat between two `try` blocks, inside neither.

    An entry arriving at the canonical path after the superseded bundle had
    moved raised from outside any failure boundary, so the live database stayed
    under its internal aside name with no reversal attempted — an outage, from
    a command whose entire purpose is to avoid one.
    """
    volume, target, restored = _rollback_fixture(tmp_path)
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 10)
    before = _fingerprint(volume)
    real_claim = backup_module._claim
    intruder = b"an entry that arrived mid-install"

    def claim(source, destination, journal=None):
        real_claim(source, destination, journal)
        # Somebody recreates the canonical database the instant it is vacated.
        if Path(source) == target and not target.exists():
            target.write_bytes(intruder)

    monkeypatch.setattr(backup_module, "_claim", claim)

    with pytest.raises(backup_module.IncompleteReversal) as raised:
        install_rollback(restored, target, now=1800000042)

    monkeypatch.undo()
    # Reversal was ATTEMPTED, which is the whole difference. Outside the
    # boundary the check raised with nothing tried; inside it, the undo runs,
    # declines to clobber the interloper — that is the conservative answer, not
    # a failure — and the pause goes down before the locks are released.
    assert target.read_bytes() == intruder, "the interloper was destroyed"
    assert raised.value.paused, "no fail-safe pause was written"
    assert backup_module._pause_marker(target).exists(), (
        "the locks were released over a mixed bundle with no worker pause"
    )
    assert set(before) <= set(_fingerprint(volume))


def test_an_unreversible_install_fails_with_the_bundle_paused(
    tmp_path, monkeypatch
) -> None:
    """Releasing the locks over a mixed bundle is the outcome to prevent.

    `_undo` used to print what it could not reverse and return, so the caller
    could not tell a complete reversal from a partial one and released both
    writer locks over whatever subset survived — a candidate database left
    canonical and unpaused among the possibilities. The reversal now reports,
    and a caller that cannot prove it pauses whatever is there.
    """
    volume, target, restored = _rollback_fixture(tmp_path)
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 10)
    marker = backup_module._pause_marker(target)
    marker.unlink(missing_ok=True)
    real_claim = backup_module._claim
    calls: list[int] = []

    def claim(source, destination, journal=None):
        # The preflight's volume probes go through here too. Counting them made
        # the fixture fail during preflight, before a single bundle member had
        # moved — a test aimed at reversal that never reached an install.
        if ".volume-probe-" in Path(source).name:
            return real_claim(source, destination, journal)
        calls.append(1)
        if len(calls) > 1:
            # The forward move fails after the first member has already gone.
            raise OSError(errno.EIO, "the install failed mid-bundle")
        return real_claim(source, destination, journal)

    real_rename = backup_module._rename_or_exdev

    def refuse_reversal(source, destination, journal=None):
        # Not the preflight's volume probes, which go through here too and
        # would fail the command before any move happened — a test that never
        # reaches its own subject.
        if ".volume-probe-" in Path(source).name:
            return real_rename(source, destination, journal)
        raise OSError(errno.EIO, "and the reversal fails too")

    monkeypatch.setattr(backup_module, "_claim", claim)
    monkeypatch.setattr(backup_module, "_rename_or_exdev", refuse_reversal)

    with pytest.raises(backup_module.IncompleteReversal) as raised:
        install_rollback(restored, target, now=1800000042)

    monkeypatch.undo()
    assert raised.value.unreversed, "an incomplete reversal reported nothing"
    assert raised.value.paused, "no fail-safe pause was written"
    assert marker.exists(), (
        "the locks were released over a mixed bundle with no worker pause"
    )
    assert "by hand" in str(raised.value)


def _stranger_archive(tmp_path: Path, name: str = "stranger.cpb") -> Path:
    """An authenticated archive of a perfectly healthy, unrelated database."""
    stranger = tmp_path / "stranger.db"
    raw = sqlite3.connect(stranger)
    try:
        raw.execute("CREATE TABLE invoices (id TEXT PRIMARY KEY, total REAL)")
        raw.execute("INSERT INTO invoices VALUES ('a', 1.0)")
        raw.commit()
    finally:
        raw.close()
    archive = tmp_path / name
    handle = ControlPlaneDatabase(stranger)
    try:
        backup_module.create_backup(handle, archive, backup_key=BACKUP_KEY_BYTES)
    finally:
        handle.close()
    for sidecar in _sidecars_of(stranger):
        sidecar.unlink(missing_ok=True)
    return archive


@pytest.mark.parametrize("apply_migrations", [True, False])
def test_restore_refuses_an_archive_of_an_unrelated_database(
    tmp_path, apply_migrations
) -> None:
    """Authentication proves who wrote the archive, not what was in it.

    `create_backup` encrypts whatever database it is handed. The integrity and
    foreign-key checks on the way back out pass on any healthy SQLite file, so
    a stranger's database restored cleanly — and with migrations applied, the
    control-plane schema was built around their tables.
    """
    archive = _stranger_archive(tmp_path)
    destination = tmp_path / "restored" / "control-plane.db"
    destination.parent.mkdir()

    with pytest.raises(
        backup_module.BackupError, match="does not contain a control-plane database"
    ):
        backup_module.restore_backup(
            archive,
            destination,
            backup_key=BACKUP_KEY_BYTES,
            apply_migrations=apply_migrations,
        )

    assert not destination.exists(), "a stranger's database was published"
    assert not backup_module._pause_marker(destination).exists()
    # The writer lock is excluded, as in `_fingerprint`: taking it is the guard
    # working, not the refusal leaving something behind.
    left = [
        path.name
        for path in destination.parent.iterdir()
        if not path.name.endswith(".writer.lock")
    ]
    assert left == [], left


def test_install_rollback_refuses_a_candidate_that_is_not_ours(
    tmp_path, monkeypatch
) -> None:
    """`restore --no-migrate` feeds this command directly.

    Its own check only ever asked whether the file opened as sound SQLite, so a
    candidate produced from an unrelated archive reached the canonical path and
    replaced the control-plane database with somebody else's schema.
    """
    volume, target, restored = _rollback_fixture(tmp_path)
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 10)
    # A stranger's database standing in for the restored candidate, with the
    # pause marker the preflight requires.
    raw = sqlite3.connect(restored)
    try:
        raw.execute("DROP TABLE IF EXISTS accounts")
        for row in raw.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall():
            raw.execute(f"DROP TABLE IF EXISTS {row[0]}")
        raw.execute("CREATE TABLE invoices (id TEXT PRIMARY KEY)")
        raw.commit()
    finally:
        raw.close()
    for sidecar in _sidecars_of(restored):
        sidecar.unlink(missing_ok=True)
    before = _fingerprint(volume)

    with pytest.raises(RollbackError, match="not a control-plane database"):
        install_rollback(restored, target, now=1800000042)

    assert _fingerprint(volume) == before, "the live bundle was disturbed"


def test_a_pristine_bootstrap_snapshot_is_still_restorable(tmp_path) -> None:
    """`migrate --backup-first` snapshots BEFORE the first migration.

    The archive that makes a fresh boot recoverable holds an empty file with no
    ledger and no tables — a control-plane database at revision zero. An
    identity check that demanded a ledger would refuse the one restore point a
    first deployment has.
    """
    pristine = tmp_path / "pristine.db"
    handle = ControlPlaneDatabase(pristine)
    archive = tmp_path / "bootstrap.cpb"
    try:
        backup_module.create_backup(handle, archive, backup_key=BACKUP_KEY_BYTES)
    finally:
        handle.close()
    destination = tmp_path / "restored" / "control-plane.db"
    destination.parent.mkdir()

    restored = backup_module.restore_backup(
        archive, destination, backup_key=BACKUP_KEY_BYTES
    )
    try:
        assert destination.exists()
        assert restored.query_one(
            "SELECT COUNT(*) AS n FROM schema_migrations"
        )["n"] > 0, "the restore did not migrate the pristine database"
    finally:
        restored.close()


def test_a_move_whose_destination_vanished_is_reported_as_unreversed(
    tmp_path, monkeypatch
) -> None:
    """Both names empty is a LOST member, not a reversed one.

    `_undo` skipped any move whose destination was gone, on the reasoning that
    there was nothing to put back. For a move that never published that is
    right; for a completed one it means the file exists under neither name. The
    reversal then reported nothing outstanding, so `IncompleteReversal` and its
    fail-safe pause were skipped and the writer locks were released over a
    bundle missing a member.
    """
    volume, target, restored = _rollback_fixture(tmp_path)
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 10)
    backup_module._pause_marker(target).unlink(missing_ok=True)
    real_claim = backup_module._claim
    calls: list[Path] = []

    def claim(source, destination, journal=None):
        if ".volume-probe-" in Path(source).name:
            return real_claim(source, destination, journal)
        if calls:
            # The second move fails, sending the install into reversal — by
            # which time the first move's destination is gone.
            raise OSError(errno.EIO, "the install failed mid-bundle")
        real_claim(source, destination, journal)
        calls.append(Path(destination))
        # Something else removes the file this move had just published.
        Path(destination).unlink()
        return None

    monkeypatch.setattr(backup_module, "_claim", claim)

    with pytest.raises(backup_module.IncompleteReversal) as raised:
        install_rollback(restored, target, now=1800000042)

    monkeypatch.undo()
    assert calls, "the fixture never completed a move to lose"
    lost = calls[0]
    assert any(move.destination == lost for move in raised.value.unreversed), (
        f"the lost member was not reported: {raised.value.unreversed}"
    )
    assert raised.value.paused, "no fail-safe pause was written"
    assert backup_module._pause_marker(target).exists()


@pytest.mark.parametrize("member", ["", ".workers-paused"])
def test_a_candidate_member_removed_after_preflight_stops_the_install(
    tmp_path, monkeypatch, member
) -> None:
    """The loop consumed pathnames, not the entries that passed validation.

    A candidate member removed between preflight and its move was silently
    SKIPPED — so the database could simply not be installed and the command
    still reported success, leaving the canonical path empty after the live
    bundle had been moved aside.
    """
    volume, target, restored = _rollback_fixture(tmp_path)
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 10)
    before = _fingerprint(volume)
    victim = Path(f"{restored}{member}")
    real_preflight = backup_module._preflight

    def preflight(*arguments, **keywords):
        result = real_preflight(*arguments, **keywords)
        victim.unlink()
        return result

    monkeypatch.setattr(backup_module, "_preflight", preflight)

    with pytest.raises(RollbackError, match="not the entry that passed"):
        install_rollback(restored, target, now=1800000042)

    monkeypatch.undo()
    assert _fingerprint(volume) == before, "the live bundle did not come back"


def test_a_candidate_replaced_after_preflight_is_not_installed(
    tmp_path, monkeypatch
) -> None:
    """A same-path replacement is a file nobody validated.

    Installed, it lands at the canonical path beside the validated
    generation's sidecars — a mixed SQLite generation, reported as a
    successful rollback.
    """
    volume, target, restored = _rollback_fixture(tmp_path)
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 10)
    before = _fingerprint(volume)
    real_preflight = backup_module._preflight

    def preflight(*arguments, **keywords):
        result = real_preflight(*arguments, **keywords)
        # Written elsewhere and renamed in, so the replacement's inode is
        # allocated while the original is still linked.
        replacement = restored.with_name("a-successor")
        replacement.write_bytes(b"a database nobody validated")
        os.replace(replacement, restored)
        return result

    monkeypatch.setattr(backup_module, "_preflight", preflight)

    with pytest.raises(RollbackError, match="not the entry that passed"):
        install_rollback(restored, target, now=1800000042)

    monkeypatch.undo()
    assert _fingerprint(volume) == before
    assert restored.read_bytes() == b"a database nobody validated"


@pytest.mark.parametrize("sentinel", ["symlink", "hard-link"])
def test_the_failsafe_pause_does_not_follow_an_alias(tmp_path, sentinel) -> None:
    """This runs when the volume is already in a state nobody planned.

    `write_text` follows an alias, so a symlink or hard link sitting at the
    marker path was opened and TRUNCATED — error handling for a recoverable
    move failure erasing an unrelated file, or the surviving database itself.
    """
    volume = tmp_path / "data"
    volume.mkdir()
    target = volume / "control-plane.db"
    target.write_bytes(b"the database that survived")
    bystander = volume / "somebody-elses.txt"
    bystander.write_bytes(b"not yours to truncate")
    marker = backup_module._pause_marker(target)
    if sentinel == "symlink":
        marker.symlink_to(bystander)
    else:
        os.link(bystander, marker)

    paused = backup_module._install_failsafe_pause(target)

    assert bystander.read_bytes() == b"not yours to truncate", (
        "the fail-safe pause truncated a file it did not own"
    )
    assert target.read_bytes() == b"the database that survived"
    # A symlink is not a pause anything reads; a regular file at that name is.
    assert paused is (sentinel == "hard-link")


def test_a_dangling_target_symlink_is_not_a_freed_target(tmp_path, monkeypatch) -> None:
    """`--target-already-freed` promises the whole bundle is absent.

    `exists()` follows symlinks, so a DANGLING one at `--target` answered
    "nothing here". The install then moved that unowned link under a superseded
    name and published over a path that was never free.
    """
    volume, target, restored = _rollback_fixture(tmp_path)
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 10)
    for member in backup_module._bundle(target):
        member.unlink(missing_ok=True)
    target.symlink_to(volume / "nothing-is-here")
    before = _fingerprint(volume)

    # Refused as an OCCUPIED entry rather than accepted as an empty name. The
    # message names the shape because that is what the check now asks about.
    with pytest.raises(RollbackError, match="is not a regular file"):
        install_rollback(restored, target, now=1800000042, target_already_freed=True)

    assert os.path.lexists(target), "the refusal removed the operator's entry"
    assert target.is_symlink()
    assert _fingerprint(volume) == before


def test_a_canonical_member_removed_after_it_lands_is_not_a_success(
    tmp_path, monkeypatch
) -> None:
    """Pinning the candidate covers the way IN; this covers the way out.

    A member can be published and then removed by something else before the
    install finishes. Reporting `workers: paused` and a sidecar list at that
    point tells an operator the rollback is installed when the canonical
    database is not there — an outage described as a success.
    """
    volume, target, restored = _rollback_fixture(tmp_path)
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 10)
    before = _fingerprint(volume)
    # Interposed on `_publish`, which BOTH publication routes go through:
    # `_constrain` makes this a cross-filesystem install, so `_rename_or_exdev`
    # answers EXDEV and the copy path runs — patching the rename alone caught
    # nothing and the test never reached its own subject.
    real_publish = backup_module._publish

    def publish(temporary, destination):
        real_publish(temporary, destination)
        if Path(destination) == target:
            # Somebody removes the canonical database the instant it lands.
            Path(destination).unlink()

    monkeypatch.setattr(backup_module, "_publish", publish)

    with pytest.raises(backup_module.IncompleteReversal) as raised:
        install_rollback(restored, target, now=1800000042)

    monkeypatch.undo()
    # Three mechanisms in sequence, which is what the state deserves. The
    # post-move check notices the canonical database is gone; the reversal
    # cannot put it back, because the fixture deleted it and both its names are
    # now empty; and the caller therefore pauses whatever remains rather than
    # releasing the locks over a bundle missing a member.
    assert raised.value.unreversed, "the lost member went unreported"
    assert raised.value.paused, "no fail-safe pause was written"
    assert backup_module._pause_marker(target).exists()
    assert before, "the fixture volume was empty"

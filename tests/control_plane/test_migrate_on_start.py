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
    for flag in ("install-rollback", "--restored", "--superseded-to"):
        assert flag in doc, flag

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
    """The sibling publication site, which the review did not name.

    `restore_backup` renames the operator's rollback into place with the same
    gap, and it is the worse of the two: a restore lost to an unsynced directory
    entry is discovered with the service already stopped and the superseded
    database already moved aside.
    """
    database = _database(tmp_path)
    database.migrate()
    archive = tmp_path / "control-plane.cpb"
    create_backup(database, archive, backup_key=BACKUP_KEY_BYTES)
    database.close()

    target = tmp_path / "restored" / "control-plane.db"
    trace = _durability_trace(monkeypatch, lambda published: published == target)
    restored = restore_backup(
        archive, target, backup_key=BACKUP_KEY_BYTES, apply_migrations=False
    )
    restored.close()

    assert "rename" in trace, trace
    landed = trace[: trace.index("rename") + 2]
    assert landed[-3:] == ["fsync:file", "rename", "fsync:dir"], trace


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


def test_the_install_frees_the_volume_before_copying_onto_it(
    tmp_path, monkeypatch
) -> None:
    """The low-space branch, executed rather than asserted about.

    The documented `/tmp` staging only moved WHERE the restore was written. The
    install still copied a third database-sized file onto a volume already
    holding the superseded database and the archive, so the recovery ended in
    `ENOSPC` exactly as before, with a good backup sitting right there. Renaming
    the superseded file aside frees nothing: same filesystem, same blocks.

    So: the superseded database becomes an off-volume archive, that archive is
    read back, and only then are its blocks released.
    """
    volume, target, restored = _rollback_fixture(tmp_path)
    staging = restored.parent
    # Room for exactly one database, which is not enough while the superseded
    # one is still on the volume — the situation the runbook could not survive.
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 3 // 2)

    report = install_rollback(
        restored, target, backup_key=BACKUP_KEY_BYTES, superseded_to=staging, now=1800000042
    )

    assert target.exists()
    # The restore is what is installed, not the database it supersedes.
    assert "migrated_only" not in _tables(target)
    assert not list(volume.glob("*.superseded-*")), "blocks were never released"

    superseded = Path(str(report["superseded_kept_as"]))
    assert superseded.parent == staging, "the archive stayed on the volume it must free"
    assert superseded.exists()
    assert report["freed_bytes"] > 0

    # Not merely a file with the right name: it has to be the superseded
    # database, openable with the same key and the same restore command.
    recovered = restore_backup(
        superseded,
        tmp_path / "recovered.db",
        backup_key=BACKUP_KEY_BYTES,
        apply_migrations=False,
    )
    recovered.close()
    assert "migrated_only" in _tables(tmp_path / "recovered.db")


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

    report = install_rollback(restored, target, backup_key=BACKUP_KEY_BYTES, now=1800000042)

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
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 3 // 2)

    install_rollback(
        restored,
        target,
        backup_key=BACKUP_KEY_BYTES,
        superseded_to=restored.parent,
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

    report = install_rollback(restored, target, backup_key=BACKUP_KEY_BYTES, now=1800000042)

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
        install_rollback(restored, target, backup_key=BACKUP_KEY_BYTES, now=1800000042)

    assert _fingerprint(volume) == before, "the volume was modified by a refused install"


def test_nothing_is_deleted_when_the_superseded_archive_will_not_open(
    tmp_path, monkeypatch
) -> None:
    """The read-back is the gate, and it must gate a real deletion.

    Writing the archive and deleting the original on the strength of `create_backup`
    returning would destroy the only copy of every write taken after the
    migration if that archive were unopenable. The original still exists at this
    point, so failing here costs nothing.
    """
    volume, target, restored = _rollback_fixture(tmp_path)
    monkeypatch.setattr(backup_module, "_authenticated_digest", lambda *a, **k: None)
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 3 // 2)

    with pytest.raises(RollbackError, match="does not authenticate"):
        install_rollback(
            restored,
            target,
            backup_key=BACKUP_KEY_BYTES,
            superseded_to=restored.parent,
            now=1800000042,
        )

    aside = volume / "control-plane.db.superseded-1800000042"
    assert "migrated_only" in _tables(aside), "the superseded database was destroyed"
    assert not list(restored.parent.glob("*.superseded-*.cpb")), "a bad archive was kept"
    assert restored.exists(), "the restore was consumed by a failed install"


def test_archiving_onto_the_volume_being_freed_is_refused(tmp_path, monkeypatch) -> None:
    """Staging on the same volume releases nothing and fills it further."""
    volume, target, restored = _rollback_fixture(tmp_path)
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 3 // 2)

    with pytest.raises(RollbackError, match="on the volume being freed"):
        install_rollback(
            restored,
            target,
            backup_key=BACKUP_KEY_BYTES,
            superseded_to=volume,
            now=1800000042,
        )


def _fingerprint(directory: Path) -> dict[str, bytes]:
    """Every file under `directory`, by name and content."""
    return {
        path.name: path.read_bytes()
        for path in sorted(directory.iterdir())
        if path.is_file()
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
        install_rollback(restored, target, backup_key=BACKUP_KEY_BYTES, now=1800000042)

    assert _fingerprint(volume) == before, "a refused install modified the volume"


def test_installing_a_database_over_itself_is_refused(tmp_path, monkeypatch) -> None:
    """`--restored` equal to `--target` would supersede the file being installed."""
    volume, target, _restored = _rollback_fixture(tmp_path)
    _constrain(monkeypatch, volume, capacity=target.stat().st_size * 10)
    before = _fingerprint(volume)

    with pytest.raises(RollbackError, match="same file"):
        install_rollback(target, target, backup_key=BACKUP_KEY_BYTES, now=1800000042)

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

    install_rollback(restored, target, backup_key=BACKUP_KEY_BYTES, now=1800000042)

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
        restored, target, backup_key=BACKUP_KEY_BYTES, now=1800000042
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
                restored, target, backup_key=BACKUP_KEY_BYTES, now=1800000042
            )
    finally:
        writer.release_process_lock()
        writer.close()

    installed = {name: body for name, body in _fingerprint(volume).items()
                 if not name.endswith(".writer.lock")}
    assert installed == {name: body for name, body in before.items()
                         if not name.endswith(".writer.lock")}

    # And it succeeds once the writer lets go, so the guard is a gate rather
    # than a wall.
    install_rollback(restored, target, backup_key=BACKUP_KEY_BYTES, now=1800000042)
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

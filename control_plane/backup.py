"""Authenticated control-plane SQLite backup with explicit paused restore."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from control_plane.db import ControlPlaneDatabase

MAGIC = b"ABCP1\x00"
NONCE_BYTES = 12


class BackupError(RuntimeError):
    pass


def _key(value: bytes) -> bytes:
    if len(value) != 32:
        raise BackupError("a separate 32-byte control-plane backup key is required")
    return value


def create_pre_migrate_backup(
    database: ControlPlaneDatabase,
    *,
    backup_key: bytes,
    now: float | None = None,
    directory: Path | None = None,
) -> Path | None:
    """Snapshot before the first pending migration; None when none are pending.

    The file uses the plan's `<db>.pre-migrate-<rev>-<epoch>.bak` name and the
    same authenticated archive format as `create_backup`, so it is restored with
    `abrolia-control-plane restore` and the dedicated backup key.
    """
    if not database.pending_migrations(directory):
        return None
    revision = database.applied_revision()

    # A persistently failing migration restarts the container in a loop, and
    # every boot reached this line: one full encrypted snapshot per attempt,
    # each under a fresh epoch, on a 1 GB volume. That turns a recoverable
    # failed deploy into a full disk — losing the ability to take a restore
    # point at all, and eventually the database's own room to write.
    existing = _reusable_pre_migrate_backup(database, revision)
    if existing is not None:
        return existing

    stamp = int(time.time() if now is None else now)
    base = f"{database.path.name}.pre-migrate-{revision}-{stamp}"
    target = database.path.with_name(f"{base}.bak")
    # Two attempts inside one second is not exotic — a container that fails fast
    # and restarts does exactly that. `create_backup` refuses to overwrite, so
    # without a disambiguator the collision raises and the boot fails for a
    # reason that has nothing to do with the migration. The suffix is bounded;
    # reuse above already prevents the unbounded case.
    for suffix in range(1, 10):
        if not target.exists():
            break
        target = database.path.with_name(f"{base}-{suffix}.bak")
    return create_backup(database, target, backup_key=backup_key)


def _reusable_pre_migrate_backup(
    database: ControlPlaneDatabase, revision: object
) -> Path | None:
    """An existing snapshot of THIS database at THIS revision, or None.

    Same revision is not sufficient on its own. A migration can fail, be dropped
    from the next image so the container serves at the unchanged revision, take
    writes, and only then meet a new migration — at which point the old archive
    matches by revision but no longer matches the data. So the archive is reused
    only when nothing has been written since it was taken, checked against the
    database file and its WAL sidecar.

    Reuse rather than prune: deleting a restore point to save space is the one
    move that turns a disk problem into a data-loss problem.
    """
    candidates = sorted(
        database.path.parent.glob(
            f"{database.path.name}.pre-migrate-{revision}-*.bak"
        )
    )
    if not candidates:
        return None
    archive = candidates[-1]
    try:
        taken_at = archive.stat().st_mtime
    except OSError:
        return None
    for companion in (
        database.path,
        database.path.with_name(f"{database.path.name}-wal"),
    ):
        try:
            if companion.stat().st_mtime > taken_at:
                return None
        except FileNotFoundError:
            continue
        except OSError:
            return None
    return archive


def create_backup(
    database: ControlPlaneDatabase, target: Path | str, *, backup_key: bytes
) -> Path:
    destination = Path(target)
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_archive: Path | None = None
    plaintext: bytearray | None = None
    try:
        with sqlite3.connect(":memory:") as output:
            database.connection.backup(output)
            plaintext = bytearray(output.serialize())
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = AESGCM(_key(backup_key)).encrypt(nonce, bytes(plaintext), MAGIC)
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination.name}.", dir=destination.parent, delete=False
        ) as archive_file:
            temporary_archive = Path(archive_file.name)
            archive_file.write(MAGIC)
            archive_file.write(nonce)
            archive_file.write(ciphertext)
            archive_file.flush()
            os.fsync(archive_file.fileno())
        os.chmod(temporary_archive, 0o600)
        os.replace(temporary_archive, destination)
        temporary_archive = None
        return destination
    finally:
        if plaintext is not None:
            plaintext[:] = b"\x00" * len(plaintext)
        if temporary_archive is not None:
            temporary_archive.unlink(missing_ok=True)


def restore_backup(
    archive: Path | str,
    target: Path | str,
    *,
    backup_key: bytes,
    apply_migrations: bool = True,
) -> ControlPlaneDatabase:
    """Restore into a new, worker-paused database.

    `apply_migrations=False` is the rollback path: restoring a pre-migrate
    archive from the new image must not immediately reapply the migration the
    archive exists to undo.
    """
    source = Path(archive)
    destination = Path(target)
    if destination.exists():
        raise FileExistsError(destination)
    payload = source.read_bytes()
    if not payload.startswith(MAGIC) or len(payload) <= len(MAGIC) + NONCE_BYTES + 16:
        raise BackupError("unsupported or truncated control-plane backup")
    nonce_start = len(MAGIC)
    nonce = payload[nonce_start : nonce_start + NONCE_BYTES]
    ciphertext = payload[nonce_start + NONCE_BYTES :]
    try:
        plaintext = bytearray(AESGCM(_key(backup_key)).decrypt(nonce, ciphertext, MAGIC))
    except InvalidTag as error:
        raise BackupError("backup authentication failed") from error
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination.name}.", dir=destination.parent, delete=False
        ) as output:
            temporary = Path(output.name)
            output.write(plaintext)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        try:
            with sqlite3.connect(temporary) as connection:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    raise BackupError("restored SQLite integrity check failed")
                connection.execute("PRAGMA foreign_keys=ON")
                violations = connection.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise BackupError("restored SQLite foreign-key check failed")
        except sqlite3.DatabaseError as error:
            raise BackupError("restored payload is not a valid SQLite database") from error
        os.replace(temporary, destination)
        temporary = None
        restored = ControlPlaneDatabase(destination)
        if apply_migrations:
            restored.migrate()
        restored.pause_workers()
        return restored
    finally:
        plaintext[:] = b"\x00" * len(plaintext)
        if temporary is not None:
            temporary.unlink(missing_ok=True)

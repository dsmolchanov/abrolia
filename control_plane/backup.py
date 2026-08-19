"""Authenticated control-plane SQLite backup with explicit paused restore."""

from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import tempfile
import time
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from control_plane.db import ControlPlaneDatabase

TAG_BYTES = 16
SCRATCH_DIR_ENV = "ABROLIA_BACKUP_SCRATCH_DIR"
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
    existing = _reusable_pre_migrate_backup(database, revision, backup_key)
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


CHUNK_BYTES = 1 << 16


def _materialise(database: ControlPlaneDatabase, destination: Path) -> None:
    """Write the database's image to a file on disk, not into memory.

    The Machine has 512 MiB of RAM against a 1 GiB volume, so anything that
    holds a database-sized buffer — let alone several — can OOM while trying to
    decide whether a snapshot is still usable, and leave the container unable to
    migrate or serve at all. Disk is the resource this deployment has.
    """
    output = sqlite3.connect(destination)
    try:
        database.connection.backup(output)
    finally:
        output.close()


def _digest_file(path: Path) -> bytes:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.digest()


def _authenticated_digest(archive: Path, backup_key: bytes) -> bytes | None:
    """Digest of the archive's plaintext, or None if it will not open.

    Decrypts in chunks and keeps none of it: each block is hashed and dropped.
    The digest is returned only after `finalize_with_tag` succeeds, so an
    unauthenticated archive can never be mistaken for a matching one — and a
    rotated key or a truncated file is rejected here rather than satisfying the
    backup-first gate with a snapshot nobody can open.
    """
    prologue = len(MAGIC) + NONCE_BYTES
    try:
        size = archive.stat().st_size
        with open(archive, "rb") as handle:
            header = handle.read(prologue)
            if len(header) < prologue or not header.startswith(MAGIC):
                return None
            if size < prologue + TAG_BYTES:
                return None
            nonce = header[len(MAGIC) :]

            # The tag lives at the end, so read it first and then stream the
            # ciphertext between. Reading the body whole — even to slice the tag
            # off it — would put the entire archive in memory and defeat the
            # point of chunking the decryption.
            handle.seek(size - TAG_BYTES)
            tag = handle.read(TAG_BYTES)

            decryptor = Cipher(
                algorithms.AES(_key(backup_key)), modes.GCM(nonce, tag)
            ).decryptor()
            decryptor.authenticate_additional_data(MAGIC)
            digest = hashlib.sha256()
            handle.seek(prologue)
            remaining = size - prologue - TAG_BYTES
            while remaining > 0:
                chunk = handle.read(min(CHUNK_BYTES, remaining))
                if not chunk:
                    return None
                remaining -= len(chunk)
                digest.update(decryptor.update(chunk))
            digest.update(decryptor.finalize())
    except (OSError, InvalidTag, BackupError, ValueError):
        return None
    return digest.digest()


def _reusable_pre_migrate_backup(
    database: ControlPlaneDatabase, revision: object, backup_key: bytes
) -> Path | None:
    """An existing snapshot that is still an exact image of this database.

    Compares CONTENT, not timestamps. Timestamps were the obvious choice and the
    wrong one: a migration that fails still opens a transaction and checkpoints
    on close, so the database and WAL mtimes advance even though it rolled back
    and nothing was committed. Every restart therefore looked like a change,
    rejected the archive and wrote another — leaving in place the accumulation
    this check exists to prevent.

    The digest moves as soon as anything is genuinely committed, which is the
    case that matters for safety: a migration can be dropped from the next
    image, the container can then serve at the unchanged revision and take
    writes, and an archive matching only by revision would be a restore point
    that silently loses them.

    Reuse rather than prune. Deleting a restore point to reclaim space is the one
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
    archived = _authenticated_digest(archive, backup_key)
    if archived is None:
        return None
    scratch: Path | None = None
    try:
        scratch_root = os.environ.get(SCRATCH_DIR_ENV) or tempfile.gettempdir()
        with tempfile.NamedTemporaryFile(
            prefix=f".{database.path.name}.compare.", dir=scratch_root, delete=False
        ) as handle:
            scratch = Path(handle.name)
        _materialise(database, scratch)
        current = _digest_file(scratch)
    except (OSError, sqlite3.DatabaseError):
        return None
    finally:
        if scratch is not None:
            scratch.unlink(missing_ok=True)
    return archive if hmac.compare_digest(archived, current) else None


def create_backup(
    database: ControlPlaneDatabase, target: Path | str, *, backup_key: bytes
) -> Path:
    """Encrypt an image of the database into an authenticated archive.

    Both the image and the encryption go through disk in chunks: the same
    memory ceiling applies here as to the reuse check, and taking a snapshot is
    exactly when the volume is most likely to be under pressure.

    The layout is unchanged — MAGIC, nonce, ciphertext, 16-byte tag — so
    `restore_backup` reads archives written before and after this change
    identically.
    """
    destination = Path(target)
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_archive: Path | None = None
    image: Path | None = None
    try:
        # NOT beside the database. The image and the encrypted archive are each
        # about the size of the database, and putting both on the 1 GiB data
        # volume alongside the live file means a database near a third of the
        # volume exhausts it while trying to take a backup — every deployment
        # with a pending migration would then fail closed and never serve. The
        # RAM problem this replaced must not simply become a disk problem.
        # `SCRATCH_DIR` lets an operator point this at whatever the machine has.
        scratch_root = os.environ.get(SCRATCH_DIR_ENV) or tempfile.gettempdir()
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination.name}.image.", dir=scratch_root, delete=False
        ) as handle:
            image = Path(handle.name)
        _materialise(database, image)

        nonce = os.urandom(NONCE_BYTES)
        encryptor = Cipher(
            algorithms.AES(_key(backup_key)), modes.GCM(nonce)
        ).encryptor()
        encryptor.authenticate_additional_data(MAGIC)
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination.name}.", dir=destination.parent, delete=False
        ) as archive_file:
            temporary_archive = Path(archive_file.name)
            archive_file.write(MAGIC)
            archive_file.write(nonce)
            with open(image, "rb") as source:
                for chunk in iter(lambda: source.read(CHUNK_BYTES), b""):
                    archive_file.write(encryptor.update(chunk))
            archive_file.write(encryptor.finalize())
            archive_file.write(encryptor.tag)
            archive_file.flush()
            os.fsync(archive_file.fileno())
        os.chmod(temporary_archive, 0o600)
        os.replace(temporary_archive, destination)
        temporary_archive = None
        return destination
    finally:
        if image is not None:
            image.unlink(missing_ok=True)
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
    # Streamed, not buffered. Holding the payload, a ciphertext slice, the
    # decrypted bytes and a copy of them meant a ~125 MiB archive could OOM the
    # 512 MiB Machine — taking down the rollback procedure precisely for the
    # larger databases whose migrations are most likely to need it.
    prologue = len(MAGIC) + NONCE_BYTES
    size = source.stat().st_size
    if size <= prologue + TAG_BYTES:
        raise BackupError("unsupported or truncated control-plane backup")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with open(source, "rb") as archive_file:
            header = archive_file.read(prologue)
            if not header.startswith(MAGIC):
                raise BackupError("unsupported or truncated control-plane backup")
            nonce = header[len(MAGIC) :]
            archive_file.seek(size - TAG_BYTES)
            tag = archive_file.read(TAG_BYTES)
            decryptor = Cipher(
                algorithms.AES(_key(backup_key)), modes.GCM(nonce, tag)
            ).decryptor()
            decryptor.authenticate_additional_data(MAGIC)
            archive_file.seek(prologue)
            remaining = size - prologue - TAG_BYTES
            with tempfile.NamedTemporaryFile(
                prefix=f".{destination.name}.", dir=destination.parent, delete=False
            ) as output:
                temporary = Path(output.name)
                while remaining > 0:
                    chunk = archive_file.read(min(CHUNK_BYTES, remaining))
                    if not chunk:
                        raise BackupError("truncated control-plane backup")
                    remaining -= len(chunk)
                    output.write(decryptor.update(chunk))
                # Only now is the plaintext authentic. Nothing has read the file
                # yet, and a failure here deletes it in the `finally` below, so
                # unverified bytes never reach the caller.
                try:
                    output.write(decryptor.finalize())
                except InvalidTag as error:
                    raise BackupError("backup authentication failed") from error
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
        if temporary is not None:
            temporary.unlink(missing_ok=True)

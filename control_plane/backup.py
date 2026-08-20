"""Authenticated control-plane SQLite backup with explicit paused restore."""

from __future__ import annotations

import errno
import hashlib
import hmac
import os
import sqlite3
import stat
import tempfile
import time
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from control_plane.db import ControlPlaneDatabase, ProcessAlreadyRunning

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
        ),
        reverse=True,
    )
    if not candidates:
        return None
    # EVERY candidate, not just the lexicographically greatest one. That name is
    # not authoritative: an archive left by a clock that later stepped back
    # sorts highest forever, so a corrupt or old-key file in that position hid
    # every valid archive beneath it. Each restart then wrote another lower-named
    # backup that would likewise never be considered — the unbounded growth this
    # reuse check exists to prevent, reintroduced by the way it picked.
    authenticated = [
        (archive, digest)
        for archive in candidates
        if (digest := _authenticated_digest(archive, backup_key)) is not None
    ]
    if not authenticated:
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
    # The image is taken once and compared against each authenticated archive:
    # materialising a database-sized file per candidate is exactly the disk
    # pressure this runs under.
    for archive, archived in authenticated:
        if hmac.compare_digest(archived, current):
            return archive
    return None


def _publish(temporary: Path, destination: Path) -> None:
    """Rename into place, then make the DIRECTORY ENTRY itself durable.

    `fsync` on the file persists its CONTENTS; it says nothing about the name
    that finds them. After `os.replace` returns, the new entry lives only in the
    parent directory's own unsynced metadata — so a power loss between this
    function returning and the migration committing can leave the archive's
    bytes on disk with nothing linking to them, while the upgraded database
    survives. The rollback would be gone at exactly the moment it is needed,
    which is the single guarantee this module exists to provide.

    Both publication sites go through here rather than each remembering to sync:
    `create_backup` writes the restore point, and `restore_backup` publishes the
    database an operator is mid-rollback with. The second had the same gap and
    is the more dangerous of the two, because a lost restore is discovered with
    the service already stopped.

    Raising is correct and load-bearing. `db.main` treats an `OSError` from the
    snapshot as fail-closed and returns before `migrate()`, so a directory that
    will not sync stops the migration instead of silently proceeding without a
    durable restore point.
    """
    os.replace(temporary, destination)
    try:
        _fsync_directory(destination.parent)
    except OSError:
        # Undo the rename. Leaving the file behind is worse than not writing it:
        # `_reusable_pre_migrate_backup` would find this archive on the next
        # boot, skip the snapshot, and migrate against a restore point whose
        # name was never made durable — turning a fail-closed boot into a
        # fail-open one, which is the exact shape of bug this module keeps
        # finding in itself. The caller's `finally` cannot do it: the temporary
        # path no longer exists, so its `unlink(missing_ok=True)` is a no-op.
        destination.unlink(missing_ok=True)
        raise


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
        _publish(temporary_archive, destination)
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
    # Exclusive ownership of the path being written, for the same reason
    # `install-rollback` takes it: publishing a database and its pause marker
    # under a live writer means the two can interleave with whatever that writer
    # is doing at the same path. Taken through `ControlPlaneDatabase`, which
    # opens no connection until one is asked for, so this locks without touching
    # SQLite — and held until the restore is complete and paused.
    guard = ControlPlaneDatabase(destination)
    try:
        guard.acquire_process_lock()
    except ProcessAlreadyRunning as error:
        raise BackupError(
            f"a control-plane writer already owns {destination};"
            " stop it before restoring over that path"
        ) from error
    try:
        # Re-checked INSIDE the lock. The check above runs before the lock is
        # held, so another process could create that path in between and this
        # would overwrite it — the refusal to clobber an existing target is the
        # point, and a check made outside the lock is one the lock cannot back.
        if destination.exists():
            raise FileExistsError(destination)
        return _restore_locked(
            source,
            destination,
            backup_key=backup_key,
            apply_migrations=apply_migrations,
        )
    finally:
        guard.release_process_lock()
        guard.close()


def _restore_locked(
    source: Path,
    destination: Path,
    *,
    backup_key: bytes,
    apply_migrations: bool,
) -> ControlPlaneDatabase:
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
        # `with sqlite3.connect(...)` commits; it does NOT close. The connection
        # stayed open and its `-wal`/`-shm` stayed on disk under the TEMPORARY
        # name, which the publication below then renamed away from — leaving two
        # orphaned sidecars per restore on the volume whose exhaustion this
        # module exists to survive. Close it, and take its sidecars with it.
        connection = sqlite3.connect(temporary)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise BackupError("restored SQLite integrity check failed")
            connection.execute("PRAGMA foreign_keys=ON")
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise BackupError("restored SQLite foreign-key check failed")
        except sqlite3.DatabaseError as error:
            raise BackupError("restored payload is not a valid SQLite database") from error
        finally:
            connection.close()
            for sidecar in _sidecars(temporary):
                sidecar.unlink(missing_ok=True)
        # The PAUSE FIRST, and durably, before the database it guards can be
        # seen. `_publish` fsyncs the database and its directory; `pause_workers`
        # only wrote and chmod'ed, so a power loss in between — or simply a
        # failed marker write — left a published database with no
        # `<db>.workers-paused` beside it. The retry then refuses, because the
        # target exists; starting it instead makes `workers_paused` false and
        # lets unreconciled jobs run against a freshly restored database, which
        # is the one thing the marker exists to stop.
        #
        # Written against the TEMPORARY name and renamed with it, so the two
        # become visible together: a marker for a database that is not there yet
        # is harmless, and a database without its marker is not.
        marker = Path(f"{temporary}.workers-paused")
        try:
            marker.write_text(
                "restore requires explicit reconciliation\n", encoding="utf-8"
            )
            os.chmod(marker, 0o600)
            with open(marker, "rb") as handle:
                os.fsync(handle.fileno())
            _publish(marker, _pause_marker(destination))
        except OSError as error:
            marker.unlink(missing_ok=True)
            _pause_marker(destination).unlink(missing_ok=True)
            raise BackupError(
                f"restore could not durably pause workers: {error}"
            ) from error

        try:
            _publish(temporary, destination)
        except BaseException:
            # The database never became visible, so its marker must not stay
            # behind claiming to guard something that is not there.
            _pause_marker(destination).unlink(missing_ok=True)
            raise
        temporary = None
        restored = ControlPlaneDatabase(destination)
        if apply_migrations:
            restored.migrate()
        # Idempotent, and kept so the marker's content and mode stay owned by
        # one place. The durability above is what makes it survive; this is what
        # makes it say the same thing every other caller writes.
        restored.pause_workers()
        return restored
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


SIDECAR_SUFFIXES = ("-wal", "-shm")


def _sidecars(database: Path) -> tuple[Path, ...]:
    """The files SQLite keeps beside a database and treats as part of it.

    A `-wal` left at the canonical path is replayed into whatever database is
    sitting there — so a superseded WAL abandoned next to an installed rollback
    silently reapplies migrated pages to it. They move together or not at all.
    """
    return tuple(database.with_name(database.name + suffix) for suffix in SIDECAR_SUFFIXES)


def _pause_marker(database: Path) -> Path:
    """Kept in step with `ControlPlaneDatabase.worker_pause_path` by test."""
    return database.with_name(f"{database.name}.workers-paused")


def _free_bytes(directory: Path) -> int:
    stats = os.statvfs(directory)
    return int(stats.f_bavail * stats.f_frsize)


def _bundle(database: Path) -> tuple[Path, ...]:
    """Every file that has to travel with a database for it to be usable.

    The pause marker is one of them. It was left out of the space calculation
    and then declared mandatory after the install, so a volume with room for the
    database but not the marker copied the database successfully and failed on
    the marker with `ENOSPC` — leaving a restored database at the canonical path
    with no `workers-paused` beside it. Starting the service then resumes
    workers against unreconciled rollback data, which is the one outcome this
    command exists to prevent.
    """
    return (database, *_sidecars(database), _pause_marker(database))


def _occupied_bytes(database: Path, *, block_size: int = 0) -> int:
    """Space the bundle will take, in ALLOCATED blocks rather than logical size.

    A filesystem allocates whole blocks, so a bundle of four small files needs
    more room than the sum of their `st_size`. Reserving the logical total left
    the copy passing a check it should have failed — and the file that then ran
    out of room was the last one written, the pause marker. Round up, and add
    one block per file for the temporary the publication writes before renaming.
    """
    total = 0
    for path in _bundle(database):
        if not path.exists():
            continue
        size = path.stat().st_size
        if block_size > 0:
            size = -(-size // block_size) * block_size + block_size
        total += size
    return total


def _block_size(directory: Path) -> int:
    stats = os.statvfs(directory)
    return int(stats.f_frsize) or 4096


class RollbackError(RuntimeError):
    pass


def _fsync_directory(directory: Path) -> None:
    handle = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def _rename_or_exdev(source: Path, destination: Path) -> bool:
    """Rename if the filesystems allow it; report rather than raise if not.

    `EXDEV` is the kernel's answer to "is this a free rename or a full copy?",
    and asking it is more reliable than predicting it from `st_dev`: bind
    mounts, overlays and symlinked volumes all make the prediction wrong in one
    direction or the other. Nothing has happened when this returns False, which
    is what makes it safe to ask first and decide afterwards.
    """
    try:
        os.replace(source, destination)
    except OSError as error:
        if error.errno == errno.EXDEV:
            return False
        raise
    _fsync_directory(destination.parent)
    return True


def _copy_install(source: Path, destination: Path) -> None:
    """Chunked, because the Machine has 512 MiB against a 1 GiB volume."""
    temporary = destination.with_name(f".{destination.name}.installing")
    try:
        with open(source, "rb") as reader, open(temporary, "wb") as writer:
            for chunk in iter(lambda: reader.read(CHUNK_BYTES), b""):
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        _publish(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    source.unlink(missing_ok=True)


def _readable_sqlite(path: Path) -> None:
    """Refuse a candidate that is not a database, and leave it exactly as found.

    Read-only is not side-effect free. Opening a WAL database with `mode=ro`
    creates BOTH `-wal` and `-shm` and leaves them after close — measured, not
    assumed. The first version of this check therefore fabricated sidecars
    beside the restored database, which the install loop then dutifully copied
    to the canonical path: a check that manufactured the very artifact the
    install exists to keep straight. Anything this opens that was not here
    before goes away again.

    The URI is built from `as_uri()` rather than interpolated, so a database
    path containing `?` or `#` cannot have its remainder parsed as URI syntax —
    which would drop `mode=ro` and reopen the file read-WRITE.
    """
    absolute = path.resolve()
    before = {sidecar for sidecar in _sidecars(path) if sidecar.exists()}
    try:
        connection = sqlite3.connect(f"{absolute.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as error:
        raise RollbackError(f"{path} does not open as SQLite: {error}") from error
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise RollbackError(f"{path} fails its SQLite integrity check")
        connection.execute("PRAGMA foreign_keys=ON")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise RollbackError(f"{path} fails its SQLite foreign-key check")
    except sqlite3.DatabaseError as error:
        raise RollbackError(f"{path} is not a valid SQLite database") from error
    finally:
        connection.close()
        for sidecar in _sidecars(path):
            if sidecar not in before:
                sidecar.unlink(missing_ok=True)


def _regular_file(path: Path) -> bool:
    """A real file, not a symlink to one, not a directory, not a FIFO.

    `is_file()` follows symlinks and says nothing about the other shapes, so a
    bundle member could be a directory (the rename fails only after the database
    has moved), a FIFO (the copy blocks indefinitely with the target already
    gone), or a symlink pointing off the persistent volume (the canonical path
    ends up somewhere a Machine replacement will not preserve). `lstat` asks
    about the entry itself, which is what the installer is going to move.
    """
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _reserve_aside(target: Path, stamp: int) -> str:
    """A superseded-bundle name whose every member is free AND usable.

    `Path.rename` REPLACES an existing regular file on POSIX, silently. The name
    is second-resolution, so a retried install within the same second — or a
    caller passing a fixed `now`, or a clock that steps back — would rename the
    live database over the previous attempt's superseded copy and destroy the
    recovery point it was keeping.

    Length is checked with it, because `exists()` answers False for a name the
    filesystem cannot hold rather than rejecting it. On an ext4-like
    `NAME_MAX=255`, a 242-character basename yields a 255-character aside
    database name and a 259-character aside WAL name: the database rename
    succeeded and the WAL rename raised `ENAMETOOLONG`, leaving no canonical
    database and a retry that refuses because the target is missing. The whole
    bundle has to move as one generation, so a name is taken only when EVERY
    member of it is both free and expressible.
    """
    limit = _name_max(target.parent)
    base = f"{target.name}.superseded-{stamp}"
    for suffix in ("", *(f"-{index}" for index in range(1, 100))):
        candidate = f"{base}{suffix}"
        members = (
            candidate,
            *(f"{candidate}{sidecar}" for sidecar in SIDECAR_SUFFIXES),
            f"{candidate}.workers-paused",
        )
        if any(len(name) > limit for name in members):
            raise RollbackError(
                f"the superseded names for {target.name} would exceed the"
                f" filesystem's {limit}-character limit; rename the database to"
                " something shorter before rolling back"
            )
        if not any(target.with_name(name).exists() for name in members):
            return candidate
    raise RollbackError(
        f"every superseded name for {target.name} at {stamp} is taken;"
        " move the earlier copies aside before retrying"
    )


def _name_max(directory: Path) -> int:
    try:
        return int(os.pathconf(directory, "PC_NAME_MAX"))
    except (OSError, ValueError, AttributeError):
        return 255


def _preflight(
    restored: Path, target: Path, stamp: int, *, target_already_freed: bool
) -> str:
    """Everything that can be known before the live database is touched.

    The first version checked `restored.is_file()` and `target.exists()` and
    then started renaming, so a bundle that could never be installed — a
    truncated file, a directory, a restore with no worker pause — was discovered
    only after the live database had been moved aside and, in the pause-marker
    case, after the invalid candidate was already at the canonical path. An
    operator mid-rollback then had neither a working database nor an obvious way
    back. The checks that remain after the install stay as defence in depth.

    Returns the reserved superseded-bundle name, because reserving it is part of
    deciding whether the install can proceed at all.
    """
    if not _regular_file(restored):
        raise RollbackError(f"{restored} is not a regular file")
    if not target.exists():
        if not target_already_freed:
            raise RollbackError(
                f"nothing to supersede at {target}. If the superseded database"
                " was already archived off-volume and deleted to make room,"
                " say so with --target-already-freed; otherwise check the path."
            )
        if not target.parent.is_dir():
            raise RollbackError(f"{target.parent} is not a directory")
        # The mode exists because the documented low-space recovery ends with
        # the operator deleting the superseded database — and this command then
        # refused, because it had nothing to supersede. Following the supported
        # procedure produced an outage. It is a FLAG rather than an inference so
        # that a mistyped `--target` cannot silently install a rollback at a
        # path nobody meant.
        for member in _bundle(target):
            if member.exists():
                raise RollbackError(
                    f"{member} is still present, so the target was not freed;"
                    " remove --target-already-freed"
                )
    elif not _regular_file(target):
        raise RollbackError(f"{target} is not a regular file")
    if target.exists() and os.path.samefile(restored, target):
        # `resolve()` catches a lexical alias and a symlink, and misses two hard
        # links to one inode — under which the install would leave the
        # superseded path and the canonical target pointing at the same migrated
        # database while reporting a successful rollback. `samefile` compares
        # (st_dev, st_ino), which is the question actually being asked.
        raise RollbackError(
            f"{restored} and {target} are the same file; there is nothing to install"
        )
    marker = _pause_marker(restored)
    if not marker.exists():
        # Checked HERE rather than after the install. `restore` writes this
        # marker, so its absence means the candidate did not come from a
        # restore — and installing it would resume workers on a database nobody
        # reconciled, which is the failure the marker exists to prevent.
        raise RollbackError(
            f"{restored} has no worker pause at {marker};"
            " restore it with `abrolia-control-plane restore` rather than by hand"
        )
    # Every member that is present, on both sides, has to be the shape the
    # installer will move. A directory or FIFO in a sidecar position fails only
    # once the database has already gone.
    for bundle in (_bundle(restored), _bundle(target)):
        for member in bundle[1:]:
            if member.exists() and not _regular_file(member):
                raise RollbackError(f"{member} is not a regular file")
    _readable_sqlite(restored)

    aside = _reserve_aside(target, stamp)
    required = _occupied_bytes(restored, block_size=_block_size(target.parent))
    available = _free_bytes(target.parent)
    # Discovered, not predicted: a rename costs no blocks, and `EXDEV` is the
    # kernel's own answer to which one this is. Nothing has happened when the
    # probe returns.
    if _is_a_copy(restored.parent, target.parent) and available < required:
        # REFUSED, not resolved. An earlier version archived the superseded
        # database off-volume and deleted it to make room, which meant this
        # command could destroy the only copy of post-migration writes as a side
        # effect of an operator asking it to move a file. Freeing space is a
        # decision with data-loss consequences and it belongs to the operator,
        # who knows what else is on the machine; this says exactly what is
        # needed and stops with `/data` untouched.
        raise RollbackError(
            f"installing {restored.name} onto {target.parent} needs {required}"
            f" bytes and {available} are free. It is a copy, not a rename,"
            " because the restore is on another filesystem — so nothing is"
            " freed by moving the superseded database aside. Free space on"
            f" {target.parent} first: archive the superseded database"
            " off-volume with `abrolia-control-plane backup`, verify that"
            " archive restores, and only then delete it. Nothing has been"
            " changed."
        )
    return aside


def _is_a_copy(source: Path, destination: Path) -> bool:
    """Whether moving a file between these directories costs blocks.

    Asked by moving one, because `st_dev` and `rename(2)` disagree often enough
    to matter: bind mounts and overlays can share a device number across what
    are, for renaming purposes, different filesystems.
    """
    # A PID is not unique enough to name a temporary file with: it is reused,
    # and two containers on one volume share the namespace. `mkstemp` gets the
    # name from the kernel with O_EXCL, so the probe cannot collide with — or be
    # clobbered by — a concurrent one.
    # Reserved on BOTH sides. `mkstemp` guarantees the name is free in `source`
    # and says nothing about `destination`, which is where the probe is about to
    # land — so a concurrent probe, or any file that happens to hold that name
    # there, would be overwritten by the rename and then unlinked in the
    # `finally`. Claim it in the destination first, and only rename onto a name
    # this call owns.
    try:
        handle, name = tempfile.mkstemp(prefix=".volume-probe-", dir=source)
    except OSError:
        return True
    os.close(handle)
    probe = Path(name)
    try:
        claim, landed_name = tempfile.mkstemp(prefix=".volume-probe-", dir=destination)
    except OSError:
        probe.unlink(missing_ok=True)
        return True
    os.close(claim)
    landed = Path(landed_name)
    try:
        return not _rename_or_exdev(probe, landed)
    finally:
        probe.unlink(missing_ok=True)
        landed.unlink(missing_ok=True)


def install_rollback(
    restored: Path | str,
    target: Path | str,
    *,
    now: float | None = None,
    target_already_freed: bool = False,
) -> dict[str, object]:
    """Put a restored database where the rolled-back image will open it.

    This was eight `mv` lines in the runbook, and prose cannot be executed —
    neither by an operator under pressure nor by a test. Three of those steps
    have a failure that does not announce itself:

    1. **A sidecar can be separated from its database.** Both directions are
       damaging and one is silent: a superseded `-wal` left at the canonical
       path is replayed into the restore, reapplying the very pages the rollback
       exists to undo.
    2. **The pause marker can fail to travel**, resuming workers against a
       database nobody has reconciled.
    3. **The install can run out of room.** When the restore was staged off the
       volume, moving it in is a COPY, and renaming the superseded database
       aside frees nothing — same filesystem, same blocks. This is checked
       before anything moves and REFUSED with what is needed.

    It does not free space itself. An earlier version archived the superseded
    database off-volume and deleted it to make room, which meant a command an
    operator ran to move a file could destroy the only copy of every write taken
    after the migration. Freeing space is a decision with data-loss consequences
    and it belongs to whoever knows what else is on the machine.

    Nothing is deleted here at all: the superseded bundle is renamed aside,
    intact, and its name is in the report.
    """
    restored_path, target_path = Path(restored), Path(target)
    stamp = int(time.time() if now is None else now)

    # Take the writer lock for the whole install, and take it the same way
    # `serve` does so the two cannot disagree about which file is the lock.
    #
    # The command is documented as running with the service stopped, and it
    # asserted that rather than checking it. Renaming the database, `-wal` and
    # `-shm` out from under a live SQLite connection does not fail loudly: the
    # process keeps its file descriptors on the superseded inode, so subsequent
    # writes land in a file nothing will read again, and it can recreate stale
    # sidecars at the canonical path beside the installed rollback.
    #
    # `ControlPlaneDatabase` opens no connection until one is asked for, so this
    # takes the lock without touching SQLite. The preflight runs inside it,
    # because a check made before the lock is one another writer can invalidate.
    # Before the locks: a self-install would otherwise take the same lock twice
    # from one process and be reported as "another process still owns", which is
    # both wrong and unactionable.
    if (
        restored_path.exists()
        and target_path.exists()
        and os.path.samefile(restored_path, target_path)
    ):
        raise RollbackError(
            f"{restored_path} and {target_path} are the same file;"
            " there is nothing to install"
        )

    # BOTH ends, and in a fixed order. The candidate is a control-plane database
    # too: a `restore` still finishing at that path, or a second install reading
    # it, would have its bundle moved out from under it. Target first, then
    # source, always — two callers taking them in opposite orders is a deadlock,
    # and a consistent order is what prevents it.
    guards: list[ControlPlaneDatabase] = []
    try:
        for path, message in (
            (
                target_path,
                f"a control-plane writer still owns {target_path};"
                " stop the service before installing a rollback",
            ),
            (
                restored_path,
                f"another process still owns {restored_path};"
                " let the restore finish before installing it",
            ),
        ):
            guard = ControlPlaneDatabase(path)
            try:
                guard.acquire_process_lock()
            except ProcessAlreadyRunning as error:
                guard.close()
                raise RollbackError(message) from error
            guards.append(guard)
        aside = _preflight(
            restored_path,
            target_path,
            stamp,
            target_already_freed=target_already_freed,
        )
        return _install_rollback_locked(
            restored_path, target_path, aside=aside
        )
    finally:
        for guard in reversed(guards):
            guard.release_process_lock()
            guard.close()


def _install_rollback_locked(
    restored_path: Path, target_path: Path, *, aside: str
) -> dict[str, object]:
    volume = target_path.parent
    superseded = _bundle(target_path)
    report: dict[str, object] = {"target": str(target_path)}

    # These renames stay on the volume and cost no blocks. The pause marker
    # travels with them so that a restore whose own marker fails to arrive
    # cannot look paused because of a marker belonging to a database that is no
    # longer there.
    for path in superseded:
        if path.exists():
            path.rename(path.with_name(path.name.replace(target_path.name, aside, 1)))
    remaining = [str(path) for path in superseded if path.exists()]
    if remaining:
        raise RollbackError(
            f"superseded files remain at the canonical path: {', '.join(remaining)}"
        )
    # `None` in the already-freed mode: there was nothing to move aside, because
    # the operator archived and deleted it to make room. Naming a path that
    # holds nothing would read as a recovery copy that does not exist.
    report["superseded_kept_as"] = (
        str(volume / aside) if (volume / aside).exists() else None
    )

    # The PAUSE FIRST, for the same reason `restore_backup` publishes it first:
    # a database that becomes visible before its marker is a database that can
    # be started without one. Installing in bundle order put the database at the
    # canonical path and then the marker, so a crash or a failed copy in between
    # left a startable, unreconciled rollback.
    ordered = (
        _pause_marker(restored_path),
        restored_path,
        *_sidecars(restored_path),
    )
    destinations = (
        _pause_marker(target_path),
        target_path,
        *_sidecars(target_path),
    )
    for source, destination in zip(ordered, destinations, strict=True):
        if source.exists() and not _rename_or_exdev(source, destination):
            _copy_install(source, destination)

    if not _pause_marker(target_path).exists():
        raise RollbackError(
            "the restore is installed but its worker pause did not travel;"
            f" write {_pause_marker(target_path)} before starting anything"
        )
    report["workers"] = "paused"
    report["sidecars"] = sorted(
        path.name for path in _sidecars(target_path) if path.exists()
    )
    return report

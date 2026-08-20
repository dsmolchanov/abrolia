"""Authenticated control-plane SQLite backup with explicit paused restore."""

from __future__ import annotations

import errno
import hashlib
import hmac
import os
import re
import sqlite3
import stat
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from control_plane.db import MIGRATIONS_DIR, ControlPlaneDatabase, ProcessAlreadyRunning

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


def _require_sound_database(database: ControlPlaneDatabase) -> None:
    """Refuse to call a snapshot of a damaged database a restore point.

    Checked here rather than on the archive, because it covers BOTH paths at
    once: a snapshot taken now, and an existing one reused because its content
    matches this database. A digest proves the archive is the same as what is on
    disk; it says nothing about whether what is on disk can be restored.
    """
    connection = database.connection
    integrity = connection.execute("PRAGMA integrity_check").fetchone()
    if integrity is None or integrity[0] != "ok":
        raise BackupError(
            f"{database.path} fails its SQLite integrity check, so a snapshot of"
            " it is not a restore point; migration must not proceed"
        )
    connection.execute("PRAGMA foreign_keys=ON")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise BackupError(
            f"{database.path} fails its SQLite foreign-key check, so a snapshot"
            " of it is not a restore point; migration must not proceed"
        )


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
    # The gate this snapshot guards is "may the migration proceed", and a
    # snapshot of a corrupt database is not a restore point. `backup()`
    # materialises whatever state the file is in and the archive authenticates
    # perfectly — and a reusable archive is accepted on its digest alone, which
    # says the content matches, not that the content is sound. Prove it is
    # restorable before letting anything cross.
    _require_sound_database(database)
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
    # Regular files on THIS volume only. A symlink matching the glob — to an
    # archive in `/tmp` or any other ephemeral filesystem — authenticated
    # perfectly and was returned as the restore point, so the migration
    # proceeded believing a durable snapshot sat beside the database when
    # removing the link's target left no rollback artifact at all. The whole
    # point of this check is that something durable exists on the volume.
    candidates = sorted(
        (
            candidate
            for candidate in database.path.parent.glob(
                f"{database.path.name}.pre-migrate-{revision}-*.bak"
            )
            if _regular_file(candidate)
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


@dataclass
class _Move:
    """One member's journey, recorded as TWO facts rather than one.

    A move is a publication followed by a removal, and the removal can fail on
    its own — `EIO`, `EPERM`, a read-only remount — leaving both names present
    and the destination already published. A journal that recorded only "this
    move happened" could not describe that state, so `_undo` inferred it from
    whether the source was back: finding the source still there, it concluded
    the move had not happened and left the destination in place. Reversal then
    removed nothing, and the states that outlives are the ones this module
    exists to prevent — an installed database standing beside superseded
    sidecars, or a restore whose pause was withdrawn while its database stayed.

    `published_inode` is what makes the reversal safe to perform by name: the
    destination is removed only while that name still refers to the entry this
    move put there, the same discipline `_is_a_copy` uses for its probes.
    """

    source: Path
    destination: Path
    published_inode: tuple[int, int] | None = None
    #: False while the destination is published and the source still stands.
    source_removed: bool = False


def _claim(
    source: Path,
    destination: Path,
    journal: list[_Move] | None = None,
) -> None:
    """Move `source` onto `destination`, refusing to replace anything.

    `os.replace` is atomic and CLOBBERS, so guarding it with a check leaves a
    window: another process creates the final name in between and the rename
    destroys it silently. `os.link` is the atomic version of the question this
    code actually asks — it fails with `EEXIST` rather than replacing — so the
    name is claimed or it is not, with nothing in between for a race to occupy.

    Same-filesystem only, which every caller already is: the temporary is
    created beside its destination, and a cross-filesystem move is handled by
    `_rename_or_exdev` reporting `EXDEV` instead.
    """
    os.link(source, destination)
    # Journal the move BEFORE the unlink, which can fail on its own — `EIO`,
    # `EPERM`, a read-only remount. Unlinking first meant a caller could not
    # know the destination now existed, so `_undo` would not reverse it and the
    # bundle stayed half-apart. The link is the move; the unlink is tidying
    # after it, and `source_removed` is set only once that tidying succeeded.
    record = _Move(source, destination, published_inode=_inode(destination))
    if journal is not None:
        journal.append(record)
    try:
        source.unlink(missing_ok=True)
    except OSError:
        # Somebody has to reverse this. A journalled caller does it in `_undo`,
        # which can also put back the members that moved before this one — so
        # the exception goes there untouched. NOBODY is watching an unjournaled
        # call, and `_publish` is exactly that: the destination is published,
        # the exception escapes before the directory is even synced, and the
        # caller's cleanup unlinks a temporary that has already moved. Restore
        # then withdrew its pause marker over a database that was standing.
        if journal is not None:
            raise
        _withdraw(destination)
        raise
    record.source_removed = True


class AmbiguousPublication(BackupError):
    """The destination may or may not exist after a failed publication.

    Raised when the rollback of a publication could not be made durable. The
    caller must NOT then remove a worker pause: a database that survives without
    its marker is one that starts unpaused, which is the failure the marker
    exists to prevent, reached through the cleanup of a different failure.
    """

    def __init__(self, destination: Path) -> None:
        super().__init__(
            f"{destination} may or may not exist after a failed publication;"
            " leaving the worker pause in place"
        )
        self.destination = destination


def _withdraw(destination: Path) -> None:
    """Take a published name away, and make the ABSENCE durable too.

    Unlinking without syncing the directory leaves the same ambiguity the
    publication was careful to avoid: the entry may or may not survive a crash,
    so a caller that then removes a pause marker can leave a database present
    and unpaused. Sync, and say the state is ambiguous if even that fails.
    """
    destination.unlink(missing_ok=True)
    try:
        _fsync_directory(destination.parent)
    except OSError as error:
        raise AmbiguousPublication(destination) from error


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
    # ATOMIC, not checked. Re-checking here closed the wide window — an entry
    # created while a database-sized image was being encrypted — and left the
    # narrow one, between the check and the rename. `_claim` has no window:
    # `os.link` fails with `EEXIST` rather than replacing, so this either takes
    # a name nobody held or takes nothing.
    _claim(temporary, destination)
    try:
        _fsync_directory(destination.parent)
    except OSError:
        # Undo the rename, durably — see `_withdraw`.
        #
        # Leaving the file behind is worse than not writing it:
        # `_reusable_pre_migrate_backup` would find this archive on the next
        # boot, skip the snapshot, and migrate against a restore point whose
        # name was never made durable — turning a fail-closed boot into a
        # fail-open one, which is the exact shape of bug this module keeps
        # finding in itself. The caller's `finally` cannot do it: the temporary
        # path no longer exists, so its `unlink(missing_ok=True)` is a no-op.
        _withdraw(destination)
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
    # BEFORE the occupancy check, because the dangerous aliases are the ones
    # that are ABSENT. `<db>.workers-paused` is normally not there, so occupancy
    # said yes and the authenticated archive was published as the live pause
    # marker: the backup reported success and every worker afterwards treated
    # the database as an unreconciled restore and stopped doing durable work. A
    # nominal backup silently disabled production.
    _refuse_alias(destination, database.path, what="the database being archived")
    if _present(destination):
        # `lexists`, because a DANGLING symlink is reported absent by
        # `Path.exists()` — and `_publish`'s `os.replace` would then silently
        # replace that entry, destroying a link the operation does not own and
        # publishing the archive under a name that meant something else.
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
    _require_free_bundle(destination)
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
        # Re-checked INSIDE the lock, and over the WHOLE BUNDLE. The check
        # above runs before the lock is held, so another process could occupy
        # that path in between — the refusal to publish beside somebody else's
        # state is the point, and a check made outside the lock is one the lock
        # cannot back. Rechecking only `destination` left the sidecars
        # unguarded: publication claims the database and marker names
        # atomically but claims nothing for `-wal`/`-shm`, so a generation that
        # released its lock between the two checks could leave frames behind
        # for the restored database to replay.
        _require_free_bundle(destination)
        return _restore_locked(
            source,
            destination,
            backup_key=backup_key,
            apply_migrations=apply_migrations,
        )
    finally:
        guard.release_process_lock()
        guard.close()


def _withdraw_pause(destination: Path, owned: tuple[int, int] | None) -> None:
    """Take back the pause THIS invocation published, and no other.

    Three ways not to remove it, and the asymmetry is deliberate: a spare pause
    marker beside nothing is a harmless puzzle, while a missing one is a
    database that starts unpaused against unreconciled data.

    * Nothing was published — including the `EEXIST` case, where the marker at
      that path is another restore's.
    * The entry is no longer the one that was published.
    * A database now occupies the target. Whatever put it there, it is better
      paused than not, and this invocation cannot tell which.

    Not fsynced, unlike `_withdraw`. An unsynced removal can be resurrected by a
    crash, and a marker that comes back beside no database is the safe
    direction; raising `AmbiguousPublication` from a cleanup path would replace
    the caller's diagnosis with a symptom.
    """
    if owned is None:
        return
    pause = _pause_marker(destination)
    if _inode(pause) != owned or _present(destination):
        return
    pause.unlink(missing_ok=True)


def _require_free_bundle(destination: Path) -> None:
    """No member of the destination bundle may exist.

    The whole BUNDLE, not just the database. A `-wal` or `-shm` left by an
    earlier database at that path — an interrupted cleanup, a killed process —
    survives publication, and opening the result replays those committed frames
    into the authenticated restore. Rows are silently replaced and
    `integrity_check` still reports `ok`, so nothing downstream notices. The
    pause marker too: one left behind would make a restore that failed to write
    its own marker look paused.
    """
    if _present(destination):
        raise FileExistsError(destination)
    stale = [path for path in _bundle(destination)[1:] if _present(path)]
    if stale:
        raise BackupError(
            "restore refuses a destination with leftover SQLite state:"
            f" {', '.join(str(path) for path in stale)}."
            " Move them aside; they belong to a different database."
        )


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
        # Last look before anything becomes visible. Everything between the
        # locked check and here — decryption, an fsync of a database-sized
        # image, an integrity check — is time in which a member can appear, and
        # the publication below is the point of no return for the ones it does
        # not claim.
        _require_free_bundle(destination)
        marker = Path(f"{temporary}.workers-paused")
        # The inode THIS invocation publishes, so the cleanups below can tell
        # the marker they created from one that arrived while they were working.
        owned_marker: tuple[int, int] | None = None
        try:
            marker.write_text(
                "restore requires explicit reconciliation\n", encoding="utf-8"
            )
            os.chmod(marker, 0o600)
            with open(marker, "rb") as handle:
                os.fsync(handle.fileno())
            _publish(marker, _pause_marker(destination))
            owned_marker = _inode(_pause_marker(destination))
        except OSError as error:
            marker.unlink(missing_ok=True)
            # `owned_marker` is None on EVERY failure here, which is the point.
            # `_publish` fails with `EEXIST` precisely when another process
            # created that marker after the last occupancy check, and the
            # cleanup used to unlink it by name — destroying the pause belonging
            # to somebody else's restore while reporting only this one's error.
            _withdraw_pause(destination, owned_marker)
            raise BackupError(
                f"restore could not durably pause workers: {error}"
            ) from error

        try:
            _publish(temporary, destination)
        except AmbiguousPublication:
            # The database may have survived. Its marker STAYS: a spare pause
            # beside nothing is harmless, and a database beside nothing is a
            # database that starts unpaused.
            raise
        except BaseException:
            # This restore's database definitely never became visible, so the
            # marker this restore published must not stay behind claiming to
            # guard something that is not there — unless something else has
            # arrived at that path in the meantime, in which case removing the
            # pause is the one thing that must not happen.
            _withdraw_pause(destination, owned_marker)
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


def _writer_lock(database: Path) -> Path:
    """Where `ControlPlaneDatabase.acquire_process_lock` puts its lock file.

    Part of the namespace an operation owns even though it is not part of the
    bundle: an archive published over it is an archive published over the thing
    that decides whether anyone else may write.
    """
    return database.with_name(f"{database.name}.writer.lock")


def _namespace(database: Path) -> tuple[Path, ...]:
    """Every pathname a control-plane database's operations own."""
    return (*_bundle(database), _writer_lock(database))


def _identity(path: Path) -> tuple:
    """A name that can be compared before the entry behind it exists.

    The PARENT is resolved and the final component is not: resolving the whole
    path would follow `<db>-wal` to whatever it points at, and the question is
    about the entry itself. Resolving the parent is what catches an alias
    reached through a symlinked directory, which comparing strings does not.
    """
    try:
        parent = path.parent.resolve()
    except OSError:
        parent = path.parent
    return (parent, path.name)


def _refuse_alias(
    candidate: Path,
    owner: Path,
    *,
    what: str,
    error: type[RuntimeError] = None,  # noqa: RUF013 - resolved below
) -> None:
    """Refuse a user-supplied path that names part of `owner`'s namespace.

    Nothing here creates or opens anything: the answer has to be known BEFORE
    the operation starts, because by the time an archive has been published over
    `<db>.workers-paused` the damage is a paused production service, and by the
    time a rollback has moved a candidate's own marker aside there may be no
    database at the canonical path at all.

    Both by name and by inode. A name comparison catches the alias the operator
    typed; comparing entries catches two hard links to one file, which no amount
    of path normalisation will. `os.path.samefile` is the obvious way to ask and
    the wrong one: it FOLLOWS symlinks, so it raises `FileNotFoundError` on a
    dangling one — an entry this has to be able to reason about rather than
    crash on. `lstat` asks about the entries themselves.
    """
    error = error or BackupError
    identity = _identity(candidate)
    entry = _inode(candidate)
    for member in _namespace(owner):
        if identity == _identity(member) or (
            entry is not None and entry == _inode(member)
        ):
            raise error(
                f"{candidate} is {member}, which belongs to {what} at {owner}."
                " Choose a path outside that database's bundle; writing there"
                " would disable or destroy the thing being protected."
            )


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


def _rename_or_exdev(
    source: Path,
    destination: Path,
    journal: list[_Move] | None = None,
) -> bool:
    """Rename if the filesystems allow it; report rather than raise if not.

    `EXDEV` is the kernel's answer to "is this a free rename or a full copy?",
    and asking it is more reliable than predicting it from `st_dev`: bind
    mounts, overlays and symlinked volumes all make the prediction wrong in one
    direction or the other. Nothing has happened when this returns False, which
    is what makes it safe to ask first and decide afterwards.
    """
    try:
        _claim(source, destination, journal)
    except OSError as error:
        if error.errno == errno.EXDEV:
            return False
        raise
    _fsync_directory(destination.parent)
    return True


def _copy_install(
    source: Path,
    destination: Path,
    journal: list[_Move] | None = None,
) -> None:
    """Chunked, because the Machine has 512 MiB against a 1 GiB volume.

    The temporary name comes from the kernel, not from the destination. A
    predictable `<destination>.installing` was a path anything could already
    occupy: a directory or unwritable entry there raised only AFTER the live
    database had been renamed aside, and a regular file or symlink was followed
    and truncated — so the failure was either an outage with no canonical
    database or the destruction of an unrelated file. `mkstemp` creates it with
    `O_EXCL`, so this call owns the name it is about to publish from.
    """
    handle, name = tempfile.mkstemp(
        prefix=f".{destination.name}.installing.", dir=destination.parent
    )
    temporary = Path(name)
    try:
        with open(source, "rb") as reader, os.fdopen(handle, "wb") as writer:
            for chunk in iter(lambda: reader.read(CHUNK_BYTES), b""):
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        os.chmod(temporary, 0o600)
        _publish(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    # Recorded the instant the copy is published, and separately once the
    # source is gone — a copy whose source removal fails leaves both names
    # holding the generation, which `_undo` has to be able to tell from a move
    # that never started. See `_Move`.
    record = _Move(source, destination, published_inode=_inode(destination))
    if journal is not None:
        journal.append(record)
    try:
        source.unlink(missing_ok=True)
    except OSError:
        # Somebody has to reverse this. A journalled caller does it in `_undo`,
        # which can also put back the members that moved before this one — so
        # the exception goes there untouched. NOBODY is watching an unjournaled
        # call — `_undo`'s own cross-filesystem reversal is one — so the copy
        # it just published has to go away here or stand forever.
        if journal is not None:
            raise
        _withdraw(destination)
        raise
    record.source_removed = True


@contextmanager
def _read_only_sqlite(path: Path):
    """Open read-only, and leave the bundle exactly as it was found.

    Read-only is not side-effect free. Opening a WAL database with `mode=ro`
    creates BOTH `-wal` and `-shm` and leaves them after close — measured, not
    assumed. The first version of this check therefore fabricated sidecars
    beside the restored database, which the install loop then dutifully copied
    to the canonical path: a check that manufactured the very artifact the
    install exists to keep straight. Anything this opens that was not here
    before goes away again.

    "Was not here before" is asked with `lexists`, not `exists`. A DANGLING
    sidecar symlink answers False to `exists()`, so it was absent from the
    snapshot, present afterwards, and the cleanup below unlinked it as though
    SQLite had just created it — a nominally read-only validation deleting an
    entry the operator put there. Non-regular members are refused outright
    before anything is opened: SQLite would follow a live symlink out of the
    volume, and neither a FIFO nor a directory is a sidecar.

    Deletion is by INODE, not by name. The entries are read while the
    connection still holds them and rechecked immediately before the unlink, so
    a name reused between close and cleanup takes somebody else's file with it.

    The URI is built from `as_uri()` rather than interpolated, so a database
    path containing `?` or `#` cannot have its remainder parsed as URI syntax —
    which would drop `mode=ro` and reopen the file read-WRITE.
    """
    for sidecar in _sidecars(path):
        if _present(sidecar) and not _regular_file(sidecar):
            raise RollbackError(
                f"{sidecar} is not a regular file, so {path} is not a bundle"
                " this command can safely read; move it aside"
            )
    inherited = {sidecar for sidecar in _sidecars(path) if _present(sidecar)}
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as error:
        raise RollbackError(f"{path} does not open as SQLite: {error}") from error
    try:
        yield connection
    finally:
        created = {
            sidecar: _inode(sidecar)
            for sidecar in _sidecars(path)
            if sidecar not in inherited and _present(sidecar)
        }
        connection.close()
        for sidecar, inode in created.items():
            if inode is not None and _inode(sidecar) == inode:
                sidecar.unlink(missing_ok=True)


def _readable_sqlite(path: Path) -> None:
    """Refuse a candidate that is not a sound database."""
    with _read_only_sqlite(path) as connection:
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise RollbackError(f"{path} fails its SQLite integrity check")
            connection.execute("PRAGMA foreign_keys=ON")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise RollbackError(f"{path} fails its SQLite foreign-key check")
        except sqlite3.DatabaseError as error:
            raise RollbackError(f"{path} is not a valid SQLite database") from error


def _present(path: Path) -> bool:
    """Does an ENTRY exist here, link or not.

    `Path.exists()` follows symlinks, so a dangling one answers False — and a
    dangling `-wal` therefore skipped validation entirely, survived the
    superseded move, and sat beside the installed rollback where the next SQLite
    write follows it out of the volume. `lexists` asks about the entry, which is
    what the installer is going to move.
    """
    return os.path.lexists(path)


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
        if not any(_present(target.with_name(name)) for name in members):
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


def _refuse_overlap(restored: Path, target: Path) -> None:
    """Neither side's namespace may name any part of the other's.

    Includes each database in the other's comparison, so the plain
    self-install — the same file named twice — is one case of the general rule
    rather than a separate check that happened to be the only one made.
    """
    if _identity(restored) == _identity(target) or (
        _inode(restored) is not None and _inode(restored) == _inode(target)
    ):
        # Named separately because it is the case an operator actually reaches,
        # and "there is nothing to install" says more than "these namespaces
        # overlap". `_inode` compares entries, so two hard links to one database
        # are caught where `resolve()` would report two different paths.
        raise RollbackError(
            f"{restored} and {target} are the same file; there is nothing to"
            " install"
        )
    for candidate in _namespace(restored):
        _refuse_alias(
            candidate, target, what="the rollback target", error=RollbackError
        )
    for candidate in _namespace(target):
        _refuse_alias(
            candidate, restored, what="the restored candidate", error=RollbackError
        )


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
    # Re-asked under the locks. The check before them runs unlocked, so a
    # symlink retargeted in between could alias the two namespaces after the
    # question had already been answered.
    _refuse_overlap(restored, target)
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
            if _present(member) and not _regular_file(member):
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


def _inode(path: Path) -> tuple[int, int] | None:
    """Which filesystem entry this name refers to, or None if it refers to none.

    Compared before a cleanup unlink: a NAME can be reused by another process
    between creating a temporary and removing it, and deleting by name alone
    then removes somebody else's file.
    """
    try:
        info = path.lstat()
    except OSError:
        return None
    return (info.st_dev, info.st_ino)


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
    # The INODES this call owns. Both names can be taken by somebody else in the
    # windows below — `landed` after an `EXDEV` result, `probe` after a
    # successful move vacates it — and the cleanup used to unlink both
    # unconditionally, deleting an interloper's file. Cleanup now removes a name
    # only while it still refers to the entry this call created.
    owned = {probe: _inode(probe), landed: _inode(landed)}
    # `mkstemp` reserved the destination name so a concurrent probe could not
    # take it; release it just before claiming, because `_claim` refuses to
    # replace an existing entry and would otherwise refuse our own reservation.
    landed.unlink(missing_ok=True)
    owned[landed] = None
    try:
        moved = _rename_or_exdev(probe, landed)
        if moved:
            # The probe's inode is at `landed` now, and `probe` is vacant.
            owned[landed] = owned[probe]
            owned[probe] = None
        return not moved
    except FileExistsError:
        # Something took the name in that instant. The question cannot be
        # answered, and "assume it is a copy" is the conservative answer: it
        # leads to the space check rather than past it.
        return True
    finally:
        for path, inode in owned.items():
            if inode is not None and _inode(path) == inode:
                path.unlink(missing_ok=True)


#: `CREATE TABLE [IF NOT EXISTS] name`, in any of the quotings SQLite accepts.
_CREATE_TABLE = re.compile(
    r"""CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?["'`\[]?(\w+)""",
    re.IGNORECASE,
)
#: A `--` comment, so a commented-out statement is not read as a real one.
_SQL_COMMENT = re.compile(r"--[^\n]*")


def _tables_created_by(migrations: tuple[str, ...]) -> set[str]:
    """Every table these migration scripts create.

    Derived from the scripts rather than listed here, so it cannot fall behind
    them, and so it answers for EVERY historical revision without a table of
    revisions to maintain. No migration in this schema has ever dropped or
    renamed a table, which is what makes "created by an applied migration"
    equivalent to "present now".
    """
    names: set[str] = set()
    for migration in migrations:
        script = _SQL_COMMENT.sub(
            "", (MIGRATIONS_DIR / migration).read_text(encoding="utf-8")
        )
        names.update(_CREATE_TABLE.findall(script))
    return names


def _shipped_migrations() -> tuple[str, ...]:
    """Every migration in this image, in the order `db.migrate` applies them.

    Read from the same directory `ControlPlaneDatabase.migrate` reads, so the
    identity check and the migrator can never disagree about what this schema
    is called.
    """
    return tuple(sorted(entry.name for entry in MIGRATIONS_DIR.glob("*.sql")))


def require_control_plane_database(path: Path) -> None:
    """Establish that `path` IS the control-plane database, not merely a file.

    A recovery command that archives the wrong file is worse than one that
    refuses: the operator verifies the archive, it passes, and they delete the
    real bundle. `is_file()` does not establish this — a zero-byte file, an
    unrelated SQLite database and a symlink to either all satisfy it — and
    neither does the mere presence of a `schema_migrations` table with `name`
    and `applied_at` columns, which is one of the most widely copied
    conventions there is. Any unrelated application using it, with any
    `NNNN_something.sql` in it, passed.

    So the ledger is checked against THIS IMAGE'S migration filenames. An
    unrelated application shares the table but not the names in it, while every
    valid revision of the real database — including one stopped at a failed
    pending migration — holds a prefix of them.

    A database whose ledger runs PAST this image is accepted, and deliberately.
    An image rolled back to recover from a bad deploy sees migrations it does
    not ship, and refusing there would deny an operator the backup command at
    exactly the moment they most need one. The shared prefix is what
    establishes identity; the tail beyond it only establishes which side is
    newer.

    Leaves nothing behind: the read-only opens are the same one
    `_readable_sqlite` makes, with the same bundle validation and sidecar
    cleanup.
    """
    if not _regular_file(path):
        raise BackupError(
            f"{path} is not a regular file; ABROLIA_CONTROL_PLANE_DB must name"
            " the control-plane database itself, not a link or a directory"
        )
    try:
        _readable_sqlite(path)
        with _read_only_sqlite(path) as connection:
            ledger = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name = 'schema_migrations'"
            ).fetchone()
            ledger_columns = (
                connection.execute("PRAGMA table_info(schema_migrations)").fetchall()
                if ledger is not None
                else []
            )
            applied = (
                [
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM schema_migrations ORDER BY name"
                    )
                ]
                if ledger is not None
                else []
            )
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
    except RollbackError as error:
        raise BackupError(str(error)) from error
    if ledger is None:
        raise BackupError(
            f"{path} opens as SQLite but carries no migration ledger, so it is"
            " not the control-plane database"
        )
    # The ledger's SHAPE and CONTENT, not just its name. `CREATE TABLE
    # schema_migrations(name TEXT)` in an unrelated database passed the
    # existence check, and so did a malformed one with the wrong columns — after
    # which the operator verifies a perfectly good archive of the wrong file and
    # deletes the real one.
    columns = {row[1] for row in ledger_columns}
    if not {"name", "applied_at"} <= columns:
        raise BackupError(
            f"{path} has a `schema_migrations` table with columns {sorted(columns)},"
            " which is not the control-plane ledger"
        )
    if not applied:
        raise BackupError(
            f"{path} has a control-plane-shaped ledger with no migration"
            " recorded in it, so it is not the control-plane database"
        )
    shipped = _shipped_migrations()
    shared = min(len(applied), len(shipped))
    if applied[:shared] != list(shipped[:shared]):
        recorded = ", ".join(applied[:5]) or "nothing"
        raise BackupError(
            f"{path} records migrations this schema does not have ({recorded});"
            " it is some other application's database, not the control plane's"
        )
    # The ledger NAMES a schema; this is the schema itself. A file holding
    # nothing but `schema_migrations` and the row `0001_control_plane.sql`
    # satisfied every check above — it passes integrity and foreign-key checks
    # precisely because it has no rows and no references — and a copied ledger
    # is the easiest thing in the world to produce by accident. What the
    # recorded migrations created has to actually be there.
    missing = sorted(_tables_created_by(tuple(applied[:shared])) - tables)
    if missing:
        raise BackupError(
            f"{path} records migrations whose tables are not in it"
            f" ({', '.join(missing)}); its ledger describes a schema the file"
            " does not have, so it is not the control-plane database"
        )


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
    # The two DATABASES were compared, and nothing else. `--target
    # <restored>.workers-paused` is a different regular file, so it passed:
    # the superseded loop then moved the candidate's own marker aside, renamed
    # the candidate database onto that marker's pathname, and the missing-pause
    # check raised from outside the undo block — leaving the candidate split
    # across two names with no database at the intended canonical path, and
    # nothing to retry from. Every pathname on both sides, before any lock is
    # taken and before anything moves.
    _refuse_overlap(restored_path, target_path)

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


def _undo(moved: list[_Move]) -> None:
    """Put every completed move back, newest first, and never mask the cause.

    Publication and source removal are reversed separately, because they can
    fail separately:

    * destination gone — nothing was published, or somebody has already
      cleaned it up. Leave it.
    * published, source removed — the ordinary completed move. Put the
      generation back under its original name.
    * published, source still standing — the removal failed after the link or
      copy. The generation is already back where it started, so the reversal is
      to take the destination away, durably.

    Best effort by necessity — the filesystem that just failed may fail again —
    but each attempt is independent, so one that cannot be undone does not
    prevent the rest. The original exception is what the caller sees; a failure
    in here would replace a diagnosis with a symptom.
    """
    unreversed: list[Path] = []
    for move in reversed(moved):
        source, destination = move.source, move.destination
        try:
            if not _present(destination):
                continue
            if _inode(destination) != move.published_inode:
                # Somebody else holds this name now. Removing it or moving it
                # would destroy their file, which is the failure mode this
                # module keeps finding in its own cleanup paths.
                unreversed.append(destination)
                continue
            if not move.source_removed:
                destination.unlink()
                # Durable, like every other move this module makes. An unsynced
                # removal leaves the same ambiguity the forward publication was
                # careful to avoid.
                _fsync_directory(destination.parent)
            elif _present(source):
                unreversed.append(destination)
            else:
                if not _rename_or_exdev(destination, source):
                    # The forward direction crossed a filesystem, so the reverse
                    # does too: `os.link` answers `EXDEV` and a copy is the only
                    # way back. Both paths fsync the directory they publish
                    # into.
                    _copy_install(destination, source)
                if destination.parent != source.parent:
                    # And the directory the reversal EMPTIED. Both helpers sync
                    # only where they land, which is enough when a member moves
                    # within one directory and not enough when it does not —
                    # `install-rollback` moves between the staging directory and
                    # the volume. With no superseded bundle to restore
                    # afterwards, as in `--target-already-freed`, nothing else
                    # syncs the volume, so a crash could resurrect an arbitrary
                    # subset of the canonical members `_undo` had just reported
                    # gone: the database without its pause marker among them.
                    _fsync_directory(destination.parent)
        except OSError:
            unreversed.append(destination)
            continue
    if unreversed:
        # Reported, not swallowed. The original exception still propagates —
        # that is the diagnosis — but an operator needs to know the volume is
        # not back where it started, and which files to look at.
        print(
            "rollback could not be fully reversed; these are not where they"
            f" started: {', '.join(str(path) for path in unreversed)}",
            file=sys.stderr,
        )


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
    #
    # Every move is recorded, and any failure undoes them in reverse. A rename
    # that raises after an earlier member moved — an `EIO` on the WAL, a
    # cross-filesystem copy running out of space after the marker landed — used
    # to escape with the bundle half-apart: no canonical database, no complete
    # candidate, and a retry that refuses because the target is missing. Either
    # the original bundle stands or the new one is complete; there is no third
    # acceptable state.
    moved: list[_Move] = []
    try:
        for path in superseded:
            if _present(path):
                landed = path.with_name(
                    path.name.replace(target_path.name, aside, 1)
                )
                # Claimed, not renamed: `_reserve_aside` checks the whole bundle
                # is free and another process can still take one of those names
                # before this loop reaches it. The reservation narrows the
                # window; only the claim closes it. The journal is passed IN so
                # the move is recorded the instant the link succeeds, before the
                # unlink that can fail after it.
                _claim(path, landed, moved)
    except BaseException:
        _undo(moved)
        raise
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
    # Marker, then sidecars, then the DATABASE LAST. The database becoming
    # visible is what makes the rollback real, so everything belonging to that
    # generation has to be in place first. Publishing it before its sidecars
    # meant that a crash in between left a canonical database whose committed
    # WAL frames — a candidate inspected by a process that was then killed still
    # has them — were sitting at the staging path, so the next open silently
    # read a database missing its own latest commits.
    ordered = (
        _pause_marker(restored_path),
        *_sidecars(restored_path),
        restored_path,
    )
    destinations = (
        _pause_marker(target_path),
        *_sidecars(target_path),
        target_path,
    )
    try:
        for source, destination in zip(ordered, destinations, strict=True):
            if _present(source) and not _rename_or_exdev(
                source, destination, moved
            ):
                _copy_install(source, destination, moved)
    except BaseException:
        # Back to the original bundle, in reverse: the installed members return
        # to the candidate, and the superseded members return to the canonical
        # path. An operator retries against the state they started from.
        _undo(moved)
        raise

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

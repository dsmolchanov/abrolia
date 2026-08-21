"""Authenticated control-plane SQLite backup with explicit paused restore."""

from __future__ import annotations

import contextlib
import errno
import hashlib
import hmac
import os
import sqlite3
import stat
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


def _require_identity(path: Path, expected: tuple[int, int] | None) -> None:
    """Refuse if `path` no longer names the entry that was validated."""
    if expected is None:
        return
    if _inode(path) != expected:
        raise BackupError(
            f"{path} is not the database that was validated; it was replaced"
            " while this backup was running. Nothing has been archived — an"
            " archive of an unvalidated file is exactly what licenses deleting"
            " the real one."
        )


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


def _open_pinned(path: Path):
    """Open once, and answer every later question from the DESCRIPTOR.

    `stat` by pathname followed by `open` by pathname is two questions about two
    possibly different files. The archive's size was read one way and its bytes
    another, so replacing it in between with a same-sized archive valid under
    the same key restored a different generation — authenticated, and not the
    one the operator selected. `O_NOFOLLOW` refuses a symlink outright rather
    than following it somewhere ephemeral.
    """
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _digest_of(handle) -> bytes:
    """The digest of an OPEN file, read from where it is and left there."""
    start = handle.tell()
    try:
        handle.seek(0)
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(CHUNK_BYTES), b""):
            digest.update(chunk)
        return digest.digest()
    finally:
        handle.seek(start)


def _content_identity(path: Path) -> tuple[tuple[int, int] | None, bytes | None]:
    """What this entry IS: which inode, and which bytes — from ONE descriptor.

    The inode alone is not identity. A process holding an open descriptor can
    truncate or rewrite a file in place without changing it, so a check that
    compared only `(st_dev, st_ino)` passed and the modified member was
    installed as though it were the validated one.

    Both halves come from `fstat` on the descriptor the digest was read
    through. Taking the inode by pathname AFTER the open was the same
    ask-twice mistake one level down: a swap in between pairs one file's digest
    with another file's inode, and the pair is then trusted as an identity.

    ABSENT is `(None, None)`, and that is the only identity this returns for a
    file it could not read. An unreadable present member raises: the earlier
    version answered `(inode, None)`, so two transiently failing states
    compared EQUAL with no byte proof behind either — an install or a reversal
    licensed by a pair of failures.
    """
    if not _present(path):
        return (None, None)
    with _open_pinned(path) as handle:
        info = os.fstat(handle.fileno())
        entry = (info.st_dev, info.st_ino)
        if _inode(path) != entry:
            # The name stopped referring to this descriptor between the open
            # and now. Whatever is hashed here would not be what the next move
            # consumes.
            raise _Substituted(path)
        digest = _digest_of(handle)
        # And AGAIN, because the digest read is the long part: a rename during
        # it leaves this returning the old descriptor's identity while the move
        # that follows takes the replacement. The window cannot be closed
        # without holding the name, but an identity that spans a substitution
        # can be refused.
        if _inode(path) != entry:
            raise _Substituted(path)
        return (entry, digest)


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
    #: The bytes this move put at the destination. The inode is not enough: a
    #: process holding an open descriptor rewrites a file in place without it
    #: changing, and `_undo` then moved the MODIFIED bytes back into the
    #: canonical namespace, reported nothing outstanding, and released the
    #: locks with no fail-safe pause.
    published_digest: bytes | None = None
    #: False when the identity could not be READ after publication. Such a move
    #: is never reversed: an unprovable identity must not compare equal to
    #: another unprovable one, which is what recording `None` for both would do.
    identity_proven: bool = True
    #: False while the destination is published and the source still stands.
    source_removed: bool = False


def _claim(
    source: Path,
    destination: Path,
    journal: list[_Move] | None = None,
) -> tuple[tuple[int, int] | None, bytes | None, bool]:
    """Move `source` onto `destination`, refusing to replace anything.

    RETURNS what it published — inode, digest, and whether either could be
    established — taken from the source before the link. Every cleanup and
    every reversal downstream is authorised against this and nothing else.

    `os.replace` is atomic and CLOBBERS, so guarding it with a check leaves a
    window: another process creates the final name in between and the rename
    destroys it silently. `os.link` is the atomic version of the question this
    code actually asks — it fails with `EEXIST` rather than replacing — so the
    name is claimed or it is not, with nothing in between for a race to occupy.

    Same-filesystem only, which every caller already is: the temporary is
    created beside its destination, and a cross-filesystem move is handled by
    `_rename_or_exdev` reporting `EXDEV` instead.
    """
    # BEFORE the link, from the entry this call already owns. Deriving the
    # token by reopening the DESTINATION afterwards meant that an actor who
    # replaced it in between had their inode recorded as the published one —
    # and the withdrawal that followed was then authorised to unlink their
    # file. `os.link` makes the destination the source's inode by construction,
    # so nothing has to be read back to know what was published.
    published, digest, proven = _identity_before_publication(source)
    os.link(source, destination)
    # Journal the move BEFORE the unlink, which can fail on its own — `EIO`,
    # `EPERM`, a read-only remount. Unlinking first meant a caller could not
    # know the destination now existed, so `_undo` would not reverse it and the
    # bundle stayed half-apart. The link is the move; the unlink is tidying
    # after it, and `source_removed` is set only once that tidying succeeded.
    record = _Move(
        source,
        destination,
        published_inode=published,
        published_digest=digest,
        identity_proven=proven,
    )
    if journal is not None:
        journal.append(record)
    try:
        # The source and the destination are two names for ONE inode after
        # `os.link`. If they no longer are, something replaced the source in
        # between, and unlinking by name would delete that replacement while
        # keeping the original at the destination — a move reported as
        # successful after destroying an unrelated entry.
        if _inode(source) != published:
            raise RaceLostSource(source)
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
        _withdraw(destination, published)
        raise
    record.source_removed = True
    return (published, digest, proven)


class _Substituted(BackupError, OSError):
    """The pathname stopped referring to the entry that was open behind it."""

    def __init__(self, path: Path) -> None:
        super().__init__(
            f"{path} was replaced while it was being read; no identity can be"
            " established for it"
        )
        self.path = path


class RaceLostSource(BackupError, OSError):
    """The source name stopped referring to the entry that was just published.

    Both bases on purpose: callers of these primitives already handle `OSError`
    from a move, and the recovery is the same one — undo, or withdraw. What must
    NOT happen is unlinking the name anyway.
    """

    def __init__(self, source: Path) -> None:
        super().__init__(
            f"{source} was replaced while it was being moved; it has not been"
            " removed, and nothing further will be published from it"
        )
        self.source = source


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


def _identity_before_publication(
    source: Path,
) -> tuple[tuple[int, int] | None, bytes | None, bool]:
    """What this call is ABOUT to publish, read from the entry it already owns.

    Read before the move rather than after it, because after the move the only
    thing available is a pathname somebody else may hold. An identity that
    cannot be established is recorded as UNPROVEN rather than guessed at, and
    `_undo` never moves an unproven member into an authoritative namespace.
    """
    try:
        inode, digest = _content_identity(source)
    except OSError:
        return (None, None, False)
    return (inode, digest, True)


def _withdraw(destination: Path, published: tuple[int, int] | None) -> None:
    """Take a published name away, and make the ABSENCE durable too.

    Only while the name still holds what was published. A cleanup that unlinks
    by name after another operation has taken that name destroys their file
    while reporting this operation's error.

    Unlinking without syncing the directory leaves the same ambiguity the
    publication was careful to avoid: the entry may or may not survive a crash,
    so a caller that then removes a pause marker can leave a database present
    and unpaused. Sync, and say the state is ambiguous if even that fails.
    """
    if published is None:
        # Published, and not identifiable. Removing by name is exactly what
        # this function exists to stop, so the entry stays and the caller is
        # told the state is ambiguous — which for a restore means the worker
        # pause stays too.
        raise AmbiguousPublication(destination)
    if _inode(destination) != published:
        # Somebody else's now. Their file is not this failure's to clean up.
        return
    destination.unlink(missing_ok=True)
    try:
        _fsync_directory(destination.parent)
    except OSError as error:
        raise AmbiguousPublication(destination) from error


def _publish(
    temporary: Path, destination: Path
) -> tuple[tuple[int, int] | None, bytes | None, bool]:
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
    published, digest, proven = _claim(temporary, destination)
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
        # WITH the token `_claim` returned. Withdrawing by name meant that an
        # entry another operation had put at that path in the meantime was
        # deleted by this one's cleanup, while the error reported was the
        # directory sync that failed.
        _withdraw(destination, published)
        raise
    return (published, digest, proven)


def create_backup(
    database: ControlPlaneDatabase,
    target: Path | str,
    *,
    backup_key: bytes,
    identity: tuple[int, int] | None = None,
) -> Path:
    """Encrypt an image of the database into an authenticated archive.

    Both the image and the encryption go through disk in chunks: the same
    memory ceiling applies here as to the reuse check, and taking a snapshot is
    exactly when the volume is most likely to be under pressure.

    The layout is unchanged — MAGIC, nonce, ciphertext, 16-byte tag — so
    `restore_backup` reads archives written before and after this change
    identically.

    `identity` is the entry `require_control_plane_database` proved, and passing
    it closes the gap between proving and snapshotting. Validation opens and
    closes its own read-only connection, `database.connection` is not opened
    until `_materialise`, and the writer lock is advisory — so a rename of the
    database path in between meant this authenticated a file nobody had
    checked, reported a valid archive of the wrong generation, and the runbook
    then permitted deleting the real bundle. The entry is compared immediately
    before the image is taken and again immediately after.
    """
    destination = Path(target)
    # BEFORE the occupancy check, because the dangerous aliases are the ones
    # that are ABSENT. `<db>.workers-paused` is normally not there, so occupancy
    # said yes and the authenticated archive was published as the live pause
    # marker: the backup reported success and every worker afterwards treated
    # the database as an unreconciled restore and stopped doing durable work. A
    # nominal backup silently disabled production.
    _refuse_alias(destination, database.path, what="the database being archived")
    _require_identity(database.path, identity)
    if _present(destination):
        # `lexists`, because a DANGLING symlink is reported absent by
        # `Path.exists()` — and `_publish`'s `os.replace` would then silently
        # replace that entry, destroying a link the operation does not own and
        # publishing the archive under a name that meant something else.
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_archive: Path | None = None
    image: Path | None = None
    staging: Path | None = None
    try:
        # NOT beside the database. The image and the encrypted archive are each
        # about the size of the database, and putting both on the 1 GiB data
        # volume alongside the live file means a database near a third of the
        # volume exhausts it while trying to take a backup — every deployment
        # with a pending migration would then fail closed and never serve. The
        # RAM problem this replaced must not simply become a disk problem.
        # `SCRATCH_DIR` lets an operator point this at whatever the machine has.
        scratch_root = os.environ.get(SCRATCH_DIR_ENV) or tempfile.gettempdir()
        # A PRIVATE directory, not a shared one. `_materialise` hands SQLite a
        # PATH — it cannot be given a descriptor — so there is an unavoidable
        # moment between the image being written and this process opening it.
        # What can be removed is anyone else's ability to use that moment:
        # `mkdtemp` creates the directory 0700 with a name nobody can predict,
        # so the only writer to the staging path is this process. Without it, a
        # replacement dropped at the image's name was encrypted into the
        # archive while every source-identity check still passed, and a
        # different, perfectly valid control-plane generation silently became
        # the restore point.
        staging = Path(tempfile.mkdtemp(prefix=".abrolia-image.", dir=scratch_root))
        image = staging / f"{destination.name}.image"
        _materialise(database, image)
        # And again AFTER. `_materialise` is where `database.connection` first
        # opens, by path, so the check before it proves nothing about the file
        # SQLite actually read.
        _require_identity(database.path, identity)

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
            # The staged image is UNLINKED and read from the descriptor that
            # created it — no second open by pathname, because the scratch
            # directory is not this process's alone and a replacement dropped
            # at that name between `_materialise` and here would be encrypted
            # into the archive while every source-identity check still passed.
            # A different, perfectly valid control-plane generation would
            # become the restore point, and the runbook then licenses deleting
            # the current database against it.
            #
            # Unlinking first also makes the name unusable by anyone else and
            # removes the cleanup the `finally` would otherwise have to do.
            with _open_pinned(image) as source:
                image.unlink(missing_ok=True)
                image = None
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
        if staging is not None:
            with contextlib.suppress(OSError):
                staging.rmdir()
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
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        # ONE descriptor, and the size read from IT. Stat-by-name then
        # open-by-name is two questions about two possibly different files:
        # swapping the archive in between for another of the same size, valid
        # under the same key, restored a different generation — authenticated,
        # and not the one the operator asked for.
        with _open_pinned(source) as archive_file:
            size = os.fstat(archive_file.fileno()).st_size
            if size <= prologue + TAG_BYTES:
                raise BackupError("unsupported or truncated control-plane backup")
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
        # Sound is not the same as OURS. The checks above pass on any healthy
        # SQLite file, so an archive taken of an unrelated database restored
        # cleanly and — with migrations applied — had the control-plane schema
        # built around a stranger's tables. Asked here, before the pause marker
        # or the database has been published.
        try:
            require_control_plane_image(temporary)
        except RollbackError as error:
            raise BackupError(
                f"the archive does not contain a control-plane database: {error}"
            ) from error
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
            owned_marker, _digest, _proven = _publish(
                marker, _pause_marker(destination)
            )
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
    origin = _inode(source)
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
        # The token comes FROM the publication. Reading it back off the
        # destination afterwards described whoever held that name by then: an
        # actor replacing it in that interval had their identity journalled, the
        # final validation failed, and `_undo` was then authorised to move their
        # file back over the validated candidate — losing the only retryable
        # rollback generation without reporting an incomplete reversal.
        published = _publish(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    # Recorded the instant the copy is published, and separately once the
    # source is gone — a copy whose source removal fails leaves both names
    # holding the generation, which `_undo` has to be able to tell from a move
    # that never started. See `_Move`.
    # The bytes this call COPIED, taken from the temporary it owns rather than
    # from the destination it has just let go of.
    landed_inode, landed_digest, landed_proven = published
    record = _Move(
        source,
        destination,
        published_inode=landed_inode,
        published_digest=landed_digest,
        identity_proven=landed_proven,
    )
    if journal is not None:
        journal.append(record)
    try:
        # A copy has two different inodes by construction, so the ownership
        # question is asked of the SOURCE: is this still the entry whose bytes
        # were just copied? If not, removing it destroys somebody else's file.
        if _inode(source) != origin:
            raise RaceLostSource(source)
        source.unlink(missing_ok=True)
    except OSError:
        # Somebody has to reverse this. A journalled caller does it in `_undo`,
        # which can also put back the members that moved before this one — so
        # the exception goes there untouched. NOBODY is watching an unjournaled
        # call — `_undo`'s own cross-filesystem reversal is one — so the copy
        # it just published has to go away here or stand forever.
        if journal is not None:
            raise
        _withdraw(destination, landed_inode)
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
) -> tuple[str, dict[Path, tuple[tuple[int, int] | None, bytes | None]]]:
    """Everything that can be known before the live database is touched.

    The first version checked `restored.is_file()` and `target.exists()` and
    then started renaming, so a bundle that could never be installed — a
    truncated file, a directory, a restore with no worker pause — was discovered
    only after the live database had been moved aside and, in the pause-marker
    case, after the invalid candidate was already at the canonical path. An
    operator mid-rollback then had neither a working database nor an obvious way
    back. The checks that remain after the install stay as defence in depth.

    Returns the reserved superseded-bundle name, because reserving it is part of
    deciding whether the install can proceed at all — and the ENTRY behind each
    candidate pathname, so the install can require that what it moves is what
    was validated here rather than whatever holds the name by then.
    """
    # Re-asked under the locks. The check before them runs unlocked, so a
    # symlink retargeted in between could alias the two namespaces after the
    # question had already been answered.
    _refuse_overlap(restored, target)
    if not _regular_file(restored):
        raise RollbackError(f"{restored} is not a regular file")
    # `lexists`, like every other member check. `exists()` follows symlinks, so
    # a DANGLING one at `--target` answered "nothing here", took the
    # already-freed branch, and the install then moved that unowned link under
    # a superseded name and published over a path that was never free.
    if not _present(target):
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
            if _present(member):
                raise RollbackError(
                    f"{member} is still present, so the target was not freed;"
                    " remove --target-already-freed"
                )
    elif not _regular_file(target):
        raise RollbackError(f"{target} is not a regular file")
    marker = _pause_marker(restored)
    if not _present(marker):
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
    # `restore --no-migrate` hands its output straight to this command, so an
    # archive of an unrelated database reaches the canonical path through a
    # check that only ever asked whether the file opened.
    require_control_plane_image(restored)

    # Read AFTER every validation above, so this is what passed — and BY
    # CONTENT, not only by inode. A process holding an open descriptor can
    # truncate or rewrite a member in place without the inode changing, so an
    # inode-only comparison passed and the modified file was installed as
    # though it were the validated one.
    try:
        candidate = {member: _content_identity(member) for member in _bundle(restored)}
    except OSError as error:
        raise RollbackError(
            f"a member of {restored} is present and could not be read, so the"
            f" candidate cannot be validated: {error}"
        ) from error

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
    return aside, candidate


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


def _table_names(objects: dict[str, str]) -> set[str]:
    return {
        name.split(":", 1)[1] for name in objects if name.startswith("table:")
    }


def _reference_schema(
    migrations: tuple[str, ...],
) -> tuple[dict[str, str], dict[str, set[str]]]:
    """The schema those migration scripts produce, by RUNNING them.

    Table names alone are not identity: a file with the exact ledger and
    `accounts(x)` beside equally hollow look-alikes for the rest passes
    integrity and foreign-key checks — it passes them BECAUSE it is empty — and
    was archived as the control-plane database. A mistyped path or a
    schema-damaged file could then authorise deleting the real one.

    Executed into `:memory:` rather than described in a table here, so the
    columns, keys, constraints and indexes come from the same source of truth
    the migrator uses and cannot drift from it, and so every historical revision
    is answered without a catalogue of revisions to maintain.
    """
    reference = sqlite3.connect(":memory:")
    try:
        for migration in migrations:
            reference.executescript(
                (MIGRATIONS_DIR / migration).read_text(encoding="utf-8")
            )
        objects = _schema_objects(reference)
        return objects, _table_columns(reference, _table_names(objects))
    finally:
        reference.close()


def _table_columns(connection: sqlite3.Connection, tables: set[str]) -> dict[str, set[str]]:
    """Each table's column names, for the comparison a NEWER database needs."""
    return {
        table: {
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        for table in tables
    }


def _schema_objects(connection: sqlite3.Connection) -> dict[str, str]:
    """Every declared object and its definition, whitespace-normalised.

    `schema_migrations` is excluded: `migrate()` creates it itself rather than
    through a script, so it is not part of what the scripts describe. SQLite's
    own `sqlite_%` objects are excluded for the same reason.
    """
    return {
        f"{row[0]}:{row[1]}": " ".join(str(row[2]).split())
        for row in connection.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL"
            " AND name NOT LIKE 'sqlite_%' AND name != 'schema_migrations'"
        )
    }


def _shipped_migrations() -> tuple[str, ...]:
    """Every migration in this image, in the order `db.migrate` applies them.

    Read from the same directory `ControlPlaneDatabase.migrate` reads, so the
    identity check and the migrator can never disagree about what this schema
    is called.
    """
    return tuple(sorted(entry.name for entry in MIGRATIONS_DIR.glob("*.sql")))


def require_control_plane_database(path: Path) -> tuple[int, int]:
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

    RETURNS the entry it proved, so the caller can require that the same one is
    still there when the snapshot is taken. This function opens and closes its
    own read-only connection and the writer lock is advisory, so without that
    the proof and the archive could be about two different files.
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
            declared = _schema_objects(connection)
            declared_columns = _table_columns(connection, _table_names(declared))
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
    # The ledger NAMES a schema; this is the schema itself, compared object by
    # object against what those exact migration scripts produce. Checking that
    # the table NAMES exist was not enough: a file with the right ledger and
    # `accounts(x)` beside equally hollow look-alikes passed, and passed the
    # integrity and foreign-key checks precisely because it was empty.
    #
    # Only what the recorded prefix should have created is required. Objects
    # BEYOND it are ignored, so a database from a newer image than this one is
    # still recognised — the same asymmetry the ledger comparison makes, and for
    # the same reason.
    reference, reference_columns = _reference_schema(tuple(applied[:shared]))
    # A database whose ledger runs PAST this image is compared more loosely,
    # and it has to be. `0009` adding a column to `accounts` changes that
    # table's stored definition, so requiring byte-equality would reject the
    # newer database outright — during a rollback FROM that release, which is
    # the one situation this command exists for. The operator would be unable
    # to archive post-migration writes before freeing the volume: the recovery
    # procedure stopping at its own data-preservation gate.
    #
    # What still has to hold is that every object the recorded prefix declares
    # is present, and that each of its tables still has at least the columns
    # that prefix gave it. Migrations add; they have never removed. A hollow
    # look-alike fails this exactly as it fails the strict comparison.
    newer = [name for name in applied if name not in set(shipped)]
    if newer:
        wrong = sorted(
            [name for name in reference if name not in declared]
            + [
                f"table:{table}"
                for table, expected in reference_columns.items()
                if not expected <= declared_columns.get(table, set())
            ]
        )
        why = (
            f"records migrations this image does not ship ({newer[0]}) and its"
            " schema is missing what the ones it does ship declare"
        )
    else:
        wrong = sorted(
            name
            for name, definition in reference.items()
            if declared.get(name) != definition
        )
        why = "records migrations whose schema is not the one they declare"
    if wrong:
        # TABLES first among however few are shown. Sorted plainly, `index:`
        # sorts before `table:` and an operator looking at a file whose every
        # table is wrong was told about five indexes.
        wrong.sort(key=lambda name: (not name.startswith("table:"), name))
        raise BackupError(
            f"{path} {why} ({', '.join(wrong[:5])}); its ledger describes a"
            " database this file is not, so archiving it would authorise"
            " deleting the real one"
        )
    entry = _inode(path)
    if entry is None:
        raise BackupError(f"{path} vanished while it was being validated")
    return entry


def require_control_plane_image(path: Path) -> None:
    """The same identity question, asked of a candidate rather than a source.

    `create_backup` will encrypt whatever `ControlPlaneDatabase` it is handed,
    and a direct caller can hand it an unrelated file. Integrity and
    foreign-key checks then pass on the way back out — an unrelated database
    passes them easily — so `restore` could publish it, `restore --no-migrate`
    could hand it to `install-rollback`, and the canonical path would end up
    holding somebody else's schema, or nothing. Authentication proves the
    archive was written by this key; it says nothing about what was in it.

    A PRISTINE database is accepted, and has to be: `migrate --backup-first`
    snapshots before the FIRST migration, so the archive that makes a fresh
    boot recoverable contains an empty file with no ledger and no tables. That
    is a control-plane database at revision zero. A file with no ledger and
    tables in it is somebody else's.
    """
    with _read_only_sqlite(path) as connection:
        objects = _schema_objects(connection)
        ledger = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table'"
            " AND name = 'schema_migrations'"
        ).fetchone()
    if ledger is None:
        if objects:
            raise RollbackError(
                f"{path} has no migration ledger but is not empty either, so"
                " it is not a control-plane database at any revision"
            )
        return
    try:
        require_control_plane_database(path)
    except BackupError as error:
        raise RollbackError(str(error)) from error


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
        aside, candidate = _preflight(
            restored_path,
            target_path,
            stamp,
            target_already_freed=target_already_freed,
        )
        return _install_rollback_locked(
            restored_path, target_path, aside=aside, candidate=candidate
        )
    finally:
        for guard in reversed(guards):
            guard.release_process_lock()
            guard.close()


class IncompleteReversal(RollbackError):
    """A failed install could not be fully undone, and the volume is mixed.

    Raised INSTEAD of the original failure, because it describes a worse state
    than the one that caused it: the canonical path holds some subset of two
    generations, and the writer locks are about to be released over it. The
    constructor installs a fail-safe pause first — whatever database is sitting
    there, it must not start — and carries the outstanding moves so recovery is
    by hand rather than by guess.
    """

    def __init__(self, target: Path, unreversed: list[_Move]) -> None:
        outstanding = "; ".join(
            f"{move.destination} should be {move.source}" for move in unreversed
        )
        paused = _install_failsafe_pause(target)
        super().__init__(
            f"the rollback install failed and could not be fully reversed."
            f" {target} is neither the original bundle nor a complete"
            f" replacement. Outstanding: {outstanding}."
            + (
                f" A worker pause has been written to {_pause_marker(target)};"
                " leave it there until the bundle is repaired by hand."
                if paused
                else f" A worker pause could NOT be written to"
                f" {_pause_marker(target)}: do not start the service."
            )
        )
        self.target = target
        self.unreversed = unreversed
        self.paused = paused


def _install_failsafe_pause(target: Path) -> bool:
    """Pause whatever ends up at `target`, and say whether it worked.

    Best effort, because the filesystem that just failed may fail again — but
    the attempt has to be made before the locks are released. Written to the
    canonical name directly rather than through `_claim`: an existing marker is
    the outcome this wants, so refusing to replace one would defeat it.
    """
    marker = _pause_marker(target)
    try:
        # `O_EXCL | O_NOFOLLOW`, not `write_text`. Writing by name FOLLOWS an
        # alias: a symlink or hard link left at the marker path — and this
        # function runs precisely when the volume is in a state nobody planned
        # — was opened and TRUNCATED, so error handling for a recoverable move
        # failure could erase an unrelated file, or the surviving database
        # itself if the link pointed there.
        descriptor = os.open(
            marker,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
            0o600,
        )
    except FileExistsError:
        # Something already occupies the name. If it is an ordinary file the
        # pause is established — that entry is what `workers_paused` reads —
        # and this must not touch it. Anything else, and no safe pause exists.
        return _regular_file(marker)
    except OSError:
        return False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(
                b"an interrupted rollback install left this bundle mixed;"
                b" reconcile it by hand before starting anything\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(marker.parent)
    except OSError:
        return False
    return True


def _undo(moved: list[_Move]) -> list[_Move]:
    """Put every completed move back, newest first, and never mask the cause.

    RETURNS the moves it could not reverse, rather than printing them. Printing
    made an incomplete reversal indistinguishable from a complete one to the
    caller, which then released both writer locks over whatever subset had been
    restored — a candidate database left canonical and unpaused among the
    possibilities.

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
    unreversed: list[_Move] = []
    for move in reversed(moved):
        source, destination = move.source, move.destination
        try:
            if not _present(destination):
                if move.source_removed and not _present(source):
                    # Published, its source removed, and then the destination
                    # taken away by something else: BOTH names are empty and
                    # that member is gone. Treating an absent destination as
                    # "already reversed" meant `_undo` returned nothing
                    # outstanding, so `IncompleteReversal` and its fail-safe
                    # pause were skipped and the locks were released over a
                    # bundle missing a file.
                    unreversed.append(move)
                continue
            if not move.identity_proven or _content_identity(destination) != (
                move.published_inode,
                move.published_digest,
            ):
                # Not what this move put there. Either somebody else holds the
                # name — removing or moving it would destroy their file — or the
                # bytes were rewritten in place, which keeps the inode and which
                # a reversal must not carry into the canonical namespace. Both
                # are "this is not mine to move"; both are reported.
                unreversed.append(move)
                continue
            if not move.source_removed:
                destination.unlink()
                # Durable, like every other move this module makes. An unsynced
                # removal leaves the same ambiguity the forward publication was
                # careful to avoid.
                _fsync_directory(destination.parent)
            elif _present(source):
                unreversed.append(move)
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
        except (OSError, BackupError):
            # `BackupError` too, and specifically `AmbiguousPublication`, which
            # a cross-filesystem `_copy_install` raises and which is NOT an
            # `OSError`. It escaped this handler and abandoned every reversal
            # still queued behind it, so one member that could not be made
            # durable stopped the ones that could.
            unreversed.append(move)
            continue
    return unreversed


#: An identity no real member can have. A present member that will not open
#: must never compare equal to anything — including another unreadable member —
#: so the failure gets a value of its own rather than a plausible-looking one.
_UNREADABLE: tuple[None, str] = (None, "unreadable")


def _identity_or_unreadable(path: Path):
    try:
        return _content_identity(path)
    except OSError:
        return _UNREADABLE


def _install_rollback_locked(
    restored_path: Path,
    target_path: Path,
    *,
    aside: str,
    candidate: dict[Path, tuple[tuple[int, int] | None, bytes | None]],
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
    # ONE failure boundary, from the first superseded move to the last
    # invariant check. The post-move assertions used to sit between two `try`
    # blocks rather than inside either: a canonical entry appearing in that
    # interval raised with the live database still under its aside name — an
    # outage with no reversal attempted — and the pause-marker assertion had the
    # same shape after publication, leaving an installed database runnable with
    # no marker beside it. If it can fail after the first member moves, it
    # belongs in here.
    moved: list[_Move] = []
    # What the superseded bundle IS before anything moves, so the final check
    # can say whether the recovery copy this command promises to keep is
    # actually there and unmodified.
    # Hard refusal, not `_identity_or_unreadable`: an unreadable member
    # recorded as UNREADABLE would compare equal to itself at the end, so two
    # failures would agree that the recovery copy is intact. The live bundle is
    # readable or this command does not touch it.
    try:
        superseded_before = {
            member: _content_identity(member) for member in superseded
        }
    except OSError as error:
        raise RollbackError(
            f"a member of {target_path} is present and could not be read, so"
            f" the superseded bundle cannot be accounted for: {error}"
        ) from error
    aside_names = tuple(
        member.with_name(member.name.replace(target_path.name, aside, 1))
        for member in superseded
    )
    try:
        # These renames stay on the volume and cost no blocks.
        for path, landed in zip(superseded, aside_names, strict=True):
            if _present(path):
                # Claimed, not renamed: `_reserve_aside` checks the whole bundle
                # is free and another process can still take one of those names
                # before this loop reaches it. The reservation narrows the
                # window; only the claim closes it. The journal is passed IN so
                # the move is recorded the instant the link succeeds, before the
                # unlink that can fail after it.
                _claim(path, landed, moved)
        remaining = [str(path) for path in superseded if _present(path)]
        if remaining:
            raise RollbackError(
                f"superseded files remain at the canonical path:"
                f" {', '.join(remaining)}"
            )
        # `None` in the already-freed mode: there was nothing to move aside,
        # because the operator archived and deleted it to make room. Naming a
        # path that holds nothing would read as a recovery copy that does not
        # exist.
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
        for source, destination in zip(ordered, destinations, strict=True):
            # The ENTRY that passed preflight, not whatever holds the name now.
            # Between validation and this loop an external actor can remove a
            # candidate member — the loop skipped it silently, so the database
            # could simply not be installed and the command still reported
            # success — or replace it, in which case an unvalidated file went to
            # the canonical path beside the validated generation's sidecars.
            validated = candidate.get(source, (None, None))
            if _identity_or_unreadable(source) != validated:
                raise RollbackError(
                    f"{source} is not what passed validation; the rollback"
                    " candidate changed under this command and nothing"
                    " further will be installed"
                )
            if validated[0] is None:
                continue
            if not _rename_or_exdev(source, destination, moved):
                _copy_install(source, destination, moved)

        # EVERY landed member, by content, plus the superseded bundle this
        # command promised to keep. Checking only that the canonical database
        # and marker NAMES exist let a landed WAL be removed — losing committed
        # frames — or any member be replaced after publication, and still
        # reported `workers: paused`. A missing aside member was likewise
        # invisible while the report claimed the superseded bundle was kept.
        for source, destination in zip(ordered, destinations, strict=True):
            expected = candidate.get(source, (None, None))
            if expected[0] is None:
                continue
            if _identity_or_unreadable(destination)[1] != expected[1]:
                raise RollbackError(
                    f"the rollback install is incomplete: {destination} is not"
                    " the validated content. Nothing at that path may be"
                    " started."
                )
        for member, landed in zip(superseded, aside_names, strict=True):
            if superseded_before[member][0] is None:
                continue
            if _identity_or_unreadable(landed)[1] != superseded_before[member][1]:
                raise RollbackError(
                    "the rollback install is incomplete: the superseded"
                    f" {landed} is not what was moved aside. Nothing at"
                    f" {target_path} may be started."
                )
    except BaseException as error:
        # Back to the original bundle, in reverse: the installed members return
        # to the candidate, and the superseded members return to the canonical
        # path. An operator retries against the state they started from.
        unreversed = _undo(moved)
        if unreversed:
            # The locks are about to be released over a canonical path that is
            # neither the original bundle nor a complete replacement. Whatever
            # database is there must not start: pause it, durably, and say
            # exactly which moves are outstanding so the recovery is by hand
            # rather than by guess.
            raise IncompleteReversal(target_path, unreversed) from error
        raise

    report["workers"] = "paused"
    report["sidecars"] = sorted(
        path.name for path in _sidecars(target_path) if path.exists()
    )
    return report

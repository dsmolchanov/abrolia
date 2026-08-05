"""Authenticated control-plane backup and isolated restore contracts.

A live Fly smoke is intentionally omitted. Even with a unique deterministic app
name and a ``finally`` deprovision call, Fly cleanup may end in the adapter's
``UNKNOWN`` state after a network failure. That cannot guarantee exact-resource
cleanup, so the opt-in test would violate the requested safety boundary. The
fully mocked Fly lifecycle remains covered in ``test_fly_provisioner.py``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from control_plane.backup import (
    MAGIC,
    NONCE_BYTES,
    BackupError,
    create_backup,
    restore_backup,
)
from control_plane.cli import _resume_deletions_if_running
from control_plane.config import ConfigurationError
from control_plane.repositories.accounts import AccountsRepository
from control_plane.repositories.jobs import JobsRepository

BACKUP_KEY = b"b" * 32
WRONG_KEY = b"w" * 32


def _pending_job(cp_stack, *, canary: str = "backup-job-payload-canary") -> str:
    workflow = cp_stack.onboarding.workflow_for_household(cp_stack.household.id)
    with cp_stack.database.write() as connection:
        job_id, created = cp_stack.jobs.create(
            connection,
            household_id=cp_stack.household.id,
            workflow_id=workflow.id,
            kind="email_identity",
            operation="ensure",
            intent_key="backup-pending-job",
            request={"opaque": canary},
            provider="fake-email",
        )
    assert created
    return job_id


def _write_authenticated_payload(
    path: Path, plaintext: bytes, *, key: bytes = BACKUP_KEY
) -> None:
    nonce = bytes(range(NONCE_BYTES))
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, MAGIC)
    path.write_bytes(MAGIC + nonce + ciphertext)


def test_authenticated_round_trip_preserves_rows_and_pauses_workers(
    cp_stack, tmp_path: Path
) -> None:
    raw_magic = "backup-magic-token-canary"
    cp_stack.auth.issue_token(
        raw_magic,
        purpose="login",
        account_id=cp_stack.account.id,
        expires_at=1_900_000_000,
    )
    job_id = _pending_job(cp_stack)
    archive = create_backup(
        cp_stack.database, tmp_path / "control-plane.cpb", backup_key=BACKUP_KEY
    )
    target = tmp_path / "restored-control-plane.db"

    restored = restore_backup(archive, target, backup_key=BACKUP_KEY)
    try:
        assert restored.query_one("PRAGMA integrity_check")[0] == "ok"
        assert restored.query("PRAGMA foreign_key_check") == []
        assert restored.pragma() == {
            "journal_mode": "wal",
            "synchronous": 2,
            "foreign_keys": 1,
        }
        assert restored.migrate() == []
        accounts = AccountsRepository(restored, cp_stack.cipher, cp_stack.lookup)
        jobs = JobsRepository(restored, cp_stack.cipher, cp_stack.lookup)
        restored_account = accounts.get(cp_stack.account.id)
        assert restored_account.recovery_email == cp_stack.account.recovery_email
        assert jobs.request(job_id) == {"opaque": "backup-job-payload-canary"}

        assert restored.workers_paused
        assert restored.worker_pause_path.read_text(encoding="utf-8").strip() == (
            "restore requires explicit reconciliation"
        )
        assert restored.worker_pause_path.stat().st_mode & 0o777 == 0o600
        before = jobs.get(job_id)
        assert jobs.lease("restored-worker", now=1_850_000_000) is None
        assert jobs.get(job_id).attempts == before.attempts == 0

        restored.resume_workers()
        assert not restored.workers_paused
        leased = jobs.lease("restored-worker", now=1_850_000_000)
        assert leased.id == job_id
        assert leased.attempts == 1
    finally:
        restored.close()

    assert not cp_stack.database.workers_paused
    assert target.stat().st_mode & 0o777 == 0o600


def test_restore_pause_blocks_sessionless_deletion_resume(
    cp_stack, tmp_path: Path
) -> None:
    with cp_stack.database.write() as connection:
        connection.execute(
            "UPDATE households SET status = 'deleting' WHERE id = ?",
            (cp_stack.household.id,),
        )
    archive = create_backup(
        cp_stack.database, tmp_path / "deletion-pending.cpb", backup_key=BACKUP_KEY
    )
    restored = restore_backup(
        archive, tmp_path / "deletion-pending.db", backup_key=BACKUP_KEY
    )
    calls: list[tuple[int, float | None]] = []
    deletion = SimpleNamespace(
        resume_pending=lambda *, limit, now=None: calls.append((limit, now)) or ["resumed"]
    )
    active = SimpleNamespace(database=restored, deletion=deletion)
    try:
        assert restored.workers_paused
        assert _resume_deletions_if_running(active, limit=10, now=1_850_000_000) == []
        assert calls == []

        restored.resume_workers()
        assert _resume_deletions_if_running(
            active, limit=10, now=1_850_000_001
        ) == ["resumed"]
        assert calls == [(10, 1_850_000_001)]
    finally:
        restored.close()


def test_archive_header_is_minimal_and_contains_no_key_or_secret_material(
    cp_stack, tmp_path: Path
) -> None:
    raw_magic = "archive-magic-token-canary"
    raw_provider = "archive-provider-payload-canary"
    cp_stack.auth.issue_token(
        raw_magic,
        purpose="login",
        account_id=cp_stack.account.id,
        expires_at=1_900_000_000,
    )
    _pending_job(cp_stack, canary=raw_provider)
    archive = create_backup(
        cp_stack.database, tmp_path / "opaque.cpb", backup_key=BACKUP_KEY
    )
    blob = archive.read_bytes()

    assert blob.startswith(MAGIC)
    assert len(blob) > len(MAGIC) + NONCE_BYTES + 16
    assert b"SQLite format 3" not in blob
    for canary in (
        raw_magic.encode(),
        raw_provider.encode(),
        cp_stack.account.recovery_email.encode(),
        cp_stack.session.token.encode(),
        cp_stack.session.csrf_token.encode(),
        BACKUP_KEY,
        cp_stack.config.encryption_keys[cp_stack.config.active_encryption_key_version],
        cp_stack.config.lookup_hmac_key,
        cp_stack.config.token_hmac_key,
    ):
        assert canary not in blob
    # The only unauthenticated-parser-visible metadata is a fixed magic/version
    # and a random AEAD nonce. There are no JSON names, identifiers, or key hints.
    visible_header = blob[: len(MAGIC) + NONCE_BYTES]
    assert visible_header[: len(MAGIC)] == MAGIC
    assert b"token" not in visible_header.lower()
    assert b"email" not in visible_header.lower()
    assert archive.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("mutation", ["wrong-key", "ciphertext", "nonce", "magic"])
def test_wrong_key_and_any_archive_tamper_fail_closed_without_partial_target(
    cp_stack, tmp_path: Path, mutation: str
) -> None:
    archive = create_backup(
        cp_stack.database, tmp_path / "source.cpb", backup_key=BACKUP_KEY
    )
    payload = bytearray(archive.read_bytes())
    key = BACKUP_KEY
    if mutation == "wrong-key":
        key = WRONG_KEY
    elif mutation == "ciphertext":
        payload[-1] ^= 1
    elif mutation == "nonce":
        payload[len(MAGIC)] ^= 1
    else:
        payload[0] ^= 1
    archive.write_bytes(payload)
    target = tmp_path / f"restored-{mutation}.db"

    with pytest.raises(BackupError):
        restore_backup(archive, target, backup_key=key)
    assert not target.exists()
    assert not target.with_name(f"{target.name}.workers-paused").exists()
    assert list(tmp_path.glob(f".{target.name}.*")) == []


def test_backup_and_restore_refuse_existing_targets_without_overwrite(
    cp_stack, tmp_path: Path
) -> None:
    archive = tmp_path / "occupied.cpb"
    archive.write_bytes(b"archive-sentinel")
    with pytest.raises(FileExistsError):
        create_backup(cp_stack.database, archive, backup_key=BACKUP_KEY)
    assert archive.read_bytes() == b"archive-sentinel"

    source = create_backup(
        cp_stack.database, tmp_path / "source.cpb", backup_key=BACKUP_KEY
    )
    target = tmp_path / "occupied.db"
    target.write_bytes(b"database-sentinel")
    with pytest.raises(FileExistsError):
        restore_backup(source, target, backup_key=BACKUP_KEY)
    assert target.read_bytes() == b"database-sentinel"


def test_authenticated_corrupt_sqlite_and_foreign_key_violation_are_rejected(
    tmp_path: Path,
) -> None:
    corrupt = tmp_path / "corrupt.cpb"
    _write_authenticated_payload(corrupt, b"not a SQLite database")
    corrupt_target = tmp_path / "corrupt.db"
    with pytest.raises(BackupError, match="integrity|valid SQLite"):
        restore_backup(corrupt, corrupt_target, backup_key=BACKUP_KEY)
    assert not corrupt_target.exists()

    invalid_db = tmp_path / "foreign-key-source.db"
    with sqlite3.connect(invalid_db) as connection:
        connection.executescript(
            "CREATE TABLE parent (id INTEGER PRIMARY KEY);"
            "CREATE TABLE child (parent_id INTEGER REFERENCES parent(id));"
            "INSERT INTO child (parent_id) VALUES (42);"
        )
    foreign_key_archive = tmp_path / "foreign-key.cpb"
    _write_authenticated_payload(foreign_key_archive, invalid_db.read_bytes())
    foreign_key_target = tmp_path / "foreign-key.db"
    with pytest.raises(BackupError, match="foreign-key"):
        restore_backup(
            foreign_key_archive, foreign_key_target, backup_key=BACKUP_KEY
        )
    assert not foreign_key_target.exists()


def test_backup_key_is_required_and_distinct_from_application_keys(
    cp_stack, tmp_path: Path
) -> None:
    with pytest.raises(BackupError, match="separate 32-byte"):
        create_backup(cp_stack.database, tmp_path / "missing-key.cpb", backup_key=b"")

    application_keys = (
        cp_stack.config.encryption_keys[cp_stack.config.active_encryption_key_version],
        cp_stack.config.lookup_hmac_key,
        cp_stack.config.token_hmac_key,
    )
    for application_key in application_keys:
        duplicated = replace(cp_stack.config, backup_key=application_key)
        with pytest.raises(ConfigurationError, match="distinct|independent"):
            duplicated.validate()


def test_two_backups_use_fresh_nonce_and_distinct_ciphertext(
    cp_stack, tmp_path: Path
) -> None:
    first = create_backup(
        cp_stack.database, tmp_path / "first.cpb", backup_key=BACKUP_KEY
    ).read_bytes()
    second = create_backup(
        cp_stack.database, tmp_path / "second.cpb", backup_key=BACKUP_KEY
    ).read_bytes()
    first_nonce = first[len(MAGIC) : len(MAGIC) + NONCE_BYTES]
    second_nonce = second[len(MAGIC) : len(MAGIC) + NONCE_BYTES]
    assert first_nonce != second_nonce
    assert first != second

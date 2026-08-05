"""Бэкап и восстановление: снимок, шифрование, smoke-набор после восстановления.

Главный тест здесь — не «файл создался», а «из него поднимается рабочая база».
Бэкап, который никто не восстанавливал, — это надежда, а не бэкап.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cloud.core.approvals import ApprovalStore
from hermes_cloud.core.backup import (
    BACKUP_SUFFIX,
    DAY,
    KEEP_DAYS,
    MAGIC,
    BackupError,
    integrity_problem,
    list_backups,
    make_backup,
    prune_backups,
    read_backup,
    restore_backup,
)
from hermes_cloud.core.commitments import CommitmentStore
from hermes_cloud.core.db import open_database
from hermes_cloud.core.memory import MemoryStore
from hermes_cloud.execute.reminder import ReminderStore

KEY = "ключ-этого-household-а"
OTHER_KEY = "ключ-другой-семьи"
CHAT = "-100990000101"
ACTOR = "990000001"
NOW = 1_800_000_000.0
OCCUPIED = "чужие данные".encode()


@pytest.fixture()
def live(tmp_path: Path):
    """База с данными, которые должны пережить восстановление."""
    database = open_database(tmp_path / "hermes.db")
    approvals = ApprovalStore(database)
    reminders = ReminderStore(database)
    commitments = CommitmentStore(database)
    memory = MemoryStore(database)

    staged = approvals.stage(
        kind="reminder", payload={"kind": "reminder", "text": "взнос"},
        chat=CHAT, actor=ACTOR,
    )
    approval = approvals.claim_by_id(
        approval_id=staged.id, chat=CHAT, thread=None, actor=ACTOR
    )
    reminders.create(chat=CHAT, text="оплатить взнос 15 EUR", due_at=NOW + DAY,
                     approval_id=approval.id)
    commitment = commitments.propose(kind="payment", payload={"amount_cents": 1500})
    commitments.confirm(commitment.id, approval)
    statement = memory.propose(text="Лиза не ест орехи")
    memory.confirm(statement.id, approval)
    return database


def test_backup_is_encrypted_and_not_a_bare_database(live, tmp_path: Path) -> None:
    """В хранилище уезжает шифртекст, а не читаемая переписка семьи."""
    archive = make_backup(live, tmp_path / "backups", key=KEY, now=NOW)

    blob = archive.path.read_bytes()

    assert blob.startswith(MAGIC)
    assert b"SQLite format 3" not in blob, "снимок не лежит открытым"
    assert "оплатить взнос".encode() not in blob


def test_restore_brings_back_a_working_household(live, tmp_path: Path) -> None:
    archive = make_backup(live, tmp_path / "backups", key=KEY, now=NOW)

    restored_path = restore_backup(archive.path, tmp_path / "restored.db", key=KEY)

    # Smoke-набор: база открывается, миграции на месте, данные читаются.
    restored = open_database(restored_path)
    assert integrity_problem(restored_path) is None
    assert [item.text for item in ReminderStore(restored).pending()] == [
        "оплатить взнос 15 EUR"
    ]
    assert [item.payload["amount_cents"] for item in CommitmentStore(restored).confirmed()] == [
        1500
    ]
    assert [item.text for item in MemoryStore(restored).recall()] == ["Лиза не ест орехи"]
    # И база живая: в неё можно писать дальше.
    ReminderStore(restored).create(chat=CHAT, text="после восстановления", due_at=NOW)
    assert len(ReminderStore(restored).pending()) == 2


def test_another_households_key_does_not_open_the_archive(live, tmp_path: Path) -> None:
    """Компрометация хранилища не означает компрометацию всех семей сразу."""
    archive = make_backup(live, tmp_path / "backups", key=KEY, now=NOW)

    with pytest.raises(BackupError):
        read_backup(archive.path, key=OTHER_KEY)


def test_a_tampered_archive_refuses_to_restore(live, tmp_path: Path) -> None:
    """Испорченный архив обязан не открыться, а не восстановиться «почти правильно»."""
    archive = make_backup(live, tmp_path / "backups", key=KEY, now=NOW)
    blob = bytearray(archive.path.read_bytes())
    blob[-1] ^= 0x01
    archive.path.write_bytes(bytes(blob))

    with pytest.raises(BackupError):
        restore_backup(archive.path, tmp_path / "restored.db", key=KEY)

    assert not (tmp_path / "restored.db").exists(), "полурезультата не остаётся"


def test_restore_does_not_overwrite_silently(live, tmp_path: Path) -> None:
    archive = make_backup(live, tmp_path / "backups", key=KEY, now=NOW)
    occupied = tmp_path / "restored.db"
    occupied.write_bytes(OCCUPIED)

    with pytest.raises(BackupError):
        restore_backup(archive.path, occupied, key=KEY)
    assert occupied.read_bytes() == OCCUPIED

    restore_backup(archive.path, occupied, key=KEY, overwrite=True)
    assert occupied.read_bytes() != OCCUPIED


def test_missing_key_is_an_explicit_error(live, tmp_path: Path) -> None:
    with pytest.raises(BackupError):
        make_backup(live, tmp_path / "backups", key="", now=NOW)


def test_pruning_keeps_the_window_and_never_the_empty_set(live, tmp_path: Path) -> None:
    directory = tmp_path / "backups"
    old = make_backup(live, directory, key=KEY, now=NOW)
    fresh = make_backup(live, directory, key=KEY, now=NOW + 60)
    import os

    # Возраст архива берётся из mtime файла, поэтому в тесте он задаётся явно:
    # иначе «старый» и «свежий» отличались бы миллисекундами создания.
    os.utime(old.path, (NOW - (KEEP_DAYS + 1) * DAY, NOW - (KEEP_DAYS + 1) * DAY))
    os.utime(fresh.path, (NOW, NOW))

    removed = prune_backups(directory, now=NOW)

    assert removed == [old.path]
    assert [item.path for item in list_backups(directory)] == [fresh.path]

    # Даже если единственный архив просрочен, он остаётся: без копии хуже.
    os.utime(fresh.path, (NOW - (KEEP_DAYS + 10) * DAY, NOW - (KEEP_DAYS + 10) * DAY))
    assert prune_backups(directory, now=NOW) == []
    assert len(list_backups(directory)) == 1


def test_backups_are_named_by_time_and_do_not_collide(live, tmp_path: Path) -> None:
    directory = tmp_path / "backups"
    first = make_backup(live, directory, key=KEY, now=NOW)
    second = make_backup(live, directory, key=KEY, now=NOW + 3600)

    assert first.path != second.path
    assert first.path.name.endswith(BACKUP_SUFFIX)
    assert list(directory.glob("*.partial")) == [], "временных файлов не остаётся"


def test_a_non_archive_is_rejected_before_decryption(tmp_path: Path) -> None:
    junk = tmp_path / "not-a-backup.db.enc"
    junk.write_bytes(b"just a file, not an archive")

    with pytest.raises(BackupError):
        read_backup(junk, key=KEY)

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from control_plane.db import MIGRATIONS_DIR, ControlPlaneDatabase
from hermes_cloud.core.db import Database as RuntimeDatabase


def _tables(database: ControlPlaneDatabase | RuntimeDatabase) -> set[str]:
    return {
        row["name"]
        for row in database.query(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def test_migrations_are_ordered_and_idempotent(tmp_path: Path) -> None:
    database = ControlPlaneDatabase(tmp_path / "control-plane.db")
    try:
        applied = database.migrate()
        assert applied == sorted(applied)
        assert applied == [
            "0001_control_plane.sql",
            "0002_email_identity.sql",
            "0003_email_domain_claims.sql",
            "0004_google_oauth.sql",
            "0005_email_secret_installs.sql",
            "0006_channel_preferences.sql",
            "0007_channel_bindings.sql",
            "0008_runtime_health_ownership.sql",
            "0009_channel_binding_challenges.sql",
            "0010_channel_binding_chat_id.sql",
        ]
        assert database.migrate() == []
        assert database.pragma() == {
            "journal_mode": "wal",
            "synchronous": 2,
            "foreign_keys": 1,
        }
    finally:
        database.close()


def test_the_chat_id_backfill_preserves_what_the_projection_emitted(
    tmp_path: Path,
) -> None:
    """C3a acceptance 4: 0010 is behaviour-preserving for rows that exist.

    Before the split, `provisioning/planner.py` projected the manifest's
    `chat_id` FROM `external_id`, because onboarding's `primary_channel` step
    captured the chat and wrote it into the only column there was. After 0010
    the projection reads `chat_id`, so the backfill has to make those two the
    same value for every row already stored — otherwise a household that added
    nobody would get a manifest with a different `config_sha256`, and a changed
    hash is how this system says something changed.

    It also leaves `external_id` ALONE. That is deliberate and it is the honest
    half: for Telegram the column still holds a chat where the gateway means a
    sender. The gap is not introduced here — reading one column two ways has
    meant exactly this since 0007 — but after the migration it is nameable, and
    correcting it needs a user ID onboarding never captured. See M1 of
    `thoughts/shared/plans/2026-08-27-c3a-sender-identity-and-chat.md`.
    """
    staged = tmp_path / "migrations"
    staged.mkdir()
    for script in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if script.name.startswith("0010"):
            break
        (staged / script.name).write_text(script.read_text(encoding="utf-8"))

    database = ControlPlaneDatabase(tmp_path / "control-plane.db")
    try:
        database.migrate(staged)
        assert "chat_id" not in {
            row["name"]
            for row in database.query("PRAGMA table_info(channel_bindings)")
        }
        with database.write() as connection:
            connection.execute(
                "INSERT INTO households (id, slug, status, created_at, updated_at)"
                " VALUES ('h1', 'hh1', 'active', 1, 1)"
            )
            # Written the way every row before C3a was: one column, holding the
            # CHAT, for a Telegram household and a WhatsApp one.
            for row_id, channel, value in (
                ("b1", "telegram", "-100990000101"),
                ("b2", "whatsapp", "+999511234567"),
            ):
                connection.execute(
                    "INSERT INTO channel_bindings (id, household_id, channel,"
                    " external_id, actor_id, role, verified_at,"
                    " verified_by_actor_id) VALUES (?, 'h1', ?, ?, 'a1',"
                    " 'owner', 1, 'a1')",
                    (row_id, channel, value),
                )
            connection.execute(
                "INSERT INTO channel_binding_challenges (id, household_id,"
                " channel, external_id, actor_id, role, code_hash,"
                " issued_by_actor_id, expires_at, attempts, consumed_at,"
                " created_at) VALUES ('c1', 'h1', 'whatsapp', '+999511234500',"
                " 'a2', 'adult', 'digest', 'a1', 9e9, 0, NULL, 1)"
            )
        projected_before = [
            row["external_id"]
            for row in database.query(
                "SELECT external_id FROM channel_bindings ORDER BY id"
            )
        ]

        assert database.migrate() == ["0010_channel_binding_chat_id.sql"]

        rows = database.query(
            "SELECT external_id, chat_id FROM channel_bindings ORDER BY id"
        )
        # What the manifest now reads equals what it used to read.
        assert [row["chat_id"] for row in rows] == projected_before
        # And the sender column is untouched, gap and all.
        assert [row["external_id"] for row in rows] == projected_before
        # An outstanding invitation survives the migration answerable: dropping
        # its chat would make it redeem into a binding that speaks nowhere.
        assert database.query_one(
            "SELECT chat_id FROM channel_binding_challenges WHERE id = 'c1'"
        )[0] == "+999511234500"
    finally:
        database.close()


def test_failed_migration_rolls_back_every_statement(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_broken.sql").write_text(
        "CREATE TABLE should_rollback (id TEXT);\nCREATE TABLE broken (id TEXT,);\n",
        encoding="utf-8",
    )
    database = ControlPlaneDatabase(tmp_path / "control-plane.db")
    try:
        with pytest.raises(sqlite3.OperationalError):
            database.migrate(migrations)
        assert "should_rollback" not in _tables(database)
        # Not merely empty — ABSENT. The ledger is created inside the batch
        # transaction, so a failed migration on a fresh database leaves it
        # exactly as it was. Creating it beforehand autocommitted, which made
        # the file differ from a snapshot taken moments earlier and caused the
        # next boot to reject that snapshot and write another.
        assert "schema_migrations" not in _tables(database)
    finally:
        database.close()


def test_runtime_and_control_plane_schemas_never_mix(tmp_path: Path) -> None:
    control_plane = ControlPlaneDatabase(tmp_path / "control-plane.db")
    runtime = RuntimeDatabase(tmp_path / "runtime.db")
    try:
        control_plane.migrate()
        runtime.migrate()
        control_tables = _tables(control_plane)
        runtime_tables = _tables(runtime)
        assert {"accounts", "sessions", "provisioning_jobs"} <= control_tables
        assert {"events", "jobs", "effects"}.isdisjoint(control_tables)
        assert "events" in runtime_tables
        assert {"accounts", "sessions", "provisioning_jobs"}.isdisjoint(runtime_tables)
    finally:
        control_plane.close()
        runtime.close()


def test_begin_immediate_serializes_independent_writers(tmp_path: Path) -> None:
    path = tmp_path / "control-plane.db"
    first = ControlPlaneDatabase(path, timeout=2)
    second = ControlPlaneDatabase(path, timeout=2)
    first.migrate()
    # Open the second connection before the first writer owns the transaction.
    _ = second.connection
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    errors: list[BaseException] = []

    def writer_one() -> None:
        try:
            with first.write() as connection:
                connection.execute(
                    "INSERT INTO rate_limit_buckets"
                    " (bucket_hmac, kind, window_started_at, attempts, updated_at)"
                    " VALUES ('first', 'test', 1, 1, 1)"
                )
                first_entered.set()
                assert release_first.wait(2)
        except BaseException as error:  # pragma: no cover - assertion reports below
            errors.append(error)

    def writer_two() -> None:
        try:
            assert first_entered.wait(2)
            with second.write() as connection:
                second_entered.set()
                connection.execute(
                    "INSERT INTO rate_limit_buckets"
                    " (bucket_hmac, kind, window_started_at, attempts, updated_at)"
                    " VALUES ('second', 'test', 1, 1, 1)"
                )
        except BaseException as error:  # pragma: no cover - assertion reports below
            errors.append(error)

    one = threading.Thread(target=writer_one)
    two = threading.Thread(target=writer_two)
    try:
        one.start()
        two.start()
        assert first_entered.wait(2)
        assert not second_entered.wait(0.05), "second writer entered before the first commit"
        release_first.set()
        one.join(2)
        two.join(2)
        assert not one.is_alive() and not two.is_alive()
        assert errors == []
        assert second_entered.is_set()
        assert [row["bucket_hmac"] for row in first.query(
            "SELECT bucket_hmac FROM rate_limit_buckets ORDER BY bucket_hmac"
        )] == ["first", "second"]
    finally:
        release_first.set()
        one.join(2)
        two.join(2)
        first.close()
        second.close()


def test_startup_lock_rejects_a_second_process(tmp_path: Path) -> None:
    path = tmp_path / "control-plane.db"
    owner = ControlPlaneDatabase(path)
    helper = Path(__file__).with_name("chaos_child.py")
    project_root = Path(__file__).parents[2]
    python_path = str(project_root)
    if os.environ.get("PYTHONPATH"):
        python_path += os.pathsep + os.environ["PYTHONPATH"]
    environment = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": python_path,
    }
    try:
        owner.acquire_process_lock()
        blocked = subprocess.run(
            [sys.executable, str(helper), str(path)],
            cwd=project_root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        assert blocked.returncode == 23
        assert str(path) not in blocked.stdout + blocked.stderr
        owner.release_process_lock()
        accepted = subprocess.run(
            [sys.executable, str(helper), str(path)],
            cwd=project_root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        assert accepted.returncode == 0
    finally:
        owner.close()

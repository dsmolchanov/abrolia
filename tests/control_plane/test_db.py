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


def test_0010_retires_legacy_rows_instead_of_guessing_their_identities(
    tmp_path: Path,
) -> None:
    """C3a acceptance 4, restated after two rewrites turned out to be wrong.

    The criterion was "behaviour-preserving": a household that added nobody
    projects the same manifest. Two backfills were written to satisfy it
    literally, and both invented identities the schema never recorded —
    `chat_id = external_id` answered the chat question with a sender, and
    `external_id = actor_id` assumed every household-local actor name was a
    transport sender.

    The second one does not merely produce a wrong row; it ABORTS. Both shapes
    below are parameterised here because both were live against 0009:

    * one household, one actor, two senders — the arrangement the pre-0010
      challenge API allowed — collides on
      `UNIQUE (household_id, channel, external_id)` and takes the whole
      migration down with it;
    * two households that both named an adult `synthetic-adult` migrate
      cleanly and then hold ONE `external_id` between them, which makes
      `WhatsAppGatewayRouter.route` answer `ambiguous_sender` for both.
      `_reject_foreign_holder` guards `external_id` across households and has
      never guarded `actor_id`.

    So the migration retires them. What is preserved is not the row but the
    HOUSEHOLD's ability to get a correct one: `ensure_owner_binding` re-seeds
    the owner from the durable onboarding result on the next revision, now
    reading `actor_id` for the sender and `chat_id` for the chat — which is
    what a rewrite here could never have produced.
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
        with database.write() as connection:
            for household in ("h1", "h2"):
                connection.execute(
                    "INSERT INTO households (id, slug, status, created_at,"
                    " updated_at) VALUES (?, ?, 'active', 1, 1)",
                    (household, f"hh-{household}"),
                )
            for row_id, household, channel, external_id, actor_id in (
                # The owner shape every live row has: a CHAT in the sender
                # column, an internal name in the actor column.
                ("b1", "h1", "telegram", "-100990000101", "synthetic-owner"),
                # One actor, two senders — this is what aborted the rewrite.
                ("b2", "h1", "whatsapp", "+999511111", "synthetic-adult"),
                ("b3", "h1", "whatsapp", "+999511222", "synthetic-adult"),
                # The same actor name in a second household — this is what
                # migrated cleanly and then broke gateway routing for both.
                ("b4", "h2", "whatsapp", "+999511333", "synthetic-adult"),
            ):
                connection.execute(
                    "INSERT INTO channel_bindings (id, household_id, channel,"
                    " external_id, actor_id, role, verified_at,"
                    " verified_by_actor_id) VALUES (?, ?, ?, ?, ?, 'adult', 1,"
                    " 'synthetic-owner')",
                    (row_id, household, channel, external_id, actor_id),
                )
            connection.execute(
                "INSERT INTO channel_binding_challenges (id, household_id,"
                " channel, external_id, actor_id, role, code_hash,"
                " issued_by_actor_id, expires_at, attempts, consumed_at,"
                " created_at) VALUES ('c1', 'h1', 'whatsapp', '+999511444',"
                " 'synthetic-adult', 'adult', 'digest', 'synthetic-owner',"
                " 9e9, 0, NULL, 1)"
            )

        # It runs at all, which the rewrite did not.
        assert database.migrate() == ["0010_channel_binding_chat_id.sql"]

        assert database.query("SELECT id FROM channel_bindings") == []
        # The outstanding invitation goes with them: it names a chat nobody
        # captured, and a legacy internal-actor challenge could not redeem
        # anyway now that `_insert` refuses an actor that is not the sender.
        assert database.query("SELECT id FROM channel_binding_challenges") == []
        # The households themselves are untouched — this retires bindings, not
        # families, and the next revision re-seeds the owner's row.
        assert len(database.query("SELECT id FROM households")) == 2
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

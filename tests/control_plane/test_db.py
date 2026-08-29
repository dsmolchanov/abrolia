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
            "0011_channel_preference_fallback.sql",
            "0012_channel_binding_published.sql",
        ]
        assert database.migrate() == []
        assert database.pragma() == {
            "journal_mode": "wal",
            "synchronous": 2,
            "foreign_keys": 1,
        }
    finally:
        database.close()


def test_0010_repairs_what_has_provenance_and_retires_what_does_not(
    tmp_path: Path,
) -> None:
    """C3a acceptance 4, restated after three drafts of this migration.

    The criterion was "behaviour-preserving". Taken literally it produced two
    opposite mistakes, and the parameterisation below is one case per mistake
    so neither can come back silently.

    Rewriting every row invented identities the schema never recorded, and
    `SET external_id = actor_id` ABORTED on `UNIQUE (household_id, channel,
    external_id)` for the one-actor-two-senders shape the pre-0010 challenge
    API allowed. Deleting every row then revoked identities that were
    recoverable: nothing re-seeds an owner binding, because `migrate` never
    calls `DesiredSpecPlanner.issue` and neither does startup, so a live
    household migrated cleanly and stopped routing.

    What separates them is provenance, and it is a property of the row. An
    OWNER row was written with `actor_id = channel_public["actor_id"]` — the
    sender onboarding captured, the same value that becomes `actors.owner` and
    is compared against `message.from.id`. Repairing it reads a field that
    already holds the answer. An ADULT row's actor is free text typed at the
    challenge endpoint, with no transport provenance anywhere in the schema.
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
            for household in ("h1", "h2", "h3", "h4"):
                connection.execute(
                    "INSERT INTO households (id, slug, status, created_at,"
                    " updated_at) VALUES (?, ?, 'active', 1, 1)",
                    (household, f"hh-{household}"),
                )
            for row_id, household, channel, external_id, actor, role, at in (
                # A live household: owner plus an adult.
                ("b1", "h1", "telegram", "-100990000101", "990000001", "owner", 1),
                # One actor over two senders — the shape that aborted the
                # rewrite outright.
                ("b2", "h1", "whatsapp", "+999511111", "synthetic-adult", "adult", 2),
                ("b3", "h1", "whatsapp", "+999511222", "synthetic-adult", "adult", 3),
                # Two owner rows, predating the guarantee that there is one.
                ("b4", "h2", "telegram", "-100990000202", "990000002", "owner", 1),
                ("b5", "h2", "telegram", "-100990000203", "990000009", "owner", 5),
                # Two households recorded under ONE owner actor — the shape
                # that migrated cleanly and then made the gateway answer
                # `ambiguous_sender` for both.
                ("b6", "h3", "whatsapp", "+999511333", "990000003", "owner", 1),
                ("b7", "h4", "whatsapp", "+999511444", "990000003", "owner", 1),
            ):
                connection.execute(
                    "INSERT INTO channel_bindings (id, household_id, channel,"
                    " external_id, actor_id, role, verified_at,"
                    " verified_by_actor_id) VALUES (?, ?, ?, ?, ?, ?, ?, 'o')",
                    (row_id, household, channel, external_id, actor, role, at),
                )
            connection.execute(
                "INSERT INTO channel_binding_challenges (id, household_id,"
                " channel, external_id, actor_id, role, code_hash,"
                " issued_by_actor_id, expires_at, attempts, consumed_at,"
                " created_at) VALUES ('c1', 'h1', 'whatsapp', '+999511555',"
                " 'synthetic-adult', 'adult', 'digest', 'o', 9e9, 0, NULL, 1)"
            )

        # It runs at all, which the rewrite did not. Everything from 0010
        # onward applies here — `migrate()` reads the real directory while the
        # staged one stops short of it — so this names the migration under test
        # rather than the whole tail, which would need editing for every
        # migration that follows.
        assert database.migrate()[0] == "0010_channel_binding_chat_id.sql"

        rows = database.query(
            "SELECT id, household_id, external_id, chat_id FROM channel_bindings"
            " ORDER BY id"
        )
        # The live owner keeps routing AND gains the sender it should always
        # have had: `external_id` is the identity ingest reports, `chat_id` is
        # the conversation that used to occupy it. Retiring this row would have
        # left a deployed household unreachable with nothing to re-seed it.
        # `b5` survives `b4` as the newest owner row of its household.
        assert [(r["id"], r["household_id"]) for r in rows] == [
            ("b1", "h1"), ("b5", "h2"),
        ]
        assert (rows[0]["external_id"], rows[0]["chat_id"]) == (
            "990000001", "-100990000101",
        )
        assert (rows[1]["external_id"], rows[1]["chat_id"]) == (
            "990000009", "-100990000203",
        )
        # h3 and h4 are both gone: neither claim on the shared actor can be
        # preferred, and an unroutable household is recoverable where one
        # routed to somebody else's runtime is not.
        assert not database.query(
            "SELECT id FROM channel_bindings WHERE household_id IN ('h3', 'h4')"
        )
        # Adults and outstanding invitations carry no provenance and go.
        assert database.query("SELECT id FROM channel_binding_challenges") == []
        # This retires bindings, not families.
        assert len(database.query("SELECT id FROM households")) == 4
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


def test_0011_backfills_from_the_revision_that_is_actually_serving(
    tmp_path: Path,
) -> None:
    """C3c Phase 1: the backfill keys on `config_revisions.status`.

    The obvious key is `households.current_config_revision`, and it is wrong.
    `schedule_runtime_rollout` advances that column when the job is QUEUED, so
    a household mid-rollout carries a revision nothing is serving yet.
    Backfilling from it would publish bindings against a revision the runtime
    has never seen — the exact confusion this column exists to remove.

    Three households, three shapes, one migration.
    """
    staged = tmp_path / "migrations"
    staged.mkdir()
    for script in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if script.name.startswith("0012"):
            break
        (staged / script.name).write_text(script.read_text(encoding="utf-8"))

    database = ControlPlaneDatabase(tmp_path / "control-plane.db")
    try:
        database.migrate(staged)
        with database.write() as connection:
            for household, current in (
                # Settled: serving revision 3, nothing in flight.
                ("h1", 3),
                # Mid-rollout: serving 3, but `current_config_revision` already
                # names 4 because the job is queued.
                ("h2", 4),
                # Never activated anything.
                ("h3", 1),
            ):
                connection.execute(
                    "INSERT INTO households (id, slug, status, created_at,"
                    " updated_at, current_config_revision) VALUES (?, ?,"
                    " 'active', 1, 1, ?)",
                    (household, f"hh-{household}", current),
                )
            # `activated_at` is set on every ACTIVE revision because
            # `BootstrapService.activate` writes it in the same statement as
            # the status (`bootstrap.py:422`) and is the column's sole writer,
            # so `status = 'active' AND activated_at IS NULL` does not occur.
            # The fixture said otherwise, which is why it could not express the
            # in-flight case below at all: with no activation instant to
            # compare against, every binding of an active household looks
            # equally like a member of the serving revision.
            for household, revision, status, activated_at in (
                ("h1", 3, "active", 100.0),
                ("h2", 3, "active", 100.0),
                ("h2", 4, "issued", None),
                ("h3", 1, "issued", None),
            ):
                connection.execute(
                    "INSERT INTO config_revisions (id, household_id, revision,"
                    " schema_version, manifest_ciphertext, encryption_key_version,"
                    " manifest_sha256, status, created_at, activated_at) VALUES"
                    " (?, ?, ?, 1, X'00', 'v1', ?, ?, 1, ?)",
                    (
                        f"{household}-r{revision}",
                        household,
                        revision,
                        f"{household}{revision}" + "0" * (64 - len(household) - 1),
                        status,
                        activated_at,
                    ),
                )
            # Verified BEFORE their household's revision activated, so revision
            # 3's manifest carries them.
            for household in ("h1", "h2", "h3"):
                connection.execute(
                    "INSERT INTO channel_bindings (id, household_id, channel,"
                    " external_id, chat_id, actor_id, role, verified_at,"
                    " verified_by_actor_id) VALUES (?, ?, 'telegram', ?, ?, ?,"
                    " 'owner', 1, ?)",
                    (
                        f"b-{household}",
                        household,
                        f"99000000{household[-1]}",
                        f"-10099000010{household[-1]}",
                        f"99000000{household[-1]}",
                        f"99000000{household[-1]}",
                    ),
                )
            # And one verified AFTER h2's revision 3 activated: a member staged
            # for the in-flight revision 4. Revision 3's manifest has no pair
            # for them, so publishing them at 3 would route their messages to a
            # runtime that must deny the turn — and would leave a non-NULL row
            # that revision 4's failure could no longer retire.
            connection.execute(
                "INSERT INTO channel_bindings (id, household_id, channel,"
                " external_id, chat_id, actor_id, role, verified_at,"
                " verified_by_actor_id) VALUES ('b-h2-staged', 'h2', 'telegram',"
                " '990000029', '-100990000109', '990000029', 'adult', 150.0,"
                " '990000002')"
            )

        assert "0012_channel_binding_published.sql" in database.migrate()

        published = dict(
            (row["id"], row["published_revision"])
            for row in database.query(
                "SELECT id, published_revision FROM channel_bindings"
            )
        )
        # The settled household publishes at the revision it serves.
        assert published["b-h1"] == 3
        # The mid-rollout household publishes at 3 — what it SERVES — and not
        # at 4, which is only what it is rolling out.
        assert published["b-h2"] == 3
        # But its member staged for 4 stays STAGED. Having an active revision
        # does not make every one of a household's bindings a member of it, and
        # publishing this row is the failure the column exists to prevent.
        assert published["b-h2-staged"] is None
        # Nothing is serving h3, so nothing of its is routable.
        assert published["b-h3"] is None
    finally:
        database.close()


def _rehearsal_module():
    """`scripts/rehearse_0012_routing.py` is a script, not an importable module."""
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "scripts" / "rehearse_0012_routing.py"
    spec = importlib.util.spec_from_file_location("rehearse_0012_routing", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _database_with_0012_pending(tmp_path: Path) -> Path:
    """A control-plane database migrated up to, but not including, 0012."""
    staged = tmp_path / "migrations"
    staged.mkdir()
    for script in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if script.name.startswith("0012"):
            break
        (staged / script.name).write_text(script.read_text(encoding="utf-8"))
    path = tmp_path / "source-control-plane.db"
    database = ControlPlaneDatabase(path)
    try:
        database.migrate(staged)
    finally:
        database.close()
    return path


@pytest.mark.parametrize("outcome", ["rehearsed", "nothing_to_rehearse"])
def test_the_rehearsal_does_not_leave_a_copy_of_the_database_behind(
    tmp_path: Path, monkeypatch, outcome: str
) -> None:
    """The copy is bounded to the operation that needs it.

    This script is pointed at REAL data by design — that is what makes it worth
    running — so the copy it takes is the most sensitive thing it touches. It
    took that copy into a `mkdtemp` directory nothing ever removed, leaving
    plaintext channel identifiers and every other retained control-plane row in
    an unnamed `/tmp` directory after every run, successful or not.

    Both exits are covered because the leak was in neither branch's code: it was
    in the `finally` that closed the connection and stopped there, so every path
    out of the function leaked equally.
    """
    module = _rehearsal_module()
    source = _database_with_0012_pending(tmp_path)
    if outcome == "nothing_to_rehearse":
        # Already migrated, so the script returns early — the path that reaches
        # its `return` without doing any of the work.
        already = ControlPlaneDatabase(source)
        try:
            already.migrate()
        finally:
            already.close()

    work_dir = tmp_path / "scratch-that-must-not-survive"

    def fake_mkdtemp(*args, **kwargs) -> str:
        work_dir.mkdir()
        return str(work_dir)

    monkeypatch.setattr(module.tempfile, "mkdtemp", fake_mkdtemp)
    code = module.main(["rehearse_0012_routing.py", str(source)])

    assert code == (0 if outcome == "rehearsed" else 2)
    assert not work_dir.exists(), (
        "a copy of the control-plane database outlived the rehearsal"
    )

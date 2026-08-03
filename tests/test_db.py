"""Транзакционный слой: миграции и настройки долговечности."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_cloud.core.db import Database, open_database


def test_migrations_apply_once_and_are_recorded(tmp_path: Path) -> None:
    database = Database(tmp_path / "hermes.db")
    applied = database.migrate()
    assert applied == ["0001_init.sql"]
    assert database.migrate() == [], "повторный запуск не должен ничего применять"

    names = {row["name"] for row in database.query("SELECT name FROM schema_migrations")}
    assert names == {"0001_init.sql"}


def test_durability_pragmas_are_set(tmp_path: Path) -> None:
    """WAL + synchronous=FULL — это и есть «fsync до ACK»."""
    database = open_database(tmp_path / "hermes.db")
    assert database.query_one("PRAGMA journal_mode")[0].lower() == "wal"
    assert database.query_one("PRAGMA synchronous")[0] == 2  # FULL


def test_failed_migration_leaves_no_partial_schema(tmp_path: Path) -> None:
    """Незаписанная миграция не должна оставлять половину таблиц."""
    broken = tmp_path / "migrations"
    broken.mkdir()
    (broken / "0001_broken.sql").write_text(
        "CREATE TABLE good (id TEXT);\nCREATE TABLE bad (id TEXT,);\n", encoding="utf-8"
    )
    database = Database(tmp_path / "hermes.db")
    with pytest.raises(sqlite3.OperationalError):
        database.migrate(broken)

    tables = {
        row["name"]
        for row in database.query("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "good" not in tables
    assert not database.query("SELECT name FROM schema_migrations")


def test_write_rolls_back_on_error(tmp_path: Path) -> None:
    database = open_database(tmp_path / "hermes.db")
    with pytest.raises(RuntimeError), database.write() as connection:
        connection.execute(
            "INSERT INTO events (id, source, external_id, raw, received_at,"
            " status, updated_at) VALUES ('x', 's', 'e', X'00', 1, 'received', 1)"
        )
        raise RuntimeError("сбой посреди транзакции")

    assert database.query("SELECT id FROM events") == []

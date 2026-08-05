"""Транзакционный слой: SQLite в режиме WAL с fsync до подтверждения.

Инварианты (канонический план, Locked Decisions → «Ingress», «Хранение»):

* **fsync до ACK.** `synchronous=FULL` + WAL: коммит возвращает управление
  только после fsync журнала. Событие, за которое мы ответили «принято»,
  переживает выключение питания — иначе входящее письмо теряется молча.
* **Одна схема, переносимая на Postgres.** В запросах нет SQLite-специфики
  (`rowid`, `INSERT OR REPLACE`, склейка через `||`), идентификаторы — UUID в
  виде текста, а не автоинкременты. Миграции — нумерованные `.sql`, они
  диалектные по определению и переписываются при миграции на Postgres.
* **Писатель один и явный.** Все записи идут через `write()`, который
  открывает `BEGIN IMMEDIATE`: конкурирующий писатель ждёт на блокировке, а не
  падает с `database is locked` посреди транзакции.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# Ожидание блокировки писателя. Больше, чем самая долгая транзакция воркера.
BUSY_TIMEOUT_SECONDS = 30.0


def new_id() -> str:
    """UUID4 без дефисов: переносится в Postgres как `uuid`, а не как serial."""
    return uuid.uuid4().hex


class Database:
    """Подключение к базе household'а с применёнными миграциями."""

    def __init__(self, path: Path | str, *, timeout: float = BUSY_TIMEOUT_SECONDS) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self._connection: sqlite3.Connection | None = None

    # --- подключение --------------------------------------------------------

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                self.path,
                timeout=self.timeout,
                isolation_level=None,  # транзакциями управляем сами
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            # FULL, а не NORMAL: NORMAL в WAL допускает потерю последних
            # коммитов при отключении питания — для ingress это потеря письма.
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            self._connection = connection
        return self._connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- транзакции ---------------------------------------------------------

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Транзакция на запись: BEGIN IMMEDIATE → COMMIT (fsync) / ROLLBACK."""
        connection = self.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")

    def query(self, sql: str, params: dict | tuple = ()) -> list[sqlite3.Row]:
        return list(self.connection.execute(sql, params))

    def query_one(self, sql: str, params: dict | tuple = ()) -> sqlite3.Row | None:
        rows = self.connection.execute(sql, params).fetchmany(1)
        return rows[0] if rows else None

    # --- миграции -----------------------------------------------------------

    def migrate(self, directory: Path | None = None) -> list[str]:
        """Применить неприменённые миграции по возрастанию имени."""
        directory = directory or MIGRATIONS_DIR
        connection = self.connection
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name TEXT PRIMARY KEY,"
            " applied_at REAL NOT NULL)"
        )
        applied = {row["name"] for row in connection.execute("SELECT name FROM schema_migrations")}
        freshly: list[str] = []
        for script in sorted(directory.glob("*.sql")):
            if script.name in applied:
                continue
            # BEGIN/COMMIT внутри самого скрипта: executescript коммитит
            # открытую транзакцию перед стартом, поэтому обернуть его снаружи
            # нельзя — миграция применилась бы по частям, без отметки о себе.
            name_literal = script.name.replace("'", "''")
            body = "\n".join((
                "BEGIN IMMEDIATE;",
                script.read_text(encoding="utf-8"),
                "INSERT INTO schema_migrations (name, applied_at)"
                f" VALUES ('{name_literal}', strftime('%s','now'));",
                "COMMIT;",
            ))
            try:
                connection.executescript(body)
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            freshly.append(script.name)
        return freshly


def open_database(path: Path | str) -> Database:
    """Открыть базу и применить миграции — обычная точка входа приложения."""
    database = Database(path)
    database.migrate()
    return database

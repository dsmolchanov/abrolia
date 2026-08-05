"""Бэкап и восстановление: снимок SQLite, зашифрованный ключом household'а.

Снимок делается онлайн-бэкапом SQLite, а не копированием файла. Копия `.db`
мимо API в режиме WAL — это копия без части свежих транзакций и с шансом
получить нечитаемый файл; `Connection.backup` даёт согласованный снимок под
писателем, не останавливая работу.

Шифрование — не «на всякий случай». Бэкап лежит в объектном хранилище, то есть
за пределами household-инстанса, и это единственное место, где вся переписка
семьи собрана одним файлом. Ключ — per-household: компрометация хранилища не
должна означать компрометацию всех семей сразу (`docs/SECURITY.md`, T10).

Формат архива:

    HRMSB1 | salt(16) | nonce(12) | AES-256-GCM(gzip(снимок))

Аутентифицированное шифрование выбрано намеренно: испорченный или подменённый
архив обязан не расшифроваться, а не восстановиться «почти правильно».
Восстанавливать базу из тихо повреждённого бэкапа хуже, чем не восстановить.
"""

from __future__ import annotations

import gzip
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from hermes_cloud.core.db import Database

MAGIC = b"HRMSB1"
SALT_BYTES = 16
NONCE_BYTES = 12
KEY_BYTES = 32

# Параметры scrypt: ~64 МБ памяти на вывод ключа. Бэкап делается раз в сутки,
# так что дороговизна вывода здесь ничего не стоит нам и дорого стоит перебору.
SCRYPT_N = 2**16
SCRYPT_R = 8
SCRYPT_P = 1

ENV_BACKUP_KEY = "HERMES_BACKUP_KEY"
BACKUP_SUFFIX = ".db.enc"
# Скользящее окно из data map: бэкапы живут 30 дней.
KEEP_DAYS = 30
DAY = 86_400.0


class BackupError(RuntimeError):
    """Архив не тот, ключ не тот или снимок не читается."""


@dataclass(frozen=True)
class Archive:
    path: Path
    created_at: float
    size: int


def _derive(key: str, salt: bytes) -> bytes:
    if not key:
        raise BackupError(f"не задан ключ бэкапа (${ENV_BACKUP_KEY})")
    import hashlib

    return hashlib.scrypt(
        key.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=KEY_BYTES,
        # OpenSSL по умолчанию не даёт scrypt больше 32 МБ, а мы просим 64:
        # предел приходится поднимать явно, иначе вывод ключа падает.
        maxmem=2 * 128 * SCRYPT_N * SCRYPT_R,
    )


def _snapshot_bytes(database: Database) -> bytes:
    """Согласованный снимок базы через онлайн-бэкап SQLite."""
    import tempfile

    with tempfile.TemporaryDirectory() as workspace:
        target = Path(workspace) / "snapshot.db"
        destination = sqlite3.connect(target)
        try:
            database.connection.backup(destination)
        finally:
            destination.close()
        return target.read_bytes()


def make_backup(
    database: Database,
    directory: Path | str,
    *,
    key: str | None = None,
    now: float | None = None,
) -> Archive:
    """Сделать зашифрованный снимок и положить его в каталог."""
    now = time.time() if now is None else now
    key = key if key is not None else os.environ.get(ENV_BACKUP_KEY, "")
    salt = os.urandom(SALT_BYTES)
    nonce = os.urandom(NONCE_BYTES)
    payload = gzip.compress(_snapshot_bytes(database))
    sealed = AESGCM(_derive(key, salt)).encrypt(nonce, payload, MAGIC)

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    path = directory / f"hermes-{stamp}{BACKUP_SUFFIX}"
    # Сначала во временный файл, потом атомарное переименование: наполовину
    # записанный архив не должен выглядеть как готовый.
    partial = path.with_suffix(path.suffix + ".partial")
    with partial.open("wb") as handle:
        handle.write(MAGIC + salt + nonce + sealed)
        handle.flush()
        os.fsync(handle.fileno())
    partial.replace(path)
    return Archive(path=path, created_at=now, size=path.stat().st_size)


def read_backup(archive: Path | str, *, key: str | None = None) -> bytes:
    """Расшифровать архив и вернуть снимок базы."""
    key = key if key is not None else os.environ.get(ENV_BACKUP_KEY, "")
    blob = Path(archive).read_bytes()
    header = len(MAGIC) + SALT_BYTES + NONCE_BYTES
    if len(blob) <= header or not blob.startswith(MAGIC):
        raise BackupError(f"{archive}: это не архив hermes")
    salt = blob[len(MAGIC):len(MAGIC) + SALT_BYTES]
    nonce = blob[len(MAGIC) + SALT_BYTES:header]
    try:
        payload = AESGCM(_derive(key, salt)).decrypt(nonce, blob[header:], MAGIC)
    except Exception as error:  # InvalidTag и всё, что ещё придёт из библиотеки
        raise BackupError(
            f"{archive}: не расшифровывается — неверный ключ или повреждённый архив"
        ) from error
    return gzip.decompress(payload)


def restore_backup(
    archive: Path | str, target: Path | str, *, key: str | None = None, overwrite: bool = False
) -> Path:
    """Восстановить базу из архива. Существующий файл не трогаем без спроса."""
    target = Path(target)
    if target.exists() and not overwrite:
        raise BackupError(f"{target} уже существует; восстановление отменено")
    snapshot = read_backup(archive, key=key)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".partial")
    with partial.open("wb") as handle:
        handle.write(snapshot)
        handle.flush()
        os.fsync(handle.fileno())
    partial.replace(target)
    problem = integrity_problem(target)
    if problem is not None:
        raise BackupError(f"восстановленная база не проходит проверку: {problem}")
    return target


def integrity_problem(path: Path | str) -> str | None:
    """`PRAGMA integrity_check` — None означает «база цела»."""
    connection = sqlite3.connect(path)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    return None if result and result[0] == "ok" else str(result)


def list_backups(directory: Path | str) -> list[Archive]:
    directory = Path(directory)
    if not directory.is_dir():
        return []
    found = [
        Archive(path=path, created_at=path.stat().st_mtime, size=path.stat().st_size)
        for path in directory.glob(f"*{BACKUP_SUFFIX}")
    ]
    return sorted(found, key=lambda item: item.created_at, reverse=True)


def prune_backups(
    directory: Path | str, *, keep_days: int = KEEP_DAYS, now: float | None = None
) -> list[Path]:
    """Убрать архивы старше окна. Последний не удаляется никогда.

    Окно — из data map (бэкапы, 30 дней). Оговорка про последний архив нужна
    для случая, когда бэкапы не делались месяц: остаться совсем без копии хуже,
    чем передержать одну.
    """
    now = time.time() if now is None else now
    archives = list_backups(directory)
    removed: list[Path] = []
    for archive in archives[1:]:
        if archive.created_at <= now - keep_days * DAY:
            archive.path.unlink()
            removed.append(archive.path)
    return removed

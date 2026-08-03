"""Конфигурация household'а из окружения.

Конфиг плоский и приходит из env (и из `.env` при локальной работе); главное
здесь — **секреты читаются из окружения и никогда не попадают ни в репозиторий,
ни в логи** (`docs/privacy/data-map.md`, S3).

Кто есть кто в household'е — не здесь, а в `core/runcontext.py`: права актора
и конфиг рантайма живут врозь, чтобы «поправить настройку» нельзя было заодно
и раздать права.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENV_DB = "HERMES_DB"
ENV_CHAT = "HERMES_CHAT"
ENV_THREAD = "HERMES_THREAD"
ENV_LANGUAGE = "HERMES_FAMILY_LANGUAGE"
ENV_MODEL = "HERMES_EXTRACTION_MODEL"
ENV_EFFORT = "HERMES_EXTRACTION_EFFORT"
ENV_TELEGRAM = "TELEGRAM_BOT_TOKEN"

DEFAULT_DB_PATH = "data/hermes.db"
DEFAULT_LANGUAGE = "русский"
# Модель извлечения выбрана бенчмарком Фазы 1 (bench/README.md).
DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_EFFORT = "medium"


def load_dotenv(path: Path | str = ".env") -> None:
    """Подтянуть `.env`, если он есть. В проде переменные ставит Fly secrets."""
    file = Path(path)
    if not file.is_file():
        return
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Config:
    database_path: Path
    chat: str
    thread: int | None
    language: str
    model: str
    effort: str | None
    telegram_token: str | None

    @property
    def has_telegram(self) -> bool:
        return bool(self.telegram_token)

    def require_chat(self) -> str:
        if not self.chat:
            raise RuntimeError(
                f"не задан ${ENV_CHAT} — некуда отправлять карточку"
            )
        return self.chat


def load_config(*, env: dict[str, str] | None = None) -> Config:
    source = dict(os.environ if env is None else env)
    thread_raw = source.get(ENV_THREAD, "").strip()
    effort = source.get(ENV_EFFORT, "").strip()
    return Config(
        database_path=Path(source.get(ENV_DB) or DEFAULT_DB_PATH),
        chat=source.get(ENV_CHAT, "").strip(),
        thread=int(thread_raw) if thread_raw else None,
        language=source.get(ENV_LANGUAGE) or DEFAULT_LANGUAGE,
        model=source.get(ENV_MODEL) or DEFAULT_MODEL,
        effort=effort or DEFAULT_EFFORT,
        telegram_token=source.get(ENV_TELEGRAM) or None,
    )

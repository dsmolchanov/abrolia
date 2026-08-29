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
from dataclasses import dataclass, field
from pathlib import Path

from hermes_cloud.core.runtime_manifest import maybe_load_runtime_manifest

ENV_DB = "HERMES_DB"
ENV_CHAT = "HERMES_CHAT"
ENV_THREAD = "HERMES_THREAD"
ENV_LANGUAGE = "HERMES_FAMILY_LANGUAGE"
ENV_MODEL = "HERMES_EXTRACTION_MODEL"
ENV_EFFORT = "HERMES_EXTRACTION_EFFORT"
ENV_TELEGRAM = "TELEGRAM_BOT_TOKEN"
ENV_GOOGLE_TOKEN = "HERMES_GOOGLE_TOKEN"
ENV_CALENDAR = "HERMES_CALENDAR_ID"
ENV_GMAIL_ADDRESS = "HERMES_GMAIL_ADDRESS"
ENV_GMAIL_PASSWORD = "HERMES_GMAIL_APP_PASSWORD"
ENV_GMAIL_LABEL = "HERMES_GMAIL_LABEL"
ENV_LEGACY_IMAP_TEST_ONLY = "HERMES_LEGACY_IMAP_TEST_ONLY"
ENV_EMAIL_PROVIDER = "HERMES_EMAIL_PROVIDER"
ENV_EMAIL_ADDRESS = "HERMES_EMAIL_ADDRESS"
ENV_EMAIL_IDENTITY = "HERMES_EMAIL_IDENTITY_ID"
ENV_EMAIL_BINDING_REVISION = "HERMES_EMAIL_BINDING_REVISION"
ENV_EMAIL_SECRET_NAMES = "HERMES_EMAIL_SECRET_NAMES"
ENV_SMTP_HOST = "HERMES_SMTP_HOST"
ENV_SMTP_PORT = "HERMES_SMTP_PORT"
ENV_VERTEX_EU_ENABLED = "HERMES_VERTEX_EU_ENABLED"

DEFAULT_DB_PATH = "data/hermes.db"
DEFAULT_LANGUAGE = "русский"
# Модель извлечения выбрана бенчмарком Фазы 1 (bench/README.md).
DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_EFFORT = "medium"
DEFAULT_CALENDAR = "primary"
# Ярлык — вся граница доступа к ящику: письмо без него мы не запрашиваем.
DEFAULT_GMAIL_LABEL = "Hermes"
# Хост берётся из конфига, а не зашивается: у семьи может быть не Gmail.
DEFAULT_SMTP_HOST = "smtp.gmail.com"
DEFAULT_SMTP_PORT = 465


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
    google_token_path: Path | None
    calendar_id: str
    gmail_address: str
    gmail_app_password: str | None = field(repr=False)
    gmail_label: str
    legacy_imap_test_only: bool
    smtp_host: str
    smtp_port: int
    telegram_token: str | None = field(repr=False)
    email_provider: str = "synthetic"
    email_address: str = ""
    #: Where the family is written to when the primary channel refuses a
    #: message. It comes from the manifest and from nowhere else: an
    #: environment variable would be a second place to state the family's own
    #: address, and the control plane is the one that knows which account is
    #: the fallback owner. No manifest means no fallback, which fails closed
    #: to sending nothing.
    email_fallback: str = ""
    email_identity_id: str | None = None
    email_binding_revision: int | None = None
    email_secret_names: tuple[str, ...] = ()
    timezone: str | None = None
    country_code: str | None = None
    residency_mode: str | None = None
    config_revision: int | None = None
    config_sha256: str | None = None
    manifest_path: Path | None = None
    primary_channel: str | None = None

    @property
    def has_telegram(self) -> bool:
        return bool(self.telegram_token)

    @property
    def has_gmail(self) -> bool:
        """Deprecated compatibility flag for the test-only IMAP adapter."""
        return bool(
            self.legacy_imap_test_only
            and self.manifest_path is None
            and self.gmail_address
            and self.gmail_app_password
        )

    @property
    def has_email_identity(self) -> bool:
        return bool(
            self.manifest_path is not None
            and self.email_address
            and self.email_identity_id
            and self.email_binding_revision
        )

    @property
    def has_calendar(self) -> bool:
        """Календарь подключён, только если токен ассистента реально существует.

        Путь в переменной без файла — не «почти подключён», а не подключён:
        событие уедет файлом .ics, и семья ничего не потеряет.
        """
        return self.google_token_path is not None and self.google_token_path.is_file()

    def require_chat(self) -> str:
        if not self.chat:
            raise RuntimeError(
                f"не задан ${ENV_CHAT} — некуда отправлять карточку"
            )
        return self.chat


def load_config(
    *, env: dict[str, str] | None = None, manifest_path: Path | str | None = None
) -> Config:
    source = dict(os.environ if env is None else env)
    manifest = maybe_load_runtime_manifest(manifest_path, env=source)
    if (
        manifest is not None
        and manifest.residency_mode == "eu-strict"
        and (source.get(ENV_VERTEX_EU_ENABLED) or "").strip().casefold()
        not in {"1", "true", "yes", "on"}
    ):
        raise RuntimeError(
            f"residency_mode eu-strict requires ${ENV_VERTEX_EU_ENABLED}; refusing downgrade"
        )
    thread_raw = source.get(ENV_THREAD, "").strip()
    effort = source.get(ENV_EFFORT, "").strip()
    legacy_imap_test_only = (
        source.get(ENV_LEGACY_IMAP_TEST_ONLY) or ""
    ).strip().casefold() in {"1", "true", "yes", "on"}
    raw_email_revision = (source.get(ENV_EMAIL_BINDING_REVISION) or "").strip()
    manifest_email = manifest.email if manifest else None
    email_address = (
        manifest_email.agent_inbox
        if manifest_email is not None
        else (source.get(ENV_EMAIL_ADDRESS) or "").strip()
    )
    email_fallback = manifest_email.fallback if manifest_email is not None else ""
    email_identity_id = (
        manifest_email.provider_binding_ref
        if manifest_email is not None
        else (source.get(ENV_EMAIL_IDENTITY) or "").strip() or None
    )
    secret_names = tuple(
        item.strip()
        for item in (source.get(ENV_EMAIL_SECRET_NAMES) or "").split(",")
        if item.strip()
    )
    if manifest_email is not None and manifest_email.secret_binding_ref:
        secret_names = (manifest_email.secret_binding_ref,)
    return Config(
        database_path=Path(source.get(ENV_DB) or DEFAULT_DB_PATH),
        chat=manifest.primary_chat_id if manifest else source.get(ENV_CHAT, "").strip(),
        thread=int(thread_raw) if thread_raw else None,
        language=manifest.family_language if manifest else source.get(ENV_LANGUAGE) or DEFAULT_LANGUAGE,
        model=source.get(ENV_MODEL) or DEFAULT_MODEL,
        effort=effort or DEFAULT_EFFORT,
        telegram_token=source.get(ENV_TELEGRAM) or None,
        google_token_path=(
            Path(source[ENV_GOOGLE_TOKEN]) if source.get(ENV_GOOGLE_TOKEN) else None
        ),
        calendar_id=source.get(ENV_CALENDAR) or DEFAULT_CALENDAR,
        gmail_address=(
            manifest.email.agent_inbox
            if manifest else (source.get(ENV_GMAIL_ADDRESS) or "").strip()
        ),
        gmail_app_password=(
            source.get(ENV_GMAIL_PASSWORD) or None
            if legacy_imap_test_only and manifest is None
            else None
        ),
        gmail_label=source.get(ENV_GMAIL_LABEL) or DEFAULT_GMAIL_LABEL,
        legacy_imap_test_only=legacy_imap_test_only,
        smtp_host=source.get(ENV_SMTP_HOST) or DEFAULT_SMTP_HOST,
        smtp_port=int(source.get(ENV_SMTP_PORT) or DEFAULT_SMTP_PORT),
        timezone=manifest.timezone if manifest else None,
        country_code=manifest.country_code if manifest else None,
        residency_mode=manifest.residency_mode if manifest else None,
        config_revision=manifest.config_revision if manifest else None,
        config_sha256=manifest.config_sha256 if manifest else None,
        manifest_path=Path(manifest.source) if manifest else None,
        primary_channel=manifest.primary_channel if manifest else None,
        email_provider=(
            manifest_email.provider_kind
            if manifest_email is not None
            else (source.get(ENV_EMAIL_PROVIDER) or "synthetic").strip()
        ),
        email_address=email_address,
        email_fallback=email_fallback,
        email_identity_id=email_identity_id,
        email_binding_revision=(
            manifest.config_revision
            if manifest is not None
            else int(raw_email_revision) if raw_email_revision else None
        ),
        email_secret_names=secret_names,
    )

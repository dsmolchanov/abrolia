"""Приём письма из файла — первый вход конвейера.

Тот же путь потом переиспользуют nerve-webhook и IMAP-поллер: разбор,
дедупликация и запись события общие, различается только транспорт. Поэтому
здесь нет ничего специфичного для файла, кроме чтения байтов.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from hermes_cloud.core.events import EventStore
from hermes_cloud.ingest.rfc822 import Ingested, ingest_rfc822

SOURCE_INJECT = "inject"


def ingest_bytes(
    store: EventStore, raw: bytes, *, source: str = SOURCE_INJECT
) -> Ingested:
    """Разобрать письмо и зафиксировать событие (повтор — no-op)."""
    return ingest_rfc822(
        store,
        source=source,
        provider_event_id=f"inject:{hashlib.sha256(raw).hexdigest()}",
        raw_bytes=raw,
    )


def ingest_file(store: EventStore, path: Path | str, *, source: str = SOURCE_INJECT) -> Ingested:
    return ingest_bytes(store, Path(path).read_bytes(), source=source)

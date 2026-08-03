"""Приём письма из файла — первый вход конвейера.

Тот же путь потом переиспользуют nerve-webhook и IMAP-поллер: разбор,
дедупликация и запись события общие, различается только транспорт. Поэтому
здесь нет ничего специфичного для файла, кроме чтения байтов.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from hermes_cloud.core.events import Accepted, EventStore
from hermes_cloud.ingest.eml import ParsedEmail, external_id, parse_eml

SOURCE_INJECT = "inject"


@dataclass(frozen=True)
class Ingested:
    accepted: Accepted
    parsed: ParsedEmail

    @property
    def created(self) -> bool:
        return self.accepted.created

    @property
    def event_id(self) -> str:
        return self.accepted.event.id


def ingest_bytes(
    store: EventStore, raw: bytes, *, source: str = SOURCE_INJECT
) -> Ingested:
    """Разобрать письмо и зафиксировать событие (повтор — no-op)."""
    parsed = parse_eml(raw)
    accepted = store.append(
        source=source,
        external_id=external_id(parsed, raw),
        raw=raw,
        # Ключ цепочки, а не отправитель: два письма одного треда должны
        # обрабатываться по порядку, письма разных семейных тем — параллельно.
        context_key=parsed.thread_key,
    )
    return Ingested(accepted=accepted, parsed=parsed)


def ingest_file(store: EventStore, path: Path | str, *, source: str = SOURCE_INJECT) -> Ingested:
    return ingest_bytes(store, Path(path).read_bytes(), source=source)

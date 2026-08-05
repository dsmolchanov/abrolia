"""Провенанс: откуда взялась эта сумма — и что остаётся, когда письмо удалено.

Правило простое и жёсткое: **в `evidence_refs` не хранится текст**. Только
домен отправителя, дата письма, хэш содержимого и офсеты. Цитата в карточке
собирается из живого `events.raw` в момент показа. Когда письмо удалено по
сроку хранения, ссылка остаётся разрешимой как след — «письмо от schule.example
от 01.09.2026, содержимое удалено по сроку хранения», — и это честнее, чем и
цитата, пережившая удаление, и молчание.

Хэш содержимого нужен ровно для одного: если письмо ещё живо, видно, что
цитируется тот же текст, по которому делалось извлечение, а не другой.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any

from hermes_cloud.core.db import Database, new_id

# Сколько текста показываем в карточке. Цитата — опора для человека, а не
# пересказ письма.
QUOTE_LIMIT = 200

TEXT_SOURCE_GONE = "источник удалён по сроку хранения"


def content_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sender_domain(address: str | None) -> str | None:
    """Домен, а не адрес: адрес — персональные данные, домен — источник."""
    if not address or "@" not in address:
        return None
    return address.rsplit("@", 1)[1].strip().strip(">").lower() or None


def find_span(text: str, needles: list[str]) -> tuple[int, int] | None:
    """Найти предложение, в котором впервые встречается одна из опор.

    Опоры — то, что человек будет проверять глазами: сумма, дата, место. Если
    ни одна не нашлась, спана нет: выдумывать «примерное место» бессмысленно.
    """
    for needle in needles:
        needle = (needle or "").strip()
        if len(needle) < 3:
            continue
        position = text.find(needle)
        if position < 0:
            continue
        start = max(
            (text.rfind(mark, 0, position) for mark in (".", "!", "?", "\n")),
            default=-1,
        )
        end_candidates = [
            index
            for index in (text.find(mark, position + len(needle)) for mark in (".", "!", "?", "\n"))
            if index >= 0
        ]
        end = min(end_candidates) + 1 if end_candidates else len(text)
        return max(0, start + 1), min(end, start + 1 + QUOTE_LIMIT)
    return None


@dataclass(frozen=True)
class ExtractionRun:
    id: str
    event_id: str | None
    model: str
    prompt_sha: str
    input_tokens: int
    output_tokens: int
    created_at: float


@dataclass(frozen=True)
class EvidenceRef:
    id: str
    extraction_run_id: str
    event_id: str | None
    sender_domain: str | None
    message_date: str | None
    content_sha: str
    span_start: int | None
    span_end: int | None
    created_at: float

    @classmethod
    def from_row(cls, row: Any) -> EvidenceRef:
        return cls(
            id=row["id"],
            extraction_run_id=row["extraction_run_id"],
            event_id=row["event_id"],
            sender_domain=row["sender_domain"],
            message_date=row["message_date"],
            content_sha=row["content_sha"],
            span_start=row["span_start"],
            span_end=row["span_end"],
            created_at=row["created_at"],
        )

    def provenance(self) -> str:
        """Строка происхождения, не зависящая от того, жив ли ещё носитель."""
        source = self.sender_domain or "неизвестный отправитель"
        when = f", письмо от {self.message_date}" if self.message_date else ""
        return f"{source}{when}"


class EvidenceStore:
    def __init__(self, database: Database, *, clock=time.time) -> None:
        self.db = database
        self.clock = clock

    def record_run(
        self,
        *,
        event_id: str | None,
        model: str,
        prompt_sha: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        now: float | None = None,
    ) -> ExtractionRun:
        now = self.clock() if now is None else now
        run_id = new_id()
        with self.db.write() as connection:
            connection.execute(
                "INSERT INTO extraction_runs (id, event_id, model, prompt_sha,"
                " input_tokens, output_tokens, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, event_id, model, prompt_sha, input_tokens, output_tokens, now),
            )
        return ExtractionRun(
            id=run_id, event_id=event_id, model=model, prompt_sha=prompt_sha,
            input_tokens=input_tokens, output_tokens=output_tokens, created_at=now,
        )

    def add_ref(
        self,
        *,
        extraction_run_id: str,
        event_id: str | None,
        text: str,
        sender: str | None = None,
        message_date: str | None = None,
        needles: list[str] | None = None,
        now: float | None = None,
    ) -> EvidenceRef:
        """Записать ссылку на источник: метаданные и офсеты, без текста."""
        now = self.clock() if now is None else now
        span = find_span(text, needles or [])
        ref_id = new_id()
        with self.db.write() as connection:
            connection.execute(
                "INSERT INTO evidence_refs (id, extraction_run_id, event_id, sender_domain,"
                " message_date, content_sha, span_start, span_end, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ref_id, extraction_run_id, event_id, sender_domain(sender), message_date,
                    content_sha(text), span[0] if span else None, span[1] if span else None, now,
                ),
            )
        return self.get(ref_id)  # type: ignore[return-value]

    def get(self, ref_id: str) -> EvidenceRef | None:
        row = self.db.query_one("SELECT * FROM evidence_refs WHERE id = ?", (ref_id,))
        return EvidenceRef.from_row(row) if row else None

    def for_run(self, extraction_run_id: str) -> list[EvidenceRef]:
        rows = self.db.query(
            "SELECT * FROM evidence_refs WHERE extraction_run_id = ? ORDER BY created_at",
            (extraction_run_id,),
        )
        return [EvidenceRef.from_row(row) for row in rows]

    def quote(self, ref: EvidenceRef) -> str | None:
        """Собрать цитату из живого письма. None — носителя больше нет.

        Именно так провенанс переживает retention: ссылка остаётся, цитата —
        нет. Вызывающий обязан показать `TEXT_SOURCE_GONE`, а не пустоту.
        """
        if ref.event_id is None or ref.span_start is None or ref.span_end is None:
            return None
        row = self.db.query_one("SELECT raw FROM events WHERE id = ?", (ref.event_id,))
        if row is None or not row["raw"]:
            return None
        from hermes_cloud.ingest.eml import parse_eml

        text = parse_eml(row["raw"]).text
        if content_sha(text) != ref.content_sha:
            # Текст не тот, по которому делалось извлечение: цитировать его
            # как источник нельзя.
            return None
        quote = text[ref.span_start:ref.span_end].strip()
        return re.sub(r"\s+", " ", quote) or None

    def render_source(self, ref: EvidenceRef) -> str:
        """Строка «Источник» для карточки: цитата, если она ещё есть."""
        quote = self.quote(ref)
        if quote is None:
            return f"Источник: {ref.provenance()} — {TEXT_SOURCE_GONE}"
        return f"Источник: {ref.provenance()} — «{quote}»"

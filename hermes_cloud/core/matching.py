"""Это про то же самое или про новое?

Второе письмо школы почти никогда не говорит «отмените предыдущее» — оно просто
сообщает другую дату. Если не узнать в нём прежнее обязательство, у семьи
окажется два события: одно в старую дату, другое в новую, и оба «подтверждённые».

Поэтому здесь — сопоставление нового извлечения с уже известными фактами по трём
опорам: **источник** (домен отправителя), **тип** и **близость ключевых полей**.
Ни одна из них по отдельности не решает: у школы много писем, тип совпадает у
половины, а заголовок модель каждый раз формулирует чуть иначе.

Три исхода, и третий — самый важный:

* уверенное совпадение — новая версия факта (`v2 supersedes v1`);
* нет совпадения — независимый факт;
* **похоже, но не наверняка** — два независимых кандидата и честная пометка
  «возможно, это про то же». Молча склеить два разных взноса хуже, чем показать
  семье оба и дать решить: склейку человек не заметит, дубликат — заметит.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from hermes_cloud.core.commitments import (
    STATUS_CANDIDATE,
    STATUS_CONFIRMED,
    Commitment,
    CommitmentStore,
)
from hermes_cloud.core.evidence import EvidenceStore

# Веса опор. Сумма — 1.0; пороги ниже подобраны так, чтобы одного совпавшего
# домена не хватало (у школы много писем), а домена с похожим заголовком —
# хватало.
WEIGHT_DOMAIN = 0.35
WEIGHT_KIND = 0.15
WEIGHT_TITLE = 0.5

SURE = 0.8
MAYBE = 0.55

# Слова, которые ничего не различают: они есть в половине школьных писем.
_NOISE = re.compile(r"\b(экскурсия|klassenfahrt|взнос|beitrag|оплата|zahlung|для|the|der|die)\b")


@dataclass(frozen=True)
class Match:
    """Найденное совпадение и насколько мы в нём уверены."""

    commitment: Commitment
    score: float

    @property
    def sure(self) -> bool:
        """Достаточно ли уверенно, чтобы объявить новое письмо новой версией."""
        return self.score >= SURE

    @property
    def maybe(self) -> bool:
        return MAYBE <= self.score < SURE


def normalize(title: str) -> str:
    lowered = _NOISE.sub(" ", (title or "").lower())
    return " ".join(re.sub(r"[^\w\s]", " ", lowered).split())


def title_similarity(left: str, right: str) -> float:
    first, second = normalize(left), normalize(right)
    if not first or not second:
        return 0.0
    return SequenceMatcher(None, first, second).ratio()


def score(
    *,
    same_domain: bool,
    same_kind: bool,
    title_left: str,
    title_right: str,
) -> float:
    return (
        (WEIGHT_DOMAIN if same_domain else 0.0)
        + (WEIGHT_KIND if same_kind else 0.0)
        + WEIGHT_TITLE * title_similarity(title_left, title_right)
    )


def _title_of(payload: dict[str, Any]) -> str:
    return str(payload.get("title") or payload.get("text") or "")


def find_match(
    commitments: CommitmentStore,
    evidence: EvidenceStore,
    *,
    kind: str,
    title: str,
    sender_domain: str | None,
    limit: int = 50,
) -> Match | None:
    """Найти самый похожий действующий факт. None — ничего похожего.

    Сопоставляемся только с действующими версиями: `superseded` уже заменено,
    `rejected` семья отвергла — возвращать их к жизни новым письмом нельзя.
    """
    best: Match | None = None
    for commitment in _live(commitments, limit=limit):
        domain = _domain_of(evidence, commitment)
        current = score(
            same_domain=bool(sender_domain) and domain == sender_domain,
            same_kind=commitment.kind == kind,
            title_left=title,
            title_right=_title_of(commitment.payload),
        )
        if best is None or current > best.score:
            best = Match(commitment=commitment, score=current)
    if best is None or best.score < MAYBE:
        return None
    return best


def _live(commitments: CommitmentStore, *, limit: int) -> list[Commitment]:
    rows = commitments.db.query(
        "SELECT * FROM commitments WHERE status IN (?, ?)"
        " ORDER BY recorded_at DESC LIMIT ?",
        (STATUS_CONFIRMED, STATUS_CANDIDATE, limit),
    )
    return [Commitment.from_row(row) for row in rows]


def _domain_of(evidence: EvidenceStore, commitment: Commitment) -> str | None:
    if commitment.extraction_run_id is None:
        return None
    refs = evidence.for_run(commitment.extraction_run_id)
    return refs[0].sender_domain if refs else None


# --- что именно изменилось ----------------------------------------------------

# Поля, изменение которых семья замечает и обсуждает.
TRACKED = ("start", "due_date", "amount_cents", "location", "title", "text")

LABELS = {
    "start": "начало",
    "due_date": "срок",
    "amount_cents": "сумма",
    "location": "место",
    "title": "название",
    "text": "текст",
}


def changes(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    """Что поменялось между версиями факта. Пусто — ничего значимого."""
    delta: dict[str, tuple[Any, Any]] = {}
    for field in TRACKED:
        was, now = previous.get(field), current.get(field)
        if was != now and (was is not None or now is not None):
            delta[field] = (was, now)
    return delta

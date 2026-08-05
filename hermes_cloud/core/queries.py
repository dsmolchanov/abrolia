"""Структурные ответы на вопросы семьи — без единой строки от модели.

Это те самые competency questions из плана: «какие платежи на этой неделе и кто
отвечает», «из какого письма взялась эта сумма», «что изменилось между первым
письмом и вторым». Они живут отдельным модулем и проверяются регрессионным
набором (`tests/test_competency.py`), потому что провал recall именно здесь —
единственный оговорённый повод заводить embeddings-индекс. Пока запросы
структурные, эта нужда не наступила.

Фильтрация по полям payload'а делается в Python, а не в SQL: `json_extract` —
чистая SQLite-специфика, а схема обязана переезжать в Postgres без переписывания
запросов.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from hermes_cloud.core.commitments import KIND_PAYMENT, Commitment, CommitmentStore
from hermes_cloud.core.evidence import EvidenceStore


@dataclass(frozen=True)
class DuePayment:
    commitment: Commitment
    due_date: date
    responsible: str | None
    amount_cents: int | None
    currency: str | None

    @property
    def text(self) -> str:
        return str(self.commitment.payload.get("text") or "")


def _due_date_of(commitment: Commitment) -> date | None:
    raw = commitment.payload.get("due_date")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        return None


def payments_due(
    commitments: CommitmentStore, *, start: date, end: date, limit: int = 100
) -> list[DuePayment]:
    """Подтверждённые платежи с дедлайном в окне, по возрастанию срока.

    Только `confirmed`: кандидат — это ещё не обязательство семьи, и назвать
    его платежом «на этой неделе» значит соврать.
    """
    found: list[DuePayment] = []
    for commitment in commitments.confirmed(kind=KIND_PAYMENT, limit=limit):
        due = _due_date_of(commitment)
        if due is None or not (start <= due <= end):
            continue
        payload = commitment.payload
        found.append(
            DuePayment(
                commitment=commitment,
                due_date=due,
                responsible=payload.get("responsible"),
                amount_cents=payload.get("amount_cents"),
                currency=payload.get("currency"),
            )
        )
    return sorted(found, key=lambda item: (item.due_date, item.commitment.id))


def source_of(
    evidence: EvidenceStore, commitment: Commitment
) -> str | None:
    """Откуда взялось это обязательство. None — прогон извлечения не записан."""
    if commitment.extraction_run_id is None:
        return None
    refs = evidence.for_run(commitment.extraction_run_id)
    if not refs:
        return None
    return evidence.render_source(refs[0])


# Поля, изменение которых семья замечает и обсуждает. Остальное — шум.
TRACKED_FIELDS = ("due_date", "start", "end", "amount_cents", "currency", "location", "text",
                  "title")


def version_delta(
    commitments: CommitmentStore, commitment_id: str
) -> dict[str, tuple[Any, Any]]:
    """Что изменилось между предыдущей версией факта и этой.

    Пустой словарь — либо версия первая, либо ничего значимого не поменялось.
    Ключ → (было, стало).
    """
    chain = commitments.chain(commitment_id)
    if len(chain) < 2:
        return {}
    previous, current = chain[-2].payload, chain[-1].payload
    delta: dict[str, tuple[Any, Any]] = {}
    for field in TRACKED_FIELDS:
        was, now = previous.get(field), current.get(field)
        if was != now and (was is not None or now is not None):
            delta[field] = (was, now)
    return delta

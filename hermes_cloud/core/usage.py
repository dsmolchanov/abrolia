"""Per-household/day cost caps with soft-limit degradation (Phase E E8).

Mechanism mirrors plan: token counter incremented after each extraction call,
soft limit checked before next model invocation. When exceeded, pipeline
degrades to extraction-only staged card with honest message, no model call.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime

from hermes_cloud.core.db import Database

# Pilot default: $5 / household / day. Configurable via HERMES_COST_CAP_USD_PER_DAY.
DEFAULT_DAILY_USD_CAP = 5.0
DEGRADED_MESSAGE = "дневной бюджет исчерпан — показываю только извлечённые карточки без AI-доработки"

# Pricing for USD estimate (claude-sonnet-5 pilot pricing). Cache read at 10%.
PRICE_PROMPT_PER_1K = 0.003  # $3 / 1M
PRICE_COMPLETION_PER_1K = 0.015  # $15 / 1M
PRICE_CACHE_READ_PER_1K = 0.0003  # $0.30 / 1M


def estimate_usd(*, prompt_tokens: int, completion_tokens: int, cache_read_tokens: int = 0) -> float:
    billable_prompt = max(0, prompt_tokens - cache_read_tokens)
    return (
        billable_prompt * PRICE_PROMPT_PER_1K / 1000.0
        + cache_read_tokens * PRICE_CACHE_READ_PER_1K / 1000.0
        + completion_tokens * PRICE_COMPLETION_PER_1K / 1000.0
    )


def today_utc() -> str:
    return datetime.now(UTC).date().isoformat()


@dataclass(frozen=True)
class UsageRow:
    household_id: str
    day: str
    prompt_tokens: int
    completion_tokens: int
    usd_estimate: float
    updated_at: float


class UsageStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, household_id: str, day: str) -> UsageRow | None:
        row = self.db.query_one(
            "SELECT * FROM usage_daily WHERE household_id = ? AND day = ?",
            (household_id, day),
        )
        if row is None:
            return None
        return UsageRow(
            household_id=row["household_id"],
            day=row["day"],
            prompt_tokens=int(row["prompt_tokens"]),
            completion_tokens=int(row["completion_tokens"]),
            usd_estimate=float(row["usd_estimate"]),
            updated_at=float(row["updated_at"]),
        )

    def is_over_budget(self, household_id: str, day: str, cap_usd: float = DEFAULT_DAILY_USD_CAP) -> bool:
        row = self.get(household_id, day)
        if row is None:
            return False
        return row.usd_estimate >= cap_usd

    def record(
        self,
        household_id: str,
        day: str,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        cache_read_tokens: int = 0,
        now: float | None = None,
    ) -> UsageRow:
        now = time.time() if now is None else now
        usd = estimate_usd(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cache_read_tokens=cache_read_tokens,
        )
        with self.db.write() as conn:
            existing = conn.execute(
                "SELECT prompt_tokens, completion_tokens, usd_estimate "
                "FROM usage_daily WHERE household_id = ? AND day = ?",
                (household_id, day),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO usage_daily "
                    "(household_id, day, prompt_tokens, completion_tokens, usd_estimate, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (household_id, day, prompt_tokens, completion_tokens, usd, now),
                )
            else:
                conn.execute(
                    "UPDATE usage_daily SET prompt_tokens = prompt_tokens + ?, "
                    "completion_tokens = completion_tokens + ?, "
                    "usd_estimate = usd_estimate + ?, updated_at = ? "
                    "WHERE household_id = ? AND day = ?",
                    (prompt_tokens, completion_tokens, usd, now, household_id, day),
                )
        row = self.get(household_id, day)
        assert row is not None
        return row

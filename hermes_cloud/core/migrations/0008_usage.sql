-- Phase 5 cost caps: per-household/day token usage for soft-limit degradation.
-- household_id is logical tenant key (matches Household.household_id / runtime manifest).
CREATE TABLE IF NOT EXISTS usage_daily (
    household_id TEXT NOT NULL,
    day TEXT NOT NULL, -- YYYY-MM-DD UTC
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    usd_estimate REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (household_id, day)
);

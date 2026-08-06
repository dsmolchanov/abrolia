-- Phase 5 pilotization: household channel preferences (canon Phase 5, §3)
-- MVP writes household-level row; schema allows post-MVP per-actor overrides.
-- Fallback must be verified owner contact email, never equal to agent inbox.

CREATE TABLE IF NOT EXISTS channel_preferences (
    subject_type TEXT NOT NULL CHECK (subject_type IN ('household', 'actor')),
    subject_id TEXT NOT NULL,
    primary_channel TEXT NOT NULL CHECK (primary_channel IN ('telegram', 'whatsapp', 'web')),
    fallback_channel TEXT NOT NULL CHECK (fallback_channel IN ('email')),
    verified_at REAL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (subject_type, subject_id),
    FOREIGN KEY (subject_id) REFERENCES households (id) ON DELETE CASCADE
);

-- Ensure only household rows in MVP; actor rows allowed post-MVP but must reference valid membership
CREATE INDEX IF NOT EXISTS channel_preferences_household
    ON channel_preferences (subject_id) WHERE subject_type = 'household';

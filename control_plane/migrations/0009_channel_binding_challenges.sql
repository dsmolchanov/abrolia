-- Phase C3: the challenge that has to be answered before a channel binding exists.
--
-- `channel_bindings` (0007) has carried `verified_at`/`verified_by_actor_id`
-- since it was created, and until now nothing in production wrote a row: the
-- columns recorded a verification that no code performed. This table is the
-- missing half — the outstanding question, so that a row in `channel_bindings`
-- can only be the answer to one.
--
-- The code itself is NEVER stored. `code_hash` is a keyed LookupHasher digest,
-- the same treatment `auth_tokens.token_hash` gives a magic link, because a
-- challenge code is a bearer credential for joining a household.
CREATE TABLE IF NOT EXISTS channel_binding_challenges (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households (id) ON DELETE CASCADE,
    channel TEXT NOT NULL CHECK (channel IN ('telegram', 'whatsapp', 'web')),
    external_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    -- A challenge can mint a second adult; it can never mint a second owner.
    -- The owner's own binding is seeded by the planner from verified
    -- onboarding, which is the only path that carries account ownership.
    role TEXT NOT NULL CHECK (role IN ('adult')),
    code_hash TEXT NOT NULL,
    issued_by_actor_id TEXT NOT NULL,
    expires_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    consumed_at REAL,
    created_at REAL NOT NULL
);

-- Verification looks a challenge up by its code alone: the person answering
-- holds the code, not the row id.
CREATE UNIQUE INDEX IF NOT EXISTS channel_binding_challenges_code
    ON channel_binding_challenges (code_hash);

CREATE INDEX IF NOT EXISTS channel_binding_challenges_household
    ON channel_binding_challenges (household_id, channel, external_id);

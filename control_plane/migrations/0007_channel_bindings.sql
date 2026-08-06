-- Phase 5 channel bindings (once-owner-authorized) — sender stored as HMAC (keyed) + plain for migration
CREATE TABLE IF NOT EXISTS channel_bindings (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households (id) ON DELETE CASCADE,
    channel TEXT NOT NULL CHECK (channel IN ('telegram', 'whatsapp', 'web')),
    external_id TEXT NOT NULL,
    external_id_hmac TEXT,
    actor_id TEXT NOT NULL,
    role TEXT NOT NULL,
    verified_at REAL NOT NULL,
    verified_by_actor_id TEXT NOT NULL,
    UNIQUE (household_id, channel, external_id)
);
CREATE INDEX IF NOT EXISTS channel_bindings_lookup ON channel_bindings (channel, external_id);
CREATE INDEX IF NOT EXISTS channel_bindings_hmac_lookup ON channel_bindings (channel, external_id_hmac);

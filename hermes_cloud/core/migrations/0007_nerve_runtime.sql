-- Durable Nerve webhook journal and short-lived attachment materialization.
-- Provider credentials and webhook secrets never enter SQLite.

CREATE TABLE IF NOT EXISTS nerve_webhook_events (
    id                    TEXT PRIMARY KEY,
    binding_identity_id   TEXT NOT NULL,
    binding_revision      INTEGER NOT NULL,
    org_id                TEXT NOT NULL,
    inbox_id              TEXT NOT NULL,
    thread_id             TEXT NOT NULL,
    message_id            TEXT NOT NULL,
    attachment_count      INTEGER NOT NULL,
    payload               BLOB NOT NULL,
    payload_sha256        TEXT NOT NULL,
    signature_sha256      TEXT NOT NULL UNIQUE,
    webhook_timestamp     INTEGER NOT NULL,
    state                 TEXT NOT NULL, -- queued|processing|materialized|dlq
    attempts              INTEGER NOT NULL DEFAULT 0,
    lease_until           REAL,
    leased_by             TEXT,
    canonical_event_id    TEXT REFERENCES events (id) ON DELETE SET NULL,
    last_error_code       TEXT,
    received_at           REAL NOT NULL,
    updated_at            REAL NOT NULL,
    UNIQUE (binding_identity_id, binding_revision, message_id)
);

CREATE INDEX IF NOT EXISTS nerve_webhook_queue
    ON nerve_webhook_events (state, received_at);

CREATE TABLE IF NOT EXISTS nerve_webhook_signatures (
    signature_sha256 TEXT PRIMARY KEY,
    nerve_event_id   TEXT NOT NULL REFERENCES nerve_webhook_events (id)
                     ON DELETE CASCADE,
    seen_at          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS nerve_attachments (
    id                     TEXT PRIMARY KEY,
    nerve_event_id         TEXT NOT NULL REFERENCES nerve_webhook_events (id)
                           ON DELETE CASCADE,
    message_id             TEXT NOT NULL,
    provider_attachment_id TEXT NOT NULL,
    filename               TEXT NOT NULL,
    content_type           TEXT NOT NULL,
    expected_size          INTEGER,
    actual_size            INTEGER NOT NULL,
    content_sha256         TEXT NOT NULL,
    classification         TEXT NOT NULL,
    content                BLOB NOT NULL,
    retention_until        REAL NOT NULL,
    created_at             REAL NOT NULL,
    UNIQUE (nerve_event_id, provider_attachment_id)
);

CREATE INDEX IF NOT EXISTS nerve_attachment_retention
    ON nerve_attachments (retention_until);

CREATE TABLE IF NOT EXISTS nerve_runtime_health (
    binding_identity_id TEXT NOT NULL,
    binding_revision    INTEGER NOT NULL,
    last_webhook_at     REAL,
    last_materialized_at REAL,
    credential_state    TEXT NOT NULL DEFAULT 'unknown', -- unknown|valid|revoked
    last_error_code     TEXT,
    updated_at          REAL NOT NULL,
    PRIMARY KEY (binding_identity_id, binding_revision)
);

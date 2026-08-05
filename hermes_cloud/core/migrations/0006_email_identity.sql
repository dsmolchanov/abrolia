-- Provider-neutral runtime email identity, ingress and delivery state.
-- Secrets are deliberately absent: only public identifiers and secret names live here.

CREATE TABLE IF NOT EXISTS email_bindings (
    identity_id          TEXT NOT NULL,
    revision             INTEGER NOT NULL,
    provider             TEXT NOT NULL,
    address              TEXT NOT NULL,
    provider_ref         TEXT,
    secret_names_json    TEXT NOT NULL DEFAULT '[]',
    state                TEXT NOT NULL, -- active|superseded|needs_attention|deleted
    activated_at         REAL NOT NULL,
    updated_at           REAL NOT NULL,
    PRIMARY KEY (identity_id, revision)
);

CREATE UNIQUE INDEX IF NOT EXISTS email_bindings_one_active
    ON email_bindings (state) WHERE state = 'active';

CREATE TABLE IF NOT EXISTS email_ingress_receipts (
    id                    TEXT PRIMARY KEY,
    binding_identity_id   TEXT,
    binding_revision      INTEGER,
    source                TEXT NOT NULL,
    provider_event_id     TEXT NOT NULL,
    canonical_message_id  TEXT,
    content_sha256        TEXT NOT NULL,
    event_id              TEXT NOT NULL REFERENCES events (id) ON DELETE CASCADE,
    state                 TEXT NOT NULL, -- materialized
    received_at           REAL NOT NULL,
    created_at            REAL NOT NULL,
    UNIQUE (source, provider_event_id)
);

CREATE INDEX IF NOT EXISTS email_ingress_event
    ON email_ingress_receipts (event_id);

CREATE TABLE IF NOT EXISTS oauth_grants (
    binding_identity_id          TEXT NOT NULL,
    binding_revision             INTEGER NOT NULL,
    encrypted_refresh_credential BLOB NOT NULL,
    key_version                  INTEGER NOT NULL,
    provider_subject             TEXT NOT NULL,
    scopes_json                  TEXT NOT NULL,
    created_at                   REAL NOT NULL,
    updated_at                   REAL NOT NULL,
    revoked_at                   REAL,
    PRIMARY KEY (binding_identity_id, binding_revision)
);

CREATE TABLE IF NOT EXISTS email_sync_state (
    binding_identity_id TEXT NOT NULL,
    binding_revision    INTEGER NOT NULL,
    cursor              TEXT,
    connected_at        REAL NOT NULL,
    last_success_at     REAL,
    backoff_until       REAL,
    health              TEXT NOT NULL,
    updated_at          REAL NOT NULL,
    PRIMARY KEY (binding_identity_id, binding_revision)
);

CREATE TABLE IF NOT EXISTS email_sends (
    effect_id                TEXT PRIMARY KEY,
    approval_id              TEXT NOT NULL,
    binding_identity_id      TEXT NOT NULL,
    binding_revision         INTEGER NOT NULL,
    request_sha256           TEXT NOT NULL,
    provider_idempotency_key TEXT NOT NULL UNIQUE,
    state                    TEXT NOT NULL, -- pending|accepted|failed|outcome_unknown
    message_id               TEXT NOT NULL,
    provider_ref             TEXT,
    error_code               TEXT,
    created_at               REAL NOT NULL,
    updated_at               REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS email_sends_binding
    ON email_sends (binding_identity_id, binding_revision, created_at);

CREATE TABLE IF NOT EXISTS email_delivery_receipts (
    effect_id       TEXT PRIMARY KEY REFERENCES email_sends (effect_id) ON DELETE CASCADE,
    approval_id     TEXT NOT NULL,
    message_id      TEXT NOT NULL,
    provider_ref    TEXT,
    state           TEXT NOT NULL,
    accepted_at     REAL,
    reconciled_at   REAL,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);

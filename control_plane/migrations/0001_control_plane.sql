-- Abrolia metadata-only onboarding control plane. This schema is deliberately
-- separate from hermes_cloud/core/migrations (one database per household).

CREATE TABLE accounts (
    id TEXT PRIMARY KEY,
    recovery_email_lookup_hmac TEXT NOT NULL UNIQUE,
    recovery_email_ciphertext BLOB NOT NULL,
    encryption_key_version TEXT NOT NULL,
    email_verified_at REAL NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active','locked','deleting','deleted')),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE households (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN (
        'draft','onboarding','provisioning','active','deleting','deleted'
    )),
    family_language TEXT,
    timezone TEXT,
    country_code TEXT,
    residency_mode TEXT NOT NULL DEFAULT 'eu-app'
        CHECK (residency_mode IN ('eu-app','eu-strict')),
    current_config_revision INTEGER NOT NULL DEFAULT 0 CHECK (current_config_revision >= 0),
    runtime_ref TEXT,
    runtime_deleted_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    deleted_at REAL
);

CREATE TABLE household_profiles (
    household_id TEXT PRIMARY KEY REFERENCES households (id) ON DELETE CASCADE,
    first_name_ciphertext BLOB NOT NULL,
    last_name_ciphertext BLOB NOT NULL,
    encryption_key_version TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE household_memberships (
    account_id TEXT NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    household_id TEXT NOT NULL REFERENCES households (id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('owner','adult')),
    status TEXT NOT NULL CHECK (status IN ('invited','active','revoked')),
    created_at REAL NOT NULL,
    accepted_at REAL,
    revoked_at REAL,
    PRIMARY KEY (account_id, household_id)
);
CREATE INDEX memberships_household_status
    ON household_memberships (household_id, status);

CREATE TABLE auth_tokens (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    purpose TEXT NOT NULL CHECK (purpose IN ('invite','login','reauth')),
    account_id TEXT REFERENCES accounts (id) ON DELETE CASCADE,
    email_lookup_hmac TEXT,
    email_reference_ciphertext BLOB,
    encryption_key_version TEXT,
    expires_at REAL NOT NULL,
    used_at REAL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    created_at REAL NOT NULL
);
CREATE INDEX auth_tokens_expiry ON auth_tokens (expires_at, used_at);

CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    account_id TEXT NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    csrf_hash TEXT NOT NULL,
    idle_expires_at REAL NOT NULL,
    absolute_expires_at REAL NOT NULL,
    reauthenticated_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    revoked_at REAL,
    security_metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    CHECK (idle_expires_at <= absolute_expires_at)
);
CREATE INDEX sessions_account_active ON sessions (account_id, revoked_at);
CREATE INDEX sessions_expiry ON sessions (absolute_expires_at, idle_expires_at);

CREATE TABLE rate_limit_buckets (
    bucket_hmac TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    window_started_at REAL NOT NULL,
    attempts INTEGER NOT NULL CHECK (attempts >= 0),
    updated_at REAL NOT NULL
);

CREATE TABLE onboarding_workflows (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL UNIQUE REFERENCES households (id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK (state IN (
        'profile_required','in_progress','runtime_provisioning','activating','complete','cancelled'
    )),
    current_step TEXT NOT NULL CHECK (current_step IN (
        'profile','email_identity','whatsapp_identity','primary_channel','runtime'
    )),
    version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    completed_at REAL
);

CREATE TABLE onboarding_steps (
    workflow_id TEXT NOT NULL REFERENCES onboarding_workflows (id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN (
        'profile','email_identity','whatsapp_identity','primary_channel'
    )),
    ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 0 AND 3),
    status TEXT NOT NULL CHECK (status IN (
        'locked','available','selected','provisioning','waiting_user',
        'verifying','verified','failed','cancelled'
    )),
    selection_kind TEXT,
    selection_ciphertext BLOB,
    result_ciphertext BLOB,
    encryption_key_version TEXT,
    public_status_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT,
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    updated_at REAL NOT NULL,
    PRIMARY KEY (workflow_id, kind),
    UNIQUE (workflow_id, ordinal),
    CHECK (status != 'verified' OR result_ciphertext IS NOT NULL OR kind = 'profile'),
    CHECK (status NOT IN ('locked','available') OR result_ciphertext IS NULL)
);

CREATE TABLE onboarding_transitions (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES onboarding_workflows (id) ON DELETE CASCADE,
    workflow_version INTEGER NOT NULL,
    command TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    step_kind TEXT,
    from_step_status TEXT,
    to_step_status TEXT,
    account_id TEXT NOT NULL REFERENCES accounts (id) ON DELETE RESTRICT,
    session_id TEXT REFERENCES sessions (id) ON DELETE SET NULL,
    request_id TEXT NOT NULL,
    related_job_id TEXT,
    redacted_metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    UNIQUE (workflow_id, workflow_version)
);

CREATE TABLE idempotency_requests (
    account_id TEXT NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    route TEXT NOT NULL,
    idempotency_key_hmac TEXT NOT NULL,
    request_sha TEXT NOT NULL,
    response_status INTEGER NOT NULL,
    response_body_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    PRIMARY KEY (account_id, route, idempotency_key_hmac)
);
CREATE INDEX idempotency_expiry ON idempotency_requests (expires_at);

CREATE TABLE provisioning_jobs (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households (id) ON DELETE CASCADE,
    workflow_id TEXT NOT NULL REFERENCES onboarding_workflows (id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN (
        'email_identity','whatsapp_identity','channel_binding','runtime','cleanup','bootstrap_cleanup'
    )),
    operation TEXT NOT NULL,
    intent_key TEXT NOT NULL UNIQUE,
    desired_revision INTEGER,
    request_sha TEXT NOT NULL,
    request_ciphertext BLOB NOT NULL,
    encryption_key_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'pending','running','waiting_user','succeeded','failed','outcome_unknown','cancelled'
    )),
    provider TEXT NOT NULL,
    external_ref_ciphertext BLOB,
    result_ciphertext BLOB,
    error_code TEXT,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    not_before REAL,
    lease_until REAL,
    leased_by TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    settled_at REAL
);
CREATE INDEX provisioning_jobs_available
    ON provisioning_jobs (status, not_before, lease_until, created_at);
CREATE INDEX provisioning_jobs_household ON provisioning_jobs (household_id, status);

CREATE TABLE external_resources (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households (id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    stable_name TEXT NOT NULL,
    external_id_ciphertext BLOB NOT NULL,
    encryption_key_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'planned','creating','ready','deleting','deleted','outcome_unknown'
    )),
    config_revision INTEGER,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (provider, resource_type, stable_name)
);

CREATE TABLE config_revisions (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households (id) ON DELETE CASCADE,
    revision INTEGER NOT NULL CHECK (revision > 0),
    schema_version INTEGER NOT NULL CHECK (schema_version > 0),
    manifest_ciphertext BLOB NOT NULL,
    encryption_key_version TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'planned','issued','claimed','active','superseded','revoked'
    )),
    created_at REAL NOT NULL,
    issued_at REAL,
    claimed_at REAL,
    activated_at REAL,
    UNIQUE (household_id, revision),
    UNIQUE (household_id, manifest_sha256)
);

CREATE TRIGGER config_revisions_immutable_payload
BEFORE UPDATE OF household_id, revision, schema_version, manifest_ciphertext,
    encryption_key_version, manifest_sha256 ON config_revisions
BEGIN
    SELECT RAISE(ABORT, 'config revision payload is immutable');
END;

CREATE TABLE bootstrap_tokens (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    household_id TEXT NOT NULL REFERENCES households (id) ON DELETE CASCADE,
    runtime_ref TEXT NOT NULL,
    config_revision INTEGER NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    expires_at REAL NOT NULL,
    claimed_at REAL,
    used_at REAL,
    revoked_at REAL,
    created_at REAL NOT NULL,
    FOREIGN KEY (household_id, config_revision)
        REFERENCES config_revisions (household_id, revision) ON DELETE CASCADE
);
CREATE INDEX bootstrap_tokens_expiry ON bootstrap_tokens (expires_at, used_at, revoked_at);

CREATE TABLE consent_receipts (
    id TEXT PRIMARY KEY,
    household_id TEXT REFERENCES households (id) ON DELETE SET NULL,
    account_id TEXT REFERENCES accounts (id) ON DELETE SET NULL,
    purpose TEXT NOT NULL,
    text_version TEXT NOT NULL,
    text_sha256 TEXT NOT NULL,
    locale TEXT NOT NULL,
    accepted_at REAL NOT NULL,
    revoked_at REAL,
    created_at REAL NOT NULL
);
CREATE INDEX consent_receipts_household ON consent_receipts (household_id, purpose);

CREATE TABLE deletion_tombstones (
    household_id_hmac TEXT PRIMARY KEY,
    deleted_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    completion_status TEXT NOT NULL CHECK (completion_status IN (
        'complete','partial','outcome_unknown'
    )),
    created_at REAL NOT NULL
);

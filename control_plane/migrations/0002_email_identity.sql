CREATE TABLE email_identities (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households (id) ON DELETE CASCADE,
    option TEXT NOT NULL CHECK (option IN ('managed_abrolia','gmail','own_domain')),
    status TEXT NOT NULL CHECK (status IN (
        'selected','provisioning','waiting_user','verified','activating','active',
        'needs_attention','disconnecting','deleted','outcome_unknown'
    )),
    address_ciphertext BLOB,
    address_lookup_hmac TEXT,
    address_masked TEXT,
    provider_subject_ciphertext BLOB,
    provider_resource_refs_json TEXT NOT NULL DEFAULT '{}',
    secret_binding_ref TEXT,
    granted_scopes_json TEXT,
    encryption_key_version TEXT NOT NULL,
    verified_at REAL,
    activated_at REAL,
    disconnected_at REAL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    CHECK ((address_ciphertext IS NULL) = (address_lookup_hmac IS NULL)),
    CHECK ((address_ciphertext IS NULL) = (address_masked IS NULL))
);
CREATE UNIQUE INDEX email_identities_one_live_household
    ON email_identities (household_id)
    WHERE status IN (
        'selected','provisioning','waiting_user','verified','activating','active',
        'needs_attention','outcome_unknown'
    );
CREATE UNIQUE INDEX email_identities_live_address
    ON email_identities (address_lookup_hmac)
    WHERE address_lookup_hmac IS NOT NULL AND status NOT IN ('disconnecting','deleted');

CREATE TABLE email_address_reservations (
    id TEXT PRIMARY KEY,
    normalized_domain TEXT NOT NULL,
    normalized_local_part TEXT NOT NULL,
    household_id TEXT NOT NULL REFERENCES households (id) ON DELETE CASCADE,
    email_identity_id TEXT NOT NULL REFERENCES email_identities (id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('held','consumed','released','expired')),
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL,
    consumed_at REAL,
    UNIQUE (normalized_domain, normalized_local_part)
);
CREATE INDEX email_reservations_expiry
    ON email_address_reservations (status, expires_at);

CREATE TABLE oauth_transactions (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households (id) ON DELETE CASCADE,
    onboarding_session_id TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    account_id TEXT NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,
    state_hash TEXT NOT NULL UNIQUE,
    pkce_verifier_ciphertext BLOB NOT NULL,
    requested_scopes_json TEXT NOT NULL,
    workflow_version INTEGER NOT NULL,
    encryption_key_version TEXT NOT NULL,
    expires_at REAL NOT NULL,
    consumed_at REAL,
    failed_at REAL,
    created_at REAL NOT NULL
);
CREATE INDEX oauth_transactions_expiry
    ON oauth_transactions (expires_at, consumed_at, failed_at);

CREATE TABLE email_activation_receipts (
    email_identity_id TEXT NOT NULL REFERENCES email_identities (id) ON DELETE CASCADE,
    desired_revision INTEGER NOT NULL CHECK (desired_revision > 0),
    runtime_ref TEXT NOT NULL,
    provider TEXT NOT NULL,
    inbound_check TEXT NOT NULL CHECK (inbound_check IN ('pending','healthy','failed')),
    outbound_check TEXT NOT NULL CHECK (outbound_check IN ('pending','healthy','failed')),
    checked_at REAL NOT NULL,
    receipt_digest TEXT NOT NULL CHECK (length(receipt_digest) = 64),
    status TEXT NOT NULL CHECK (status IN ('activating','active','needs_attention')),
    PRIMARY KEY (email_identity_id, desired_revision)
);

ALTER TABLE oauth_transactions ADD COLUMN email_identity_id TEXT REFERENCES email_identities (id) ON DELETE CASCADE;
ALTER TABLE oauth_transactions ADD COLUMN provider_subject_ciphertext BLOB;
ALTER TABLE oauth_transactions ADD COLUMN address_ciphertext BLOB;
ALTER TABLE oauth_transactions ADD COLUMN granted_scopes_json TEXT;
ALTER TABLE oauth_transactions ADD COLUMN secret_binding_ref TEXT;
ALTER TABLE oauth_transactions ADD COLUMN credential_digest TEXT;
ALTER TABLE oauth_transactions ADD COLUMN callback_at REAL;
ALTER TABLE oauth_transactions ADD COLUMN confirmed_at REAL;
ALTER TABLE oauth_transactions ADD COLUMN revoked_at REAL;
ALTER TABLE oauth_transactions ADD COLUMN revoke_requested_at REAL;
ALTER TABLE oauth_transactions ADD COLUMN revoke_completed_at REAL;

CREATE UNIQUE INDEX oauth_transactions_one_live_identity
    ON oauth_transactions (email_identity_id)
    WHERE failed_at IS NULL AND revoked_at IS NULL;

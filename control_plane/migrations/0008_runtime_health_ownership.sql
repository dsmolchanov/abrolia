ALTER TABLE email_activation_receipts
    ADD COLUMN runtime_health_status TEXT NOT NULL DEFAULT 'active'
    CHECK (runtime_health_status IN ('active','needs_attention'));

ALTER TABLE email_activation_receipts
    ADD COLUMN runtime_health_checked_at REAL NOT NULL DEFAULT 0;

ALTER TABLE email_activation_receipts
    ADD COLUMN runtime_health_owns_attention INTEGER NOT NULL DEFAULT 0
    CHECK (runtime_health_owns_attention IN (0, 1));

ALTER TABLE email_activation_receipts
    ADD COLUMN runtime_health_identity_version INTEGER;

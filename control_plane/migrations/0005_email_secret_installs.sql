-- Durable non-secret receipt for email provider secret handoff (B-02).
-- After SecretSink.install succeeds but before durable verified projection, we record
-- a receipt that can be inspected without the secret value. This allows a reclaimed
-- lease to converge to verified if the sink already contains the generation.

CREATE TABLE IF NOT EXISTS email_secret_installs (
    job_id TEXT PRIMARY KEY REFERENCES provisioning_jobs (id) ON DELETE CASCADE,
    household_id TEXT NOT NULL REFERENCES households (id) ON DELETE CASCADE,
    secret_name TEXT NOT NULL CHECK (secret_name GLOB '[A-Z]*'),
    namespace_ref TEXT NOT NULL,
    installed_at REAL NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS email_secret_installs_household_name
    ON email_secret_installs (household_id, secret_name);

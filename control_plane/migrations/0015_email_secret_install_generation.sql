-- Generation-scoped convergence for the email secret handoff (B-02 closure).
--
-- `0005` recorded a receipt keyed by `job_id` alone, and the live fallback
-- asked the sink only whether the BINDING NAME was present. Both answer a
-- weaker question than the one that matters. If generation N-1's secret
-- survives in the namespace — an incomplete teardown, a crash window — and
-- generation N's job reconciles with empty one-time material, a name-only
-- probe answers "installed", a receipt is written for N, and N is marked
-- verified while the runtime still holds N-1's credentials. Verified-but-stale,
-- on the P0 real-email path.
--
-- The generation is the provisioning job that installed the material: stable
-- across that job's retries and reconciliations, so a crash after the sink
-- write still converges, and necessarily different for a later re-provisioning,
-- so N-1's remains cannot answer for N.
--
-- `marker_name` is what makes the live probe generation-scoped. The Fly sink
-- can attest that a secret NAME exists and nothing about its value, so the
-- generation is carried in a companion marker's name and proven the only way
-- this sink can prove anything.
--
-- `sink_digest` is deliberately NOT a digest of the secret value: no value ever
-- reaches this table or this process after installation. It digests what was
-- actually attested — the namespace and the marker that proves the generation —
-- so a receipt can be checked against the sink it claims to describe.

ALTER TABLE email_secret_installs ADD COLUMN generation TEXT NOT NULL DEFAULT '';
ALTER TABLE email_secret_installs ADD COLUMN marker_name TEXT NOT NULL DEFAULT '';
ALTER TABLE email_secret_installs ADD COLUMN sink_digest TEXT NOT NULL DEFAULT '';

-- Rows written by `0005` carry no generation and must never satisfy a
-- generation check. The empty default is that: `_email_secret_installed`
-- compares against the current job's generation, which is never empty, so a
-- legacy receipt is inert rather than dangerously permissive.
CREATE INDEX IF NOT EXISTS email_secret_installs_generation
    ON email_secret_installs (household_id, generation);

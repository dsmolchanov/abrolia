-- Make a failing boot archive visible somewhere other than stderr.
--
-- Three individually correct decisions compose into invisibility:
--
--   * #111 made the deploy gate excuse `backup_stale`, because a stale backup
--     is a reason TO deploy and gating on it deadlocked production for nine
--     days;
--   * #118 made the boot archive non-fatal, because a full volume must not
--     turn into an outage;
--   * the skip reason went only to the container's stderr.
--
-- Together: a writer that fails at every boot looks exactly like a healthy
-- system. `/healthz` reported `healthy` with no blockers while production had
-- had no restore point for ten days, and the only trace was a log line nobody
-- reads until they already suspect the answer.
--
-- `backup_stale` cannot carry this by itself, because it conflates "no deploy
-- lately" — benign, and the whole reason it stopped blocking — with "the
-- writer is broken", which is not benign at all. This table separates them by
-- recording what the last attempt actually did.
--
-- One row, by construction: only the most recent attempt is actionable, and a
-- growing table on a 1 GiB volume would be its own problem.

CREATE TABLE IF NOT EXISTS boot_archive_attempts (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    attempted_at REAL NOT NULL,
    outcome TEXT NOT NULL CHECK (
        outcome IN ('written', 'skipped_interval', 'failed')
    ),
    -- Non-secret by construction: a reason string and a path, never a value.
    -- The archive's own contents never reach this process after installation.
    detail TEXT NOT NULL DEFAULT ''
);

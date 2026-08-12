-- Timestamp paired with the last durable Gmail History cursor. It bounds an
-- expired-history resync to an overlap window instead of scanning the inbox.
ALTER TABLE email_sync_state ADD COLUMN cursor_observed_at REAL;

UPDATE email_sync_state
SET cursor_observed_at = COALESCE(last_success_at, connected_at, updated_at)
WHERE cursor_observed_at IS NULL;

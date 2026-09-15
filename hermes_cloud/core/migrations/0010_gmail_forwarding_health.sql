-- Forwarding health (plan Phase 4): the daily check message the runtime sends
-- from the relay to the agent Gmail, whether it came back, and when forwarding
-- was declared stale. Tokens are HMAC digests of a date, not credentials.
ALTER TABLE gmail_forwarding_state ADD COLUMN last_check_sent_at REAL;
ALTER TABLE gmail_forwarding_state ADD COLUMN last_check_token TEXT;
ALTER TABLE gmail_forwarding_state ADD COLUMN last_check_seen_token TEXT;
ALTER TABLE gmail_forwarding_state ADD COLUMN last_check_miss_counted INTEGER NOT NULL DEFAULT 0;
ALTER TABLE gmail_forwarding_state ADD COLUMN misses INTEGER NOT NULL DEFAULT 0;
ALTER TABLE gmail_forwarding_state ADD COLUMN check_requested_at REAL;
ALTER TABLE gmail_forwarding_state ADD COLUMN stale_since REAL;
ALTER TABLE gmail_forwarding_state ADD COLUMN stale_notified_at REAL;

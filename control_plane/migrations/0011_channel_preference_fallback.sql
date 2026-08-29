-- C4a: the fallback a preference names is a REFERENCE, not a second copy.
--
-- `channel_preferences` has had `fallback_channel = 'email'` since 0006 and no
-- way to say WHICH email. The repository took the address as an argument and
-- compared it against another argument, so the rule "the fallback is a
-- verified owner contact and never an agent inbox" was enforced by whoever
-- called it — and nothing ever called it.
--
-- The column added here holds the ACCOUNT whose verified contact address is
-- the fallback, not the address. Copying the address would put one identity in
-- two tables, which is the defect the whole C3a debt plan is about: the copy
-- goes stale, and the stale copy is the one that fails closed nowhere.
-- `accounts.recovery_email_ciphertext` stays the only place the address lives,
-- `accounts.email_verified_at` is why it may be used at all, and
-- `accounts.recovery_email_lookup_hmac` is what the self-ingestion check
-- compares — against `email_identities.address_lookup_hmac`, under the same
-- key, so the comparison needs no decryption and no caller-supplied value.
ALTER TABLE channel_preferences ADD COLUMN fallback_account_id TEXT
    REFERENCES accounts (id);

-- Required in fact, nullable in the schema, for the reason 0010 states: SQLite
-- cannot add a NOT NULL column to a populated table without inventing a
-- default, and a default here would be a lie that reads as data. A trigger is
-- how this repository has expressed "required" since 0003.
--
-- Nothing is at risk from making it required immediately: `channel_preferences`
-- has never had a production writer — the audit that opened the go-live
-- checklist recorded "zero consumers/readers ... no write path" — so the table
-- is empty in every deployment and no row can be caught out by this.
CREATE TRIGGER channel_preferences_fallback_account_required
BEFORE INSERT ON channel_preferences
WHEN NEW.fallback_channel = 'email' AND NEW.fallback_account_id IS NULL
BEGIN
    SELECT RAISE(ABORT, 'channel_preferences.fallback_account_id is required');
END;

CREATE TRIGGER channel_preferences_fallback_account_required_on_update
BEFORE UPDATE ON channel_preferences
WHEN NEW.fallback_channel = 'email' AND NEW.fallback_account_id IS NULL
BEGIN
    SELECT RAISE(ABORT, 'channel_preferences.fallback_account_id is required');
END;

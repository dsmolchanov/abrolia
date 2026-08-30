-- C3f: which ACCOUNT speaks through this binding, on a channel that has no
-- gateway to identify a sender.
--
-- On Telegram and WhatsApp the sender identifies itself: a message arrives
-- carrying `message.from.id` or a normalized `+999…`, and `external_id` is what
-- the gateway matches it against. Web has no such thing. The turn arrives on an
-- authenticated session, and the only identity in hand is the account the
-- session belongs to — which `channel_bindings` could not express, so
-- `RuntimeService._web_chat` refused every role but `owner` and
-- `web_chat_turn` hardcoded `manifest.actors.owner`. A second adult holding a
-- verified, published web binding could not use web chat at all.
--
-- WHY NOT `external_id`. That was this slice's first decision and it was wrong.
-- `_insert` calls `_reject_actor_that_is_not_the_sender` for every write, no
-- path excepted, so `external_id = account_id` forces `actor_id = account_id`
-- too. The owner would then hold a second actor identity beside their
-- primary-channel one — one person twice in `actors.family` — and
-- `role_for()` would stop matching `actors.owner`, silently downgrading the
-- owner to `ROLE_FAMILY` on web and taking export and deletion with it. Wiring
-- a channel must not change anybody's privileges.
--
-- So `external_id` and `actor_id` keep meaning exactly what they mean
-- everywhere else — the person's own identity — and the account mapping gets
-- the column it always needed.
--
-- NULLABLE, deliberately. Telegram and WhatsApp bindings have no account: they
-- are identified by their sender. Only a web seat carries one, and the partial
-- unique index says so precisely — one seat per account per channel per
-- household, and no constraint at all on the rows that legitimately have none.
ALTER TABLE channel_bindings ADD COLUMN account_id TEXT REFERENCES accounts (id);

CREATE UNIQUE INDEX IF NOT EXISTS channel_bindings_account_seat
    ON channel_bindings (household_id, channel, account_id)
    WHERE account_id IS NOT NULL;

-- The challenge needs the same column, because the OWNER redeems the code.
--
-- `verify_binding_challenge` is gated by `current_household_owner_mutation`, so
-- the principal at redemption time is the owner rather than the member being
-- bound. There is therefore no moment at which the adult's own session could
-- supply their account, and the seat has to learn it when the owner names the
-- member they are inviting — which is at issue time, here.
ALTER TABLE channel_binding_challenges ADD COLUMN account_id TEXT REFERENCES accounts (id);

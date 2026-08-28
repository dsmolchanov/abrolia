-- C3a: separate a sender's IDENTITY from the CHAT it speaks in.
--
-- `channel_bindings.external_id` has been asked two incompatible questions
-- since 0007. `gateway/whatsapp_router.py` matches it against an incoming
-- SENDER; `control_plane/provisioning/planner.py` projected it as the
-- manifest's `chat_id`, the conversation the assistant SPEAKS IN. For the
-- owner the two coincide, because onboarding's `primary_channel` step wrote
-- one value that happens to answer both. For a second member on the same
-- channel they cannot, and there was no third option — which is why
-- `_reject_unrepresentable_member` refused an adult on the primary channel
-- outright rather than write a row whose deployment would fail later.
--
-- After this migration each column answers exactly one question:
--
--   external_id -- the SENDER's identity on this channel (Telegram user ID,
--                  WhatsApp phone, web account ref). What the gateway matches.
--   chat_id     -- the CONVERSATION this binding speaks in. What the manifest
--                  projects and `verified_actor_chat_pairs` pairs with an actor.
--
-- UNIQUENESS IS UNCHANGED, AND DELIBERATELY SO. `UNIQUE (household_id,
-- channel, external_id)` from 0007 already says exactly what C3a wants it to
-- say — one sender identity is one binding — so there is no index to replace,
-- and adding a second index over the same three columns would buy a b-tree on
-- every write and nothing else. What changes is the MEANING: `chat_id` is not
-- part of any unique key, so two members may now share one conversation. That
-- is the shape the store could not hold before.
ALTER TABLE channel_bindings ADD COLUMN chat_id TEXT;

-- A challenge has to carry the chat too, or verification would have nothing to
-- write into the column above and the invitation would silently rebuild the
-- conflation it exists to remove.
ALTER TABLE channel_binding_challenges ADD COLUMN chat_id TEXT;

-- BACKFILL, and the honest gap in it.
--
-- Every existing row holds what onboarding's `primary_channel` step captured,
-- which is the CHAT — the planner read it as `chat_id`. Copying it into
-- `chat_id` therefore preserves exactly today's behaviour and is correct for
-- the manifest. It leaves `external_id` holding a chat ID rather than a sender
-- ID, which for Telegram are different values, so a strict-mode gateway lookup
-- against a migrated row matches a chat where it means to match a sender.
--
-- That gap is ALREADY LIVE — it is what reading one column two ways has meant
-- since 0007 — and this migration does not widen it. What it changes is that
-- the gap becomes nameable: after this, a Telegram row with
-- `external_id = chat_id` is visibly un-migrated data rather than an ambiguity
-- in the schema. Correcting it needs the owner's Telegram user ID, which
-- onboarding never captured; `reset_from(PRIMARY_CHANNEL)` rewrites the owner
-- binding, and B-07 keeps every household synthetic, so nothing real is
-- stranded by leaving it. See M1 in
-- `thoughts/shared/plans/2026-08-27-c3a-sender-identity-and-chat.md`.
UPDATE channel_bindings SET chat_id = external_id WHERE chat_id IS NULL;
UPDATE channel_binding_challenges SET chat_id = external_id WHERE chat_id IS NULL;

-- SQLite cannot add a NOT NULL column to a populated table without inventing a
-- default, and an empty-string default would be a lie that reads as data. The
-- column is therefore nullable in the schema and required by a trigger, the
-- same treatment `email_identities.domain_lookup_hmac` gets in 0003. A NULL
-- `chat_id` would reach the manifest as a binding that speaks nowhere, so it
-- must fail at the INSERT rather than at a deployment nobody is watching.
CREATE TRIGGER channel_bindings_chat_id_required
BEFORE INSERT ON channel_bindings
WHEN NEW.chat_id IS NULL
BEGIN
    SELECT RAISE(ABORT, 'channel_bindings.chat_id is required');
END;

CREATE TRIGGER channel_bindings_chat_id_required_on_update
BEFORE UPDATE ON channel_bindings
WHEN NEW.chat_id IS NULL
BEGIN
    SELECT RAISE(ABORT, 'channel_bindings.chat_id is required');
END;

CREATE TRIGGER channel_binding_challenges_chat_id_required
BEFORE INSERT ON channel_binding_challenges
WHEN NEW.chat_id IS NULL
BEGIN
    SELECT RAISE(ABORT, 'channel_binding_challenges.chat_id is required');
END;

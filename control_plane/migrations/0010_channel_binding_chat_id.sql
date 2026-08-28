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

-- LEGACY ROWS ARE RETIRED, NOT REWRITTEN.
--
-- Every row written before this migration carries identities that were never
-- provenanced. `external_id` holds whatever onboarding's `primary_channel`
-- step captured — the CHAT — and `actor_id` holds a household-local name the
-- control plane invented. Neither can be turned into the string the channel's
-- ingest produces, because that string was never recorded. Nothing in the
-- schema even distinguishes a Telegram user ID from an internal name: there is
-- no provenance column and never was.
--
-- Two rewrites were tried and both were wrong, which is why this is a DELETE:
--
--   SET chat_id = external_id   -- answers the chat question with a chat that
--                                  is really a chat only by accident, and for
--                                  WhatsApp is the sender, which ingest never
--                                  reports as a conversation;
--   SET external_id = actor_id  -- assumes every historical actor was a
--                                  transport sender. The pre-0010 challenge
--                                  API let ONE internal actor hold several
--                                  external IDs, so two such rows collide on
--                                  `UNIQUE (household_id, channel, external_id)`
--                                  and the migration ABORTS — reproduced
--                                  against a 0009 database. Worse is the case
--                                  that succeeds: two households that both
--                                  named an adult `synthetic-adult` end up
--                                  holding one `external_id` between them, and
--                                  `WhatsAppGatewayRouter.route` answers
--                                  `ambiguous_sender` for both. Nothing guards
--                                  `actor_id` across households; only
--                                  `external_id` was ever checked.
--
-- A third heuristic would be the fourth guess about identity in this slice.
-- Guessing is the defect, so this migration stops guessing: rows whose
-- identities cannot be known are removed, and the household re-establishes
-- them from a source that does know.
--
-- The cost is small and asymmetric, which is what makes this the safe option
-- rather than merely the honest one. OWNER rows cost almost nothing:
-- `DesiredSpecPlanner.issue` calls `ensure_owner_binding` on every revision
-- and re-seeds them from the durable onboarding result, now reading `actor_id`
-- for the sender and `chat_id` for the conversation — so the next plan
-- restores them CORRECTLY, which a rewrite here could not have done. ADULT
-- rows are not reconstructible: their challenges were consumed, and those
-- members must be re-invited. Under B-07 every household is synthetic, and
-- `channel_bindings` had no production writer at all before C3 (#72), so the
-- set being retired is small and contains no real person's identity.
--
-- Outstanding challenges go for the same reason. An invitation issued before
-- this migration names a chat nobody captured, so redeeming it would write the
-- very row this statement is removing — and a legacy internal-actor challenge
-- could not redeem at all, because `_insert` now refuses an actor that is not
-- the sender. An invitation that cannot produce a usable binding is better
-- withdrawn than left to fail in someone's hands.
DELETE FROM channel_bindings;
DELETE FROM channel_binding_challenges;

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

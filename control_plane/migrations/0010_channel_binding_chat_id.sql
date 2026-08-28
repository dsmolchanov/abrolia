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

-- LEGACY ROWS: REPAIR WHAT CARRIES PROVENANCE, RETIRE WHAT DOES NOT.
--
-- Two earlier drafts of this section were wrong in opposite directions, and
-- both are worth naming because the rule below is what survives them.
--
-- Rewriting everything invented identities the schema never recorded.
-- `SET external_id = actor_id` across all rows assumed every historical actor
-- was a transport sender; the pre-0010 challenge API let ONE internal actor
-- hold several external IDs, so two such rows collided on
-- `UNIQUE (household_id, channel, external_id)` and ABORTED the migration —
-- reproduced against a 0009 database. The case that succeeded was worse: two
-- households that both named an adult `synthetic-adult` ended up holding one
-- `external_id` between them, and `WhatsAppGatewayRouter.route` answers
-- `ambiguous_sender` for both.
--
-- Deleting everything then revoked identities that were perfectly recoverable.
-- An existing household with a deployed runtime migrated cleanly and became
-- unroutable, because nothing re-seeds its owner row: `migrate` does not call
-- `DesiredSpecPlanner.issue`, and neither does the startup path, so "the next
-- revision will fix it" is only true if something happens to issue one.
--
-- The rule that separates them is PROVENANCE, and it is a property of the row
-- rather than a guess about it:
--
--   OWNER rows carry their sender already. `ensure_owner_binding` was called
--   with `actor_id = channel_public["actor_id"]` — onboarding's captured
--   sender, the same value that becomes `actors.owner` and is what
--   `message.from.id` and WhatsApp's normalized `+999…` actor are compared
--   against. The planner simply wrote it to the wrong column and put the CHAT
--   in `external_id`. Repairing such a row reads a field that already holds
--   the answer; it invents nothing.
--
--   ADULT rows carry nothing. Their `actor_id` is free text a household owner
--   typed at the challenge endpoint, documented as household-local, with no
--   transport provenance and no column that could supply it. They are retired
--   and those members are re-invited — which now captures both identities
--   correctly.
--
-- Outstanding challenges are retired for the same reason: a challenge issued
-- before this migration names no chat, and one naming an internal actor could
-- not redeem at all now that `_insert` refuses an actor that is not the
-- sender. An invitation that cannot produce a usable binding is better
-- withdrawn than left to fail in somebody's hands.
DELETE FROM channel_bindings WHERE role <> 'owner';
DELETE FROM channel_binding_challenges;

-- A household may hold only one owner binding — `_retire_superseded` deletes
-- what it replaces — but that guarantee arrived with C3 and rows predating it
-- may not honour it. Keep the newest and drop the rest, because two owner rows
-- would repair into two senders for one household and reintroduce exactly the
-- ambiguity this migration exists to remove.
DELETE FROM channel_bindings
 WHERE role = 'owner'
   AND EXISTS (
       SELECT 1 FROM channel_bindings AS newer
        WHERE newer.role = 'owner'
          AND newer.household_id = channel_bindings.household_id
          AND (newer.verified_at > channel_bindings.verified_at
               OR (newer.verified_at = channel_bindings.verified_at
                   AND newer.id > channel_bindings.id)));

-- A repair must never CREATE the collision it is meant to avoid. Two
-- households whose owners were recorded under one actor would repair into one
-- `external_id` held twice, which is the state `_reject_foreign_holder` exists
-- to prevent and which makes the gateway answer `ambiguous_sender` for both.
-- Neither claim can be preferred over the other, so both are retired and both
-- households re-onboard. Fail closed: an unroutable household is recoverable,
-- a household routed to somebody else's runtime is not.
DELETE FROM channel_bindings AS doomed
 WHERE role = 'owner'
   AND EXISTS (
       SELECT 1 FROM channel_bindings AS rival
        WHERE rival.role = 'owner'
          AND rival.channel = doomed.channel
          AND rival.actor_id = doomed.actor_id
          AND rival.household_id <> doomed.household_id);

-- What survives is repaired in place, each column taking the value that
-- answers its question. `external_id` still holds the CHAT at this point, so
-- it is read into `chat_id` before the sender overwrites it; SQLite evaluates
-- every right-hand side against the original row, so the order of the
-- assignments below does not matter.
UPDATE channel_bindings
   SET chat_id = external_id,
       external_id = actor_id
 WHERE chat_id IS NULL;

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

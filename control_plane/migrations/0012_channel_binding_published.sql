-- C3c: a binding is routable only once a revision carrying it is ACTIVE.
--
-- `gateway/whatsapp_router.py` resolves a sender with no revision predicate at
-- all, so a binding routes the instant `verify_challenge` commits it — to a
-- runtime still serving the previous revision, whose manifest has no pair for
-- that member. The runtime denies the turn and the message goes nowhere.
--
-- The gateway cannot answer this by reading the manifest. It is constructed
-- with the database, the relay keys and the sender HMAC key and holds no field
-- cipher, so a manifest is opaque to it by construction — and handing the
-- routing layer the household's plaintext configuration to answer a yes/no
-- question would be the wrong trade. It needs a fact on the row it already
-- queries.
--
--   published_revision NULL     -- staged: written, projected into the manifest
--                                  being planned, not yet routable
--   published_revision = N      -- published by revision N's activation
--
-- A revision number rather than a boolean, because a terminal rollout failure
-- has to retire exactly what a given revision staged and leave every other
-- staged row alone.
ALTER TABLE channel_bindings ADD COLUMN published_revision INTEGER;

-- BACKFILL FROM THE REVISION THAT IS ACTUALLY SERVING.
--
-- The obvious key is `households.current_config_revision`, and it is the wrong
-- one. `schedule_runtime_rollout` advances that column when the job is QUEUED,
-- so for any household with a rollout in flight it names a revision that is
-- not serving anything yet. Backfilling from it would publish bindings against
-- a revision the runtime has never seen — the exact confusion this column
-- exists to remove.
--
-- `config_revisions.status = 'active'` is the only fact that answers "serving".
-- `BootstrapService.activate` is its sole writer, and it supersedes the
-- previous active revision in the same transaction, so there is at most one
-- such row per household.
--
-- A household with NO active revision keeps NULL and is therefore staged.
-- That is correct rather than conservative: nothing is serving it, so nothing
-- should be routed to it.
UPDATE channel_bindings
   SET published_revision = (
       SELECT active.revision
         FROM config_revisions AS active
        WHERE active.household_id = channel_bindings.household_id
          AND active.status = 'active'
   )
 WHERE published_revision IS NULL;

-- C3f, round two: give every EXISTING household the web seat 0013 assumed.
--
-- 0013 added the columns and the planner started seeding a seat for households
-- planned after it. Everything already running got neither, and
-- `web_message` requires one — so an upgrade turned working web chat into 403
-- for every existing household. Found in review on #106.
--
-- Nothing re-plans on upgrade. `control_plane/provisioning/rollout.py` states
-- it plainly: neither migrations nor startup invoke the planner, and a
-- household only replans when something asks it to. So "they will get one on
-- their next revision" was not a migration path, it was a hope.
--
-- Two shapes, because they broke differently:
--
--   1. a household whose primary is NOT web has no web row at all -> insert
--      one, from the owner binding it already has;
--   2. a household whose primary IS web has a row already, written before
--      `account_id` existed, so the column is NULL and the seat lookup misses
--      it -> fill the account in.
--
-- PUBLISHED, not staged. These households are already serving a revision, and
-- `routable_web_seat` requires `published_revision IS NOT NULL`. Staging the
-- backfill would leave exactly the 403 this migration exists to remove, until
-- some unrelated rollout happened to publish it. The revision recorded is the
-- one the household is serving now, which is the revision this seat is being
-- made part of retroactively.
--
-- Only households that HAVE an owner binding and an active owner account: a
-- half-onboarded household has no seat to derive and must not be given one.

-- 1. The missing seats.
INSERT INTO channel_bindings (
    id, household_id, channel, external_id, chat_id, external_id_hmac,
    actor_id, role, verified_at, verified_by_actor_id, published_revision,
    account_id
)
SELECT
    'seat-' || owner_binding.household_id,
    owner_binding.household_id,
    'web',
    owner_binding.actor_id,
    -- Minted ONCE, here. From this point the room is durable: the planner
    -- keeps whatever chat a seat already has rather than recomputing it.
    'web:' || owner_binding.actor_id,
    NULL,
    owner_binding.actor_id,
    'owner',
    owner_binding.verified_at,
    owner_binding.verified_by_actor_id,
    households.current_config_revision,
    membership.account_id
FROM (
    SELECT b.household_id, b.actor_id, b.verified_at, b.verified_by_actor_id
    FROM channel_bindings AS b
    WHERE b.role = 'owner' AND b.channel != 'web'
    GROUP BY b.household_id
) AS owner_binding
JOIN households ON households.id = owner_binding.household_id
JOIN household_memberships AS membership
    ON membership.household_id = owner_binding.household_id
   AND membership.role = 'owner'
   AND membership.status = 'active'
WHERE households.current_config_revision > 0
  AND NOT EXISTS (
      SELECT 1 FROM channel_bindings AS existing
      WHERE existing.household_id = owner_binding.household_id
        AND existing.channel = 'web'
  );

-- 2. The primary-web households whose row predates the column.
--
-- `ensure_owner_binding` would not have fixed these on its own: its early
-- return compares role, actor and chat, so a row identical except for a NULL
-- account read as "the same owner state" and was left alone. That comparison
-- now includes the account as well, but a household that never replans would
-- still be waiting.
UPDATE channel_bindings
SET account_id = (
    SELECT membership.account_id FROM household_memberships AS membership
    WHERE membership.household_id = channel_bindings.household_id
      AND membership.role = 'owner'
      AND membership.status = 'active'
)
WHERE channel = 'web'
  AND role = 'owner'
  AND account_id IS NULL
  AND EXISTS (
      SELECT 1 FROM household_memberships AS membership
      WHERE membership.household_id = channel_bindings.household_id
        AND membership.role = 'owner'
        AND membership.status = 'active'
  );

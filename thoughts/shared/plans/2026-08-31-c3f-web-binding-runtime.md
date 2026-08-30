---
title: "C3f — The web binding the runtime never reads"
status: active
created_at: "2026-08-31"
repository: abrolia
parent_plans:
  - thoughts/shared/plans/2026-08-23-go-live-checklist.md
  - thoughts/shared/plans/2026-08-06-canon-execution-plan.md
  - thoughts/shared/plans/2026-08-27-c3a-sender-identity-and-chat.md
scope: c3f
data_policy: synthetic-only-until-explicit-gates
---

# C3f — The web binding the runtime never reads

## The defect

`web` has been a first-class binding channel since C3 — `CHANNELS` includes it
(`control_plane/repositories/bindings.py:94`), the manifest projects it
(`control_plane/provisioning/manifest.py:25`), and the schema carries it. The
runtime does not read it.

`RuntimeService._web_chat` refuses every role but `owner`, and `web_chat_turn`
hardcodes `manifest.actors.owner` with `allowed_chats={"web-chat"}`
(`hermes_cloud/runtime/service.py`). So a second adult holding a *verified,
published* web binding cannot use web chat at all.

Canon Phase E item 4 names Web among the channels whose binding derives
`RunContext`, so this is promised scope rather than an enhancement.

**Why it read as current when it was stale.** The comment at
`runtime/service.py:655` defers to "C3 … when bindings get a lifecycle". C3a and
C3c *gave* bindings that lifecycle, and nothing went back to the comment. Found
by review on #101 after the 2026-08-30 validation filed it as a nit and then
declared Track C closed in the same pass.

## What is actually wrong, precisely

Three things, and only the first is the one the box named:

1. The pair is not derived from the binding.
2. **The runtime trusts a role string off the wire.** `control_plane/api/web.py`
   resolves the caller's real membership role and forwards it, and the runtime
   branches on it. That inverts the repo's own rule — `RunContext` is built from
   transport-verified provenance, never from payload — and it is the reason the
   endpoint once had to be fixed for defaulting to `owner` when the membership
   lookup failed. A role that arrives as a string is a role that can be wrong.
3. **Nothing maps an account to a web binding.** `household_memberships` has no
   `actor_id`, and `web` had no convention for `external_id` — tests use
   `synthetic-web-seat`.

## The decision

**A `account_id` column on `channel_bindings` (migration `0013`).** The web
seat's `external_id` and `actor_id` stay the person's actor identity, exactly as
on every other channel, and the new column carries the account→seat mapping that
has no other home.

**Corrected 2026-08-31, before implementation.** The first version of this plan
decided the opposite — that `external_id` IS the account id on web, on the
reasoning that `external_id` means "the sender" and on web the sender is the
authenticated account. That is wrong, and the invariant that makes it wrong was
written down before this slice existed:

> `_insert` calls `_reject_actor_that_is_not_the_sender` — "the one place that
> no path can go around, including `ensure_owner_binding`"
> (`control_plane/repositories/bindings.py:907-916`).

Every binding row must satisfy `actor_id == external_id`, because the runtime
authorizes the transport sender and a binding under any other name would never
match a message. So `external_id = account_id` forces `actor_id = account_id`,
and two consequences follow that no wiring fix should carry:

* the owner acquires a SECOND actor identity — their primary-channel actor plus
  their account id — so `actors.family` holds one person twice, which is the
  case the planner's own C3a comment says `parse_runtime_manifest` rejects;
* `role_for(account_id)` no longer equals `actors.owner`, so **the owner is
  silently downgraded to `ROLE_FAMILY` on web** and loses the owner-only tools,
  export and deletion among them.

A privilege change is not an acceptable side effect of wiring a channel. The
column costs a migration and touches the export and delete surfaces, and it is
the only shape that leaves every existing role exactly as it was.

Rejected, still: a verified sender-to-actor mapping carried through the
manifest. The `_insert` docstring names it as "a real design … that this code
does not have and must not pretend to", and it is a much larger slice than
giving web a seat.

## Design

```
account (authenticated session)
  └─ channel_bindings (household, 'web', account_id = <session account>)
       ├─ actor_id  ─┐
       └─ chat_id   ─┴─> forwarded as trusted provenance to the runtime
                          └─> build_run_context(household, actor_id, chat_id)
                               ├─ role  <- manifest.actors  (NOT the wire)
                               └─ denied unless the exact pair is verified
```

* **Routability is C3c's, unchanged.** The seat lookup requires
  `published_revision IS NOT NULL`, the same predicate `resolve_sender` uses, so
  a member verified mid-rollout is not routed to a runtime still serving N-1.
* **The role stops travelling.** The control plane sends `actor_id` and
  `chat_id`; the runtime derives the role from its own manifest via
  `Household.role_for`, and `knows_binding` denies a cross-pair. An adult seeded
  into `actors.family` by C3a lands `ROLE_FAMILY` with no further work.
* **Both ends change together.** C5a is the precedent: the gateway signed
  `body|timestamp` while the runtime verified the bare body, each with its own
  passing tests, and no WhatsApp message could reach a household. One test
  therefore drives a payload through the client AND the runtime endpoint rather
  than asserting each side's idea of the contract separately.
* **Fail closed.** No published web seat means 403, not a guessed identity.

## Files

**Files:** `control_plane/migrations/0013_channel_binding_account.sql`,
`control_plane/repositories/bindings.py`, `control_plane/privacy/export.py`,
`control_plane/privacy/delete.py`, `tests/control_plane/test_schema_contract.py`,
`tests/control_plane/test_db.py`, `tests/control_plane/test_export_delete.py`,
`control_plane/provisioning/planner.py`, `control_plane/api/web.py`,
`control_plane/runtimes/chat_client.py`, `hermes_cloud/runtime/service.py`,
`tests/control_plane/test_web_chat_api.py`, `tests/test_runtime_web_chat.py`,
`tests/control_plane/test_channel_bindings.py`,
`tests/control_plane/test_bootstrap.py` — every household now stages TWO
bindings, the primary and the seat, and the activation-scoping case counts them.

**Branches:** `feat/c3f-web-binding-runtime`.

## Acceptance

- [x] An owner reaches web chat through a seeded, published web binding rather
      than a synthesized id, and KEEPS `ROLE_OWNER` while doing so.
      (`tests/test_runtime_web_chat.py::test_web_chat_runs_the_dialogue_loop_and_records_usage`
      asserts the derived role is `owner`.)
- [x] A second adult with a verified, published web binding reaches web chat and
      is attributed to their OWN actor, with `ROLE_FAMILY`.
      (`test_a_second_adults_web_seat_speaks_as_themselves`.)
- [x] A household member with no published web seat is refused, and a seat whose
      revision has not activated is refused. (`routable_web_seat` applies C3c's
      predicate; the endpoint answers 403 `no web seat in the active revision`.)
- [x] The runtime derives the role from the manifest; no role travels at all
      any more. (`test_an_adult_cannot_borrow_the_owners_chat` pins cross-pair
      denial.)
- [x] One test carries a payload through the client and the runtime endpoint
      together, so the two ends cannot drift as they did in C5a.
      (`test_the_control_planes_payload_is_the_one_this_runtime_reads`, checked
      in both directions: renaming `chat_id` on the client fails it.)

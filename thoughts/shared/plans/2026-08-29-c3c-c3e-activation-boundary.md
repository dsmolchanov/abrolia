---
title: "C3c + C3e — activation is the boundary, and nothing waits for it"
status: active
created_at: "2026-08-29"
repository: abrolia
parent_plans:
  - thoughts/shared/plans/2026-08-23-go-live-checklist.md
scope: c3c-c3e
data_policy: synthetic-only-until-explicit-gates
---

# C3c + C3e — activation is the boundary, and nothing waits for it

The checklist carries these as two items. They are one defect with two
consumers, and writing them apart would build two overlapping lifecycles for
the same event.

`BootstrapService.activate` is the moment a revision becomes the one being
served: it sets `config_revisions.status = 'active'`, supersedes the previous
revision, and moves the household to `active`. Everything about "which
configuration is real" is decided there.

Two things do not wait for it.

* **The gateway** (C3c). `verify_challenge` commits the binding row
  immediately, and `whatsapp_router` resolves a sender with
  `SELECT household_id FROM channel_bindings WHERE channel = ? AND external_id = ?`
  — no revision predicate. So between the write and activation, the new
  member's traffic is routed to a runtime still serving N−1, whose manifest
  has no pair for them. The runtime denies it and the message goes nowhere.
* **The rollout job** (C3e). `_settle_runtime_ready` settles `succeeded`
  immediately after launch. A runtime that never claims its token, or whose
  activation is rejected, leaves the household at `provisioning` on revision N
  with a succeeded job `lease` will never revisit.

One event, two consumers that ignore it. That is the slice.

## What already exists, and must not be rebuilt

`config_revisions.status` is already a lifecycle:
`planned → issued → claimed → active → superseded | revoked`
(`0001_control_plane.sql:224`). There is already an authoritative answer to
"which revision is serving", and `activate` is already the single writer of it.

Neither half below needs a new notion of activation. They need to *read* the
one that exists.

## D1. A binding is routable only once a revision carrying it is active

The gateway cannot answer this by reading the manifest. `WhatsAppGatewayRouter`
is constructed with the database, the relay keys and the sender HMAC key
(`whatsapp_router.py:87-89`) — no field cipher — so a manifest is opaque to it
by construction, and giving it one would hand the routing layer the household's
plaintext configuration to answer a yes/no question. It needs a fact on the row
it already queries.

```sql
ALTER TABLE channel_bindings ADD COLUMN published_revision INTEGER;
```

* `verify_challenge` writes the row with `published_revision` NULL — staged.
  The planner projects staged rows into the manifest exactly as now, because
  the revision being planned is what will publish them.
* `activate` sets `published_revision = <revision>` for every binding of that
  household that is still staged.
* The gateway adds `AND published_revision IS NOT NULL` to both lookups.

Why a revision number rather than a boolean: it answers "published by what",
which is what a rollback needs in order to retire only what a failed revision
staged.

**Terminal rollback retires what it staged.** A rollout that fails terminally
must clear the staged rows it created, or the member is unroutable forever AND
their sender stays taken: `issue_challenge` refuses a tuple this household
already holds ("already bound to that member"), and `_reject_foreign_holder`
refuses it to every other household. So the owner cannot re-invite them and
nobody else can claim the identity either. This is the half most likely to be
dropped, and it is the one that turns a delay into a dead end.

## D2. A rollout is not succeeded until the revision is active

`_settle_runtime_ready` stops settling `succeeded` at launch. The job stays
non-terminal until `activate` runs, and `activate` settles it.

The job is therefore reachable by `lease` while it waits, which is the point:
a runtime that never claims its token leaves work a worker will come back to
rather than a succeeded row nothing revisits.

**Scoped deliberately to pre-mutation certainty.** After a failure that reached
the provider, the Machine may already carry N's configuration, and no
database-only write can honestly say which revision is serving. C3b narrowed
its recovery to pre-mutation failures for exactly this reason and that
narrowing stands. Reconverging the provider — asking the runtime what it is
actually running — needs a capability the provisioner protocol does not have,
and is not in this slice. What IS in scope is that the job stays open, so the
uncertain case is visible as unfinished rather than recorded as done.

## The window this does not close

A binding is staged the moment it is verified and published when the revision
activates. Between those, the member cannot be routed — that is the point —
but they also cannot be told why. The endpoint already returns the revision;
surfacing "pending activation" to the owner is a product decision, not this
slice.

## Risks

* **Ordering.** D1 and D2 must land together. Publishing on activation while
  the job still settles at launch means a rollout that never activates leaves
  bindings staged forever with no job to retire them — strictly worse than
  today, where they are at least routable.
* **The gateway predicate is a deny.** A migration that leaves existing rows
  NULL makes every current household unroutable. `0011` must backfill
  `published_revision = households.current_config_revision` for bindings of
  households that are `active`, and only those.
* **`lease` must not spin.** A job left open pending activation will be
  re-leased. It needs a not-before or a waiting status, or the worker busy-
  loops on a household that is waiting for a runtime to call in.

## Acceptance

1. **A staged binding does not route.** Verify a binding; assert the gateway
   answers `unknown_sender` for that sender until activation, and resolves it
   afterwards.
2. **Activation publishes exactly its own household's staged rows**, and does
   not touch another household's.
3. **A terminal rollout failure retires what it staged**, and the sender can
   be issued again afterwards.
4. **A rollout that never activates leaves the job open**, and the household
   is not `active` — asserted through the worker, not by reading a status the
   test wrote.
5. **The migration leaves every existing household routable.** A database at
   `0010` with an active household and bindings routes identically before and
   after.
6. **The worker does not spin** on a job waiting for activation.

Criterion 5 is the one that breaks production if skipped, and criterion 3 is
the one that turns a bug into a support ticket.

#### Inventory — C3c and C3e activation boundary

**Files:** `control_plane/migrations/0011_channel_binding_published.sql`,
`control_plane/repositories/bindings.py`,
`control_plane/provisioning/bootstrap.py`,
`control_plane/provisioning/worker.py`, `gateway/whatsapp_router.py`,
`tests/control_plane/test_channel_bindings.py`,
`tests/control_plane/test_bootstrap.py`,
`tests/control_plane/test_provisioning_jobs.py`,
`tests/test_gateway_routing.py`.

**Branches:** `codex/c3c-c3e-activation-boundary`.

One slice, not two. Both halves hang off `BootstrapService.activate`, and
splitting them would mean two mechanisms for one event — plus a window where
one half is live and the other is not, which the Risks section shows is worse
than either end state.

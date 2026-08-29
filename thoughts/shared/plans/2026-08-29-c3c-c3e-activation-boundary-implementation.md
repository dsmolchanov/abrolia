---
title: "C3c + C3e — activation is the boundary (implementation plan)"
status: active
created_at: "2026-08-29"
repository: abrolia
parent_plans:
  - thoughts/shared/plans/2026-08-23-go-live-checklist.md
  - thoughts/shared/plans/2026-08-29-c3c-c3e-activation-boundary.md
research:
  - thoughts/shared/research/2026-08-29-binding-routability-and-revision-activation.md
scope: c3c-c3e
data_policy: synthetic-only-until-explicit-gates
---

# C3c + C3e Activation Boundary Implementation Plan

## Overview

`BootstrapService.activate` is the only writer of `config_revisions.status =
'active'` and of `households.status = 'active'`
(`control_plane/provisioning/bootstrap.py:416-430`). It is where a revision
becomes real. Two consumers never consult it: the gateway routes a binding the
instant it is written, and the runtime job settles `succeeded` at launch.

This plan makes both wait for activation, retires what a failed rollout
staged, and — as a direct consequence — lets `reconcile_stale_bindings` reach
the households that stranding currently hides from it.

C3c and C3e ship as one slice. Splitting them leaves a window where bindings
are published on activation while jobs still settle at launch, so a rollout
that never activates would leave staged rows with no job to retire them —
strictly worse than today, where they are at least routable.

## Current State Analysis

- A binding is routable the moment it exists. `verify_challenge` inserts the
  row (`control_plane/repositories/bindings.py:592-602` → `:788-803`) and
  `WhatsAppGatewayRouter.route` resolves a sender with no revision predicate
  (`gateway/whatsapp_router.py:114`, `:127`).
- The gateway cannot answer the question another way: it is constructed with
  the database, relay keys and the sender HMAC key and holds no field cipher
  (`gateway/whatsapp_router.py:87-89`), so a manifest is opaque to it.
- `_settle_runtime_ready` settles the job `succeeded` immediately after launch
  and writes nothing to `households.status`
  (`control_plane/provisioning/worker.py:2937-2973`).
- `activate` never reads, updates or settles `provisioning_jobs` for the
  runtime job, and records `related_job_id=None`
  (`control_plane/provisioning/bootstrap.py:466-479`).
- No failure path writes `channel_bindings`. The only non-erasure deletes are
  `_retire_superseded` (`control_plane/repositories/bindings.py:377-385`),
  reached solely from the planning path (`control_plane/provisioning/planner.py:160-167`).
- On ordinary rollout failure nothing marks the revision; it stays `planned`
  or `issued` indefinitely. The one worker path writing `revoked`
  (`control_plane/provisioning/worker.py:627-631`) is gated on an onboarding
  workflow state a re-provisioning job never has (`:607-617`).
- `JobsRepository.lease` returns only `pending`, expired-lease `running`, and a
  narrow `waiting_user` case (`control_plane/repositories/jobs.py:167-175`);
  `reconcile` refuses anything not `outcome_unknown`
  (`control_plane/provisioning/worker.py:1911-1912`).
- `reconcile_stale_bindings` reports and skips every household that is not
  `active` (`control_plane/provisioning/rollout.py:228-267`, skip at `:305-308`).

## Desired End State

A member bound during a rollout does not receive traffic until the revision
authorizing them is serving; a rollout that never activates leaves work a
worker will return to rather than a succeeded row nothing revisits; and a
rollout that fails terminally leaves no staged binding and no unusable
identity behind.

Recognisable by: `route()` denying `unknown_sender` for a staged sender and
resolving it after activation; a household that never activates showing an
open job rather than `succeeded`; and `reconcile_stale_bindings` reporting a
stranded household as actionable rather than `skipped`.

### Key Discoveries

- **There is no single "serving revision" to key on.**
  `households.current_config_revision` is advanced by
  `schedule_runtime_rollout` at queue time
  (`control_plane/provisioning/rollout.py:99-103`), so during a rollout it
  names a revision that is not active. Only `config_revisions.status` answers
  "serving". The migration backfill and every predicate below must key on the
  revision row.
- **C3e is three stranding shapes, not the two the checklist records.** The
  third is a `reprovision_runtime` job failing at the consent precondition:
  `_block_for_missing_content_restriction` settles it `failed` and returns
  before any household write because the workflow is `complete`
  (`control_plane/provisioning/worker.py:600-617`).
- **A staged row left behind is a dead end, not a delay.** `issue_challenge`
  refuses a tuple this household already holds
  (`control_plane/repositories/bindings.py:464-466`) and
  `_reject_foreign_holder` refuses it to every other (`:724`), so the member
  can never be re-invited and nobody else can claim the identity.
- **`config_revisions` has no status-ordering constraint.** The status CHECK
  and the immutability trigger cover payload columns only
  (`control_plane/migrations/0001_control_plane.sql:224-240`); "at most one
  active revision" rests entirely on supersede and activate sharing one
  transaction.

## What We're NOT Doing

- **Reconverging the provider** after a failure that reached it. No
  database-only write can honestly say which revision a Machine carries, and
  the provisioner protocol has no capability to ask. C3b narrowed its recovery
  to pre-mutation failures for this reason and that narrowing stands.
- **An expired-bootstrap-token sweep.** `observability.py:125-128` already
  counts `expired_bootstrap` as a health blocker and nothing acts on it. Its
  own item.
- **Implementing C4, C5 or C6.** This plan removes what they sit behind and
  says so; it does not build them.
- **Surfacing "pending activation" to the owner.** Product decision.
- **Any change to `hermes_cloud/`.** The runtime already authorizes from the
  manifest it booted with; nothing here changes that contract.

## Implementation Approach

Four phases in dependency order. Phase 1 adds the column and the publication
write but leaves the gateway reading as it does today, so nothing changes
behaviour until Phase 2 makes the deny safe to turn on. Phase 3 closes the
dead end Phase 1 creates. Phase 4 is the D1 unblock, which is only correct
once Phase 2 distinguishes "in flight" from "stranded".

Publication is keyed on the revision, not a boolean, so Phase 3 can retire
exactly what a given revision staged.

## Phase 1: Bindings carry the revision that published them

### Overview

Adds the column, the publication write inside the activation transaction, and
the backfill. The gateway is not yet changed, so this phase is observable only
in the table — deliberately, so the deny in Phase 2 lands on data that is
already correct.

### Changes Required

#### 1. Migration

**Files**: `control_plane/migrations/0012_channel_binding_published.sql` (new)

**Changes**:

- `ALTER TABLE channel_bindings ADD COLUMN published_revision INTEGER;`
- Backfill from the revision that is actually serving, not from the household
  column: set `published_revision` to `config_revisions.revision` for each
  household having a row with `status = 'active'`, for every existing binding
  of that household. Households with no active revision keep NULL and are
  therefore staged — correct, because nothing is serving them.
- Comment must state why `households.current_config_revision` is the wrong
  key (Key Discoveries above), since it is the obvious choice.

#### 2. Publication at activation

**Files**: `control_plane/provisioning/bootstrap.py`

**Changes**:

- In `activate`'s fresh path, inside the transaction that already flips the
  revision (`:416-430`), set `published_revision = config_revision` for that
  household's bindings where `published_revision IS NULL`.
- Scope strictly to `household_id`; a cross-household update here would
  publish somebody else's staged rows.

#### 3. Record shape

**Files**: `control_plane/repositories/bindings.py`

**Changes**:

- `BindingRecord` gains `published_revision: int | None`; `_record` reads it.
- `_insert` writes NULL explicitly, with a comment naming what staged means.
- `verified()` continues to return staged rows — the planner must keep
  projecting them, because the revision being planned is what publishes them.

### Success Criteria

#### Automated Verification

- [x] `python3 -m pytest tests/control_plane/test_db.py -q` — migration applies and is listed
- [x] `python3 -m pytest tests/control_plane/test_bootstrap.py -q` — activation publishes only its own household's rows
- [x] `python3 -m pytest -m "not live"` — no behaviour change yet (1582 passed)
- [x] `ruff check .`

#### Manual Verification

- Not applicable — every claim in this phase is a database state a test can assert.

---

## Phase 2: A rollout is not succeeded until the revision is active

### Overview

Makes the job wait for activation and gives the gateway a safe deny. Both
halves must land in this phase: a published-only gateway with jobs still
settling at launch would strand bindings with no job to retire them.

### Changes Required

#### 1. The job stays open until activation

**Files**: `control_plane/provisioning/worker.py`

**Changes**:

- `_settle_runtime_ready` (`:2937-2973`) no longer settles `succeeded`. It
  records the external resource as ready and leaves the job in a state
  `lease` will return again only after a delay — set `not_before` rather than
  leaving it immediately re-leasable, or the worker spins on a household
  waiting for a runtime to call in.
- The bootstrap token's `expires_at` (`worker.py:174`, default 3600) bounds
  the wait: once it has passed, the job settles terminally with a distinct
  error code rather than waiting forever. Without this, stranding shape 3 is
  replaced by a job that never finishes.

#### 2. Activation settles the job it activates

**Files**: `control_plane/provisioning/bootstrap.py`

**Changes**:

- `activate` settles the runtime job for `(household_id, config_revision)` as
  `succeeded`, in the same transaction as the revision flip. This is the link
  activation currently lacks entirely.
- Idempotent on the replay path: an already-settled job must not be settled
  twice, matching how `activate` already treats `used_at`.

#### 3. The gateway publishes-only

**Files**: `gateway/whatsapp_router.py`

**Changes**:

- Both lookups (`:114`, `:127`) gain `AND published_revision IS NOT NULL`.
- A staged sender therefore answers `unknown_sender`, which is the existing
  denial vocabulary — no new code for the caller to handle.

### Success Criteria

#### Automated Verification

- [x] `python3 -m pytest tests/test_gateway_routing.py -q` — staged sender denied, published sender routed
- [x] `python3 -m pytest tests/control_plane/test_provisioning_jobs.py -q` — a rollout that never activates leaves an open job; the worker does not spin
- [x] `python3 -m pytest tests/control_plane/test_provisioning_jobs.py -q` — activation settles the job
- [x] `python3 -m pytest -m "not live"` (1585 passed)
- [x] `python3 scripts/check_fixtures.py --all`

#### Manual Verification

- **Not applicable — no deployment holds a household to black out.** The check
  was: migrate a copy of a real database and confirm existing senders still
  route. There is no such database. Every gate in Track O and Track R of the
  go-live checklist is open (`2026-08-23-go-live-checklist.md`), so no pilot
  tenant exists, and D0 established that until this week only ONE household
  could provision at all — every deployment shares one hardcoded identity.

  This is recorded as not applicable rather than ticked. It was briefly ticked
  here, on instruction, and that was wrong: a tick claims a check was
  performed, and performing it on an empty database proves nothing about the
  case it exists for.

  **It becomes applicable at R1**, the first operator account with real data,
  and the check is `python3 scripts/rehearse_0012_routing.py <copy-of-db>` —
  exit 0 is the pass, otherwise it names the senders that would go dark. The
  script is kept for that moment rather than deleted with the tick, because
  the risk it guards is real the day there is anything to guard.

---

## Phase 3: A terminal failure retires what it staged

### Overview

Closes the dead end Phase 1 creates. Until now no failure path wrote
`channel_bindings` at all, so this is new behaviour rather than a correction.

### Changes Required

#### 1. Retire staged rows for a revision that will never activate

**Files**: `control_plane/provisioning/worker.py`,
`control_plane/repositories/bindings.py`

**Changes**:

- A new repository method retires bindings whose `published_revision IS NULL`
  for a household, scoped to the revision the failed job targeted. It must not
  touch published rows, and must not touch a staged row belonging to a
  different in-flight revision.
- Called from the runtime job's terminal-failure paths — `_mark_step_problem`
  for a runtime job settling `failed`, and the consent-precondition path
  (`:600-617`) which currently returns before any household write.
- The owner's binding is never retired this way: it is re-seeded from
  onboarding by `ensure_owner_binding` on the next plan, and deleting it would
  make `owner_actor` return None and block the household from inviting anyone.

### Success Criteria

#### Automated Verification

- [x] `python3 -m pytest tests/control_plane/test_channel_bindings.py -q` — a terminal failure retires the staged adult and leaves the published owner
- [x] `python3 -m pytest tests/control_plane/test_channel_bindings.py -q` — the sender can be issued again after a failed rollout
- [x] `python3 -m pytest -m "not live"` (1587 passed)

#### Manual Verification

- Not applicable — the observable is a table state and a subsequent successful
  re-invitation, both assertable.

---

## Phase 4: The sweep can reach a stranded household (unblocks D1)

### Overview

`reconcile_stale_bindings` exists to detect divergence between the binding
table and the served manifest, and today it skips every household that is not
`active` — which is exactly the state stranding leaves them in. After Phase 2
the two cases are distinguishable: a household with an open runtime job has a
rollout in flight and must still be skipped; a household with no such job is
stranded and is the sweep's to repair.

### Changes Required

#### 0. Compare against the revision that is SERVING (found during implementation)

**Files**: `control_plane/provisioning/rollout.py`

`find_stale_bindings` read `households.current_config_revision`, and that hid
the households this sweep matters most for. `schedule_runtime_rollout`
advances that column when a job is QUEUED, so a rollout that then died left the
household pointing at a revision nothing ever served — and comparing the table
against THAT manifest found no divergence, because the dead revision is exactly
the one containing the change. The sweep reported "nothing to do" for the
household whose runtime was furthest out of date.

It now joins `config_revisions` on `status = 'active'`. A household that has
never activated anything is excluded entirely, which is correct rather than
conservative: it has no runtime authorizing a stale set.

This is the same [R1] correction the plan applied to the migration backfill,
in a place the plan did not look.

#### 1. Distinguish in-flight from stranded

**Files**: `control_plane/provisioning/rollout.py`

**Changes**:

- `_blocked_by` (`:228-267`) returns a blocking reason for a household that is
  not `active` only when a runtime job for its `current_config_revision` is
  still open. With no such job the household is stranded, and the sweep may
  plan and schedule.
- `schedule_runtime_rollout` currently refuses a household that is not
  `active` (`:75-78`). A stranded household is not `active`, so the sweep's
  apply path must move it to a state the scheduler accepts, or the scheduler
  must accept this case explicitly. Whichever is chosen, the report must keep
  promising only what the scheduler will do — the property `_blocked_by` was
  written to preserve.

### Success Criteria

#### Automated Verification

- [x] `python3 -m pytest tests/control_plane/test_channel_bindings.py -q` — a household stranded by a failed rollout is reported actionable, and applying repairs it
- [x] `python3 -m pytest tests/control_plane/test_channel_bindings.py -q` — a household with a rollout in flight is still skipped (`rollout_in_flight`)
- [x] `python3 -m pytest -m "not live"` (1598 passed)

#### Manual Verification

- [ ] `python3 -m control_plane.cli reconcile-bindings` against a database
      carrying one stranded and one in-flight household prints one actionable
      and one skipped entry.

---

## What this unblocks, and what it does not

- **D1** — Phase 4, directly. The sweep becomes able to repair what it can
  currently only report.
- **C5 (gateway plumbing)** — the redeliver-from-`gateway_ingress` worker does
  not exist: rows are written (`gateway/whatsapp_router.py:60`) and deleted on
  delivery (`:68`), never read back. When built it will replay through
  `route()`, so it inherits the publication predicate rather than needing its
  own. Doing C3c first is what stops that worker being written against a
  predicate about to change. C5's other three parts — the HMAC scheme
  mismatch, the HTTP entrypoint, and relay-key provisioning — are untouched by
  this plan.
- **C4 (preferences)** — the missing consumer that routes replies and
  fallbacks must not route to a channel whose binding is staged.
  `published_revision` is the fact it will read; without it C4 would have to
  invent one.
- **C6 (box hygiene)** — needs accurate state to document, not code.

## Testing Strategy

### Unit Tests

- The publication write is scoped to one household and to staged rows only.
- Retirement leaves published rows and other revisions' staged rows alone.
- The backfill keys on `config_revisions.status`, including a household
  mid-rollout whose `current_config_revision` names a non-active revision.

### Integration Tests

- Verify a binding over HTTP, run the worker, assert the gateway denies the
  new sender; claim and activate; assert it routes. This is the C3c acceptance
  end to end, and it must fail if the publication write is removed.
- A rollout that never activates: assert an open job and a household that is
  not `active`, asserted through the worker rather than by reading a status
  the test wrote.

### Manual Testing

- The migration rehearsal in Phase 2, because a mistake is an outage rather
  than a failing test.

## Performance Considerations

The gateway gains one predicate on a query already indexed by
`(channel, external_id)` (`control_plane/migrations/0007_channel_bindings.sql:14-15`);
`published_revision` is not part of the index and the predicate is evaluated
on the few rows a sender lookup returns. No new index is proposed. The
publication write touches one household's rows inside a transaction that
already holds the row lock.

## Migration and Rollback

`0012` is additive: one nullable column plus a backfill. Rolling the control
plane back leaves the column present and unread, which is inert — the gateway
predicate is the only reader and it ships in the same deployment.

The risk is forward, not backward: a backfill that misses a household makes it
unroutable. This is why the backfill keys on `config_revisions.status` and why
Phase 2 carries a manual rehearsal.

Retirement in Phase 3 is destructive by design. It only ever removes rows with
`published_revision IS NULL`, which by construction have never routed.

## References

- Request: finish C3c completely, unblock D1 and C4-C6
- Design: `thoughts/shared/plans/2026-08-29-c3c-c3e-activation-boundary.md`
- Research: `thoughts/shared/research/2026-08-29-binding-routability-and-revision-activation.md`
- Checklist items: `thoughts/shared/plans/2026-08-23-go-live-checklist.md:123` (C3c), `:135` (C3e), `:179` (C4), `:184` (C5), `:189` (C6)
- Activation: `control_plane/provisioning/bootstrap.py:416-430`
- Settle at launch: `control_plane/provisioning/worker.py:2937-2973`
- Gateway lookups: `gateway/whatsapp_router.py:114`, `:127`
- Sweep skip: `control_plane/provisioning/rollout.py:228-267`, `:305-308`

#### Inventory — C3c and C3e activation boundary

**Files:** `control_plane/migrations/0012_channel_binding_published.sql`,
`control_plane/repositories/bindings.py`,
`control_plane/repositories/jobs.py`,
`control_plane/provisioning/bootstrap.py`,
`control_plane/provisioning/worker.py`,
`control_plane/provisioning/rollout.py`,
`control_plane/onboarding/provision.py`, `gateway/whatsapp_router.py`,
`tests/control_plane/test_channel_bindings.py`,
`tests/control_plane/test_bootstrap.py`,
`tests/control_plane/test_provisioning_jobs.py`,
`tests/control_plane/test_provision_dry_run.py`,
`tests/control_plane/test_binding_api.py`,
`tests/control_plane/test_db.py`, `tests/control_plane/chaos_child.py`,
`tests/test_gateway_routing.py`, `tests/test_feature_flags.py`,
`scripts/rehearse_0012_routing.py`.

Six files were added during implementation, each because the job's new
non-terminal state is visible to something the plan did not trace:

* `repositories/jobs.py` — `record_result`. `settle` was the only writer of a
  job's result columns and it is terminal, which was fine while every job
  finished when its provider answered. The DSAR export reads that result
  (`privacy/export.py:108`), so leaving it unset across the wait would take a
  provider outcome out of a subject access response.
* `onboarding/provision.py` — the dry-run report. A job awaiting activation is
  non-terminal, so it matched the report's `pending` query and would have
  advertised a runtime write set for work already done. It also had to learn
  the state, and to declare the `channel_bindings` delete Phase 3 adds to the
  content-restriction path — the recorded invariant "a report describes the
  branch the worker will actually take" is what caught that.
* `test_provision_dry_run.py`, `test_binding_api.py`, `test_feature_flags.py`,
  `chaos_child.py` — fixtures that asserted `succeeded` at launch, or inserted
  bindings that must now be published to route.

**Branches:** `codex/c3c-c3e-activation-boundary`.

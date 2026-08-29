---
title: "C3c + C3e — activation is the boundary, and nothing waits for it"
status: active
created_at: "2026-08-29"
repository: abrolia
parent_plans:
  - thoughts/shared/plans/2026-08-23-go-live-checklist.md
research:
  - thoughts/shared/research/2026-08-29-binding-routability-and-revision-activation.md
scope: c3c-c3e
data_policy: synthetic-only-until-explicit-gates
---

# C3c + C3e — activation is the boundary, and nothing waits for it

Grounded in
`thoughts/shared/research/2026-08-29-binding-routability-and-revision-activation.md`,
which traced the whole path first. Three of its findings changed this design;
they are marked **[R]**.

## The defect

`BootstrapService.activate` is the only writer of `config_revisions.status =
'active'` and of `households.status = 'active'` (`bootstrap.py:416-430`). It is
where a revision becomes real. Two consumers never ask:

* **the gateway** resolves a sender with no revision predicate at all
  (`whatsapp_router.py:114, :127`), so a binding routes the instant
  `verify_challenge` commits it — to a runtime still serving N−1, whose
  manifest has no pair for that member;
* **the runtime job** settles `succeeded` at launch (`worker.py:2959-2966`) and
  writes nothing to `households.status`, so a runtime that never activates
  leaves the household at `provisioning` with a job `lease` will never return
  (`jobs.py:167-175`).

One event, two consumers ignoring it. That is one slice, not two.

## What the research changed

**[R1] There is no single "serving revision" fact to consult.**
`households.current_config_revision` is advanced by `schedule_runtime_rollout`
at QUEUE time (`rollout.py:99-103`), so during a rollout it names a revision
that is not active. Only `config_revisions.status` answers "serving". Any
predicate added below must key on the revision row, not the household column —
the obvious shortcut is wrong.

**[R2] C3e is three stranding shapes, not the two the checklist records.** The
third is a `reprovision_runtime` job failing at the consent precondition:
`_block_for_missing_content_restriction` settles it `failed` and returns before
any household write, because the workflow is `complete` (`worker.py:607-617`).
It is not reachable by `reconcile` either (`worker.py:1911-1912`).

**[R3] D1's sweep cannot repair what C3e strands.** `_blocked_by` refuses any
household that is not `active` (`rollout.py:228-267`), so exactly the
households left at `provisioning` are reported and skipped (`:305-308`). The
mechanism built to detect divergence is blind to the divergence this slice
causes. Fixing C3e is what makes D1's sweep able to reach them.

## Design

### D1. A binding is routable only once a revision carrying it is active

The gateway cannot read a manifest — it is constructed with the database, relay
keys and sender HMAC key and no field cipher (`whatsapp_router.py:87-89`) — so
it needs a fact on the row it already queries.

```sql
ALTER TABLE channel_bindings ADD COLUMN published_revision INTEGER;
```

* `verify_challenge` writes it NULL: staged. The planner keeps projecting
  staged rows, because the revision being planned is what will publish them.
* `activate` sets `published_revision = <revision>` for that household's staged
  rows, in the transaction that already flips the revision (`bootstrap.py:416-430`).
* The gateway adds `AND published_revision IS NOT NULL` to both lookups.

A revision number rather than a boolean, so a rollback can retire only what a
given revision staged.

**Terminal rollback must retire what it staged.** Nothing today writes
`channel_bindings` on any failure path **[R]** — the only non-erasure deletes
are on the planning path (`bindings.py:377-385`). A staged row left behind is
not a delay but a dead end: `issue_challenge` refuses a tuple this household
already holds, and `_reject_foreign_holder` refuses it to every other, so the
member can never be re-invited and nobody else can claim the identity.

### D2. A rollout is not succeeded until the revision is active

`_settle_runtime_ready` stops settling at launch. The job stays non-terminal,
and `activate` settles it — which also gives activation the link to the job it
currently lacks entirely (`activate` passes `related_job_id=None` and never
touches `provisioning_jobs` for the runtime job) **[R]**.

Scoped to pre-mutation certainty, as C3b was: after a failure that reached the
provider, no database-only write can honestly say which revision is serving.
Reconverging the provider needs a capability the provisioner protocol does not
have and is **not** in this slice. What is in scope is that the uncertain case
stays visible as unfinished rather than recorded as done.

## Risks

* **Both halves must land together.** Publishing on activation while the job
  still settles at launch leaves staged rows with no job to retire them —
  strictly worse than today, where they are at least routable.
* **The gateway predicate is a deny.** `0011` must backfill
  `published_revision` for existing bindings of households whose revision is
  `active`, or every current household goes dark. Key the backfill on
  `config_revisions.status`, not on `households.current_config_revision` **[R1]**.
* **`lease` must not spin** on a job left open pending activation: it needs a
  `not_before` or a waiting status.
* **Bootstrap tokens expire** (default 1h, `worker.py:174`) and nothing sweeps
  an expired one back into a household status. A job held open pending an
  activation that can no longer happen needs a terminal path, or shape 3 is
  replaced by a job that waits forever.

## Acceptance

1. A staged binding does not route; it routes after activation.
2. Activation publishes only its own household's staged rows.
3. A terminal rollout failure retires what it staged, and the sender can be
   issued again afterwards.
4. A rollout that never activates leaves the job open and the household not
   `active` — asserted through the worker, not by reading a status the test wrote.
5. The migration leaves every existing household routable, keyed on
   `config_revisions.status='active'`.
6. The worker does not spin on a job awaiting activation.
7. A household stranded by a failed rollout is reachable by
   `reconcile_stale_bindings` rather than skipped **[R3]**.

Criterion 5 breaks production if skipped; criterion 3 turns a bug into a
support ticket.

## Deliberately not here

* **Reconverging the provider** after a failure that reached it — needs a
  protocol capability that does not exist.
* **An expired-token sweep.** `observability.py:125-128` already counts
  `expired_bootstrap` as a health blocker and nothing acts on it; that is its
  own item.
* **Telling the owner a member is pending activation.** Product decision.

#### Inventory

Carried by
`thoughts/shared/plans/2026-08-29-c3c-c3e-activation-boundary-implementation.md`,
which is where execution is tracked. A design document that also claimed the
branch would make two steps claim it, and the scope gate refuses to guess
between them — correctly, since the point of that gate is that scope is stated
rather than inferred. The same split is used by
`2026-08-27-c3b-revision-rollout.md`.

---
title: "C6a — a successful activation is not a stale projection"
status: active
created_at: "2026-08-29"
repository: abrolia
parent_plans:
  - thoughts/shared/plans/2026-08-23-go-live-checklist.md
scope: c6a
data_policy: synthetic-only-until-explicit-gates
---

# C6a — a successful activation is not a stale projection

Found while reviewing #86 and deliberately not patched there: the honest fix
needed a distinction the code did not have, and the PR was about the activation
boundary rather than about reconcile. It is pre-existing on `main` and this is
its slice.

## The defect

`ProvisioningWorker.reconcile` asks `_runtime_projection_is_current` and then
uses the answer for a different question:

```python
projection_current = self._runtime_projection_is_current(runtime_state, job)
if not projection_current:
    if inspected.state is InspectState.READY and inspected.result:
        return self._cleanup_cancelled_result(job, inspected.result, provider)
```

`_runtime_projection_is_current` requires `household_status == 'provisioning'`
and a workflow in `{runtime_provisioning, activating}`. **Successful activation
is precisely what ends both**: `BootstrapService.activate` moves the household
to `active` and the workflow to `complete`.

So a job whose revision activated reads as "not current", and the READY branch
concludes the provider resource is an orphan. `_cleanup_cancelled_result` marks
the `external_resources` row `deleting` and enqueues a `bootstrap_cleanup` job
with `cleanup_authorization: "runtime_cancelled"` — **teardown of the runtime
currently serving the household**, triggered by an operator reconciling a job
that succeeded.

Reproduced during the #86 review by instrumenting the branch:

```
RECON inspect=absent proj_current=False state={'job_status': 'outcome_unknown',
  'workflow_state': 'complete', 'household_status': 'active',
  'current_config_revision': 2, 'runtime_ref': 'synthetic-runtime:…'}
```

With the dry-run fake, `inspect` answers ABSENT and the job settles
`cancelled_provider_absent` — wrong but harmless. With a real provider the
Machine is READY, and the destructive branch is the one that runs.

The path in: `launch` starts the Machine and then raises, so the job settles
`outcome_unknown` (stamping `settled_at`, so `activate` skips it when the
Machine claims its token). The revision goes live, the job stays terminal-but-
wrong, and an operator reconciles it.

## The invariants

Stated before the code, and read back against the diff before pushing — the
practice C5c paid for skipping and C5d and C5e proved.

**J1 — "This job's projection is not current" and "this provider resource is
an orphan" are different questions.** They diverge in exactly one case: the job
is stale BECAUSE IT SUCCEEDED. Every place that answers the second with the
first must first ask whether the revision activated.

**J2 — Activation is the authority on whether a revision is serving, and the
household must still exist to serve it.**

`config_revisions.status = 'active'` has one writer,
`BootstrapService.activate`, and it supersedes the previous row in the same
transaction. The worker's own bookkeeping — `job_status`, `settled_at`, the
presence of an unused bootstrap token — is a record of what the worker
observed, and each can be stale or absent for reasons that say nothing about
whether the runtime is serving. Three defects in #86 were this same
substitution.

An active revision alone is not enough, though, and the review found the case:
**erasure is a different lifecycle.** `DeletionService` moves the household to
`deleting` and deletes its runtime WITHOUT superseding `config_revisions`, so
the revision row still reads `active` while nothing is serving. Since every
guard below declines to settle a serving job, reading the revision alone left
an `outcome_unknown` runtime job unresolvable — and `privacy/delete.py` treats
exactly those as unresolved, so the erasure was blocked for good.

A deletion that cannot finish is worse than the teardown this slice prevents.
One is a runtime nobody meant to destroy; the other is a person's data that
cannot be removed, which has no operator workaround.

**J3 — No terminal outcome may contradict a serving revision.** A job whose
revision is active is not `failed` and not `cancelled`. Durable history and the
privacy export would say the opposite of the thing actually running.

**J4 — Teardown needs proof of orphanhood, not absence of currency.** Deleting
a household's runtime is irreversible for that household, so the bar is
"nothing is serving this" rather than "this job is not the current intent".
Where that proof is unavailable, declining is correct: a resource left in place
is visible and reversible, and one destroyed is neither.

## Design

One helper — `_revision_is_serving(connection, job)` — asking J2's question,
and every consumer of the currency guard that leads to a terminal or
destructive outcome consults it first.

Consumers, all three found by grepping the guard rather than by following the
reported instance:

* `reconcile`'s not-current branch, READY → `_cleanup_cancelled_result`. The
  reported one, and the destructive one.
* `reconcile`'s not-current branch, ABSENT → `_settle_superseded_runtime_absent`.
  Not destructive, but writes `cancelled` over a job whose revision serves,
  which J3 forbids.
* `_settle_activation_deadline`, already reached when a wait ends. #86 drafted
  a guard here and reverted it as untestable; with the reconcile path fixed the
  case is reachable and it comes back with a test.

## Acceptance

* A job settled `outcome_unknown` whose revision then activates reconciles to
  `succeeded`, and no cleanup job is enqueued.
* Its `external_resources` row is not marked `deleting`.
* The recorded provider result survives that settle — the COALESCE contract
  from #86, exercised by a caller that reports a status alone.
* A genuinely superseded job — revision not active, provider READY — still
  reaches cleanup, so the fix does not disable the branch it guards.
* A household being erased settles its runtime job, so nothing
  `privacy/delete.py` reads as unresolved is left behind.
* Full suite green, ruff and sanitizer clean.

## Deliberately not here

* **The broad `except` clauses elsewhere in `worker.py`.** Three defects in the
  gateway were programming errors converted into silent retries by an
  `except Exception` on a retry path, and `worker.py` has the same shape in
  places. It is a real cleanup and it is not this defect; auditing it under a
  slice about activation authority would be a second change hiding inside the
  first.

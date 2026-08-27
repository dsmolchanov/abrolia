---
title: "C3b — Roll a revision out to an already-active household"
status: active
created_at: "2026-08-27"
repository: abrolia
parent_plans:
  - thoughts/shared/plans/2026-08-23-go-live-checklist.md
scope: c3b
data_policy: synthetic-only-until-explicit-gates
---

# C3b — Roll a revision out to an already-active household

Verifying a channel binding plans configuration revision N and nothing deploys
it. `control_plane/api/bindings.py` calls `planner.issue()` and returns; no
`ensure_runtime` job is created and `households.current_config_revision` is
never advanced. The endpoint reports success and revision N while the deployed
runtime stays on N−1, so the gateway can route the new sender to a runtime that
does not know them.

## Why this is not a small change, contrary to the first estimate

The go-live checklist recorded C3b as "the worker requires
`household_status = 'provisioning'`, so it has to drive that transition
deliberately". That was read off the job-creation site alone. Reading the
worker's own guards changes the picture.

A runtime job's progress is gated by **four independent currency checks**, each
asking "is this job still acting on current state" and each naming a
phase-specific expectation for the **onboarding** workflow:

| site | phase | expects `workflow_state` in |
|---|---|---|
| `worker.py:2662` | `_runtime_projection_is_current`, used by reconcile | `{runtime_provisioning, activating}` |
| `worker.py:2711` | before issuing the bootstrap token | `{runtime_provisioning, activating}` |
| `worker.py:2819` | before launch | `{activating}` |
| `worker.py:2849` | at settle | `{activating, complete}` |

A household adding a member finished onboarding long ago: its workflow sits at
`complete` (`provisioning/bootstrap.py:440`) and its household row at `active`.
It matches none of the first three.

The other clauses in those guards — `household_status`,
`current_config_revision`, `runtime_ref` — carry the actual currency meaning
and apply unchanged. Only the workflow-state clause is onboarding-specific.
That is the whole of the problem, and it is why the fix is neither trivial nor
enormous: it is one concept, restated in four places.

Worth noting that this file already carries a warning about exactly that shape
(`worker.py:112-118`): a caller that restated the worker's question instead of
asking it checked the wrong thing, and the remedy was to ask once.

## The fork, and the decision

### (a) Reuse the onboarding workflow — rejected

Drive `complete` → `runtime_provisioning` → `activating` → `complete` for a
household that is not onboarding. The state vocabulary permits it
(`0001_control_plane.sql:99-101`) and no schema change is needed.

Rejected because `workflow.state` is **user-visible**: it reaches the
onboarding page through `OnboardingSnapshot`
(`repositories/onboarding.py:98`). A household that finished onboarding months
ago would show as mid-provisioning while an adult is added. That is a real
regression introduced to serve an internal mechanism — the page would be
telling the family something untrue about themselves.

It also overloads a record of *how this household was set up* into a log of
every configuration change since, and re-stamps `completed_at` on each one.

### (b) A distinct re-provisioning operation — **decided**

A second `operation` on the existing `runtime` job kind, e.g.
`reprovision_runtime`, whose currency check expects `workflow_state` to stay
`complete` while `household_status`, `current_config_revision` and
`runtime_ref` carry the same meanings they already do.

Concretely: the four literal state sets become one place that answers "which
workflow states are current for THIS job", parameterised by operation.
`ensure_runtime` keeps today's phase-specific sets exactly; the new operation
expects `complete` throughout. Every other clause is untouched.

This is a small behavioural change wrapped around a consolidation the file
already argues for. The consolidation is the risky half — it edits the code
that decides whether a job may act — so it lands with its own tests before the
new operation is wired to anything.

## Sequencing: after C3a, not before

C3b was going to be built first, on the reasoning that C3a should not inherit
an un-deployed revision path. Having read the guards, the order should reverse,
for a reason that is about testability rather than effort:

**C3b's acceptance criterion cannot be written honestly until C3a lands.** The
thing to prove is "a member added to a live household reaches the runtime". Today
a second member on the primary channel is refused outright — C3a is what makes
one representable — so the only member C3b could roll out is one on a secondary
channel, which is the narrow case rather than the one the feature exists for.
Building C3b first means shipping the safety-critical worker change against a
test that exercises the edge and not the centre.

C3a has no such dependency: its acceptance is about what the manifest contains
and whether `parse_runtime_manifest` accepts it, which is complete without a
rollout.

**Built ahead of C3a anyway, 2026-08-27, by decision.** The consequence is
recorded rather than hidden: the end-to-end test binds a member on a SECONDARY
channel, because a member on the primary channel is still refused until C3a.
The rollout mechanism is fully exercised; the case the feature exists for is
not, and gets its test when C3a lands.

## Acceptance

1. **A live household reaches revision N.** Verify a binding on an active
   household; assert an `ensure`-class job exists for revision N, that
   `current_config_revision` advances, and that the worker carries it to
   `succeeded` without `_ProjectionCancelled`.
2. **The onboarding page does not change.** `OnboardingSnapshot.state` stays
   `complete` across the whole rollout. This is the criterion that rejects (a),
   so it is asserted rather than assumed.
3. **The currency guards still refuse stale work.** For each of the four
   phases, a job whose revision no longer matches, or whose `runtime_ref`
   changed, is still cancelled — proven for the existing operation AND the new
   one, because the consolidation is where that property could be lost
   silently.
4. **Onboarding is unchanged.** The first-time path produces the same job,
   states and transitions as before; the consolidation is behaviour-preserving
   for `ensure_runtime`.
5. **A failed rollout does not strand the household.** If the runtime job
   settles unsuccessfully, `household_status` does not remain `provisioning`
   forever with no job to advance it.

Criterion 5 is the one most likely to be skipped and most likely to hurt: it is
the difference between a household that briefly shows as provisioning and one
that is stuck there.

## Risks

- **The consolidation touches the code that prevents acting on stale state.**
  A subtle loosening would not fail a test that only exercises the happy path —
  hence criterion 3 covering all four phases explicitly.
- **Re-bootstrap semantics — settled 2026-08-27, and it costs the family
  nothing.** The question was whether reusing the bootstrap/activation path
  imposes anything on the household. It does not: the bootstrap token is a
  MACHINE credential living in the runtime's own secret namespace, and
  `_bootstrap_transport_allowed` accepts it only over the private `.flycast`
  host (`api/internal_bootstrap.py:41-63`) — a family member cannot reach that
  endpoint at all. Consent is unaffected for a separate reason:
  `required_consent_purposes` derives the owed set from the email provider and
  the WhatsApp number type (`privacy/consent.py:165-179`), neither of which
  membership changes, so no new receipt comes due. **Nobody re-authorizes.**

  What it does cost is a restart. `_machine_payload` embeds
  `abrolia_revision` and `HERMES_CONFIG_SHA256` in the Machine config
  (`provisioning/fly.py:483-513`), so a new revision fails `_machine_matches`
  and `_ensure_machine` updates the Machine — which replaces its config and
  restarts it. The volume is mounted, so nothing is lost; the household's
  assistant is briefly unavailable. That is the same cost every configuration
  change already carries, and adding a member is a configuration change.
- **Concurrency.** Two members verified in quick succession plan revisions N and
  N+1. The intent key is `{household}:runtime:{revision}` so the jobs are
  distinct, but the household's `current_config_revision` is a single value —
  the second rollout must not be cancelled by the first, nor vice versa.

## Deliberately not here

- **C3a**, which this now waits on — see Sequencing.
- **Avoiding the restart.** A rollout that changed only the member list could
  in principle be delivered without replacing the Machine config. Whether that
  is worth a second deployment path is a product question, not a defect.

#### Inventory

Carried by the go-live checklist's "Inventory — C3b revision rollout" step,
which is where execution is tracked. A design document that also claimed the
branch made two steps claim it, and the scope gate refuses to guess between
them — correctly, since the whole point of that gate is that scope is stated
rather than inferred.

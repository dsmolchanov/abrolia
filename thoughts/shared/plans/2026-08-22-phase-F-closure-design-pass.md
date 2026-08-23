---
title: "Phase F Closure — Erasure Sequence Guarantee and the Reconcile Contract Hoist"
status: implemented
created_at: "2026-08-22"
repository: abrolia
branch: codex/phase-F-allowlist-at-dispatch
parent_plans:
  - thoughts/shared/plans/2026-08-06-canon-execution-plan.md
  - thoughts/shared/plans/2026-08-06-phase-DE-pilot.md
scope: phase-F-closure
data_policy: synthetic-only-until-explicit-gates
---

## Implementation status (2026-08-22, written before first execution)

All edits below are in the working tree; **nothing has been executed yet**
(the venv lacks pytest and every install attempt hit a tooling outage).
A static audit against the implementation has been done: dispatch order,
derived-ref strings (`google-oauth:<id>`, `nerve-org:` prefix), parent
settlement code `cancelled_and_compensated`, immediate leasability of jobs
created without `not_before`, stub surfaces, and the env-flag defaults
(unset = brake ON — which is why the forward-fail hoist test sets the three
switches to `1` explicitly).

- `contracts.py`: `Provisioner` declares `reconcile`.
- `fakes.py`: `DeterministicFakeProvisioner.reconcile` added.
- `worker.py`: hoisted shutdown route above the brake; required-reconcile
  gate (`failed/provider_cannot_reconcile`); unguarded tail replaced by the
  explicit `outcome_unknown/reconcile_unsupported` refusal.
- `tests/control_plane/test_real_email_wiring.py`: the nine-case sequence
  test; `_NoReconcileEmail`; `_quarantine_job` generalized over kinds;
  three hoist tests (shutdown-without-method ×4 providers, forward-without-
  method ×4, kind-gap refusal ×2 schema kinds).
- `AGENTS.repo-invariants.md`: new half "**Reconcile dispatches exhaustively;
  adapter shape gates nothing about teardown.**" under the call-time
  precondition entry.

## Execution results (2026-08-22, first validation round)

- The nine-case sequence test: **green on first execution**. Finding 1's
  premise is settled empirically at this head.
- Hoist tests: 9/10 green; the `fake-email` shutdown case exposed a wrong
  assertion in the TEST (the worker correctly refuses an adapter with no
  derivable teardown contract rather than scheduling a cleanup its
  deprovisioner would refuse) — fixed to assert the refusal.
- Full wiring module (66 cases): green — every existing pin held.
- Full non-live suite surfaced 10 real failures OUTSIDE the wiring module,
  all in tests whose stub adapters lacked `reconcile` and silently depended
  on the deleted tail. Each was renegotiated onto the new contract:
  - `test_provisioning_jobs.py`: absent-unknown now resumes through the
    adapter's reconcile (`failed/provider_absent` was the tail's decision);
    late-cancel-after-waiting stays quarantined under its own reason instead
    of settling failed (real-provider teardown covered in the wiring module).
  - `test_email_option_flags.py`: `RecordingProvisioner` declares
    `reconcile`; the quarantined-job test drives the scheduled cleanup to an
    actual deprovision; the brake-return test supplies the intent fields and
    secret namespace forward resume legitimately requires.
  - `test_consent_withdrawal.py`: the property is now "teardown performed
    without the withdrawn consent, through deprovision, never inspect".
  - `email/test_identity.py`: the canary rides through a stub that
    reproduces what the deleted tail did, so the normalization property
    keeps a vehicle that tries to carry it; fixture adds the secret
    namespace forward resume requires.
- After rewrites: all 10 green individually; `ruff check .` clean.
- `test_packaging.py` fails only because the uv-built venv lacks `pip`
  (environmental; the test shells out to `python -m pip wheel`).

Validation still owed: full non-live suite after rewrites (in flight);
mutation checks from both items above.

## Round eleven (2026-08-23) — the synthetic derivation

Codex's verdict on `6591143` itself came back clean, but the CI run exposed a
plan-scope deviation first (five paths the canon Phase F step never declared;
fixed docs-only in the commit before this one), and the eleventh generation
then found one real blocker: `_derived_teardown_ref` knew Google's and Nerve's
contracts but refused the synthetic one — while `_validate_email_external_ref`
has always enforced `external_ref == synthetic-email:<identity_id>` on every
synthetic result. The finding was correct, and it convicted us with our own
invariant: a computable-before-call reference left quarantined forever behind
a refusal.

- Fix shape: derivation keys on the ADAPTER'S OWN DECLARATION
  (`email_public_provider == "synthetic"`), not on a registry-name list —
  the queued `synthetic` alias is then covered by construction, and a stub
  that declares no contract still refuses whichever name it sits under.
  That preserved the hoist pin's refusal case (`_NoReconcileEmail` declares
  `"nerve"`); only its rationale changed.
- `test_an_ambiguous_synthetic_job_tears_down_the_reference_it_can_derive`:
  four origins (cancel, reset, withdrawal, deletion) drive an external_ref-less
  synthetic job through reconcile → derived-ref cleanup → disconnect
  lifecycle; no inspect, no ensure; parent `cancelled_and_compensated`;
  identity `deleted`; reservation `released`; household gone on erasure.
  Fixture note: no `external_resources` row on the three command origins —
  ambiguity means nothing durable was recorded — while erasure keeps one,
  deletion awaiting an outstanding resource being the exception.
- Mutations M1–M4 re-proved KILLED at this head; M5 (derivation disabled)
  added and KILLED. Full non-live suite green, ruff clean.

## Round twelve (2026-08-23) — the late ProviderWaiting

The eleventh generation's verdict found one more member of the family, and
this one was a race no existing test held: a call still IN FLIGHT when the
deletion transaction commits is reached by nothing delete() runs — the sweep
cancels only `pending`/`waiting_user`, deliberately leaving running work to
prove its outcome — and if that answer comes back as `ProviderWaiting`,
`_schedule_cancelled_waiting_cleanup` refused the `running` row (its guard
accepted only `outcome_unknown`). The handler then settled `waiting_user`,
which nothing in erasure reads: not the sweep that already ran, not
`resume`'s unresolved scan (`running`/`outcome_unknown` only). A live
org/inbox/key behind an erasure that completes anyway.

- Fix shape, per the reviewer's prescription: deletion ownership is asked
  BEFORE the status restriction. A `running` row of a household whose status
  is `deleting`/`deleted` is admitted; the SAME transaction settles it
  `outcome_unknown`, records the external resource under the answer's own
  reference, and schedules the cleanup (`jobs.create` is intent-idempotent
  and `_external_resource` upserts by stable name, so the later probe pass
  through reconcile re-enters harmlessly). A returned WorkResult is only
  telemetry — handlers settle themselves — so the transition lives in the
  write, which is also what makes it atomic.
- Reference-less waiting shapes go through the handler's ownership branch:
  BYO DNS legitimately names no resource yet, and the synthetic validator
  REFUSES a reference on any waiting answer outright. For them there is no
  scheduled cleanup from the answer — the job lands unresolved where
  reconcile routes it to the shutdown probe, which derives what Round five's
  machinery established (or refuses with a named reason).
- `test_a_waiting_answer_arriving_after_erasure_begins_is_still_torn_down`
  parameterizes all four provider contracts over their REAL waiting shapes:
  google-oauth (`google-oauth:<id>`), nerve-managed (canonical-JSON
  reference with typed attachment readiness), nerve-byo-domain (DNS wait,
  no reference), fake-email (no reference, contract-forbidden). Each parks
  its adapter on a gate inside `ensure`, leases, begins erasure while the
  call is parked, releases the answer, and drives reconcile → cleanup →
  parent `cancelled_and_compensated` → identity deleted → reservation
  released → resume complete → household row gone. The with-ref arms pin
  that the teardown was scheduled by the answering worker itself, before
  any reconcile.
- Mutations M6 (a running row is never admitted) and M7 (erasure never owns
  the reference-less answer) both KILLED; M1–M5 re-proved at this head.
- `.check-fixtures-allow`: one exact-value entry — the managed contract's
  `<local_part>@abrolia.com` is production by definition, so the regression
  cannot use a documentation-domain placeholder.

# Phase F Closure — Erasure Sequence Guarantee and the Reconcile Contract Hoist

## Overview

PR #70 reached the CLAUDE.md hand-back signal after eight review rounds, each
finding one defect at the next layer of a path that was never designed
end-to-end: brake → deletion → reconcile → shutdown probe → cleanup → secret
deletion. The handoff stopped the fix loop and named two deliberate pieces of
design work instead of a ninth patch. The round-nine Codex verdict on head
`a0ac5f3` then asked for exactly one of them.

This plan covers both. It is written against `a0ac5f3`.

## Item 1 — The erasure sequence, proven end-to-end

**The demand.** Codex round nine, two inline blockers:

1. `ProvisioningWorker._reconcile`'s deletion-owned route schedules a cleanup
   that allegedly cannot finish: `_delete_email_binding_secret` accepts only an
   identity marked `disconnecting`, which deletion allegedly never sets.
2. `test_erasure_teardown_reaches_a_terminal_state` stops at the
   `disconnecting` transition — nothing drives the scheduled cleanup, settles
   the parent, or resumes the deletion, so the loop the test exists to kill
   could return without a red suite.

**Finding 1 looks stale.** Commit `a0ac5f3` is precisely the fix it asks for:
the deletion transaction (`DeletionService.delete`) marks every household email
identity `disconnecting` in the same durable transaction that writes
`households.status = 'deleting'`, so any job deletion owns has its identity
already inside the lifecycle `_delete_email_binding_secret` requires. But the
honest answer to a possibly-stale blocker is evidence, not argument — and the
evidence is the same artifact finding 2 demands.

**The work.** One parameterized sequence test,
`test_erasure_sequence_settles_every_job_and_completes_deletion`, over all
three real email providers × three origins of ambiguity:

- **braked** — reclaimed lease settled `outcome_unknown` by the incident brake;
- **pre-quarantined** — `withdrawal_requires_reconciliation`, the code any
  one-shot reclassification used to skip;
- **late-ambiguous** — leased at deletion time, timed out *after* the deletion
  transaction, invisible to anything that stamps jobs.

Each case drives the whole chain with the real worker and the real
`DeletionService`: origin → delete → (identity `disconnecting`) → reconcile →
`_shutdown_probe` derives the teardown reference (no `external_ref` in the
request — the timeout-without-answer case) → cleanup runs `_cleanup` → parent
settles `cancelled_and_compensated` → `resume` finds nothing unresolved →
household row deleted.

**Acceptance**

- [x] The sequence test is green for all nine cases.
- [x] If any case is red, the defect is fixed at the invariant and the finding
      was real — say so in the thread. (No case was red against `a0ac5f3`'s
      implementation; the one early red was a wrong assertion about the fake,
      which has no derivable teardown contract by design.)
- [x] The reply to the round-nine findings quotes the passing sequence as the
      evidence for finding 1 and lands the test finding 2 asked for.
- [x] Mutation check: reverting the `disconnecting` transition in
      `delete.py`, or keying ownership on the job's error code again, turns the
      sequence test red. (M1 kills.)

## Item 2 — The reconcile-contract hoist

**The defect class.** `_reconcile` dispatches by kind, but its email branch is
gated on `getattr(provider, "reconcile", None)` — an attribute the
`Provisioner` protocol does not even declare. An email adapter without it falls
to a tail whose first act is `provider.inspect(...)`, and `inspect` is a
RECOVERY path on every real email adapter: Nerve reissues the API key and
rotates the webhook; Google OAuth calls `ensure`. A shutdown job would reach
that tail too, because the shutdown route sits inside the
`callable(reconcile)` branch. All three adapters define `reconcile` today — a
property of the adapters, not a guarantee of the worker — and the schema
already permits `whatsapp_identity` and `channel_binding` kinds that Phase E
will create, which today would land in the same unguarded tail.

**The renegotiation, stated exactly.** What changes and what must not:

- Shutdown routing moves ABOVE the adapter-shape gate: every `email_identity`
  job whose error carries the reconciliation suffix goes to `_shutdown_probe`
  regardless of what the adapter implements. Cancel, withdrawal and reset
  reconciles are UNCHANGED — they already routed there through the same
  predicate; only their position in the function moves.
- Forward reconcile REQUIRES `reconcile`. An adapter without it fails closed
  with `provider_cannot_reconcile`; it never falls through to a mutating
  inspect. `DeterministicFakeProvisioner` gains `reconcile` delegating to
  `ensure` — the same delegation `NerveManagedEmailProvisioner.reconcile` and
  its siblings already define — so synthetic onboarding keeps its exact
  semantics while the fake stops being the proof that the tail is reachable.
- The unguarded tail is deleted. Every kind the schema allows is handled
  explicitly; the end of `_reconcile` becomes an explicit fail-closed refusal,
  so a Phase E kind cannot silently acquire an inspect path by omission.
- The `Provisioner` protocol declares `reconcile`.
- `_run_once` is NOT touched: `lease` never hands out `outcome_unknown`, so
  quarantined work reaches the worker only through `reconcile`, and the
  reclaimed branch there is crash recovery for live households' forward work —
  the one place the recovery-inspect contract is legitimate.

**Acceptance**

- [x] New tests: an email adapter without `reconcile` fails closed under
      reconcile, including a SHUTDOWN job for such an adapter (today that
      reaches `inspect`); a stub whose `inspect` raises proves nothing in
      `_reconcile` inspects anymore. Plus: the two schema kinds Phase E has
      not created yet are refused, not probed.
- [x] Existing cancel/withdrawal pins stay green
      (`test_late_waiting_response_after_cancel_stays_reconcilable`,
      `test_reconciling_a_cleanup_tears_down_instead_of_probing`, the
      secret-handoff convergence suites over the fake). Two pins asserted the
      deleted tail's own semantics (`failed/provider_absent` on absence) and
      were renegotiated onto the contract rather than kept — recorded in the
      execution-results section above.
- [x] Full non-live suite + ruff green; mutation checks killed (removing the
      hoisted route, re-adding the tail, dropping the fake's `reconcile`) —
      all four via `.phase-f-mutations.py`.

## Non-goals

- No change to the brake, the allowlist-at-dispatch authorization, or
  deletion-owned routing semantics — those are #70's reviewed contracts.
- No widening of the erasure exemption beyond deletion-owned work.
- No new job kinds, flags, or migrations.

## References

- Handoff report of 2026-08-22 (eight rounds, stop rationale, two flagged items).
- Round-nine Codex inline findings on `a0ac5f3` (worker.py:1812,
  test_real_email_wiring.py:1299).
- `AGENTS.repo-invariants.md` — "A precondition is enforced where the provider
  is CALLED", "Withdrawal tears down what it can NAME", "Erasure owns its
  ambiguity".

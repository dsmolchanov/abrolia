---
date: 2026-08-29T09:53:55+02:00
researcher: Codex
git_commit: 48bbdfbd4fb33226311f2800d9f1dd2c563bdb32
branch: codex/c3c-c3e-activation-boundary
repository: abrolia
topic: "How a channel binding becomes routable, and how a config revision becomes the one being served"
tags: [research, codebase, channel-bindings, gateway, provisioning, bootstrap, config-revisions, c3c, c3e]
status: complete
last_updated: 2026-08-29
last_updated_by: Codex
---

# Research: How a channel binding becomes routable, and how a config revision becomes the one being served

**Date**: 2026-08-29T09:53:55+02:00
**Researcher**: Codex
**Git Commit**: 48bbdfbd4fb33226311f2800d9f1dd2c563bdb32
**Branch**: codex/c3c-c3e-activation-boundary
**Repository**: abrolia

## Research Question

How does a channel binding become routable, and how does a config revision become the one actually being served? Trace end to end: `verify_challenge` writing the binding row, `DesiredSpecPlanner` projecting it into a manifest, `schedule_runtime_rollout` queuing the job, the provisioning worker's runtime phases (prepare, bootstrap token issue, launch, settle), `BootstrapService.claim` and `activate`, `config_revisions.status` transitions, `households.status` / `current_config_revision`, and finally `gateway/whatsapp_router.py` resolving a sender to a household. Identify every place that decides "which revision is serving" and every consumer that acts without consulting it — in particular the gateway's sender lookup and `_settle_runtime_ready`. Also document what happens on terminal rollout failure and whether anything retires or revokes a binding.

## Summary

**A binding becomes routable the instant it is written.** There is no publication step. `verify_challenge` inserts the row (`control_plane/repositories/bindings.py:592-602` → `:788-803`) and `gateway/whatsapp_router.py:98-137` resolves a sender with `SELECT household_id FROM channel_bindings WHERE channel = ? AND external_id = ?` — no predicate on revision, household status, or anything else. Routability is a property of the row existing.

**A revision becomes the one being served at exactly one place**: `BootstrapService.activate` sets `config_revisions.status = 'active'` (`control_plane/provisioning/bootstrap.py:421-424`), supersedes the previous active revision (`:416-420`), and sets `households.status = 'active'` with `current_config_revision` (`:426-430`). It is the sole writer of `status='active'` for both tables.

Between those two facts there is a window, and three findings characterise it.

**1. "Which revision is serving" is recorded three times, by different actors, at different moments — and they do not agree during a rollout.**
`households.current_config_revision` is advanced by `schedule_runtime_rollout` when the job is *queued* (`control_plane/provisioning/rollout.py:99-103`), long before anything is serving it. `config_revisions.status='active'` moves only at activation. So during a rollout the household's `current_config_revision` names a revision that is *not* active. Only `config_revisions.status` answers "serving"; `households.current_config_revision` answers "being rolled out". The worker's currency guards deliberately use the latter, because their question is "is this job still current", not "what is live".

**2. Two consumers act without consulting activation at all.**
The gateway (above) is one. The other is `_settle_runtime_ready` (`control_plane/provisioning/worker.py:2937-2973`), which settles the runtime job `succeeded` immediately after launch and writes nothing to `households.status`. Activation is not linked to the job in any direction: `activate` never reads, updates, or settles `provisioning_jobs` for the runtime job, and passes `related_job_id=None` to its own transition record (`bootstrap.py:466-479`).

**3. Terminal rollout failure retires nothing.** No failure or recovery path in the codebase writes `channel_bindings` — the only non-erasure, non-migration deleter is `_retire_superseded` (`bindings.py:377-385`), reached solely from `ensure_owner_binding` on the *planning* path. And on ordinary rollout failure nothing marks the revision: it remains `planned` or `issued` indefinitely, because the only worker path that writes `revoked` (`worker.py:627-631`) is gated on an onboarding workflow state a re-provisioning job never has.

The compounding consequence: at least three shapes leave a household at `status='provisioning'` with no job `JobsRepository.lease` will ever return again, and `reconcile_stale_bindings` — the sweep built to detect exactly this divergence — reports and skips those households, because `_blocked_by` refuses any household that is not `active` (`rollout.py:228-267`, skip at `:305-308`).

## Detailed Findings

### The write path: binding → manifest → revision → job

`verify_binding_challenge` does four things in one transaction (`control_plane/api/bindings.py:122-179`): verifies the challenge and writes the binding row, plans a revision, and schedules the rollout. The module docstring states the intent — neither half may be observed alone.

- `verify_challenge` (`control_plane/repositories/bindings.py:508`) commits the row through `_insert` (`:756`, INSERT at `:788-803`). The row carries no publication state; the columns are identity, role and verification provenance.
- `DesiredSpecPlanner.issue` reads every verified binding (`bindings.py:155` `verified()`) and projects each into `ChannelBindingV1` (`control_plane/provisioning/planner.py:196`). The manifest is a projection of the table, not a second record.
- `ConfigRepository.create_revision` inserts the revision as `planned` (`control_plane/repositories/configs.py:49-63`, literal at `:52`), called only from `planner.py:245`.
- `schedule_runtime_rollout` (`control_plane/provisioning/rollout.py:36`) refuses unless the household is `active` and the workflow `complete` (`:75-78`), creates the job with `intent_key = f"{household_id}:runtime:{revision}"` (`:89`), and then advances `households.status='provisioning'` and `current_config_revision = revision` (`:99-103`).

### The worker's runtime phases

`_finish_runtime` (`control_plane/provisioning/worker.py:2792`) runs the phases:

1. **prepare** — the provider call; `_finish_runtime` writes the `external_resources` row with `status="creating"` as soon as it returns (`worker.py:2822-2828`).
2. **bootstrap token issue** — `configs.issue_bootstrap` (`worker.py:2812-2821`) mints the token and moves the revision `planned|issued|claimed → issued` (`configs.py:123-128`). In the same transaction the worker writes `households.runtime_ref` (`worker.py:2830`).
3. **launch** — guarded, then `provider.launch` if the adapter has one (`worker.py:2930`).
4. **settle** — `_settle_runtime_ready` (`worker.py:2937-2973`) settles the job `succeeded` and marks the external resource `ready`.

All four phases are gated by currency checks reading `households.status`, `current_config_revision`, `runtime_ref` and the workflow state; the reconcile-time predicate is `_runtime_projection_is_current` (`worker.py:2753`).

### Claim and activation

`BootstrapService.claim` (`bootstrap.py:152-212`) validates the token binding, expiry, and that the revision is `issued` or `claimed`, then writes exactly two things: `bootstrap_tokens.claimed_at` (`:194-197`) and `config_revisions.status='claimed'` with `claimed_at` (`:198-202`, guarded `status='issued'`). It performs no consent check.

`BootstrapService.activate` (`bootstrap.py:214-485`) is where a revision becomes live. Its fresh path validates consent currency (`:307-313`), claim-before-activate (`:318-319`), a `claimed` revision exactly (`:322-336`), manifest re-hash (`:337-339`), email health (`:340-351`), onboarding step verification (`:355-366`), and owner membership (`:409-415`). Its write set:

| # | Table | Written |
|---|---|---|
| 1-3 | `email_identities`, `email_activation_receipts` | identity to `activating` then `active`; receipt upserted (`:379-408`) |
| 4 | `config_revisions` (previous) | `status='superseded'` (`:416-420`) |
| 5 | `config_revisions` (this) | `status='active'`, `activated_at` (`:421-424`) |
| 6 | `bootstrap_tokens` | `used_at` (`:425`) |
| 7 | `households` | `status='active'`, `current_config_revision` (`:426-430`) |
| 8-9 | `onboarding_workflows`, transition log | skipped entirely when the workflow is already `complete` (`:455-460`) |

Rows 4 and 5 running in one transaction are the only thing enforcing "at most one active revision per household" — there is no partial unique index (see Architecture Documentation).

### Every place that decides "which revision is serving"

| Fact | Writer(s) | Meaning |
|---|---|---|
| `config_revisions.status='active'` | `bootstrap.py:421-424` only | the revision genuinely being served |
| `households.current_config_revision` | `rollout.py:99-103` (at queue), `bootstrap.py:426-430` (at activation), `worker.py:2734-2738` (on some failures) | the revision being rolled out |
| `households.status` | `rollout.py:99-103` → `provisioning`; `bootstrap.py:426-430` → `active` | whether a rollout is in flight |

`config_revisions.status` full lifecycle, with the guard each writer uses: `planned` (`configs.py:49-63`) → `issued` (`configs.py:123-128`, guard `IN ('planned','issued','claimed')`) → `claimed` (`bootstrap.py:198-202`, guard `='issued'`) → `active` (`bootstrap.py:421-424`) → `superseded` (`bootstrap.py:416-420`). `revoked` is written from four places: `worker.py:627-631` (content-restriction block), `onboarding/service.py:1341-1345` (reset, includes `active`), `:1435-1439` (cancel, excludes `active`), `privacy/withdraw.py:165-170` (withdrawal that stops the runtime).

### Consumers that act without consulting it

**The gateway.** `WhatsAppGatewayRouter.route` (`gateway/whatsapp_router.py:98-137`) has two lookups — HMAC-strict at `:114` and plaintext at `:127` — both `WHERE channel = ? AND external_id[_hmac] = ?`. Neither joins `households` or `config_revisions`. The router is constructed with the database, relay keys and the sender HMAC key (`:87-89`) and holds no field cipher, so it cannot read a manifest to answer the question another way.

**`_settle_runtime_ready`.** Settles `succeeded` at launch (`worker.py:2959-2966`). Its guard accepts `households.status` of either `provisioning` or `active` (`:2949-2958`) and leaves it as found. Activation is the only writer of `active`.

The complete consumer set for `channel_bindings` outside migrations is small: the lifecycle in `repositories/bindings.py`, the DSAR export (`control_plane/privacy/export.py:245-248`), the planner's projection via `verified()`, the sweep's read (`rollout.py:146`), and the gateway's two lookups.

### Terminal rollout failure

**Nothing retires a binding.** Production writers of `channel_bindings` are: `_insert` (`bindings.py:788-803`), the two `_retire_superseded` deletes (`:377-385`), migration `0010`, and `ON DELETE CASCADE` from household erasure (`0007_channel_bindings.sql:4`, `privacy/delete.py:384`). `_retire_superseded` is reached only from `ensure_owner_binding`, itself called only from `planner.py:160-167` — a planning path. No failure or recovery path touches the table.

**Nothing marks the revision.** `schedule_runtime_rollout` never writes `config_revisions`. On failure the revision stays `planned` (failed before `issue_bootstrap`) or `issued` (failed after) indefinitely; the only worker path writing `revoked` (`worker.py:627-631`) is preceded by a workflow-state gate (`:611-617`) that a `reprovision_runtime` job never passes, because its workflow is `complete` by construction (`_workflow_states_for`, `worker.py:148-150`).

**Three shapes strand a household**, each leaving `status='provisioning'` with no leasable job (`JobsRepository.lease` selects only `pending`, expired-lease `running`, and a narrow `waiting_user` case — `repositories/jobs.py:167-175`):

1. `reprovision_runtime` fails *after* prepare. `_mark_step_problem` calls `_restore_settled_household` (`worker.py:2249-2253`), which returns without writing when an `external_resources` row already exists for the revision (`:2718-2724`) — and `_finish_runtime` creates that row as soon as prepare returns.
2. `reprovision_runtime` fails at the consent precondition. `_block_for_missing_content_restriction` settles the job `failed` (`worker.py:600-606`) then returns before any household write, because the workflow is `complete` (`:607-617`).
3. Any runtime job settles `succeeded` and the runtime never activates. Nothing sweeps expired bootstrap tokens back into a household status; `claim`/`activate` simply refuse with `BootstrapGone` (`bootstrap.py:173-176`).

`reconcile` refuses anything that is not `outcome_unknown` (`worker.py:1911-1912`), so a `failed` job has no operator path at all.

**The sweep cannot repair them.** `find_stale_bindings` considers only households with `current_config_revision > 0` (`rollout.py:182-184`), and `_blocked_by` (`:228-267`) returns `household_status_<status>` for anything not `active`; `reconcile_stale_bindings` then records `action="skipped"` and continues (`:305-308`).

## Code References

- `control_plane/api/bindings.py:122-179` - verify endpoint: binding write, plan, and rollout scheduling in one transaction
- `control_plane/repositories/bindings.py:508` - `verify_challenge`; `:756`/`:788-803` - the only INSERT
- `control_plane/repositories/bindings.py:377-385` - the only non-erasure deletes, on the planning path
- `control_plane/provisioning/planner.py:196` - binding → `ChannelBindingV1` projection; `:245` - revision creation
- `control_plane/provisioning/rollout.py:99-103` - `current_config_revision` advanced at queue time
- `control_plane/provisioning/rollout.py:228-267` - `_blocked_by`; `:305-308` - stranded households skipped
- `control_plane/provisioning/worker.py:2792` - `_finish_runtime`; `:2812-2821` token issue; `:2830` runtime_ref; `:2930` launch
- `control_plane/provisioning/worker.py:2937-2973` - `_settle_runtime_ready`, succeeded at launch
- `control_plane/provisioning/worker.py:2683-2738` - `_restore_settled_household` and its two early returns
- `control_plane/provisioning/bootstrap.py:152-212` - `claim`; `:214-485` - `activate`; `:416-430` - the activation flip
- `control_plane/repositories/configs.py:49-63` - `planned`; `:123-128` - `issued`
- `gateway/whatsapp_router.py:87-89` - router construction (no cipher); `:98-137` - the two sender lookups
- `control_plane/repositories/jobs.py:167-175` - what `lease` can return
- `control_plane/migrations/0001_control_plane.sql:216-240` - `config_revisions` schema, CHECKs and the immutability trigger

## Architecture Documentation

- **The manifest is a projection, not a second record.** Everything the runtime sees is derived from `channel_bindings` at plan time (`planner.py:196`), which is why the table and the served manifest can diverge without either being internally inconsistent.
- **Currency guards ask about the rollout, not about what is live.** All four runtime checkpoints compare against `households.current_config_revision` and `runtime_ref` — the rollout's own coordinates. This is deliberate; their question is "is this job still acting on the state it was leased for".
- **Status ordering is enforced in application SQL, not the schema.** `config_revisions` has a status CHECK (`0001_control_plane.sql:224-226`) and an immutability trigger covering six payload columns (`:235-240`), but `status` is not among them. Nothing at the database level prevents `planned → active` or two `active` rows; that invariant rests entirely on the supersede and activate statements sharing one transaction (`bootstrap.py:416-424`).
- **Token liveness is three nullable timestamps**, not a status column: `claimed_at`, `used_at`, `revoked_at` (`0001_control_plane.sql:242-256`). `activate` sets `used_at` and never `revoked_at`.

## Historical Context (from thoughts/)

- `thoughts/shared/plans/2026-08-23-go-live-checklist.md:123` - C3c recorded rather than fixed: "closing it needs a staged/published lifecycle on the binding, not a predicate" (`:429-432`)
- `thoughts/shared/plans/2026-08-23-go-live-checklist.md:135` - C3e records two stranding paths; this research finds three (the consent-precondition shape is not among the two)
- `thoughts/shared/plans/2026-08-23-go-live-checklist.md:374-383` - C3b's recovery deliberately narrowed to pre-mutation failures, keyed on the `external_resources` row, "which leaves the original symptom rather than replacing it with a quieter one"
- `thoughts/shared/plans/2026-08-27-c3b-revision-rollout.md` - the four currency checkpoints and why re-provisioning is its own operation
- `thoughts/shared/plans/2026-08-28-c3a-identity-debt.md` - D1 introduced `find_stale_bindings` / `reconcile_stale_bindings`

## Related Research

- `thoughts/shared/research/2026-08-05-real-dns-nerve-login-cleanup-runbook.md` - earlier research on Phase 2.4 validation and cleanup

## Open Questions

- **Does any runtime actually call `claim`/`activate` today, or is activation only exercised by tests?** The bootstrap protocol exists and is tested (`tests/control_plane/test_bootstrap.py`), but this research did not establish whether a deployed runtime performs it, which bounds how often stranding shape 3 occurs in practice.
- **What should `households.current_config_revision` mean?** It is written at queue time and again at activation, so it names different things at different moments. Whether that is intended or incidental is not recorded anywhere found.
- **Is there an intended sweep for expired bootstrap tokens?** `observability.py:125-128` counts `expired_bootstrap` as a health blocker, but nothing acts on it.
- **Permalinks**: not applicable — commit `48bbdfb` is not pushed to any remote branch.

---
title: "Go-Live Checklist — From Synthetic-Only to First Pilot Tenants"
status: active
created_at: "2026-08-23"
repository: abrolia
parent_plans:
  - thoughts/shared/plans/2026-08-06-canon-execution-plan.md
  - thoughts/shared/plans/2026-08-06-phase-DE-pilot.md
  - thoughts/shared/plans/2026-08-22-phase-F-closure-design-pass.md
scope: go-live
data_policy: synthetic-only-until-explicit-gates
---

# Go-Live Checklist — From Synthetic-Only to First Pilot Tenants

Ground-truth audit performed 2026-08-23 against the merged tree (#70 on main),
after the canon validation of the same date. Every verdict below carries
file:line evidence from that audit; the Phase F acceptance items were verified
green in `thoughts/shared/implementations/2026-08-23-canon-execution-plan-validation.md`.

## Why the doors are still locked (the one-paragraph version)

**Updated 2026-08-31.** Track C is closed; the doors are held by Track O alone.

C3f closed the same day it was opened, and the correction that opened it stands
as written below — the 2026-08-30 validation filed the web-binding gap as a nit
and declared the track closed in the same pass, and review on #101 refused that. The first version of this line
said Track C was closed outright, which review on #101 refused — correctly, and
against evidence already in this repository: the 2026-08-30 validation report
flagged the web-binding gap as unfinished canon scope in the same pass that
declared the track closed. A promotion gated on that sentence would have shipped
with the second adult unable to use Web at all.

`docs/privacy/processors.md:3` states that DPA + SCC are in effect for P1
(Anthropic) and P4 (Resend), and that **P2 (Fly.io) has no executed DPA, no TIA,
and no Art. 27 representative**. B-07 is therefore still open and the data
policy remains synthetic-only. Every product flag is off by design.

The three half-built Phase E surfaces this paragraph originally named are done:
`channel_preferences` has a writer and a consumer (C4a/C4b), the
`channel_bindings` challenge → verify lifecycle exists and is staged/published
against the revision that authorizes it (C3/C3a/C3c), and the web chat runs on
the household runtime's own model loop (C2). What remains is operator work —
the legal pack, a release tag and restore drill, and the per-transition live
batteries — then the staged flips the runbook already fixes.

## Track O — Operator/legal gates (cannot be delegated; block ALL real data)

> **Sequenced runbook: `docs/canon-closure-runbook.md`** (added 2026-09-01).
> Every remaining open canon box in one dependency-ordered list, with the
> acceptance artifact or command each is measured by, each command run against
> the checkout before being written down. Track O's boxes are O1–O11 there.

- [ ] **O1. Legal pack signatures (B-07, P0).** DPA 28(3) + SCC module 2 + TIA
  for P1 Anthropic, P2 Fly.io, P4 Resend. **Partially executed as of
  2026-08-30**: P1 and P4 carry ✅ DPA + ✅ SCC (incorporated by reference,
  copies committed under `docs/privacy/vendor-dpas/`); **P2 Fly.io remains ⏳ on
  all three**, and every TIA is still ⏳, as is the Art. 27 representative. The
  Art 9(2)(a) condition was recorded by counsel 2026-08-12
  (`docs/privacy/dpia.md` R7). Remaining for this box: the Fly.io DPA, the
  TIAs, and the Art. 27 appointment;
  `processors.md` registry updated with dates ONLY AFTER signature (canon
  Phase A rule). Gate: nothing in Track R starts before this box is ticked.
#### Inventory — O0 the deploy gate that blocked its own remedy

**Files:** `.github/workflows/deploy-production.yml`,
`deploy/control-plane/readyz-deploy-gate.jq`,
`tests/control_plane/test_deploy_gate.py`, `tests/test_deploy_workflow.py`,
`docs/onboarding-runbook.md`,
`deploy/control-plane/required-runtime-config.txt`,
`tests/control_plane/test_required_config.py`,
`control_plane/backup.py`, `control_plane/db.py`,
`deploy/control-plane/Dockerfile`,
`tests/control_plane/test_migrate_on_start.py`,
`docs/control-plane-restore.md`,
`docs/canon-closure-runbook.md`,
`tests/test_canon_closure_runbook.py`,
`thoughts/shared/implementations/2026-09-02-v0.1.0-restore-drill.md`, `control_plane/observability.py`,
`control_plane/api/app.py`, `control_plane/models.py`, `control_plane/cli.py`,
`tests/control_plane/test_serve_maintenance.py`,
`control_plane/migrations/0016_boot_archive_outcome.sql`,
`tests/control_plane/test_db.py`, `tests/control_plane/test_observability.py`,
`tests/control_plane/test_schema_contract.py`,
`tests/control_plane/test_export_delete.py`.

**Branches:** `fix/deploy-gate-backup-deadlock`,
`fix/deploy-required-secrets-preflight`, `fix/deploy-verify-same-predicate`,
`fix/readyz-gate-status-contract`, `fix/boot-durability-archive`,
`docs/canon-closure-operator-runbook`,
`docs/runbook-completeness-and-rollout-gates`,
`fix/boot-archive-failure-is-visible`,
`docs/manual-backup-does-not-trap-the-writer`,
`docs/v0.1.0-release-tag-and-restore-drill`,
`fix/o2-stays-open-until-the-staging-drill`,
`feat/backups-do-not-depend-on-deploys`,
`docs/canon-status-2026-09-02`,
`docs/o2-nerve-cross-org-closed`.

**Round 2, 2026-08-31 — what the gate fix uncovered.** Removing the mask let a
deploy through for the first time since 2026-08-22, and the new image would not
boot: `ABROLIA_RUNTIME_MODEL_API_KEY` became required at boot in #71
(2026-08-25) and was set nowhere. The machine crash-looped ten times and
stopped, so production went DOWN — the readiness gate had been accidentally
shielding a deployment that was already broken, and the shield was always going
to fail the moment anyone deployed.

Restored by setting the secret (operator-run; the value never entered this
repository or a transcript). Now guarded:
`deploy/control-plane/required-runtime-config.txt` lists every boot-critical
name, and the deploy resolves each against the app's Fly secrets or
`fly.toml [env]` BEFORE it mutates anything.

The list is proved by execution, not by copying the rules:
`tests/control_plane/test_required_config.py` removes each name from an
otherwise-valid production environment and asserts the boot actually refuses —
so a name that stops being required fails the suite rather than quietly
over-demanding. Writing it that way immediately found one over-declaration:
`ABROLIA_CONTROL_PLANE_BACKUP_KEY` does NOT refuse `serve`, and is conditionally
fatal instead — the machine starts fine until a deploy carries a migration, and
then `migrate --backup-first` fails closed and it will not start at all.

**Round 3, 2026-08-31 — the same fix, one step later.** The deploy after #114
preflighted, gated, mutated and came up HEALTHY on v44 — and still reported
failure, because the POST-deploy verification kept the two defects the
pre-deploy gate had just been fixed for: `curl --fail` dying on the 503, and
`.status == "ready"` demanding a backup that only a migration-carrying deploy
can produce.

An instance was fixed and the invariant was not, which is the third time that
shape has appeared in this stretch of work. Both ends now read one predicate
file, and `test_both_ends_of_the_deploy_ask_the_same_readiness_question` pins
that they keep doing so.

Dry-running the check against the real app before shipping found a second bug in
it: `flyctl secrets list --json` returns `name`, not `Name`, so the first
version reported EVERY secret missing and would have failed every deploy — a
safety check that becomes a new outage. Both directions are now verified against
production: all names present today, and the historical absence of
`ABROLIA_RUNTIME_MODEL_API_KEY` is caught by name.

Found 2026-08-31 while checking why a merged PR had not shipped: **production
deploys had been failing for nine days**, and every merge since 2026-08-22 —
C3f, C5, C6, B-02 — is sitting on `main` unshipped.

`/readyz` reports `backup_stale` past 26 hours
(`MAXIMUM_BACKUP_AGE_SECONDS`, `control_plane/api/app.py:38`). The archive is
written at CONTAINER START by the Dockerfile's `migrate --backup-first`, and
while the service runs it cannot be written at all: the backup CLI takes the
process lock and refuses with "Stop the service first."
(`control_plane/cli.py:274-278`). So a deploy is the only thing that refreshes
the backup — and the deploy gate required a fresh backup.

The timeline is exact: last success 2026-08-22 09:34Z, first failure
2026-08-23 11:59Z (26.4 hours later, the first attempt past the threshold),
then fourteen consecutive failures, none of which reached `flyctl`. The app was
healthy throughout — `/healthz` 200, workers RUNNING, no pending jobs. Only the
backup clock had run out.

`backup_stale` is now the one blocker that does not hold a deploy, because it
is a reason TO deploy. Every other blocker still does: a database, volume,
worker or provider problem is a state a deploy makes worse. The predicate lives
in a `.jq` file that the workflow and the test both read, so the gate and its
test cannot drift.

**Round 4, 2026-08-31 — the exception inherited a hole, and the deploy shipped.**
Consolidating both ends onto one predicate was right, and it carried one thing
the post-deploy check had been doing for free: rejecting a body that states no
readiness status at all. The `backup_stale` arm tested only the blocker list,
so `{"blockers":["backup_stale"]}` and `{"status":"broken","blockers":
["backup_stale"]}` were both read as a green light — on the surface that
previously refused them, so this was a real loss of coverage rather than a
theoretical one. Codex raised it as a P1 `[BLOCKER]` on #115; the
`review-lane-fast` topic meant the finding did not hold the merge, so it landed
on `main` and was fixed forward in #117. The arm now names the status it
excuses, both consumers inherit it from the one file, and two regressions land
with it: the malformed-status cases as a parameterized class, and a guard that
the workflow still evaluates this filter at both ends rather than growing a
second inline spelling.

**Shipped 2026-08-31 19:26Z.** Run `33430439419`: authorize, control plane and
landing all green — the first successful production deploy since 2026-08-22,
ending nine days in which every merge sat on `main` unshipped. The control
plane came up on the new image (machine `85e649c449e9e8`), `/healthz` reports
`healthy` with no blockers, and `/readyz` reports `not_ready` with
`backup_stale` alone, which is exactly the shape the gate now accepts at both
ends.

**Still open after the deploy, and not a gate defect.** `backup_stale` did not
clear, and will not: `create_pre_migrate_backup` returns `None` when no
migration is pending (`control_plane/backup.py:75-76`), so the boot-time
archive is written only by a deploy that happens to carry one. This step
already said as much in Round 3 — "a backup that only a migration-carrying
deploy can produce" — but the consequence is now observable rather than
predicted: production's newest archive is ~237 hours old and ageing, and no
deploy will refresh it on its own. The gate is right to stop blocking on it.
Investigating why turned up something worse than the trigger — see Round 5.

**Round 5, 2026-09-01 — the readiness signal had no writer at all.** "Only a
migration-carrying deploy can produce it" was still too generous. The two
halves were never connected in the first place:

- `/readyz` reads the newest `<db>.parent/backups/*.cpb`
  (`observability.py:150-157`). That is its entire definition of "the backup".
- The only automated writer, `create_pre_migrate_backup`, writes
  `<db>.pre-migrate-<rev>-<epoch>.bak` **beside the database** — a different
  directory AND a different extension — and only when a migration is pending.

So even a migration-carrying deploy would not have cleared `backup_stale`.
`.cpb` appears exactly once in the whole of `control_plane/`, at the line that
READS it; nothing writes one. The archive production was ageing from was an
operator's manual `abrolia-control-plane backup`, and after it aged out
nothing was ever going to replace it. The gate comment's premise — "the
archive is written at CONTAINER START by `migrate --backup-first`" — was
simply false, and it is the reason the deadlock looked like a clock problem
rather than a missing feature.

The two archives stay distinct, because they are distinct: a pre-migrate
snapshot is a rollback point for one migration and FAILS CLOSED, while a
durability archive is a floor and must never stop a container from starting.
`create_boot_archive` writes the second one, into the directory `/readyz`
actually reads, on every boot past a 6-hour interval:

- **Interval, not content, for crash-loop protection.** The pre-migrate path
  compares images because a failed migration leaves the database
  byte-identical; here it legitimately differs every boot, so content would
  never match and every restart would write a full archive onto a 1 GiB
  volume. Six hours sits well under the 26-hour staleness threshold, so a
  daily deploy keeps the signal green, and bounds a restart loop to four
  archives a day.
- **Non-fatal.** Every failure is reported and skipped. A full volume must not
  become an outage, which is exactly the difference from `--backup-first`.
- **Retention prunes only `boot-*.cpb`.** Operator archives live in the same
  directory — the runbook puts them there — and pruning by extension would
  delete the offsite copy taken before a risky migration, which is the one
  archive that matters most. That property has its own regression.

Nine tests land with it, including the defect stated directly
(`test_backup_first_alone_writes_nothing_the_readiness_probe_can_see`) and a
restore round-trip, because an archive that cannot be restored is not a
restore point. The probe's own accessor is used to assert visibility rather
than a second opinion about the path.

- [x] **O0a. Backups must not depend on deploys.** *Closed 2026-09-02 by an
  in-process periodic archive — the first of the two options this box named.
  Opened 2026-08-31, and deliberately NOT fixed then, because it needed a
  design rather than a cron entry.* The only backup path runs at container start, and the CLI refuses to
  run beside a live service, so today the system can only back up by
  RESTARTING. That is why the 26-hour alarm was really measuring deploy
  cadence, and why it will fire again on any quiet day. The honest options are
  an in-process periodic backup (the serving process already holds the lock) or
  a scheduled restart; both are real work and neither is a workflow file.
  Until one lands, `/readyz` reports `not_ready` on any day without a deploy.

  **The in-process option landed.** `take_periodic_archive` runs from the
  `serve` worker loop on the same five-minute tick the retention and
  runtime-health tasks already use, and `create_boot_archive` still owns the
  decision — the loop asks often and cheaply, the interval decides, so a
  frequent tick does not mean a frequent archive.

  Two design points, both load-bearing:

  - **It opens its OWN connection.** `_materialise` calls
    `connection.backup()` OUTSIDE the database mutex, so driving it from the
    worker thread on the serving connection would race the API threads
    already on it. A second connection to the same file is what SQLite's
    online backup API is for, needs no lock held across a full-size copy, and
    so cannot stall request handling.
  - **`preserve_journal_mode=True`**, because opening a connection normally
    sets `journal_mode=WAL` and REWRITES the header — of a file another
    connection is serving from. That property is pinned directly rather than
    behaviourally: the database is already WAL in every case the suite builds,
    so setting it again is a no-op and a mutation dropping the flag passed
    every behavioural assertion.

  Failure stays non-fatal and is recorded where `/readyz` reads it, so a
  periodic writer that starts failing says `backup_writer: failed` rather than
  looking like a quiet week.

  **Round 2, 2026-09-02 — Codex found the defect returning through two other
  doors, and was right about both.** The first cut put the archive last inside
  the loop's single shared `try`, so a task that failed persistently —
  `retention.run()` raising, leaving its own "next at" unadvanced so it
  retried on every 0.5s tick — jumped to the outer handler before the archive
  branch was ever reached. Service up, nothing visibly wrong, stale backup 26
  hours later: O0a exactly, by a different route. Each task now advances its
  own schedule BEFORE running and has its own failure boundary, and the
  archive runs first because its starvation is the silent one.

  The second: `_take_periodic_archive` caught only `(BackupError, OSError)`,
  while `take_periodic_archive` reaches `PRAGMA integrity_check`, a second
  `sqlite3.connect` and `connection.backup()` — all of which raise
  `sqlite3.Error`, which is neither. Those escaped without recording an
  attempt, so `/readyz` kept reporting the previous `written` while nothing
  was being written. That is the invisibility `backup_writer` exists to end,
  reintroduced by the writer it was built for.

- [ ] **O2. Release tag + restore drill.** *Tag DONE; drill PARTIAL — the
  staging half is unperformed, so this box stays open — 2026-09-02, evidence in
  `thoughts/shared/implementations/2026-09-02-v0.1.0-restore-drill.md`.*
  `v0.1.0` is the first tag this repository has ever carried, annotated at
  `cc86bfb`. The rehearsal ran against the CURRENT Phase E schema (27 tables,
  16 migrations, newest `0016`): 56 checks, 0 failures — integrity and
  foreign-key checks clean, 0600 pause marker, row counts identical table by
  table, the Phase E surfaces verified by content, all four refusals (existing
  target, wrong key, tampered archive, corrupt file), a smoke without leasing
  whose export carried no credential hash, `resume-jobs` resuming only on
  request, and a NEW onboarding driven to `config_revisions.revision = 1` on
  the restored database.
  **Why this box is NOT ticked.** The staging half is unperformed: an isolated
  volume on a Machine with no public route, the actual production archive
  rather than an equivalent database at the same schema, and the teardown.
  Step 6's job reconciliation is also unexercised, because the seeded jobs
  were `pending` and nothing was `running`/`outcome_unknown`.

  It was briefly ticked on 2026-09-02 with that caveat written beside it, and
  Codex was right to refuse it (#128). This box is a prerequisite for the live
  batteries and for promotion, so a tick here is read downstream as "recovery
  is proven" — and half a recovery exercise must not be able to satisfy a
  real-data gate. The schema evidence is real and stands on its own; it is
  partial evidence toward this box, not closure of it. Ticking resumes when a
  staging transcript exists.
  Original text: Zero git tags exist today. Tag the
  release, then repeat the Phase B isolated backup/restore procedure on the
  Phase E schema (now incl. `channel_preferences`/`channel_bindings`/usage
  tables): `integrity_check`, `foreign_key_check`, pause marker 0600, smoke
  without leasing, resume-jobs, new onboarding through rev 1, destroy temp app.
  Backup-before-migrate itself already landed and fails closed
  (`tests/control_plane/test_migrate_on_start.py`).
- [ ] **O3. Per-transition live gates.** Each promotion in Track R requires its
  manual battery per `docs/onboarding-runbook.md` §Rollout (~line 484):
  receive, approved send, restart/cursor resume, reconnect, export, revoke,
  delete — recorded PII-safe.

## Track C — Code closure (synthetic-safe; ordered by dependency and leverage)

True-state summary from the 2026-08-23 audit. **Every row below was closed
between 2026-08-23 and 2026-08-29** by the slices listed against it; the table
is kept as the record of what the audit found, not as current state:

| Surface | Closed by |
|---|---|
| Cost caps | C1 — `budget_exceeded` emitted at all three uncapped call sites |
| Observability | C1 + C4b — `budget_exceeded` and `primary_unavailable` now actually emitted |
| Web chat | C2 — runtime model loop, CSRF, e2e tests |
| Channel prefs | C4a + C4b — writer, fallback reference, consumer |
| Channel bindings | C3 + C3a + C3c — challenge lifecycle, sender/chat split, staged/published |
| Shared WA gateway | C5a–C5e — one signature, WAL reader, relay key, entrypoint, lookup |
| Web bindings | C3f — the seat, the account mapping, and the role the runtime derives |

The original audit rows:

| Surface | Storage/schema | Enforcement/runtime | Lifecycle/wiring | Verdict |
|---|---|---|---|---|
| Cost caps | ✓ `0008_usage.sql` | ✓ email-extraction only | ✗ dialogue+web uncapped | PARTIAL |
| Observability | ✓ loggers both sides | ✓ `/healthz`+runtime `/health` | ✗ `ALERTS` defined, emitted nowhere | PARTIAL |
| Web chat | ✓ PWA packaged/served | ✓ session auth + server-side scoping | ✗ model path unwired (`web_loop` set nowhere); ✗ CSRF on `/api/web/message`; no e2e test | PARTIAL |
| Channel prefs | ✓ table+CHECKs | ✗ zero consumers/readers; fallback-sender absent; self-injection check is dead code | ✗ no write path | PARTIAL |
| Channel bindings | ✓ schema w/ authorization columns | ✓ RunContext derivation proven (manifest pairs, cross-pair denial) | ✗ no challenge flow, no writer, real external IDs rejected (`models.py:35-38`), second-adult flow absent | PARTIAL (biggest build) |
| Shared WA gateway | — | ✓ exact-match, deny unknown/ambiguous, inbound HMAC fail-closed (tested) | ✗ no ingress redelivery reader; outbound HMAC scheme mismatch vs runtime verifier; no HTTP entrypoint/deploy; no key provisioning | PARTIAL |

Ordered slices:

- [x] **C1. Cap every model call; emit the alerts that exist.** *(TODAY)*
  `is_over_budget` guards only `pipeline.py:284`. The WhatsApp dialogue
  (`pipeline.py:443`), Telegram dialogue (`pipeline.py:566`) and web channel
  (`hermes_cloud/channels/web.py:44`) call the model unchecked and unrecorded.
  Fix at the call site (repo invariant: precondition enforced where the provider
  is CALLED): over-budget dialogue replies with the honest degraded message
  instead of a model call; web mirrors it; `budget_exceeded` becomes the first
  actually-emitted member of `hermes_cloud/core/observability.py::ALERTS`.
  Prove in `tests/test_cost_caps.py`.
- [x] **C2. Wire the web chat to a real model path.** Decide the seam first:
  `control_plane/api/web.py:306` asks for `active.web_loop` which nothing sets,
  and `control_plane/` deliberately never imports the runtime runner. Either
  the household runtime serves chat (WSGI route beside `/health`) or the
  control plane proxies to it — a small design pass, then implementation.
  Same pass: add the same-origin/CSRF checks every sibling form endpoint has
  (`api/web.py:35-67` vs the bare :261-275), and the authenticated e2e test.
- [x] **C3. The binding lifecycle.** Challenge → owner verification → row write
  with real `verified_at/verified_by_actor_id`; lift the `synthetic-`-only
  restriction on external IDs (`models.py:232-237`) per channel with format
  validation; second-adult binding flow (planner currently hardcodes
  `family=(owner,)` — `provisioning/planner.py:137-146`). This is the
  foundation the gateway lookup and preferences routing both consume.
  **Scope corrected 2026-08-26.** The second adult is NOT deliverable in this
  slice and the reason is structural — see C3a below. What C3 delivers is the
  writer, the challenge lifecycle, and the manifest as a projection of the
  table.
- [x] **C3a. Separate a sender's identity from the chat it speaks in.**
  *Design written 2026-08-27:
  `thoughts/shared/plans/2026-08-27-c3a-sender-identity-and-chat.md`.*
  `channel_bindings.external_id` answers two incompatible questions:
  `gateway/whatsapp_router.py:128` matches it against an incoming SENDER, and
  the manifest projection reads it as `chat_id`, the place the assistant
  SPEAKS. For the owner the two coincide because onboarding wrote one value
  that happens to answer both; for a second adult on the same channel they
  cannot — the household's chat collides with the owner's row under
  `UNIQUE (household_id, channel, external_id)`, and an identity of their own
  makes the projection emit two verified chats for the primary channel, which
  `parse_runtime_manifest` (`runtime_manifest.py:342-347`) refuses outright.
  Both reproduced 2026-08-26. Needs a distinct `chat_id` column, a migration
  for 0007's rows, and the three consumers — gateway lookup, planner
  projection, runtime manifest — moved together. Blocks the second adult on
  the primary channel; adults on other channels already work.

- [x] **C3b. Roll a revision out to an already-active household.**
  *Design written 2026-08-27:
  `thoughts/shared/plans/2026-08-27-c3b-revision-rollout.md`. Sequencing
  corrected there — C3b now waits on C3a, because its acceptance criterion
  cannot be written honestly until a second member is representable.* Verifying a
  binding plans revision N, but nothing schedules `ensure_runtime` or advances
  `households.current_config_revision`, so the runtime stays on N-1 and never
  sees the new member. The only existing path
  (`provisioning/worker.py:2579-2606`) also flips the household to
  `provisioning` and drives the ONBOARDING workflow to `runtime_provisioning`
  — first-time semantics that a live household gaining a member should not
  inherit. Re-provisioning is its own lifecycle, not a copied block.

- [x] **C3c. A binding must not route before the revision that authorizes it.**
  `gateway/whatsapp_router.py:113-116, 126-129` resolves a sender with no
  household or revision predicate, and `verify_challenge` commits the row
  immediately. So between the write and activation the gateway routes the new
  member's traffic to a runtime still serving N-1, whose manifest has no pair
  for them — the runtime denies it and the member's message goes nowhere. C3b
  narrowed that window from unbounded to "until the rollout activates"; it did
  not close it. Closing it needs a staged/published lifecycle on the binding:
  the planner includes staged rows, the gateway exposes them only once
  `BootstrapService.activate` publishes the revision, and a terminal rollback
  retires them. Its own slice because it is a lifecycle, not a predicate.

- [x] **C3e. A rollout is not terminal until a revision is active.** Two
  stranding paths this slice does NOT close, both found in review and both
  needing a lifecycle rather than a patch. (1) `_settle_runtime_ready` settles
  `succeeded` right after launch, so a runtime that never claims its token, or
  whose activation is rejected, leaves the household `provisioning` at N with a
  succeeded job `lease` will never revisit — and the failure recovery handles
  only `failed`. (2) After a failure that reached the provider, the Machine may
  already carry N's config, so no database-only write can honestly say which
  revision is serving. The fix for both is the same shape: keep the job
  non-terminal until activation, link the bootstrap token to it, and reconverge
  the provider before declaring a previous revision active. C3b's recovery is
  deliberately narrowed to pre-mutation failures so it never asserts what it
  cannot know.

- [x] **C3d. Tests that reach what they claim to cover.** Two C3b regressions
  assert the right things without touching the path they are named for. The
  rollout test calls the repository, planner and `schedule_runtime_rollout`
  directly, bypassing `verify_binding_challenge` — delete the endpoint's
  scheduling call and every test stays green. The currency-guard test calls
  `_workflow_states_for` alone, so none of the four checkpoints runs with a
  stale revision or runtime ref; drop the revision clause from any of them and
  nothing fails. Needs one authenticated HTTP regression carrying a
  secondary-channel binding through worker, claim and activation, and a
  parameterized worker regression over the checkpoints and the fields each one
  actually reads.

  **Scope narrowed 2026-08-28, by measurement.** This first asked for "both
  operations × both stale fields × all four checkpoints". Two thirds of that
  matrix pins nothing, and deleting each clause and counting failures is how
  that was established rather than argued:

  * the two operations exercise the same clauses. What differs between them is
    `_workflow_states_for`, which has its own test, so every case ran twice and
    failed twice for one cause;
  * the checkpoints do not all read both fields. The bootstrap checkpoint and
    the reconcile predicate never read `runtime_ref`, so a case asserting they
    refuse a stale one would assert something untrue — and for reconcile there
    is no expected reference to compare against at all until the provider has
    answered.

  What is required is one case per (checkpoint, field that checkpoint reads),
  which is five plus the reconcile case. Every clause is then pinned by exactly
  one case.

- [x] **C3f. The web binding the runtime never reads.** *Done 2026-08-31 —
  `thoughts/shared/plans/2026-08-31-c3f-web-binding-runtime.md`.* *Opened 2026-08-31 by
  review on #101; the gap itself was recorded in the 2026-08-30 validation
  report and wrongly filed as a nit.* `web` is a first-class binding channel
  (`control_plane/repositories/bindings.py:94`, `models.py:260`) and the
  manifest carries verified web bindings — but the runtime does not consume
  them. `control_plane/api/web.py::web_message` resolves the caller's REAL
  membership role and forwards it (`api/web.py:322-329`, and it fails closed
  rather than defaulting to owner), while
  `hermes_cloud/runtime/service.py::_web_chat` rejects every role but `owner`
  and `web_chat_turn` hardcodes `manifest.actors.owner` with
  `allowed_chats={"web-chat"}` instead of deriving the pair from the published
  binding. So a verified adult Web binding is unusable end to end.

  The comment at `runtime/service.py:655` defers this to "C3 ... when bindings
  get a lifecycle" — a condition C3a and C3c satisfied, which is precisely how
  the deferral came to read as current while being stale. Canon Phase E item 4
  names Web among the channels whose binding derives `RunContext`, so this is
  promised scope, not an enhancement.

  Closes when the runtime derives the actor and chat from the published Web
  binding and an owner/adult regression carries both roles through the whole
  flow — or when the plan explicitly narrows Web to owner-only and says why.

- [x] **C4. Make preferences real.** Production write path (API/onboarding);
  consumer that routes replies/fallbacks; self-contained agent-inbox rejection
  (replace dead `_validate_no_self_ingestion`, persist the fallback ref);
  permanent-failure → short email fallback sender + `primary_unavailable`
  emission (`observability.py:104` label exists, never emitted).
  **Split 2026-08-29 into C4a and C4b**, because the write path and the
  consumer answer different questions and the second is larger than the audit
  read.

- [x] **C4a. Give `channel_preferences` a writer, and a fallback it can check.**
  The table has had a schema, CHECKs and an index since `0006` and no writer at
  all — `container.py` never even built the repository, so no code path could
  have written a row. Seeded now by `DesiredSpecPlanner.issue` from the same
  `primary_channel` result it builds `channels.primary` from.
  **`primary_channel` is a projection, not a second choice**, and no endpoint
  sets it: two records of one fact disagree the moment either moves, which is
  the conflation C3a spent five rounds removing from `channel_bindings`, and
  the way to change the channel is to re-run the step that proves it.
  **The fallback is a reference, not a copy**: `0011` adds
  `fallback_account_id`, the address stays in `accounts`, and the
  self-ingestion rule is answered by comparing `recovery_email_lookup_hmac`
  with the household's live `email_identities.address_lookup_hmac` — same
  `LookupHasher`, so no decryption and nothing supplied by the caller. That
  replaces `_validate_no_self_ingestion`, which read a row, described in
  comments what a real check would need, and returned None.

- [x] **C4b. The consumer, and the alert nobody emits.** Route replies and
  fallbacks through the preference; permanent-failure → short email fallback
  sender; emit `primary_unavailable` (`hermes_cloud/core/observability.py:106`
  — the label exists and nothing has ever emitted it, exactly as
  `budget_exceeded` did before C1). Its first question is one C4a deliberately
  did not answer: the runtime reads a manifest, not the control plane's
  tables, so a preference reaches it either as a manifest projection — which
  makes changing one a revision, with C3b's rollout behind it — or not at all.
- [x] **C5. Gateway plumbing.** Redeliver-from-`gateway_ingress` worker (rows
  are written and never read back); HTTP entrypoint + narrow deploy unit;
  relay-key provisioning path.

  **Correction: `provisioning/secrets.py` is not absent.** This box has said
  "planned, absent" since it was written, and the file has been on disk
  throughout — `FlySecretSink` and `InMemorySecretSink`, installing secret
  material over stdin, used by the email providers and by the runtime launch
  path. `tests/control_plane/test_channel_bindings.py` repeats the claim in a
  comment. What is actually missing is narrower and worth naming precisely,
  because the wrong version of it made C3c pin a test against a file that
  already existed: **there is no relay KEY**. Nothing generates a per-household
  relay secret, nothing installs it as `ABROLIA_WHATSAPP_RELAY_SECRET`, nothing
  computes the `external_id_hmac` the strict-mode gateway matches on, and
  `WhatsAppGatewayRouter.relay_keys` is populated only by tests. The sink to
  put a key in is there; the key is not.

  Remaining, as three slices:

  - [x] **C5b. The ingress WAL is read back.** `GatewayStore` persists before
    ACK and deletes on delivery, and nothing between those ever reads a row —
    durability that was written and never spent. Also splits `hmac_rejected`,
    which answered four different questions and decided the row's fate by
    which `return` happened to run, into terminal and retryable outcomes: a
    payload a present key rejects can never verify, and keeping it stored a
    family's message body indefinitely.
  - [x] **C5c. The relay key exists.** *Done — see the inventory below.* Generate it, install it through the sink
    above, and backfill `external_id_hmac` — which C3c deliberately left NULL,
    with `test_hmac_column_stays_null_until_c5_provisions_the_key` as the test
    that should start failing here. Until this lands every binding is invisible
    to a strict-mode gateway, and C5b's `relay_key_absent` is the outcome that
    waits for it.
  - [x] **C5d. Something calls the gateway.** *Done — see the inventory below.*
  - [x] **C5e. Something the gateway can read bindings from.** *Done — see the
    inventory below.* Resolved as an authenticated lookup rather than a
    replicated projection, and the reason is specific: routability changes at
    exact moments and five slices exist to make them exact, so replication lag
    would sit inside that decision and re-open them. Original text follows.
     The deploy unit
    and the store lifecycle it needs. A gateway on its own Fly volume opens a
    database the control plane never wrote to, so it starts, passes its health
    check and routes nobody — every real sender an `unknown_sender`. It holds
    no field cipher by design (C5c's K1), so it cannot simply be given the
    control plane's database without the keys that separation exists to
    withhold. The two candidate answers — an authenticated lookup endpoint, or
    a replicated read projection — are different systems with different failure
    modes, which is why this is a slice and not a patch. C5d refuses to start
    on a missing database rather than inventing an empty one, which is the part
    that does not need this answer. `handle_webhook` has no caller
    outside tests and there is no `deploy/gateway/`. The HTTP entrypoint and
    its narrow deploy unit, plus whatever schedules C5b's worker — which C5b
    deliberately left uncalled rather than inventing a scheduler for a process
    that does not exist yet.

- [x] **C5a. One signature between the gateway and the runtime.** *Done — see
  the inventory below.* `relay_hmac` signed `body|timestamp` and
  `verify_webhook` verified the bare body, so the runtime rejected every
  delivery the gateway signed and no WhatsApp message could reach a household
  at all. Confirmed by running one payload through both ends rather than by
  reading them. Both sides had passing tests, because each signed and verified
  with its own helper — the fixture agreed with the code and the code
  disagreed with the other end.

  The gateway's scheme is the one kept, and not because it came first: a
  signature over the body alone leaves the timestamp unauthenticated, so a
  captured body can be replayed forever by attaching a fresh one and every
  freshness check ever added would pass it. The runtime now reads
  `X-Relay-Timestamp` — which the gateway has always sent and this end never
  read, the reason the drift went unnoticed — verifies `body|timestamp`, and
  enforces the same replay window the gateway does.
- [x] **C6a. A successful activation is not a stale projection.** *Added to
  this list 2026-08-30 — the slice merged as #96 with an inventory below but no
  box, so the checklist could not show it.* `_runtime_projection_is_current`
  required `household_status = 'provisioning'` and a workflow in
  `{runtime_provisioning, activating}` — the exact pair that SUCCESSFUL
  activation ends, so a job whose revision went live read as "not current".
  Found while reviewing #86 and deliberately not patched there.
- [x] **C6b. A retry path catches only the failures it is prepared for.**
  *Added to this list 2026-08-30 — merged as #97, same reason.* Deferred out of
  C6a, which said auditing it there would be a second change hiding inside the
  first. Three defects in the C5 slices shared one shape: a broad `except`
  around an operation with a RETRY POLICY turns a `NameError` or `TypeError`
  into a silent, permanent retry — a programming error wearing a transient
  error's clothes.
- [x] **C6. Box hygiene.** *Done 2026-08-30.* `phase-DE-pilot.md` went from 12
  open boxes to 3 (the two operator items and the CI-only gate box); its six
  Phase E acceptance commands now name tests that exist; the flag box was
  corrected from "six" to the four that survived #69/#70; and the stale
  "expect 626+ / 215+" suite counts were replaced with the measured 1686. In
  this file, thirteen merged slices were ticked, C6a/C6b gained the boxes they
  never had, and the opening paragraph and audit table — both of which still
  described the three half-built surfaces as current — were rewritten. In
  `canon-execution-plan.md`, four wrong test paths were fixed and the A/E boxes
  the evidence supports were closed. Original text: update
  `phase-DE-pilot.md` checkboxes to the audited
  truth (several `[ ]` are done-but-renamed — e.g. flags boxes closed by #70,
  preferences storage landed in `control_plane/migrations/0006` not
  `hermes_cloud/core/migrations/0008`), so the acceptance commands name tests
  that exist. Stale boxes mislead exactly like the retired flags did.

#### Inventory — C6 the commands a plan tells you to run

**Files:** `tests/test_plan_commands.py`, `tests/test_check_fixtures.py`, `.gitignore`.

**Branches:** `fix/plan-commands-and-web-binding-gate`, `fix/plan-guard-shell-spellings`, `fix/plan-guard-narrow-contract`.

Review on #101 found C6's own fix incomplete: the acceptance commands under
boxes were corrected, the Cross-Phase block was not, and the commit asserted
that every command had been re-run. Three plans carried the defect
(`phase-DE-pilot` four Phase E files + `tests/test_config.py` + `python -m
check_fixtures`, `phase-A` the same sanitizer line, `phase-C` three BYO test
files planned and never written).

Third occurrence of the class, so it is a rule rather than another fix:
`tests/test_plan_commands.py` asserts every `tests/**`, `scripts/**` and
`python -m` reference inside a fenced plan block resolves. Verified in both
directions — reintroducing each defect fails the test with plan and line.

The failure mode is why it is worth a test: `pytest a_missing_file.py` exits 4
and prints no test failures, so a gate that proves nothing looks exactly like a
gate that passed, and the box above it still gets ticked by whoever ran it.

`.gitignore` gains `.DS_Store`, which a `git add -A` had swept into the commit.

**Round 2, 2026-08-31.** Review found the guard itself half-built and one of the
fixes dishonest, and both were the same mistake as the round before — asserting
a command works without running it in the context the plan sends a reader to.

* `_TEST_PATH` matched only `*.py`, so DIRECTORY targets were invisible.
  `pytest tests/control_plane/email -k byo` is the form five plan commands use,
  and a stale directory exits "no tests ran" — quieter than a stale file. The
  guard now validates every `tests/**` and `scripts/**` argument, file or
  directory, and the mutation case is a missing directory.
* The sanitizer line was changed to a command that still cannot run locally:
  `--require-deny` exits 2 without the private deny-list, which this same
  session had documented twice. Both plans now split the LOCAL invocation
  (`--all`, exits 0, actually scans) from the CI-only one, and
  `tests/test_check_fixtures.py` pins both contexts so a plan cannot promise
  behaviour the script does not have.

**Round 3, 2026-08-31.** The guard shipped twice with holes, and the second
round's fix had the same shape as the first: a regex over raw text recognises
only the spellings its author pictured. `_PATH_ARGUMENT` anchored on a bare
`tests/` prefix, so `pytest "tests/x"` and `pytest ./tests/x` — ordinary shell,
both accepted by pytest — matched nothing and a stale target written either way
stayed invisible.

Now tokenised with `shlex`, so quoting and `./` are handled by the same rules
the shell uses rather than by a pattern. The fallback for an unbalanced quote
strips the quote characters itself, so prose inside a fence degrades toward
SEEING a path rather than passing one through.

The deeper fix is the coverage. Until this round the guard was exercised only by
whatever the plans happened to contain — which is exactly why both holes
shipped: no plan had a quoted target, so nothing failed when the parser could
not read one. `referenced_paths` is now a named function with its own
parameterized tests over five spellings × missing file and directory, plus the
no-false-positive and case-selector cases. A guard whose only coverage is the
data it currently governs proves the data is clean, not that the guard works.

**Round 4, 2026-08-31 — scope stated instead of chased, by operator decision.**
Review found `shlex` keeps an operator attached to the word before it, so
`pytest tests/x.py; echo done` tokenised as `tests/x.py;` and the guard reported
an EXISTING file as missing. Correct, and the fourth round on one function.

Two facts sized it: the failure is a false POSITIVE — the opposite direction
from the class this guard exists to catch, and loud rather than silent — and
there were zero such lines in any plan. The form every plan does use
(`… 2>&1 | head`) parses correctly, because a space-separated redirection is its
own word.

So the guard stopped trying to be a shell. The supported form is now STATED —
plan commands are space-separated — and an operator flush against a path is
reported as the style error it is, naming the missing space rather than blaming
a file that exists. Three rounds had each shaved one spelling and found another
waiting; a bounded rule the plans already follow ends that, where a better
parser would only have moved the edge.

Pinned by five operators × the attached form, and by the space-separated forms
the plans actually use, `2>&1` among them.

#### Inventory — C1 + C2, with the O1 evidence they shipped beside

**Files:** `hermes_cloud/runner/pipeline.py`, `hermes_cloud/channels/web.py`,
`hermes_cloud/core/observability.py`, `hermes_cloud/runtime/service.py`,
`control_plane/api/web.py`, `control_plane/container.py`,
`control_plane/runtimes/*`, `web/static/app.js`, `tests/test_cost_caps.py`,
`tests/test_runtime_web_chat.py`, `tests/control_plane/test_web_chat_api.py`,
`tests/control_plane/test_frontend_branding.py`, `docs/privacy/processors.md`,
`docs/privacy/vendor-dpas/*`, `.check-fixtures-allow`,
`hermes_cloud/runner/model.py`, `hermes_cloud/core/usage.py`,
`control_plane/config.py`, `control_plane/provisioning/worker.py`,
`tests/control_plane/conftest.py`,
`tests/control_plane/test_provisioning_jobs.py`, `tests/test_whatsapp.py`,
`docs/onboarding-runbook.md`, `tests/control_plane/test_config.py`,
`tests/control_plane/test_export_delete.py`,
`hermes_cloud/runtime/service.py`.

**Branches:** `codex/go-live-c1-c2`.

Three of these are not obvious from the slice titles, so they are named with
their reason rather than left to be inferred:

- `tests/control_plane/test_frontend_branding.py` — putting `/api/web/message`
  behind `require_private_mutation` changed the refusal ORDER on that endpoint
  (403 before auth). The branding test asserted the old order, so it is a
  consequence of C2, not an unrelated edit.
- `.check-fixtures-allow` — the archived vendor DPAs are third-party legal
  prose carrying vendor org contacts, which the fixture sanitizer reads as
  live contact data. Allowlisted under the landing-page org-contact precedent.
- `docs/privacy/*` — O1 is a Track O item, not Track C. Its evidence rode this
  branch because the DPA verification happened in the same sitting; the step
  declares it here so the path is not undeclared, and O1 itself stays open on
  P2, the TIAs and the Art 27 mandate.

The last seven arrived with the review round below and are consequences of
fixing C1's cap where the provider is actually called: `model.py` and
`usage.py` hold the guard, `config.py` and `worker.py` install the model
credential C2 needed and never had, and the three test files are the stubs
whose signatures the moved contract changed.

Track C's later slices get their own inventory sections and their own
`**Branches:**` lines. C3 must not be added to this one — a step that
accumulates branches stops bounding any of them.

#### Inventory — C3 binding lifecycle and the second adult

**Files:** `control_plane/repositories/bindings.py`,
`control_plane/migrations/0009_channel_binding_challenges.sql`,
`control_plane/api/bindings.py`, `control_plane/api/app.py`,
`control_plane/provisioning/planner.py`, `control_plane/container.py`,
`control_plane/repositories/__init__.py`, `control_plane/models.py`,
`control_plane/privacy/retention.py`, `control_plane/api/dependencies.py`,
`control_plane/api/web.py`, `tests/control_plane/conftest.py`,
`tests/control_plane/test_channel_bindings.py`,
`tests/control_plane/test_binding_api.py`, `tests/control_plane/test_db.py`,
`tests/control_plane/test_manifest.py`,
`tests/control_plane/test_consent_withdrawal.py`.

**Branches:** `codex/go-live-c3-bindings`.

Four of these are consequences rather than choices, named so they are not read
as scope creep:

- `control_plane/models.py` and `control_plane/privacy/retention.py` — a new
  table must declare a privacy classification and be retired by the sweep.
  Three separate gates fail otherwise, which is the schema contract working.
- `tests/control_plane/conftest.py`, `test_manifest.py`,
  `test_consent_withdrawal.py`, `test_db.py` — `DesiredSpecPlanner` gained a
  repository argument, so every construction of it moved, and the migration
  list is asserted explicitly.

**This step's `**Branches:**` line cannot make the gate pass while the branch
is stacked.** `codex/go-live-c3-bindings` sits on `codex/go-live-c1-c2`, which
is unmerged, so `git diff main...HEAD` returns the union of both slices and the
C1+C2 paths read as undeclared by THIS step. Adding them here, or adding this
branch to the C1+C2 step, would each defeat the bounding the gate exists for.
The scope becomes well-defined the moment #71 merges and this branch rebases
onto main — which is the honest reading: a stacked branch does not have a
scope of its own until its base has one.

#### Inventory — C3 review follow-ups

**Files:** `control_plane/repositories/bindings.py`,
`control_plane/provisioning/planner.py`, `control_plane/privacy/export.py`,
`tests/control_plane/test_channel_bindings.py`,
`tests/control_plane/test_export_delete.py`,
`tests/control_plane/test_binding_api.py`.

**Branches:** `codex/c3-review-followups`.

Four findings that arrived on #72 after its last fix and merged untriaged.
All four were reproduced before anything changed. They are one step because
they share a cause: C3 gave `channel_bindings` its first writer, and every
consumer that had been correct only because the table was always empty stopped
being correct at that moment — the exporter that never read it, the projection
that assumed one binding per actor, the seeding that matched a tuple without
looking at whose row it was.


#### Inventory — C4b the fallback consumer

**Files:** `hermes_cloud/channels/fallback.py`,
`hermes_cloud/channels/telegram.py`, `hermes_cloud/cli.py`,
`hermes_cloud/core/config.py`, `hermes_cloud/execute/email_send.py`,
`tests/control_plane/test_channel_preferences.py`,
`tests/test_config_and_cli.py`, `tests/test_primary_unavailable.py`.

The design question C4a left open is answered by what was already there:
`manifest.email.fallback` has carried the owner's contact address since the
first revision, and `parse_runtime_manifest` already refuses
`agent_inbox == fallback`. So the preference reaches the runtime as a manifest
projection, the planner writes both halves from one account in one
transaction, and a control-plane case asserts they agree — a projection that
may drift is the whole C3a debt plan.

`hermes_cloud/cli.py` is the ONE place the family-facing transport is built,
which is why the fallback is a decorator installed there: the pipeline, the
scheduler and the CLI reach the family from 28 call sites, and a branch at
each would be 28 chances to forget.

`hermes_cloud/execute/email_send.py` gains `send_notice` rather than a second
egress: the outgoing-mail kill switch, the binding checks and the send store
all apply to a notice, and the approval id is a named constant so nothing
downstream reads a system message as something a person agreed to.

`tests/control_plane/test_channel_preferences.py` is shared with C4a's step
above, and declared by both for the reason the gate exists — a path declared
only by another step is still undeclared by this one.

`hermes_cloud/channels/telegram.py` joined during review, and it is the more
interesting of the two findings this slice drew. `TransportError` was raised
for every HTTP answer, so a 429 rate limit and a 5xx outage looked exactly
like a blocked bot; the fallback would have written "we could not reach you"
about messages that arrive a second later, and the worker's retry would have
repeated it. The channel now says which it was, and the decorator writes only
on a definitive refusal.

**Deliberately not fixed: routing WhatsApp and web through the fallback.**
The finding is right that the production image runs
`hermes_cloud.runtime.service` and that `_pipeline` — where the decorator is
installed — is the CLI's. What it does not account for is that the deployed
runtime builds NO channel transport at all: it serves HTTP, answers web chat
inside the request, and runs the inbound Nerve and Gmail workers. There is no
primary-channel send there to fall back from, and "route each supported
primary sender" would mean building the outbound delivery path — which is
C5's relay work and Track R's staged rollout, not this slice. What C4b can
enforce, and does, is that the path cannot be added without the fallback:
`test_no_family_transport_is_built_without_the_fallback_around_it` fails the
day a transport is constructed anywhere that does not wrap it.

**Branches:** `codex/c4b-fallback-consumer`.

#### Inventory — C4a preferences writer

**Files:** `AGENTS.repo-invariants.md`,
`control_plane/channel_preferences.py`, `control_plane/container.py`,
`control_plane/owners.py`, `tests/control_plane/test_owner_predicate.py`,
`control_plane/migrations/0011_channel_preference_fallback.sql`,
`control_plane/api/onboarding.py`, `control_plane/email/service.py`,
`control_plane/privacy/export.py`,
`control_plane/providers/email/google_oauth.py`,
`tests/control_plane/email/test_google_oauth.py`,
`control_plane/provisioning/planner.py`, `tests/control_plane/conftest.py`,
`tests/control_plane/test_api.py`,
`tests/control_plane/test_export_delete.py`,
`tests/control_plane/test_channel_preferences.py`,
`tests/control_plane/test_consent_withdrawal.py`,
`tests/control_plane/test_db.py`, `tests/control_plane/test_manifest.py`.

`container.py` is here because the repository was never constructed in
production — which is the mechanical reason the table had no writer, and worth
recording rather than fixing silently. The four test files beyond the
preferences one are the planner's construction sites and the migration ledger:
`DesiredSpecPlanner` gains an argument, so `conftest.py` and the two modules
that build one directly move with it, and `test_db.py` names every migration in
order.

`tests/control_plane/test_channel_preferences.py` is rewritten rather than
extended. Its cases passed the repository both halves of the comparison —
`set_household(..., fallback_email=X, agent_inbox=Y)` — so the self-ingestion
case proved that two arguments can be compared and nothing about whether the
system knows its own inbox.

Two files joined during review, and both are the same lesson — a rule that
only holds where it is asked. `control_plane/email/service.py` refuses a
mailbox equal to an owner's own contact address at SELECTION, because the
repository's refusal alone lands after the provider has created the inbox,
where the family can no longer correct it; it is the one place both the
managed and own-domain options compose an address.
`control_plane/privacy/export.py` returns the new rows, because
`docs/privacy/data-map.md` has promised export and erasure for this table since
Phase 5 and the promise only became falsifiable once something wrote a row —
the same sequence `channel_bindings` went through in C3.

`control_plane/api/onboarding.py` joined for the third turn of the same
lesson: refusing is half the job, and a refusal has to ARRIVE as one.
`select_step` catches Pydantic's `ValidationError` and nothing else, so the
selection refusal above reached a JSON client as a 500 while the browser
route — which redirects on any `ValueError` — showed the family exactly what
it should. It is a `MailboxRefused` now, answered 409 with its own text the
way `api/bindings.py` answers a `BindingError`.

`control_plane/providers/email/google_oauth.py` is the third consumer, and the
one that could not be fixed with the other two: `gmail_agent` reaches selection
with NO address, because the address exists only once Google grants it. Its
callback compared the grant with the initiating account alone, so an adult or a
second owner could connect the mailbox the fallback owner is reached at. The
rule is now written once — `owner_contact_query` in `email/service.py` — and
executed by each path in the style it already uses, because two spellings of
one rule is how the third consumer got forgotten in the first place.

`control_plane/owners.py`, `tests/control_plane/test_owner_predicate.py` and
the `AGENTS.repo-invariants.md` entry are the FOURTH round, and they are a
different kind of change: `AGENTS.md` says a class reported twice is one
missing rule, and this one was patched four times instead. The rule — who a
household's fallback owner is, and that active means the membership and the
account both — now lives in one module, with a check that greps the control
plane for anyone answering it themselves. The fourth finding itself was that
the predicate required an active membership but not an active account, and
that the planner chose among owners with an unordered `LIMIT 1`.

**Branches:** `codex/c4a-preferences-writer`.

#### Inventory — C3b revision rollout

**Files:** `control_plane/provisioning/worker.py`,
`control_plane/provisioning/rollout.py`, `control_plane/api/bindings.py`,
`control_plane/provisioning/bootstrap.py`,
`tests/control_plane/test_provisioning_jobs.py`,
`tests/control_plane/test_channel_bindings.py`,
`tests/control_plane/test_bootstrap.py`.

**Branches:** `codex/c3b-revision-rollout`.

`worker.py` is the consolidation of four currency checks into one answer;
`rollout.py` exists so the scheduling can be tested without an HTTP client,
which is what the endpoint's own shape was preventing.

#### Inventory — C5a one signature between the gateway and the runtime

**Files:** `hermes_cloud/ingest/whatsapp_webhook.py`,
`hermes_cloud/runtime/service.py`, `tests/test_whatsapp.py`.

`gateway/whatsapp_router.py` is deliberately unchanged: it was already
signing the scheme that survives, and the defect was that the other end
verified a different one.

The regression that matters imports BOTH ends and verifies one against the
other, because that is the only place the disagreement was visible — each
side's own tests passed throughout. It fails when the runtime is reverted to
the bare-body scheme.

**Branches:** `codex/c5a-relay-hmac-reconciliation`.

#### Inventory — C5b the ingress WAL is read back

**Files:** `gateway/whatsapp_router.py`, `tests/test_gateway_routing.py`,
`tests/control_plane/test_channel_bindings.py`.

`GatewayStore` has always written a row before the webhook is ACKed and
deleted it once the runtime confirmed. Between those there was no reader, so
the durability was spent on nothing: a row a failed delivery left behind
stayed until somebody deleted the file.

The half that is not the worker is the taxonomy. `hmac_rejected` answered four
different questions and the row's fate was decided by which `return` happened
to run — which is how a payload that a present key had already rejected, and
that therefore can never verify, came to be stored indefinitely. The outcomes
now say whether redelivery could change anything: `relay_key_absent` and
`runtime_unavailable` keep the row, `hmac_rejected` drops it.

`test_channel_bindings.py` is touched for one docstring only: it repeated the
checklist's own "provisioning/secrets.py does not exist" claim about a file
that has been on disk throughout. See the correction in the C5 box above.

**Branches:** `codex/c5b-gateway-redeliver`.

#### Inventory — C5c the relay key exists

**Files:** `control_plane/crypto.py`, `control_plane/config.py`,
`control_plane/container.py`, `control_plane/cli.py`,
`control_plane/provisioning/worker.py`,
`control_plane/repositories/bindings.py`, `gateway/whatsapp_router.py`,
`tests/control_plane/conftest.py`,
`tests/control_plane/test_channel_bindings.py`,
`tests/control_plane/test_provisioning_jobs.py`.

Two things were missing and the box named neither correctly until #87 fixed
the text: `relay_keys` was populated only by tests, and `external_id_hmac` was
always NULL, so no WhatsApp message could reach a household and no binding was
visible to a strict-mode gateway.

**The relay key is DERIVED, not stored,** and that is forced rather than
preferred: `FlySecretSink` cannot read — Fly secrets are write-only — and the
gateway holds no field cipher, so neither end can fetch the other's copy. Both
derive it from one root. Storage disappears and rotation is one root change.

**The lookup digest is written at `_insert`,** the single funnel
`ensure_owner_binding` and `verify_challenge` both reach, so no writer can
produce a binding the gateway cannot see. Being a pure projection of
`external_id` is what also makes `backfill_sender_digests` possible for rows
that predate the key.

`cli.py` carries `backfill-sender-digests`, the upgrade step for a database
written before the key: a strict-mode gateway matches ONLY `external_id_hmac`,
so without it every household already onboarded goes dark the moment the key is
configured. A command rather than a migration, because SQL cannot compute a
keyed digest and a migration that needed the key would have to be handed a
secret the schema has no business holding.

The gateway's sender key is deliberately NOT the control plane's
`lookup_hmac_key`: that key digests email addresses and every other equality
lookup here, and sharing it would let the layer that resolves senders
correlate identifiers it has no business seeing.

Both primitives live in `control_plane/crypto.py` and the gateway imports
them, because the gateway already depends on `control_plane` and two
implementations of one keyed comparison is precisely the C5a defect.

**Branches:** `codex/c5c-relay-key-provisioning`.

#### Inventory — C5d something calls the gateway

**Files:** `gateway/app.py`, `pyproject.toml`, `tests/test_gateway_app.py`.

`pyproject.toml` gains `gateway*`: package discovery listed only
`hermes_cloud*`, `control_plane*` and `web*`, so the wheel the image builds
carried no `gateway/` and `python -m gateway.app` would have exited with
`No module named gateway` on every deploy.

Every piece of the WhatsApp path existed after C5c and none of it ran:
`handle_webhook` had no caller outside tests, `run_once` had no scheduler, and
there was no deploy unit.

**The boundary this slice does not cross.** `handle_webhook` verifies the
inbound signature with the HOUSEHOLD's relay key, which only something holding
those keys can produce — so the caller is the internal relay adapter, not Meta
or Telegram, who sign with an application secret this repository has never
had. Step E5's prose says the gateway answers `200 OK` "to Telegram/WhatsApp",
which reads like the other shape; it is the one place the plan and the code
disagree, and the code is what four slices were built against. The public
provider edge — application-secret verification, the subscription challenge,
provider-payload parsing — is its own slice.

**The status says what the caller should DO; the body says why.** A delivery,
a terminal denial and a WAL-retained failure all answer `200`, so the status
cannot be used to probe whether a sender is bound. A refused signature answers
`403`, and the kill switch answers `503` — the one outcome where the gateway
took no responsibility, because `handle_webhook` returns `flag_disabled`
before it persists and a `200` would drop the message during exactly the
incident the switch was thrown to contain.

**Branches:** `codex/c5d-gateway-entrypoint`.

#### Inventory — C6b a retry path catches only what it is prepared for

**Files:** `control_plane/provisioning/worker.py`,
`tests/control_plane/test_provisioning_jobs.py`,
`tests/control_plane/test_provision_dry_run.py`.

Deferred from C6a, which said auditing it there would be a second change
hiding inside the first.

Three defects in the C5 gateway slices were one shape: **a broad `except`
around an operation whose answer is "come back to this" converts a programming
error into a silent, permanent retry.** A `NameError` became
`lookup_unavailable`; a `TypeError` from a missed call site became
`runtime_unavailable`; both were found by a test disagreeing about a count
rather than by anything raising.

Not "broad excepts are bad" — the one guarding telemetry at `worker.py:273` is
correct and says so. The damage is the combination: catch everything, then
answer *try again later*.

Four sites in `ProvisioningWorker`, found by classifying every `except
Exception` in the file by what its handler DOES: the consent revoke
(`httpx` transport errors plus the socket layer's own, matching what `_run_once`
already treats as transport a few methods above), two `provider.deprovision`
calls (`ProvisioningError` — the Fly provider already wraps its transport
failures into `OutcomeUnknown` and `ProviderRejected`), and `secret_sink.install`
(`SecretInstallError`).

Three test doubles were raising bare `RuntimeError` to mean "the sink did not
answer", which was asserting that ANY exception means retry — the behaviour
being removed. They now raise what the real sink raises.

The other sixty-six `except Exception` in the tree are deliberately untouched:
most are a different shape, and folding them in would make the rule harder to
see rather than easier.

**Branches:** `codex/c6b-named-failures-on-retry-paths`.

#### Inventory — C6a a successful activation is not a stale projection

**Files:** `control_plane/provisioning/worker.py`,
`tests/control_plane/test_provisioning_jobs.py`.

Found while reviewing #86 and deliberately not patched there. Pre-existing on
`main`.

`_runtime_projection_is_current` requires `household_status = 'provisioning'`
and a workflow in `{runtime_provisioning, activating}` — and SUCCESSFUL
ACTIVATION is precisely what ends both. So a job whose revision went live read
as "not current", and reconcile's not-current branch treated a READY provider
as an orphan: `_cleanup_cancelled_result` marks the resource and runs the
deprovision through. **That is teardown of the runtime currently serving the
household**, triggered by an operator reconciling a job that succeeded.

The guard answers "is this job the household's current intent"; the branch used
it for "is this provider resource an orphan". Those diverge in exactly one
case — the job is stale BECAUSE IT SUCCEEDED — so `_revision_is_serving` asks
the authority first, and all three consumers of the currency guard that lead to
a terminal or destructive outcome now consult it. The two reconcile branches
were found by grepping the guard rather than by following the reported
instance; the third, `_settle_activation_deadline`, is the guard #86 drafted
and reverted as untestable, reachable now that reconcile no longer cancels
first.

`config_revisions.status = 'active'` is the authority because it has one
writer. `job_status`, `settled_at` and the presence of an unused bootstrap
token are the worker's own bookkeeping, and #86 fixed three defects that were
all the same substitution.

**Branches:** `codex/c6a-reconcile-activation-authority`.

#### Inventory — C5e the gateway asks and holds nothing

**Files:** `control_plane/bindings_resolution.py`,
`control_plane/api/internal_bindings.py`, `control_plane/api/app.py`,
`control_plane/config.py`, `gateway/whatsapp_router.py`, `gateway/app.py`,
`deploy/gateway/Dockerfile`, `deploy/gateway/fly.toml`,
`tests/control_plane/test_internal_bindings.py`, `tests/test_gateway_app.py`,
`tests/test_gateway_routing.py`.

**A lookup, not a projection.** Routability changes at exact moments — C3c's
activation boundary, C3e's terminal rollout, D1's re-planning, D4 and D5's
retirement — and five slices exist to make those moments exact. Replication lag
sits inside that decision and re-opens them: a retired member keeps routing, a
member staged for a failed revision starts early. A synchronous lookup has no
lag, and its hot-path dependency is bounded by C5b's terminal/retryable split.

**The rule moved to `bindings_resolution.py` and has exactly two callers** —
the gateway's local resolver and the endpoint. Two SQL statements answering
"which household holds this sender" is the C5a defect waiting to happen.

**The gateway now holds no control-plane data at all.** It opens no database:
two roots, two credentials and its own ingress WAL. C5c's K1 arrived at
completely — it cannot decrypt a manifest, correlate a keyed digest, or read a
binding; it can ask about one.

**A lookup that could not be made is retryable**, persisted with no household
because none was resolved. `_has_provenance` therefore asks about authenticity
— channel, timestamp, signature — and not about the household: requiring one
would make a control-plane restart lose messages through the check meant to
protect them.

**Branches:** `codex/c5e-gateway-binding-lookup`.

#### Inventory — C3d tests that reach what they claim to cover

**Files:** `tests/control_plane/test_binding_api.py`,
`tests/control_plane/test_provisioning_jobs.py`.

**Branches:** `codex/c3d-tests-that-reach-their-path`.

Tests only. Both gaps were confirmed by experiment before being filled:
deleting `schedule_runtime_rollout` from `verify_binding_challenge` left the
whole suite green, and so did dropping the revision clause from a currency
checkpoint. A regression that passes when the code it is named for is deleted
is worse than no regression, because it is counted.

**Scope narrowed from what this item asked for, deliberately.** C3d says "both
operations × both stale fields × all four checkpoints". What shipped is one
case per (checkpoint, field that checkpoint reads), for one operation — five
cases where the dense matrix would be twelve.

The two operations were dropped because the clauses under test are shared
code; what differs between them is `_workflow_states_for`, which has its own
test. Running every case twice pinned nothing the single run did not, measured
by deleting each clause and counting failures.

The fourth checkpoint — `_runtime_projection_is_current`, during reconcile —
IS covered, after a round trip worth recording. It was dropped on the grounds
that its revision clause changes no outcome, which is true and measured: with
the clause removed the job is resumed and the next checkpoint cancels it for
the same staleness. Review pointed out what that reasoning missed, and it was
right — the clause is what stops reconcile RE-ENTERING THE PROVIDER for a
revision nobody is serving, and repeating external work is the thing a
currency guard exists to prevent. Status is the wrong assertion for it;
provider call count is the right one.

The lesson is narrower than "cover everything": a guard whose absence changes
no observable outcome may still be the only thing preventing an external side
effect, and outcome-shaped assertions cannot see that.


## Track R — Staged rollout (fixed order; runbook §Rollout)

Prerequisite: O1 ticked. Order is not negotiable per runbook and canon.

- [ ] **R0. Operator soak prep (synthetic, can start anytime).** Dedicated
  staging org confirmed; `ABROLIA_REAL_EMAIL_HOUSEHOLD_ALLOWLIST` populated
  with the operator household only.
- [ ] **R1. Operator accounts, managed email first.** *Flipped 2026-09-03 by
  owner decision ahead of O1 — see the R1 inventory below; the battery and
  soak are still owed before this box ticks.* *Before this promotion,
  run `python3 scripts/rehearse_0012_routing.py` against a copy of the
  database it creates: `0012` decides which existing bindings the gateway will
  still route, and a household it misses goes silent rather than failing a
  test. Not applicable until here — there is no data to lose before it.*
  `ABROLIA_REAL_EMAIL_ENABLED=1` + allowlist; manual live battery O3; soak.
- [ ] **R2. First invited pilot family.** Managed `@abrolia.com` only. MVP
  surface: onboarding 3-step machine, approvals→staged effects, deletion.
- [ ] **R3. Second provider.** BYO domain after its battery; Gmail only after
  **B-06** (verification/CASA + dedicated-Gmail live gates — deferred, still
  open). Shared-WA relay last, after C5.

#### Inventory — R0 admitting a tester without a restart

Every human tester on the synthetic contour enters through one operator
invite, and `invite` took the single-writer flock that `serve` holds for the
life of the process — so each tester admitted cost a production restart. The
command is one token row and one line of operator stdout; it now runs beside
the serving process the way `withdraw-consent` does.

**Files:** `control_plane/cli.py`, `docs/onboarding-runbook.md`,
`tests/control_plane/test_invite_cli.py`.

**Branches:** `feat/invite-without-downtime`.

#### Inventory — R1 the flag flip

One line: `ABROLIA_REAL_EMAIL_ENABLED = "1"` on the synthetic control plane.
`ABROLIA_SYNTHETIC_ONLY` stays `1` — `ControlPlaneConfig.validate` gates
managed email on Nerve configuration and a non-empty household allowlist,
both already installed as secrets, not on the synthetic-only switch.

**Owner decision, 2026-09-03.** R1 proceeds on testers before the Phase A
pack (P2 Fly.io DPA/SCC, the TIAs, the Art. 27 representative are all still
⏳ in `docs/privacy/processors.md`). The scope of that decision is TESTERS —
operator accounts and invited testers whose household IDs the operator has
put on `ABROLIA_REAL_EMAIL_HOUSEHOLD_ALLOWLIST`. **O1 still gates R2**, the
first invited pilot family. Recorded here and in the execution log rather
than by flipping any registry row: the registry says what is signed, and
nothing new is. `/s/ Product owner (CEO), 2026-09-03`.

What was true at the flip: O2 closed (nerve-cloud #178), `go test ./...`
green there against Postgres, `pytest -m "not live"` green here, the
allowlist secret installed. The 0012 routing rehearsal was not run — the
production database is already at `0016`, so 0012 has long been applied
there and a rehearsal on a copy would only replay a migration the live
schema already carries. The O3 battery is owed on the day of the flip.

**Files:** `deploy/control-plane/fly.toml`.

**Branches:** `rollout/r1-real-email-operator-accounts`.

#### Inventory — R0 self-service registration for testers

Owner ask, 2026-09-03: testers register themselves from the front instead of
each one waiting for an operator `invite`. One decision changes:
`ABROLIA_SELF_SIGNUP_ENABLED=1` makes a public `request-link` for an address
with no account issue an `invite` link instead of nothing; consuming it
creates the account, household and session exactly as an operator invite
does. Existing accounts still get `login`/`reauth`, a disabled account is not
reopened, the public answer stays `accepted` either way, the rate limits
stay, and the new household starts on the synthetic providers until its UUID
is allowlisted. The flag refuses to turn on without production magic-link
delivery, so `ABROLIA_RESEND_API_KEY` and `ABROLIA_MAGIC_LINK_FROM` join the
deploy preflight. The landing page's primary action now points at
`app.abrolia.com/start`.

**Files:** `control_plane/config.py`, `control_plane/services/accounts.py`,
`control_plane/container.py`, `control_plane/api/app.py`,
`control_plane/api/web.py`, `control_plane/api/auth.py`,
`control_plane/api/dependencies.py`,
`control_plane/web/templates/start.html`,
`control_plane/web/static/onboarding.js`, `deploy/control-plane/fly.toml`,
`deploy/control-plane/required-runtime-config.txt`, `landing/index.html`,
`docs/onboarding-runbook.md`, `tests/control_plane/test_self_signup.py`,
`tests/control_plane/test_required_config.py`,
`tests/control_plane/test_auth_body_bounds.py`.

**Review follow-up (Codex on #136, P1).** The three unauthenticated auth
bodies — `request-link` JSON, the no-JS form, `consume` — were parsed before
any bound: a Pydantic parameter lets FastAPI hold the whole document, and
`request.form()` has no bound at all. With the front door public, that is an
unbounded body on a public endpoint. All three now read through the same
pre-parse bound (`MAX_AUTH_BODY_BYTES`, declared length and streamed bytes);
the regression covers declared and chunked framing on each. The second
finding — that delivering a login link to Resend sends the address to a third
party — was disputed in the thread: Resend is the contracted processor for
exactly that payload (P4, DPA + SCC ✅), the runbook documents the gate as
such, and keeping it off means no link can be delivered to anyone.

**Branches:** `feat/self-signup-for-testers`,
`fix/request-link-body-bounds`.

#### Inventory — R0 the profile is chosen, not typed

First tester through the front door (2026-09-03) got a 422 on the profile
and saw nothing: language, country and timezone were free text validated by
shape afterwards — `^[A-Z]{2}$` for the country, 2–35 characters for the
language, nothing for the timezone — and the enhanced page re-rendered the
unchanged state on any refusal. Owner ask: dropdowns. Three changes: the
vocabularies live in `control_plane/profile_choices.py` (ISO 639-1, ISO
3166-1 alpha-2, IANA zones from `tzdata`) and the form offers exactly them;
`ProfileInput` accepts exactly them, forgiving case and whitespace and naming
the field it refuses; the enhanced page prints the server's refusal (field
paths and messages only — the answer never carries the submitted value).

**Round 2, same day — the refusal, once visible, named the real cause.** It
was never the tester's values. `{{ command_fields() }}` puts the no-JS
command fields (`csrf_token`, `idempotency_key`, `version`) inside the
profile form, and the enhanced path posted `Object.fromEntries(new
FormData(form))` — all of them — to a contract with `extra="forbid"`. Every
tester hit that 422 on the first screen; the dropdowns alone would not have
fixed it. The JS now strips the three command fields (they travel as headers
on that path). Two owner asks in the same round: the `<select>`s were unstyled
(the CSS addressed `input` only), and the country list is **Europe only** —
49 states, Council of Europe plus the European states outside it — with the
timezone list narrowed to the zones those countries use (`Europe/*` plus the
Atlantic islands, Cyprus and the Caucasus).

**Files:** `control_plane/profile_choices.py`, `control_plane/models.py`,
`control_plane/api/app.py`, `control_plane/web/templates/onboarding.html`,
`control_plane/web/static/onboarding.js`,
`control_plane/web/static/onboarding.css`, `pyproject.toml`,
`tests/control_plane/test_profile_choices.py`.

**Branches:** `fix/onboarding-profile-choices`,
`fix/profile-form-sends-only-the-profile`.

#### Inventory — R0 a refusal is visible on every step, and names what to do

The first tester past the profile hit 409 on the email step and saw
nothing again: the error slot #138 added lived inside the profile form,
which is hidden from the second step on. The 409 itself is R1 working as
designed — managed email now routes to Nerve, and this household is not on
`ABROLIA_REAL_EMAIL_HOUSEHOLD_ALLOWLIST` — but "real email is not enabled
for this household" gave the tester nothing to act on and the operator no
id to act with. The slot moved to page level; the message names the
household id and the one thing the tester can do; the page shows the id
in its header so it can be sent to the operator; and every option card
carries an explicit "Continue with … →" line, because a tester read the
cards as a list and looked for a Next button that does not exist.

**Files:** `control_plane/api/app.py`, `control_plane/onboarding/service.py`,
`control_plane/web/templates/onboarding.html`,
`control_plane/web/static/onboarding.css`,
`tests/control_plane/test_onboarding_refusal_visible.py`.

**Branches:** `fix/onboarding-refusal-visible-on-every-step`.

#### Inventory — R1 the allowlist secret cannot take production down

2026-09-03, ~19:35 UTC: the operator set
`ABROLIA_REAL_EMAIL_HOUSEHOLD_ALLOWLIST` to two email addresses. `flyctl
secrets set` restarted the Machine, `ControlPlaneConfig.validate` refused
the configuration ("must be a canonical UUID"), the boot failed, and
production was down — `/readyz` unreachable, Machine `stopped` — for several
minutes until the secret was replaced by hand with a placeholder UUID. The
deploy of #140 failed in the same window and was re-run. The previous
allowlist contents were overwritten and are not recoverable; it must be
re-entered from the household ids.

The list already fails closed per household (selection refuses anyone not on
it), so a bad entry was never a reason to stop serving. Now: entries that are
not canonical UUIDs are dropped and COUNTED (never kept — they may be
addresses), an empty list enables nobody, and both conditions are named on
`/readyz` as `real_email_allowlist_invalid` / `real_email_allowlist_empty`.
The deploy gate excuses those two on the same two properties as the backup
writer: a deploy cannot fix a secret, and the condition is named. The runbook
now says what the values are and where a tester reads their id.

**Files:** `control_plane/config.py`, `control_plane/api/app.py`,
`deploy/control-plane/readyz-deploy-gate.jq`, `docs/onboarding-runbook.md`,
`tests/control_plane/test_config.py`, `tests/control_plane/test_deploy_gate.py`,
`tests/control_plane/test_allowlist_readiness.py`.

**Branches:** `fix/allowlist-secret-cannot-take-prod-down`.

#### Inventory — R1 every household, no allowlist

Owner decision, 2026-09-03, after the allowlist incident: "no allowlist,
open for all emails". Testers register themselves, and each one waited for
an operator to put a household id on a secret — the loop that produced the
outage above. `ABROLIA_REAL_EMAIL_ALL_HOUSEHOLDS=1` makes selection and
dispatch authorize every household; the allowlist is ignored, not consulted
and not reported. The live brake (`ABROLIA_REAL_EMAIL_ENABLED`) still stops
dispatch at call time, the flag is dormant while the brake is on, and the
Art. 9(4) country check is untouched: real content still needs a recorded
determination for the profile's country (DE, NL, ES today). Roll back to
the list by setting the flag to `0`. `/s/ Product owner (CEO), 2026-09-03`.

**Files:** `control_plane/config.py`, `control_plane/container.py`,
`control_plane/onboarding/service.py`,
`control_plane/provisioning/worker.py`, `deploy/control-plane/fly.toml`,
`docs/onboarding-runbook.md`,
`tests/control_plane/test_real_email_for_every_household.py`.

**Branches:** `feat/real-email-for-every-household`.

#### Inventory — R1 a pending flag is not an unknown outcome

First tester to reach the email step, 2026-09-04 09:34 UTC. The managed
adapter created the Nerve org, inbox, key and webhook, found the
`attachments` flag off for that org and settled `waiting_user` with
`nerve_attachment_flag_pending` — correct, and the page said so. Ten seconds
later the tester pressed "check again" and the job settled
`outcome_unknown`, which no tester and no page can clear: the step froze.

The reference moves. `inspect` routes to `_recover_and_probe`, which deletes
and reissues the API key and rotates the webhook secret BEFORE probing the
flag, and then answered PENDING carrying no reference at all. The worker read
the reference from the inspect job's own row — empty, because `check` had
created that job seconds earlier — and `_validated_email_waiting_result`
refuses a managed Nerve wait without one; the `ValueError` became
`OutcomeUnknown`. Worse than a stuck step: the rotated key was never
recorded, so a live Nerve credential existed that teardown could not name.

`InspectResult` now carries `external_ref`, the managed adapter returns the
rotated reference, and the worker prefers the provider's answer over the
job's own. The validator is unchanged — a wait with no reference anywhere is
still `outcome_unknown`. Separately, `reconcile` no longer takes the writer
flock (`invite` and `withdraw-consent` precedent): settling one quarantined
job should not require stopping production, and the embedded worker never
leases a job in `outcome_unknown`.

Not fixed here, and deliberately: the `attachments` flag itself is per-org
in Nerve and default-off, so every new household waits for
`nerve-flags set attachments --org <org> --enabled=true`. Whether that
should default on for Abrolia-created orgs is a Nerve decision, not a
control-plane one.

**Files:** `control_plane/provisioning/contracts.py`,
`control_plane/provisioning/worker.py`,
`control_plane/providers/email/nerve_managed.py`, `control_plane/cli.py`,
`tests/control_plane/test_managed_email_check.py`.

**Branches:** `fix/managed-email-check-keeps-the-reference`.

## Execution log

- 2026-09-03: **Real email for every household — owner decision.** After the
  allowlist secret took production down (see the inventory above), the owner
  dropped the per-household list: every self-registered household gets real
  managed email. What still holds: the live brake, the Art. 9(2)(a) consent,
  the Art. 9(4) country determination (DE, NL, ES). The scope of the earlier
  decision — testers, before the Phase A pack — is now "anyone who registers",
  and O1 still gates R2 as written.

- 2026-09-03: **R1 flipped on testers before the Phase A pack — owner
  decision.** The product owner chose to run real managed email for
  allowlisted tester households without waiting for the Fly.io DPA, the TIAs
  or the Art. 27 appointment. Recorded as a decision, not as evidence: no
  registry row in `processors.md` moves, and O1 keeps gating R2. What did
  hold at the flip: O2 (nerve-cloud #178), both suites green, `invite` no
  longer needs a restart (#133), the allowlist secret installed. Owed: the
  O3 battery on the day of the flip, the soak, and the pack itself before the
  first invited family.

- 2026-08-28: **The recovery I added could make things worse; narrowed it, and
  stopped.** Review round 3 found two things, both inside my own fix. The job
  settles `succeeded` after launch, before activation, so a runtime that never
  claims its token strands the household in a state my recovery does not cover
  — it handles `failed` only. And more seriously,
  `_restore_settled_household` writes only to the database: after a failure
  that reached the provider, the Machine may already carry N's config, so
  recording N-1 as active states something false about what is running. A
  household stuck in `provisioning` is visible and fixable; one that is falsely
  `active` is neither, because nothing goes looking.

  Narrowed to pre-mutation failures only, keyed on the `external_resources`
  row `_finish_runtime` writes as soon as `prepare` returns. The recovery now
  fixes the case it understands and declines the rest, which leaves the
  original symptom rather than replacing it with a quieter one. Proven in both
  directions: the test fails against the version that guessed.

  Both stranding paths are C3e. Stopping here rather than a fourth round: the
  previous fix introduced a failure mode, and continuing to patch provisioning
  failure paths I do not command in full is how the outcome gets worse than not
  having touched them. Suite 1539 green.

- 2026-08-28: **C3b review round 2 — the two ways a household could get
  stuck, fixed; the two thin tests recorded as C3d.**

  **A failed rollout stranded the household.** `schedule_runtime_rollout`
  moves it to `provisioning` and revision N before any provider work, because
  the currency guards read that pair at every phase. A provider rejection then
  settled the job `failed` and left the household there permanently, pointing
  at a revision that never activated, with no runnable job to move it. This was
  my own acceptance criterion 5, written and not implemented. The revision to
  return to needed no new bookkeeping: `config_revisions.status` still marks the
  serving one `active`, because activation is what supersedes it and activation
  never happened.

  **A rollout could be scheduled mid-onboarding.** Verifying a binding before
  the first rollout activated enqueued N+1 and overwrote the single
  `current_config_revision`, stranding BOTH jobs — the original no longer
  matched the revision, the new one no longer matched the workflow — and after
  prepare the cleanup could take the shared runtime. Now refused, and the
  endpoint answers 409 rather than half-applying.

  **Recorded as C3d rather than fixed:** two C3b regressions assert correctly
  without reaching what they are named for. Worth stating plainly because I
  reported the mechanism as "fully exercised" and it was not: the rollout test
  bypasses the endpoint entirely, so the feature could be deleted with every
  test still green. Suite 1538 green.

- 2026-08-28: **C3b review round 1 — I had protected half a path.**
  `_workflow_states_for` keeps a re-provisioning job at `complete` so the
  onboarding page never claims a settled household is mid-setup. Activation
  then re-stamped `completed_at`, bumped the workflow version and appended an
  `activate_runtime` transition unconditionally (`bootstrap.py:438-456`) —
  moving the date a family finished setting up to the day somebody added an
  adult, and recording a `complete -> complete` event that never happened. The
  worker's transition was guarded; the activation write was not, and it is the
  same decision one step later. Now branched on the pre-existing state, with
  the rollout recorded where it belongs: `provisioning_jobs` for the job and
  `config_revisions.activated_at` for the revision.

  The regression test was checked in BOTH directions — it fails without the
  guard and passes with it. The first version of it asserted the right things
  and never activated the new revision, so it would have passed on the broken
  implementation. Worth remembering: a test for a guard has to reach the guard.

  **Recorded as C3c rather than fixed:** a binding routes before the revision
  authorizing it is active. C3b narrowed that window from unbounded to "until
  activation"; closing it needs a staged/published lifecycle on the binding,
  not a predicate. Suite 1536 green.

- 2026-08-27: **C3b implemented; C3a designed and decided.** Both designs were
  written before either was typed, and both changed on contact with the code.

  **C3b was not the small slice the checklist recorded.** That estimate came
  from the job-creation site alone. A runtime job's progress is gated by FOUR
  independent currency checks (`worker.py:2662, 2711, 2819, 2849`), each
  restating a phase-specific expectation for the ONBOARDING workflow — the
  shape this same file warns about at line 112. A household adding a member
  matches none of them: onboarding finished, workflow `complete`. Reusing that
  workflow was rejected because `workflow.state` is USER-VISIBLE through
  `OnboardingSnapshot`; a family that finished setup months ago would be shown
  as mid-setup to serve a rollout. Instead a `reprovision_runtime` operation,
  with the four state sets consolidated into one answer parameterised by
  operation, and the rollout itself extracted to `provisioning/rollout.py` so
  it can be exercised without authenticating first.

  **Nobody re-authorizes when a member is added** — asked during review, and
  worth recording because the answer was not obvious. The bootstrap token is a
  MACHINE credential reachable only over the private `.flycast` transport
  (`internal_bootstrap.py:41-63`), and `required_consent_purposes` derives from
  the email provider and WhatsApp number type, neither of which membership
  changes. What it does cost is a restart: `_machine_payload` embeds the
  revision and manifest hash (`fly.py:483-513`), so the Machine config no longer
  matches and is replaced. Volume mounted, nothing lost, brief unavailability —
  the same cost every configuration change already carries.

  **Built ahead of C3a by decision**, against my recommendation to reverse the
  order. The consequence is recorded rather than hidden: the end-to-end test
  binds a member on a SECONDARY channel, because the primary channel is still
  refused until C3a. The mechanism is fully exercised; the case the feature
  exists for gets its test when C3a lands. Suite 1535 green.

- 2026-08-27: **A note on running the scope gate.** It compares COMMITS
  (`git diff <merge-base> HEAD`), not the working tree, so a file that is
  modified but not yet committed is invisible to it — the suite passes locally
  and CI, which only ever sees commits, fails on the same check. Run it after
  committing, not before. Cost one CI round here.

- 2026-08-27: **The caps bounded the wrong collections.**
  `MAX_OPEN_CHALLENGES` counts only UNCONSUMED challenges, so issue-then-verify
  loops freely; `verify_challenge` returns the existing binding on a repeat, so
  the binding cap never fires either. Both reported room while the endpoint
  replanned on every pass and `create_revision` inserted another encrypted
  manifest — `config_revisions` growing without limit on a shared volume, past
  two caps that were watching the wrong things.

  Fixed by refusing at ISSUE: a tuple already bound in this household is
  nothing to invite anyone to. That is the better place for the refusal
  regardless — nobody is sent a code that could not have done anything — and it
  bounds the durable collections structurally, because a verification can now
  only follow a binding that did not exist, so revisions are bounded by the
  binding cap. Suite 1532 green.

  **Also today: CI was wedged, not slow.** Both runs on `2bd7886` sat 26 hours
  reporting `queued` with zero jobs created, while the API refused to cancel
  them ("already completed") AND refused to re-run them ("already running") —
  three mutually contradictory states. Not exhausted minutes, which was the
  theory: closing and reopening the PR fired fresh events and runners were
  allocated immediately. Recorded because the diagnosis cost most of a day and
  the remedy is one command that touches no code — which matters, since the
  gate's rule is never to push a commit to shake a verdict loose.

- 2026-08-26: **A regression I introduced in the follow-up PR, caught in
  review.** Fixing "a reset onto an identical tuple crosses an onboarding
  generation invisibly" I invalidated outstanding challenges from
  `ensure_owner_binding`'s IDEMPOTENT path, using the current time as a stand-in
  for a generation boundary. The planner runs on every revision — including the
  one issued immediately after a verification — so redeeming one invitation
  deleted every other outstanding one. A household inviting two people could
  seat only the first. Reproduced: two issued, one redeemed, zero surviving.

  The deletion was also unnecessary, which is the part worth keeping. If the
  owner state did not change, an outstanding invitation still creates exactly
  the binding it always would have and no boundary is crossed; when the owner
  state DOES change, `_retire_superseded` already invalidates challenges, and
  that is the only case where the generation actually moved. Removed rather
  than narrowed. Suite 1531 green.

- 2026-08-26: **The four findings that merged untriaged on #72 are fixed.**
  All reproduced first; all four turned out to share one cause. C3 gave
  `channel_bindings` its first production writer, and three consumers that had
  been correct only because the table was always empty stopped being correct
  the moment it filled.

  **The DSAR export omitted it.** `TABLE_CLASSIFICATION` marks the table
  exportable and `docs/privacy/data-map.md:53` promises it with a ✔, and
  `HouseholdExporter.export` never read it — costless while every household's
  binding list was empty, and a genuinely incomplete subject access request
  now that it holds channel identities, actors, roles and verification
  metadata. Exported without `external_id_hmac`, which is a keyed digest of
  the column beside it and so publishes a lookup token for no reader benefit;
  challenges stay out entirely by their own classification.

  **One actor with two bindings became two family members.** The projection
  appended per BINDING, so an adult reachable on WhatsApp and on web produced
  `family=(owner, adult, adult)` — rejected by `parse_runtime_manifest` as
  `actors.family: duplicate actor`. The same unstartable-revision class as the
  primary-channel case, reached by a different route, which is what made it
  worth fixing at the projection rather than at either symptom.

  **Seeding matched a tuple without asking whose row it was.**
  `ensure_owner_binding` returned early on `(channel, external_id)` alone, so
  re-onboarding the owner onto a tuple an ADULT held wrote no owner binding at
  all while the stale owner row survived on the channel the household had just
  left; and an owner whose ACTOR changed during reset never became
  authoritative. Reconciliation now compares role, actor and tuple together,
  and anything else is a reset that retires what it replaces.

  **Nothing bounded the collections.** An authenticated owner could loop
  unique IDs, each one a stored challenge row or a binding that becomes both a
  manifest entry and a config revision. Capped per household at both ends.

  Suite 1530 green, ruff and sanitizer clean.

- 2026-08-26: **C3 review round 2 — two more authorization defects, both
  reproduced, both fixed.** This is the third generation on this branch and the
  second to find something structural, which is the signal `CLAUDE.md`
  describes; the hand-back is requested in the thread.

  **Any household member could attest a binding.** Both endpoints used
  `current_household_mutation`, which resolves the household through
  `households.for_account` — a query that accepts any ACTIVE membership and
  never reads `role`. Nothing in the dependency layer ever did. So an adult
  could issue a code, redeem it, and have `owner_actor()` record the binding
  against the OWNER's actor, adding a member to durable routing state without
  the owner being asked. `current_household_owner_mutation` now reads the
  membership row, and reads it there rather than inferring from the binding
  table, because what is in question is the ACCOUNT's authority and not which
  actor a channel maps to.

  **Re-onboarding left the old channel authorized.** `ensure_owner_binding`
  only ever inserted, so `reset_from(PRIMARY_CHANNEL)` onto a different chat
  left both rows — the projection then emits two verified chats for the primary
  channel and the runtime refuses the revision. Worse on a channel CHANGE: the
  row for the channel the household left keeps its sender routable, because
  `gateway/whatsapp_router.py` resolves senders across the whole table and has
  no notion of supersession. An owner who moves the household off a channel has
  revoked it, and the table is what the gateway believes. Establishing an owner
  binding now retires the prior one, the adult bindings on the channel becoming
  primary (unrepresentable there, and otherwise a second route to the
  unstartable manifest), and every outstanding challenge — a code issued
  against the arrangement that just changed must not redeem into its
  replacement. Suite 1526 green.

- 2026-08-26: **C3 review round 1 — five findings, three fixed, and the second
  adult turned out to be unrepresentable.** All five were verified against the
  code first.

  **Redemption was not scoped to the caller's household.** `verify_challenge`
  looked a code up globally and compared only `issued_by_actor_id`, but an
  actor ID is unique within a household and nowhere else — two households may
  both call their owner `synthetic-owner`. The colliding owner could redeem
  the other household's challenge, writing the binding into the ISSUING
  household while their own received the revision. The household is now part
  of the lookup predicate, so the collision is unreachable rather than
  unlikely.

  **Both binding endpoints buffered unbounded bodies.** The identical defect
  was fixed for `/api/web/message` in #71 and then not applied to the two
  endpoints written in the same session — the instance fixed, the invariant
  missed. The bounded reader now lives in `api/dependencies.py` and all three
  routes share it.

  **The second adult cannot be represented, and that is C3a.**
  `channel_bindings.external_id` is asked both "which sender is this" (the
  gateway) and "where does the assistant speak" (the projection). Giving an
  adult the household's chat collides under `UNIQUE (household_id, channel,
  external_id)`; giving them an identity of their own makes the runtime refuse
  the revision with `channels.primary: multiple chats`. Both reproduced. The
  conflation arrived with 0007 and this projection is merely the first thing
  to ask it for two answers at once — so C3 refuses an adult on the primary
  channel where someone asks for it, instead of writing a row whose deployment
  fails later. Adults on OTHER channels are unaffected, supported, and now
  tested through the real runtime parser.

  That last point is the testing lesson: a manifest has two contracts —
  `DesiredHouseholdSpecV1`, which the control plane writes, and
  `parse_runtime_manifest`, which the runtime reads. The original test asserted
  the first and never exercised the second, which is exactly how a revision the
  runtime cannot start looked green.

  Left to C3b and argued rather than patched: a verified binding plans a
  revision that nothing rolls out. Suite 1523 green, ruff and sanitizer clean.

- 2026-08-25: **Review round 3 — two fail-open defects in C1's own work.**
  Both were verified before changing anything and both were real.

  **`emit_alert` printed raw tenant IDs.** `RuntimeStructuredLogger`, sixty
  lines above it in the same module, refuses to start without an HMAC key so
  that identifiers reach the log as `household_id_hash` — and the alert C1
  added wrote `household_id=` verbatim to the same Fly-hosted stream from all
  three channels. Redaction now happens at the alert boundary, not at the call
  sites: a rule enforced in three places is a rule with three chances to be
  forgotten, and the fourth caller would be the leak. Without a key the
  identifier is dropped rather than printed, which is the trade the request
  logger already makes by emitting nothing at all.

  **`nan` disabled the spending brake.** Both parse sites used bare `float()`,
  and every comparison against `nan` or `inf` is False — so
  `HERMES_COST_CAP_USD_PER_DAY=nan` parsed cleanly and switched the cap OFF at
  every provider call. `parse_daily_cap_usd` is now shared by the pipeline and
  the runtime, refuses non-finite and non-positive values loudly at startup,
  and treats unset as the default rather than an error. The pipeline also
  stopped swallowing a bad value into $5: a cap the operator got wrong is a cap
  they believe they set.

  Suite 1502 green. Merged to main by admin with three findings open — the web
  approval lifecycle, the runtime's single-threaded server, and fail-closed
  behaviour when usage accounting cannot commit — each needing a design
  decision rather than a patch, and each recorded here rather than lost with
  the branch.

- 2026-08-25: **Review round 2 — the credential fix did not fix production,
  and the finding was right.** One new blocker on `3f5e9b1`: the key had been
  added as an OPTIONAL setting, installed only when present. `fly.toml:11`
  selects `fly-runtime`, nothing named `ABROLIA_RUNTIME_MODEL_API_KEY`
  anywhere, and `_finish_runtime` omitted it silently — so following the
  repository's own deploy path still produced runtimes whose every chat turn
  answered 503. All three of the finding's claims were checked and held.

  The key now joins the four inputs `validate()` already requires for
  `fly-runtime`, which is where it belonged: a runtime that cannot answer the
  surface it serves is a misconfiguration, and it fails at boot where an
  operator sees it rather than per-turn where a family does. The config test
  now omits each pinned input in turn instead of only the whole set, and
  asserts the key is not printable by a config that logs itself.

  **The backfill is an operator step, not code, and that is a finding in
  itself.** `_finish_runtime` returns early for a runtime whose job already
  succeeded, so setting the value and redeploying fixes every future runtime
  and no existing one — pinned by
  `test_redeploying_with_the_key_does_not_reach_an_existing_runtime`. No new
  provisioning code is needed because `FlySecretSink.install` IS `fly secrets
  import --stage`; the runbook now documents the operator running that same
  command by hand, over stdin so the value never reaches argv or shell
  history. Suite 1492 green.

- 2026-08-25: **Review round 1 on #71 — five of seven blockers fixed, two
  handed back.** Codex raised seven P1s across two generations. Four were
  verified against the code before anything was changed.

  **The cap was in the wrong place, and that was C1's mistake.** `is_over_budget`
  guarded each channel once per turn, but `ToolLoop.run` makes up to
  `MAX_ITERATIONS = 8` provider calls plus a retry — so a turn that crossed the
  cap on its first call paid for every call after it, on all three channels.
  The repo's own invariant says the precondition lives where the provider is
  CALLED, which is `ToolLoop._call`, not the channel. A `CostGuard` is now asked
  before EVERY call including the retry, and records each response as it
  arrives so the next check sees what was just spent. The channel check stays,
  demoted to what it always was — presentation, answering without building a
  turn. One fix, three consumers, per "fix the invariant, not the instance".

  **`/api/web/message` failed OPEN on authorization.** `except Exception: roles
  = {}` then `.get(account_id, "owner")` meant a database error, or a
  membership row removed between the household lookup and the role lookup,
  granted the owner role — which the runtime maps onto `manifest.actors.owner`,
  whose tools include export and deletion. The most power was handed out at
  precisely the moment authorization could not be established. Now the query
  propagates and an unknown or absent role is refused.

  **C2 could never have worked in production.** Provisioning installs the
  bootstrap and DSAR tokens and nothing else; `ANTHROPIC_API_KEY` appears
  nowhere outside a test skip guard. So the first real turn failed constructing
  the model client, `_web_chat` reported `chat_unavailable`, and the endpoint
  answered 503 forever. The route was right and the credential to serve it was
  never installed. It now travels the same sink path as the other runtime
  secrets, and its absence stays honest rather than fatal.

  Also fixed: the request body is bounded before it is parsed rather than after
  (the 2 000-character check ran once FastAPI had already materialised the
  document), and the synchronous 120-second runtime call moved off the event
  loop, which a single-worker Uvicorn shared with every other household.

  **Handed back, with reasons rather than patches:** web-chat turns can stage
  approvals through `propose_reminder`/`memory_append`/`propose_email`/
  `propose_whatsapp` that the web UI offers no way to confirm — telegram
  callbacks cannot recover them because `ApprovalStore.claim_by_id` requires
  the originating chat — so the assistant can promise a confirmation step that
  does not exist. And `wsgiref.simple_server` is single-threaded, so a chat turn
  blocks that household's `/healthz`, DSAR and consent-revocation routes for
  its duration. Both need a lifecycle or a concurrency model this PR does not
  have, which is the scope signal `CLAUDE.md` describes — a read-only web tool
  registry and a threaded runtime server are each their own change. Suite 1492
  green, ruff and sanitizer clean.

- 2026-08-24: **C3 landed — `channel_bindings` finally has a writer, and the
  manifest became a projection of it.** The table arrived in 0007 carrying
  `verified_at`/`verified_by_actor_id` and nothing in production ever wrote a
  row: `gateway/whatsapp_router.py:114` read it to route senders, and only
  tests ever gave it something to find, while the planner built the owner's
  binding inline. Two records of one fact, and nothing keeping them in step.
  Now `ChannelBindingsRepository` (challenge → verify → write, migration 0009)
  is the only writer, the planner SEEDS the owner's row from verified
  onboarding and PROJECTS the manifest from the table, and
  `actors.family` stops being the hardcoded `(owner,)` that made a second
  adult impossible. A verified binding issues a config revision in the same
  transaction as the row, so neither half is ever observable alone — the
  runtime reads `RunContext` from the manifest, so a row without a revision
  would be a member the runtime refuses. Owner-only, behind
  `require_private_mutation`, in `control_plane/api/bindings.py`. On the rebased
  tree the full suite is 1520 green — the 1502 that merged as `e2f82de` plus
  12 lifecycle and 6 endpoint cases — with the scope gate among them, ruff and
  sanitizer clean.

  **Two limits are pinned by tests rather than left to be discovered.**
  `external_id_hmac` stays NULL, because the digest the gateway compares in
  strict mode is keyed with a relay key the control plane does not hold and
  has no path to (`provisioning/secrets.py` is C5's, absent) — every binding
  written here is invisible to a strict-mode gateway until C5 lands, and
  `test_hmac_column_stays_null_until_c5_provisions_the_key` is what should
  start failing then. And "verified" is narrower than the column name: this
  side has no SENDER for telegram or WhatsApp, so it cannot put a code into
  the channel it is binding. The code goes to the owner to deliver, which
  proves whoever answers holds it and leaves "this ID is that person's" as the
  owner's attestation. B-07 keeps every external ID synthetic meanwhile, so
  nobody real can be attested for. Both belong to the design, not to a patch.

  **The scope gate could not pass while this branch was stacked**, and that
  was not a defect: `git diff main...HEAD` returned the union of both slices
  while `codex/go-live-c1-c2` was unmerged, so C1+C2's paths read as
  undeclared by C3's step. Resolved by the event it was waiting for — #71
  merged as `e2f82de` and this branch was rebased onto it with
  `--onto origin/main db93ccc`, replaying only the C3 commit rather than the
  seven already squashed into main. The diff is now C3's own, which is what
  makes the step's inventory the branch's actual scope.

- 2026-08-24: **PR #71 opened; the archived Anthropic DPA lost its CMS payload,
  not its text.** CI's `check_fixtures` runs with a PRIVATE deny file
  (`HERMES_EXTRA_DENY_FILE`) that does not exist locally, so Gate -1 was red in
  CI while clean here: one private pattern matched a substring inside the
  serialized Next.js/Sanity navigation payload of the archived page — website
  scaffolding a "save page" snapshot dragged along, not a word of the DPA. The
  `custom` rule refuses allowlisting by design (`check_fixtures.py:412` raises
  on a `custom` exception line), and that is right: suppressing it would put the
  protected token in the repository as a rule. So the 37 `<script>` elements
  (138 324 bytes) were deleted instead. The deletion is span-exact — the
  remaining bytes are identical to the original, proven against blob `9b989cf`
  — and the file now opens with a comment recording what was removed, the
  original's SHA-256, and the commit to audit it against; the registry row
  carries the same note. Scripts never render, so the visible DPA is unchanged:
  SCC Module Two ×5, Standard Contractual Clauses ×12, every Annex. **The first
  attempt at that comment quoted the matched token verbatim and tripped the
  same gate**, which is the rule working exactly as intended and worth
  remembering: the explanation of a redaction is inside its blast radius.

- 2026-08-24: **branch pushed; the plan-scope gate was red and is now closed.**
  `codex/go-live-c1-c2` reached `origin` at `2c99080` (the push the previous
  session could not complete). The full non-live suite then came back **1483
  passed, 1 failed** — not the 1484/exit-0 the C2b handoff recorded:
  `test_every_changed_path_is_declared_by_this_branch_s_plan_step` failed with
  "0 plan steps claim the branch". The earlier green is explainable rather than
  imaginary — the gate reads the CURRENT branch name, and the tree was
  tree-identical to `codex/phase-F-allowlist-at-dispatch`, which
  `canon-execution-plan.md:218` does claim; run under that name it passes, under
  this one it never could. CI resolves the name from `GITHUB_HEAD_REF`, so the
  PR would have opened red. Closed by giving Track C its first inventory
  section above — no code changed. The lesson is the gate's own: a scope check
  is only as good as the name it is run under, and "the suite was green on the
  other branch" is not evidence about this one.

- 2026-08-24: **C2b landed — the runtime serves `/internal/v1/web/chat` and
  builds its first ToolLoop.** The route joined `INTERNAL_ROUTES`, so the
  hoisted bearer gate covers it structurally (unconfigured token → 404 that
  denies the route exists; wrong bearer → 401). `_web_chat` validates text
  (`text_required` / `text_too_long`) and fails closed owner-only until C3
  provides an account→actor binding (`owner_role_required`). `web_chat_turn`
  is the first in-runtime dialogue bootstrap: `require_ready()` + explicit
  `load_config(manifest_path=self.manifest_path)` so language/model cannot
  drift from what provisioning wrote; Household carries
  `allowed_chats={"web-chat"}` because the bearer boundary IS the transport
  verification while manifest verified pairs cover telegram only — C3 moves
  web into the manifest. Loop rebuilt per turn inside one `open_database`
  block (`EffectJournal` binds to a connection): `ToolLoop(journal,
  Services.on(database), model=config.model, effort=config.effort,
  family_language=config.language)`; cost cap enforced at the call site
  through handle_web_message's usage seam (`HERMES_COST_CAP_USD_PER_DAY`,
  default $5) so over-budget turns answer DEGRADED_MESSAGE with zero model
  calls. Proven by `tests/test_runtime_web_chat.py` (fail-closed bearer pair;
  validation-before-model-work incl. role gate; happy path records token
  counts into `usage_daily`; budget prefill degrades silently;
  not-ready → `runtime_not_ready`). Full non-live suite green — 1484 tests,
  exit 0 (1475 prior + 4 C2a endpoint cases + these 5) — ruff clean;
  sanitizer clean.
- 2026-08-23: **C2 seam decided; control-plane half implemented.** Ground truth:
  runtimes already serve authenticated `/internal/v1/*` routes beside `/health`
  (`hermes_cloud/runtime/service.py`, hoisted bearer gate over `INTERNAL_ROUTES`)
  and the control plane calls them at `{runtime_ref}.internal:8080`
  (`control_plane/privacy/runtime.py`) — so the metadata-only plane must not
  grow a model loop, and the web chat follows the DSAR precedent: **runtime
  serves, control plane proxies**. Landed: `PrivateRuntimeWebChatClient`
  (`control_plane/runtimes/chat_client.py`, same `runtime-dsar:{ref}` bearer,
  fail-closed boundary errors) wired unconditionally into the container;
  `/api/web/message` now sits behind `require_private_mutation` (origin +
  session + X-CSRF-Token — previously bare session cookie), refuses honestly
  when `runtime_ref` is absent or the runtime unreachable, and the echo
  fallback is gone along with the hermes_cloud imports from the API layer;
  `web/static/app.js` sends the csrf double-submit header. Branding test
  updated to the house refusal order (403 before auth). Suite 1475 green,
  ruff clean, sanitizer clean (vendor org contacts allowlisted per the
  landing-page org-contact precedent). **Not yet done:** dedicated tests for the new
  endpoint behaviour (three generation attempts produced corrupted files and
  were discarded — deliberately deferred to a fresh turn) and the C2b runtime
  side itself: `/internal/v1/web/chat` route plus the first in-runtime
  ToolLoop bootstrap (the runtime process builds no Pipeline today; dialogue
  exists only in `cli.py`).
- 2026-08-23: **O1/P2 negative finding recorded.** Fly.io publishes no
  customer-facing DPA anywhere on fly.io today (ToS cross-references only
  Privacy Policy + Supplemental Terms; the Privacy Policy links only the
  sub-processors list and their EU–US Data Privacy Framework policy; the
  historical `/legal/dpa/` 404s; the hub shows only the AUP). DPF is a
  transfer mechanism, not a substitute for the Art 28(3) processor contract —
  P2's ст. 28 column stays open even with DPF. Route: written request to
  `support@fly.io` («Privacy Concerns») for their standard customer DPA +
  SCCs, dashboard org/billing settings checked meanwhile. If Fly refuses,
  §4 admission criteria force either bespoke paper or migrating pilot
  workloads to a host with a self-serve DPA — its own registry change with
  pre-use gates.
- 2026-08-23: **O1 partially advanced.** Vendor DPA routes verified from the
  vendors' own pages: Anthropic's DPA (SCC Module Two + UK/Swiss annexes,
  deemed executed) is incorporated by reference into the Commercial Terms and
  takes effect with no signature ceremony; Resend's DPA binds upon entering
  the agreement ("signature blocks for reference purposes only"; SCC modules
  1–3, Irish law). Snapshots archived as
  `docs/privacy/vendor-dpas/2026-08-23-anthropic-dpa.html` and
  `...-resend-dpa.md`; `processors.md` rows P1/P4 updated to ✅ with entities
  named (Anthropic, PBC; Plus Five Five, Inc.). **P2 Fly.io left ⏳**: its
  legal hub blocks automated verification and no executed paper was found
  today — marking it without the document would fabricate the record. O1 stays
  open: P2 paper, three TIAs, Art 27 representative mandate (self-appointment
  ruled out — the rep must be addressable *instead of* the controller), and
  vendor EU-representative addresses per §2 item 1.
- 2026-08-23: checklist created from five-area ground-truth audit.
- 2026-08-23: **C1 implemented.** `is_over_budget` now guards every model-call
  path — WhatsApp dialogue (`_handle_whatsapp_dialogue`), Telegram dialogue
  (`handle_update`), and the web channel (`handle_web_message`, optional
  `usage=` seam ready for C2 wiring) — each checking before the call and
  recording `LoopResult` tokens after it, per the repo invariant that the
  precondition lives where the provider is CALLED. Over-budget turns reply
  with the honest degraded message instead of model output; `ALERTS` gained
  `emit_alert` (unknown names raise) and `budget_exceeded` is actually emitted
  now. Proven by three new cases in `tests/test_cost_caps.py`; full non-live
  suite 1475 green, ruff clean, sanitizer clean.

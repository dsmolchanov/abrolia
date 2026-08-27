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

`docs/privacy/processors.md:3` states no DPA is signed and no transfer mechanism
is executed — B-07 is open, so the data policy remains synthetic-only. Every
product flag is off by design. And three Phase E surfaces are half-built in ways
the unticked checkboxes understate: nothing in production writes or reads
`channel_preferences`, the `channel_bindings` lifecycle (challenge → verify)
does not exist despite its schema and runtime enforcement being done, and the
web chat answers with echo fallbacks because its model path was never wired.
This checklist sequences closing those, then the staged flips the runbook
already fixes.

## Track O — Operator/legal gates (cannot be delegated; block ALL real data)

- [ ] **O1. Legal pack signatures (B-07, P0).** DPA 28(3) + SCC module 2 + TIA
  for P1 Anthropic, P2 Fly.io, P4 Resend (`docs/privacy/processors.md` §§2–4,
  every row ⏳ today); Art 9(2)(a) condition recorded by counsel;
  `processors.md` registry updated with dates ONLY AFTER signature (canon
  Phase A rule). Gate: nothing in Track R starts before this box is ticked.
- [ ] **O2. Release tag + restore drill.** Zero git tags exist today. Tag the
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

True-state summary from the 2026-08-23 audit:

| Surface | Storage/schema | Enforcement/runtime | Lifecycle/wiring | Verdict |
|---|---|---|---|---|
| Cost caps | ✓ `0008_usage.sql` | ✓ email-extraction only | ✗ dialogue+web uncapped | PARTIAL |
| Observability | ✓ loggers both sides | ✓ `/healthz`+runtime `/health` | ✗ `ALERTS` defined, emitted nowhere | PARTIAL |
| Web chat | ✓ PWA packaged/served | ✓ session auth + server-side scoping | ✗ model path unwired (`web_loop` set nowhere); ✗ CSRF on `/api/web/message`; no e2e test | PARTIAL |
| Channel prefs | ✓ table+CHECKs | ✗ zero consumers/readers; fallback-sender absent; self-injection check is dead code | ✗ no write path | PARTIAL |
| Channel bindings | ✓ schema w/ authorization columns | ✓ RunContext derivation proven (manifest pairs, cross-pair denial) | ✗ no challenge flow, no writer, real external IDs rejected (`models.py:35-38`), second-adult flow absent | PARTIAL (biggest build) |
| Shared WA gateway | — | ✓ exact-match, deny unknown/ambiguous, inbound HMAC fail-closed (tested) | ✗ no ingress redelivery reader; outbound HMAC scheme mismatch vs runtime verifier; no HTTP entrypoint/deploy; no key provisioning | PARTIAL |

Ordered slices:

- [ ] **C1. Cap every model call; emit the alerts that exist.** *(TODAY)*
  `is_over_budget` guards only `pipeline.py:284`. The WhatsApp dialogue
  (`pipeline.py:443`), Telegram dialogue (`pipeline.py:566`) and web channel
  (`hermes_cloud/channels/web.py:44`) call the model unchecked and unrecorded.
  Fix at the call site (repo invariant: precondition enforced where the provider
  is CALLED): over-budget dialogue replies with the honest degraded message
  instead of a model call; web mirrors it; `budget_exceeded` becomes the first
  actually-emitted member of `hermes_cloud/core/observability.py::ALERTS`.
  Prove in `tests/test_cost_caps.py`.
- [ ] **C2. Wire the web chat to a real model path.** Decide the seam first:
  `control_plane/api/web.py:306` asks for `active.web_loop` which nothing sets,
  and `control_plane/` deliberately never imports the runtime runner. Either
  the household runtime serves chat (WSGI route beside `/health`) or the
  control plane proxies to it — a small design pass, then implementation.
  Same pass: add the same-origin/CSRF checks every sibling form endpoint has
  (`api/web.py:35-67` vs the bare :261-275), and the authenticated e2e test.
- [ ] **C3. The binding lifecycle.** Challenge → owner verification → row write
  with real `verified_at/verified_by_actor_id`; lift the `synthetic-`-only
  restriction on external IDs (`models.py:232-237`) per channel with format
  validation; second-adult binding flow (planner currently hardcodes
  `family=(owner,)` — `provisioning/planner.py:137-146`). This is the
  foundation the gateway lookup and preferences routing both consume.
  **Scope corrected 2026-08-26.** The second adult is NOT deliverable in this
  slice and the reason is structural — see C3a below. What C3 delivers is the
  writer, the challenge lifecycle, and the manifest as a projection of the
  table.
- [ ] **C3a. Separate a sender's identity from the chat it speaks in.**
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

- [ ] **C3b. Roll a revision out to an already-active household.**
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

- [ ] **C4. Make preferences real.** Production write path (API/onboarding);
  consumer that routes replies/fallbacks; self-contained agent-inbox rejection
  (replace dead `_validate_no_self_ingestion`, persist the fallback ref);
  permanent-failure → short email fallback sender + `primary_unavailable`
  emission (`observability.py:104` label exists, never emitted).
- [ ] **C5. Gateway plumbing.** Redeliver-from-`gateway_ingress` worker (rows
  are written and never read back); reconcile outbound HMAC scheme between
  `relay_hmac` (signs `body|ts`) and `verify_webhook` (bare body) — they
  reject each other today; HTTP entrypoint + narrow deploy unit; relay-key
  provisioning path (`provisioning/secrets.py` planned, absent).
- [ ] **C6. Box hygiene.** Update `phase-DE-pilot.md` checkboxes to the audited
  truth (several `[ ]` are done-but-renamed — e.g. flags boxes closed by #70,
  preferences storage landed in `control_plane/migrations/0006` not
  `hermes_cloud/core/migrations/0008`), so the acceptance commands name tests
  that exist. Stale boxes mislead exactly like the retired flags did.

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


## Track R — Staged rollout (fixed order; runbook §Rollout)

Prerequisite: O1 ticked. Order is not negotiable per runbook and canon.

- [ ] **R0. Operator soak prep (synthetic, can start anytime).** Dedicated
  staging org confirmed; `ABROLIA_REAL_EMAIL_HOUSEHOLD_ALLOWLIST` populated
  with the operator household only.
- [ ] **R1. Operator accounts, managed email first.**
  `ABROLIA_REAL_EMAIL_ENABLED=1` + allowlist; manual live battery O3; soak.
- [ ] **R2. First invited pilot family.** Managed `@abrolia.com` only. MVP
  surface: onboarding 3-step machine, approvals→staged effects, deletion.
- [ ] **R3. Second provider.** BYO domain after its battery; Gmail only after
  **B-06** (verification/CASA + dedicated-Gmail live gates — deferred, still
  open). Shared-WA relay last, after C5.

## Execution log

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

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
`tests/control_plane/test_provisioning_jobs.py`, `tests/test_whatsapp.py`.

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

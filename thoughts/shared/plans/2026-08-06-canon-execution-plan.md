---
title: "Canon Execution Plan — Blocker Closure and Phased Implementation"
status: planning
created_at: "2026-08-06 14:00 CEST"
repository: abrolia
branch: codex/phase-4-real-actions
base_commit: b9d3614
parent_plans:
  - thoughts/shared/plans/2026-08-02-family-ops-assistant-mvp.md # v3 root canon
  - thoughts/shared/plans/2026-08-04-abrolia-phase-1-onboarding-foundation.md # CU1
  - thoughts/shared/plans/2026-08-04-abrolia-phase-2-email-identity.md # CU2
scope: canon-closure
data_policy: synthetic-only-until-explicit-gates
---

# Canon Execution Plan — Blocker Closure and Phased Implementation

## Overview

This plan is the **additional planning layer** requested for every canon phase. It does not replace v3 or CU1/CU2; it inventories every open blocker from the three validation reports and decomposes the remaining work into six gated execution phases (A–F) with exact file scopes, required changes, and acceptance criteria.

The session goal is `fix all blockers and implement canon plan`. No real family data, real DNS mutation, or real provider enablement occurs until the gates below are evidenced.

Current branch: `codex/phase-4-real-actions` (`b9d3614`) is 3 commits ahead of `main` (`a23ff33`). Working tree dirty: modified `tests/test_whatsapp.py`, untracked `landing/` + research. All probes/tests use synthetic data only.

## Blocker Inventory (source of truth)

| ID | Origin | Summary | Severity | Blocks |
|---|---|---|---|---|
| B-01 | Phase2 validation V-01 | Nerve `handler_keys.go` cross-org service-token delegation: `org_id` not resolved against authenticated tenant, `nerve:admin.billing` delegable by tenant | P0 upstream | Real Nerve rollout |
| B-02 | Phase2 validation V-02 | `secret_handoff_unknown` convergence missing durable non-secret receipt/inspection proof; crash after `SecretSink` write before durable projection stays quarantined indefinitely | P0 local | Real `@abrolia.com`/BYO rollout |
| B-03 | Phase2 validation V-03 | Provider secret shapes in allowed fields (now fixed locally, needs retention) | P1 | — (guard) |
| B-04 | Phase2 validation V-04 | TTL ownership transfer fail-closed (now fixed) — keep | P1 | — (guard) |
| B-05 | CU2 §§2.4 | Real DNS/BYO live gates not run: reload/login resume, verified DNS advance, domain race, delete/reconnect with outage/lost-response, Nerve bootstrap hard-delete (PR #64) | P0 | BYO domain GA |
| B-06 | CU2 §§2.7-2.9 | Gmail OAuth / Gmail History / UI activation not live-validated: verification/CASA, auto revocation, bounded resync, `gmail_real_enabled` gate | P0 | Gmail option b |
| B-07 | Gate -1 / `processors.md` | Legal gates: no DPA/SCC/TIA signed (P1 Anthropic, P2 Fly, P4 Resend, P8/P9 storage/logs, P11 push), no Art 9(2) condition for special categories (health/religion), lawful-bases/counsel review open, notices lack controller/contact and still describe future SCC as current | P0 legal | Any real family data incl. owner mailbox |
| B-08 | Gate -1 | Residency `eu-strict` Vertex AI EU not wired; honest wording already fixed but config enforcement needs test | P1 | `eu-strict` claim |
| B-09 | CU1 validation | Operator drills unchecked (invite replay, household profile re-login, Fly app/volume/Machine count, secret absence in DB/logs, `/healthz`/`/readyz` revision, tamper fail-closed, cross-household IDOR, export vs data-map, delete+tombstone) — synthetic-only drills but required before org migration | P1 | Dedicated staging org promotion |
| B-10 | MVP Phase 4 checks | Manual e2e bundle/supersession, staged PDF email, inject vs poll identity, WhatsApp staged-only + HMAC reject, kill-switch `email_send=0` | P1 | Phase 4 sign-off |
| B-11 | CU1 handoff | Fly topology: only `personal` org, not `abrolia-synthetic`; dedicated org migration not done | P1 | Cost/isolation + real-data consideration |
| B-12 | Phase 5 | Pilotization not implemented: `channel_preferences` routing, channel binding, shared gateway, minimal Web, cost caps, observability/metrics, upgrade/rollback drills | P1 | Pilot go |

Non-blocker debt: codex/phase-4-real-actions dirty file + untracked landing not staged; source pins already current (`c26fc41` hermes, `c6f5ed03` nerve-cloud, `e3d011e` nerve-oss) per `docs/source-pins.md`.

## Desired End State (same as canon, gated)

1. Gate -1 legal pack signed by counsel; only synthetic data until then.
2. One dedicated staging org with isolated control-plane + household runtimes; all chaos/restore/idorr drills green.
3. Three email paths (`@abrolia.com`, BYO domain, dedicated Gmail) each pass synthetic + operator-account staging with `outcome_unknown` never auto-retried and secrets absent from DB/logs/manifests.
4. Phase 4 bundle/supersession + kill-switch + WhatsApp HMAC proven manual + automated.
5. Phase 5 preferences/routing, Web, caps, observability, metrics, rollback rehearsed.
6. Independent kill switches per provider; `eu-strict` fail-closed.

## Execution Phases (additional planning)

### Phase A — Legal & Residency Gate Closure (unblocks everything real)

**Goal:** Close B-07, B-08. No code touches real family data; only docs/config.

**Files:**
- `docs/privacy/processors.md`, `lawful-bases.md`, `dpia.md`, `data-map.md`, `privacy-notice-{en,ru}.md`, `docs/SECURITY.md`, `docs/source-pins.md`, `README.md`, `hermes_cloud/core/config.py`, `control_plane/config.py`

**Changes:**
1. Name legal entities for P1/P2/P4/P8/P9/P11; record addresses/representatives.
2. Execute DPA 28(3) + SCC module 2 + TIA per `processors.md` §2; commit registry update only after signatures (separate PR before any provider flag flip).
3. Counsel selects Art 9(2) condition for health/religion in school letters (or explicit filtering policy) — update `lawful-bases.md` §3 + `dpia.md` R7 from `high/blocking` to chosen condition with rationale. Keep blocking until signed.
4. Fill controller/contact, Art 13/14 info, DPA/SCC status in notices/README — replace future-tense SCC claim with actual pending status.
5. Wire `residency_mode=eu-strict` to require Vertex AI EU client; add startup probe that crashes with `EU_STRICT_REQUIRES_VERTEX` if absent; add test `tests/test_config_and_cli.py::test_eu_strict_fails_closed_without_vertex`.

**Acceptance:**
- [ ] `processors.md` shows ✅ for P1/P2/P4 with dates; TIA docs linked; `privacy-notice-*` diff reviewed by counsel.
  *Partially met 2026-08-30:* P1 (Anthropic) and P4 (Resend) show ✅ DPA + ✅ SCC
  with dated copies under `docs/privacy/vendor-dpas/`. **Open: P2 (Fly.io) — no
  executed DPA, no SCC assessment; ALL THREE TIAs; the Art. 27 representative.**
- [x] `lawful-bases.md` R7 no longer `high/blocking`; `dpia.md` R7 residual
  stated with the condition cited. *Closed 2026-08-30, with one honest
  correction:* the Art 9(2)(a) condition was chosen 2026-08-12 and R7 is no
  longer blocking — but the residual landed at **`средний, принят`
  (medium, accepted)**, not the `low` this box predicted
  (`docs/privacy/dpia.md:57`). The gap is deliberate and documented: material
  about THIRD parties has no Art 9(2) condition and never will, so it is
  removed on request rather than made lawful. Art 9(4) checks for DE/IT/NL/ES
  are done. Ticking on the criterion this box actually gates — condition
  chosen, no longer blocking — and recording the residual as it is.
- [x] `pytest -k eu_strict` proves `eu-app` boots, `eu-strict` without vertex exits non-zero. *Re-run 2026-08-30: exit 0.*

**Gate:** No `ABROLIA_REAL_*` flag enabled until A is merged.

### Phase B — Foundation Hardening & Dedicated Org Migration (B-09, B-11)

**Goal:** Promote synthetic contour from `personal` org to `abrolia-synthetic` org and close CU1 operator drills.

**Files:**
- `deploy/control-plane/fly.toml`, `deploy/control-plane/Dockerfile`, `deploy/runtime/*`, `control_plane/provisioning/fly.py`, `control_plane/api/internal_bootstrap.py`, `hermes_cloud/runtime/service.py`, `docs/onboarding-runbook.md`, `docs/control-plane-restore.md`, `tests/control_plane/test_phase1_chaos_matrix.py`

**Changes:**
1. Create `abrolia-synthetic` Fly org; redeploy control plane + one synthetic household there; verify `fly orgs show`, one Machine `ams`, one volume `1GiB encrypted`.
2. Re-run full 8-window SIGKILL matrix via `tests/control_plane/test_phase1_chaos_matrix.py` + live checks: invite replay rejected, profile refresh/re-login preserved, exactly 1 app/volume/Machine, DB/log/API contain no secret values, `/healthz` 200 + `/readyz` 503 when workers_paused / 200 when active + revision/hash, tamper fails closed, cross-household A×B 404, export vs `data-map.md` (no secrets/hashes), delete → deprovision + tombstone rejects late bootstrap.
3. Rehearse isolated backup/restore with workers paused (repeat `docs/control-plane-restore.md`): integrity_check, zero FK violations, mode 0600 pause marker, smoke without leasing, resume-jobs, new onboarding through rev 1, destroy temp app.

**Acceptance:**
- [x] Live IDs (new org, Machines, volumes, image digests, household, rev/hash) recorded in `thoughts/shared/implementations/2026-08-04-onboarding-foundation-validation.md` addendum.
- [x] `pytest -p no:cacheprovider -m "not live"` 626+ and `tests/control_plane` 215+ remain green on new org.
- [x] Cost tag: note monthly Fly cost for synthetic org.

### Phase C — Email Identity Closure (B-01, B-02, B-05, B-06)

**Goal:** Close CU2 2.4–2.9 remaining gates. Split into three slices that can land independently.

#### C1 — Upstream & Durable Receipt (B-01, B-02)

**Files:**
- Upstream `nerve-cloud/internal/cloudapi/handler_keys.go` + `internal/store/*` + `go test ./...`
- Local `control_plane/providers/email/*.py`, `control_plane/provisioning/worker.py`, `control_plane/provisioning/secrets.py`, `control_plane/migrations/0003_*` (if needed), `tests/control_plane/email/*`, `tests/control_plane/test_provisioning_jobs.py`
- Generation-scoped convergence (2026-09-01): `control_plane/email/models.py`,
  `control_plane/crypto.py`,
  `control_plane/migrations/0015_email_secret_install_generation.sql`,
  `tests/control_plane/test_db.py`, `tests/control_plane/chaos_child.py` (the B-02 convergence regressions live beside the SIGKILL/reclaim harness there, not under `email/` — see the acceptance note)

**Branches:** `fix/b02-receipt-convergence-regression`,
`feat/c1-generation-scoped-convergence`

**Changes:**
1. **Upstream B-01:** Resolve requested `org_id` against authenticated tenant; reject A→B service-token request with 403; add negative HTTP test `TestServiceToken_CrossOrgRejected` (A billing token requests B's org). Abrolia side adds adapter assertion that tenant never calls `/v1/service-tokens`.
2. **Local B-02:** Add generation-scoped non-secret install receipt for email secrets: `email_secret_installs {job_id, generation, secret_name, installed_at, sink_digest}` derived from `SecretSink` inspection, not secret value. Worker: after `SecretSink.write`, atomically write receipt + transition to `secret_handoff_pending` before marking `verified`. Reconcile path: if `SecretSink` contains generation N (via listing staged secret names) and no receipt exists, create receipt and advance; if lease reclaimed before receipt, stay `secret_handoff_unknown` (already). No generation → no `verified`.
3. Tests: SIGKILL after sink before commit → reclaimed lease still requires receipt; no secret in DB/log/telemetry; revoked/replayed sink behavior.

**Acceptance:**
- [ ] Nerve PR with cross-org test green + `go test ./... -count=1`.
- [x] Abrolia `pytest tests/control_plane/email tests/control_plane/test_provisioning_jobs.py` proves crash-after-sink converges without operator, and hard-reclaim without sink stays `secret_handoff_unknown`.
  *Audited 2026-08-30 — half evidenced, and the box stays open for the other
  half.* The suite is green, and the SECOND clause is proven by name:
  `tests/control_plane/email/test_identity.py:478
  ::test_unknown_secret_handoff_never_verifies_from_secretless_inspect`. The
  receipt MECHANISM for the first clause is implemented and reachable —
  `control_plane/provisioning/worker.py:1056-1075` creates the
  `email_secret_installs` receipt on reclaim when the sink already contains the
  generation, exactly as the plan specified. What could not be found is a test
  that DRIVES that path: `email_secret_installs` appears in tests only in
  `test_db.py` (schema) and `test_provision_dry_run.py` (audit/export), never
  as a crash-after-sink convergence. Needs one regression, or a pointer to the
  one that exists.
  *Closed 2026-08-30, same day.* The regression exists, in two halves, and both
  were checked in the failing direction before being trusted:
  `tests/control_plane/test_provisioning_jobs.py
  ::test_reclaim_after_converged_sink_write_records_durable_receipt` (reconcile
  converges from the live sink's `contains` and WRITES the receipt — breaking
  the `INSERT` fails it) and
  `::test_durable_receipt_alone_settles_reclaim_without_live_sink` (a receipt
  row alone settles reclaim with the sink denying and `contains` never
  consulted — deadening the lookup fails it). They live beside the
  crash-window siblings in `test_provisioning_jobs.py`, not under
  `tests/control_plane/email` as this line predicted, because that is where
  the SIGKILL/reclaim harness is. The bare `except Exception` around the
  receipt I/O flagged in the 2026-08-30 validation was narrowed to
  `sqlite3.Error` in the same change (the C6b shape).
  *Narrowed 2026-08-30, by Codex review on #108, and the narrowing is right.*
  The tick stands on what this line gates — the two named proofs exist and
  drive the mechanism — but the mechanism they prove is WEAKER than this
  step's own Changes item 2 specified: the proof is name-scoped, not
  generation-scoped. The acceptance command above was also corrected in the
  same pass to collect the file the proofs live in; as first written it
  stayed green with both regressions deleted. See the open box below.

- [x] **Generation-scoped convergence — "no generation → no `verified`"
  (opened 2026-08-30 by Codex review on #108; closed 2026-09-01).** Changes item 2 specified
  `email_secret_installs {job_id, generation, secret_name, installed_at,
  sink_digest}`; the shipped schema (`0005`) carries neither `generation` nor
  `sink_digest`, and the reclaim proof consumes only the binding NAME:
  `_email_secret_installed` asks the sink `contains(namespace_ref,
  binding_ref)` and looks receipts up by `job_id` alone. Consequence: if
  generation N−1's secret survives in the namespace (incomplete teardown, a
  crash window) and generation N's job reconciles with empty one-time
  material, the name-only probe answers "installed", a receipt is written for
  N, and `_finish_step` marks N verified while the runtime holds N−1's
  credentials — verified-but-stale, on the P0 real-email path. Closes when a
  non-secret generation identifier travels through provider output, sink
  installation/proof (a versioned marker where the sink cannot attest
  values), and the receipt schema; neither a live probe nor a receipt is
  accepted unless it matches the current job's generation; and one
  parameterized regression covers both consumers (live-sink and
  durable-receipt) with an old and a new generation, asserting N stays
  unverified on N−1's remains. This is its own slice, not a fix commit: it
  changes a provider contract, the sink protocol, and a schema, which is the
  C6a→C6b / C3f shape — review found scope inside a fix, and the scope gets a
  plan rather than a patch.

  *Closed 2026-09-01 on `feat/c1-generation-scoped-convergence`.* The
  generation is the provisioning job that installed the material: stable
  across that job's own retries and reconciliations, so the legitimate
  crash-after-sink case still converges without an operator, and necessarily
  different for a later re-provisioning, so N−1's remains cannot answer for N.
  It is assigned by the control plane rather than taken from provider output —
  the party whose freshness is in question must not certify its own freshness,
  which is the one place this deviates from the box's wording and is stated
  here rather than quietly.

  It reaches the sink as a MARKER NAME (`<binding>_GEN_<digest>`), because the
  Fly sink can attest that a name exists and nothing whatever about the value
  behind it — the "versioned marker where the sink cannot attest values" this
  box asked for. Credential and marker travel in ONE `install`, which sends
  every name in a single `fly secrets import`, so no crash window can leave a
  marker attesting a credential that is not there.

  Schema `0015` adds `generation`, `marker_name` and `sink_digest`.
  `sink_digest` is deliberately not a digest of the secret value — no value
  reaches that table — but of the namespace and marker actually attested.
  Legacy `0005` rows default to an empty generation, which can never equal a
  job id, so they are inert rather than permissive; that property has its own
  regression because a "sensible" future default would silently reopen the
  hole.

  Both consumers are proven by
  `test_convergence_is_scoped_to_the_generation_that_installed_the_secret`,
  parameterized over (live-sink, durable-receipt) × (stale, fresh) — the
  `fresh` column included because without it the test passes equally well if
  nothing ever converges. Two mutations were run and each killed exactly its
  own case: reverting the live probe to `contains(namespace_ref, binding_ref)`
  fails `[stale-live-sink]`, and reverting the receipt lookup to `job_id`
  alone fails `[stale-durable-receipt]` and the legacy-row regression.

  Erasure takes the markers too, driven from the receipts rather than from a
  naming convention. Full `-m "not live"` suite green.

#### C2 — BYO Domain Live (B-05)

**Files:** `control_plane/providers/email/nerve_byo_domain.py`, `control_plane/api/email.py`, onboarding templates, `tests/control_plane/email/test_byo*`

**Changes:** Keep bounded backoff (30/60/120/300/600) + manual CHECK; no new code unless B-01 receipt requires it. Run live battery on staging synthetic org:

1. Reload/login resumes same DNS records/state (hermetic + live).
2. Wrong/partial DNS stays `waiting_user`; verified DNS advances once.
3. Two-connection writer race for same canonical domain → one owner (HMAC uniqueness).
4. Delete/reconnect: DNS present, provider unavailable, lost-response (webhook/key/inbox/domain/org order, `outcome_unknown` explicit, Nerve bootstrap PR #64 hard-delete path exercised).

**Acceptance:**
- [ ] `thoughts/shared/implementations/2026-08-05-abrolia-phase-2-email-identity-validation.md` addendum with operator-observed DNS IDs + `nerve domain` HMACs; `pytest tests/control_plane/email -k byo` green.

#### C3 — Gmail Runtime & Activation (B-06)

**Files:** `hermes_cloud/ingest/gmail_api.py`, `hermes_cloud/execute/gmail_api_send.py`, `control_plane/providers/email/google_oauth.py`, `control_plane/provisioning/manifest.py`, `hermes_cloud/core/migrations/0006_*.sql`

**Changes:**
1. Enforce `gmail_real_enabled` only with test-user allowlist + `verification/CASA` config gate; synthetic gate still permits `.test`.
2. Implement Gmail History cursor: save `historyId` on connect (no history import), `history.list` polling, INBOX filter, cursor advance only after durable append, 404 expired history → bounded resync with overlap + dedup, `needs_attention` on gap.
3. Send: deterministic RFC Message-ID from approval/effect id, timeout → `rfc822msgid:` search in SENT (once → success, rejected → failed, ambiguous → `outcome_unknown` no blind resend).
4. OAuth: PKCE, `state` one-time bound to (owner session, household, workflow rev), `prompt=select_account`, exact scopes `openid email gmail.readonly gmail.send`, address confirmation, refresh token direct to `SecretSink` (memory-only access token), disconnect revoke + delete.

**Acceptance:**
- [ ] Staging allowlisted dedicated Gmail: connect → receive → approve/send → revoke/delete each manual; `tests/test_gmail_api_*` green; no `HERMES_GMAIL_ADDRESS`/`APP_PASSWORD` in prod path (`rg` check).

**Phase C gate:** `ABROLIA_REAL_EMAIL_ENABLED` stays off until C1–C3 all green + A done.

### Phase D — Real Actions Sign-off (B-10)

**Goal:** Close MVP Phase 4 manual criteria (reuses Phase C runtime).

**Closure (2026-08-06):** Phase D is closed for the operator-approved synthetic live scope.
Fly Machine, staged PDF email, kill-switch, Calendar create→move without duplicate, WhatsApp
HMAC, and confirmed outbound are evidenced in
`thoughts/shared/implementations/2026-08-06-phase-D-real-actions-validation.md`. Dedicated Gmail
OAuth / History is explicitly deferred to the start of Phase E and remains tracked under B-06.

**Files:** `hermes_cloud/execute/gcal.py`, `hermes_cloud/execute/email_send.py`, `hermes_cloud/runner/pipeline.py`, `hermes_cloud/ingest/nerve_webhook.py`, `hermes_cloud/ingest/gmail_poll.py` (test seam), `tests/test_bundle.py`, `tests/test_supersession.py`, `tests/test_gcal.py`, `tests/test_whatsapp.py` (currently dirty), `tests/test_media_ingest.py`, `tests/test_scheduler.py`

**Changes:**
1. Clean `tests/test_whatsapp.py` diff (staged-only reply enforcement); re-run `pytest tests/test_whatsapp.py -q`.
2. Manual wedge battery on staging synthetic household:
   - `15€ excursion 12.09 → bundle (event+task) → ✅ → calendar event shared + task list → letter "moved to 19.09" → reconcile-card → ✅ → same event updated (deterministic ID)`.
   - Staged PDF email: `Kому: schule@example.de` shown, MIME attached only after ✅, delivered.
   - Dedicated Gmail test-user: inject vs Gmail API same canonical `Message-ID`/thread (only effect code differs); disconnect revokes grant (manual proof).
   - WhatsApp: household message → card/dialog, any outbound only after ✅, wrong per-household HMAC rejected.
   - `HERMES_EMAIL_SEND=0` blocks with honest message (`test_email_send.py::test_kill_switch`).
3. Keep legacy IMAP poller as internal test seam only; assert not reachable from onboarding.

**Acceptance:**
- [x] PII-safe manual transcript recorded with approval, event, effect, and message IDs. CLI/live
  transcript is the accepted evidence surface for this headless synthetic runtime; UI screenshots
  move to the Phase E Web surface.
- [x] Phase 4 suites and the full non-live suite are green in the isolated deployment worktree.

### Phase E — Pilotization (B-12)

**Goal:** Implement MVP Phase 5 (onboarding state machine, OAuth prod path, preferences/routing, bindings, gateway, Web, observability/caps).

**Files:**
- `control_plane/onboarding/{state,provision}.py`, `control_plane/providers/email/google_oauth.py`, `hermes_cloud/core/channel_preferences.py` (migration `0006_channel_preferences.sql`), `hermes_cloud/onboarding/channel_bindings.py` / `control_plane/api/channel_bindings.py`, `gateway/whatsapp_router.py`, `channels/router.py`, `channels/web.py + web/`, `hermes_cloud/runner/extraction.py`, `control_plane/observability.py`, `deploy/*`

**Changes:**
1. Durable 3-step machine: `email → WhatsApp → primary` with fix-before-effect, idempotency, downstream invalidation; `provision.py --dry-run` lists writes.
2. Google OAuth prod path (reuse C3) gated behind A; legacy env path deprecated.
3. Preferences table `channel_preferences {subject, primary_channel, fallback_channel}` household-row now, per-member schema ready; fallback is verified owner contact email, reject agent inbox; source-channel replies, permanent failure → short email fallback, `outcome_unknown` no duplicate.
4. Channel binding: once-owner-authorized challenge for Telegram/WA/Web → `RunContext {channel}`; client payload never chooses household/role; second adult separate binding.
5. Shared gateway narrow multi-tenant (no model/tools/secrets) — exact sender→household mapping, unknown/ambiguous deny, durable webhook before ACK, per-household relay-HMAC.
6. Minimal Web: authenticated chat over `runner/model.py`, PWA manifest, opt-in push; no vault/offline archive.
7. Observability: structured logs without content, `/health` (nerve key, telegram, WA instance/gateway, Google grant, DB, backup age), alerts (DLQ>0, sticky executing, primary unavailable, backup stale, budget exceeded).
8. Cost caps: per-household/day token counter (usage replies) with soft-limit degradation `extraction-only + "дневной бюджет исчерпан"`.
9. Release: tag, migrate-on-start with backup-before-migrate, restore drill.

**Acceptance:**
- [x] `tests/control_plane/test_onboarding_state.py`, `tests/control_plane/email/test_google_oauth.py`, `tests/control_plane/test_channel_preferences.py`, `tests/control_plane/test_channel_bindings.py`, `tests/test_web_channel.py` all green per plan criteria.
  *Paths corrected 2026-08-30.* Four of the five were written from the
  plan's expectations rather than the tree, so the command failed on a
  green checkout; only `test_web_channel.py` was ever at the path named.
- [ ] `provision.py --dry-run` on staging lists three steps without writes.
- [ ] Manual pilot onboarding ≤60m per runbook (step1/2/3 checks + primary switch without history loss).

### Phase F — Release Gating & Rollout

**Goal:** Independent kill switches + staged rollout.

**Files:** `control_plane/config.py`, `control_plane/feature_flags.py`,
`control_plane/onboarding/service.py`, `control_plane/provisioning/worker.py`,
`AGENTS.repo-invariants.md`, `docs/onboarding-runbook.md`,
`docs/privacy/processors.md` §4, `tests/control_plane/test_email_option_flags.py`,
`tests/control_plane/email/conftest.py`,
`tests/control_plane/test_art9_household_consent.py`,
`tests/control_plane/test_real_email_wiring.py`,
`tests/control_plane/test_provisioning_jobs.py`, `control_plane/api/app.py`,
`control_plane/email/models.py`, `control_plane/web/templates/onboarding.html`,
`tests/control_plane/test_ui_contract.py`, `tests/test_feature_flags.py`,
`gateway/whatsapp_router.py`, `control_plane/container.py`,
`control_plane/privacy/delete.py`, `tests/control_plane/test_real_email_wiring.py`,
`tests/control_plane/email/conftest.py`, `tests/control_plane/conftest.py`,
`docs/onboarding-runbook.md`,
`thoughts/shared/plans/2026-08-06-phase-DE-pilot.md`,
`.phase-f-mutations.py`, `control_plane/provisioning/contracts.py`,
`control_plane/provisioning/fakes.py`,
`tests/control_plane/email/test_identity.py`,
`tests/control_plane/test_consent_withdrawal.py`,
`.check-fixtures-allow`.

**Branches:** `codex/phase-F-email-option-kill-switches`, `codex/phase-F-hide-cut-email-cards`, `codex/phase-F-retire-the-dead-managed-switch`, `codex/phase-F-allowlist-at-dispatch`.

**Scope corrected 2026-08-21.** "feature-flag table" named a document rather
than the code, and the module that implements the switches was never listed —
which is part of how it came to have no caller:

- `control_plane/feature_flags.py` — the switches themselves. `check_provider_enabled`
  existed, was tested in isolation, and was imported by nothing outside its own
  test. `ABROLIA_GMAIL_ENABLED=0` gated nothing while the runbook table and this
  plan both described it as fail-closed.
- `control_plane/onboarding/service.py` — the call site, in `_assert_email_rollout`,
  which both `select` and `retry` already run.
- the four test modules: the switch's own behaviour, and the three suites that
  exercise gated options and therefore have to opt in.

**MVP scope, 2026-08-21.** `family_domain` and `gmail_agent` are cut from MVP and
gated. `abrolia_managed` is deliberately NOT gated yet: wiring it would take
email away from every deployment that has not set `ABROLIA_MANAGED_EMAIL_ENABLED`,
including the synthetic app, which sets none of them. That is a separate step
with a `fly.toml` change behind it.

**Round 2, 2026-08-21.** Codex found the switch was enforced only at selection.
Provisioning is queued, so an operator flipping a flag off mid-incident did not
stop the Gmail or BYO jobs already in the queue — the case the switch exists
for. The check now also runs in `ProvisioningWorker`, at both entry points that
reach a provider (`_run_once` and `_reconcile`), reading the flag at call time
and exempting shutdown work so teardown is never stranded. Recorded as an
invariant, this being the second instance of the class after the Art. 9(2)(a)
content restriction.

**Round 3, 2026-08-21.** The onboarding page still rendered all three cards. A
cut option was refused on submit, so this was a courtesy rather than a hole, but
the page and the gate must not disagree about what is on offer — the same pair
that already diverged once over the Art. 9(2)(a) consent and made browser
onboarding impossible. The page now asks the gate's own predicate
(`email_option_offered`), and enumerates options from `EMAIL_SELECTION_KINDS`
rather than a fourth hard-coded list.

**Round 4, 2026-08-23.** The closure design pass
(`thoughts/shared/plans/2026-08-22-phase-F-closure-design-pass.md`) hoisted the
reconcile contract onto the worker instead of the adapters' good manners: the
`Provisioner` protocol now declares `reconcile`
(`control_plane/provisioning/contracts.py`), and the deterministic fake
implements it (`control_plane/provisioning/fakes.py`). Two suites whose stub
adapters silently depended on the deleted inspect tail were renegotiated onto
the contract rather than left asserting its old semantics
(`tests/control_plane/email/test_identity.py`,
`tests/control_plane/test_consent_withdrawal.py`). `.phase-f-mutations.py`
is the mutation harness that proves the new erasure sequence test kills its
defects; it is committed because an uncommitted proof proves nothing to the
next reader.

**Round 5, 2026-08-23.** Codex's eleventh round found the hoisted route
half-honest: shutdown routing no longer asked the adapter's shape, but the
derived teardown reference still knew only Google's and Nerve's contracts —
so an ambiguous synthetic email job (cancel, reset, withdrawal or erasure
origin, nothing durably recorded) repeated its reconciliation error forever
with the identity held. The synthetic provisioner names its resource
`synthetic-email:<identity_id>` — `_validate_email_external_ref` enforces
exactly that at settle time — so the worker now derives it whenever the job's
own adapter declares the synthetic public identity; adapters that declare
no contract still refuse, whichever registry name they sit under. A
four-origin regression lands with the fix, and mutation M5 kills the revert.

**Round 6, 2026-08-23.** Codex's twelfth round found the race the ownership
doctrine implied but no test held: a call still IN FLIGHT when the deletion
transaction commits is touched by nothing delete() runs (the sweep cancels
only `pending`/`waiting_user`; `resume` reads only
`running`/`outcome_unknown`) — and when that call's answer arrives as
`ProviderWaiting`, `_schedule_cancelled_waiting_cleanup` refused the
`running` row outright, so the parent settled `waiting_user`, a state
nothing in erasure ever reads again, with a live org/inbox/key behind it.
The remedy asks deletion ownership BEFORE the status restriction:
a `running` row of a deletion-owned household is admitted, its parent
settled unresolved and its external resource recorded in the SAME
transaction that schedules the cleanup from the answer's own reference.
The reference-less waiting shapes (BYO DNS legitimately names no resource;
the synthetic validator forbids a reference outright) land unresolved via
the handler's ownership branch, where reconcile derives their teardown as
Round 5 established. The four-contract regression parks each adapter on a
gate mid-`ensure`, begins erasure, releases the answer, and drives cleanup,
compensation, disconnect and resume to the household row's removal;
mutations M6 (running never admitted) and M7 (erasure never owns the
reference-less answer) kill the reverts. `.check-fixtures-allow` gains one
exact-value entry: the managed contract's inbox domain is production by
definition (`<local_part>@abrolia.com` is what the validator compares
against), so the synthetic regression cannot use a documentation-domain
placeholder.

**Changes:**
1. Per-provider flags, `default off`, fail-closed, toggle tested. **Consolidated
   2026-08-22** to the short form Step F1 of `phase-DE-pilot.md` already
   sanctioned: `ABROLIA_REAL_EMAIL_ENABLED` (managed + BYO incident brake, plus
   the household allowlist), `ABROLIA_BYO_EMAIL_ENABLED` and
   `ABROLIA_GMAIL_ENABLED` (per-option product gates),
   `ABROLIA_WHATSAPP_SHARED_ENABLED` (relay). `ABROLIA_MANAGED_EMAIL_ENABLED`,
   `ABROLIA_WHATSAPP_DEDICATED_ENABLED` and `ABROLIA_WEB_PUSH_ENABLED` are
   retired: they had no call site, and each had a stronger live counterpart —
   the last two cannot even boot. Enforced by
   `tests/test_feature_flags.py::test_every_declared_flag_gates_a_call_site`.
2. Rollout order: synthetic → operator accounts → invited pilot families per provider; each transition requires `Phase A` legal + `Phase C1` receipt + `go test` + `pytest -m "not live"` + one manual live gate.

**Acceptance:**
- [ ] Flag matrix doctested; `git diff --check` + `ruff` +
  `gitleaks detect --log-opts="--all"` + `check_fixtures --all --require-deny`
  green. **CI is the only surface that can close this box (noted 2026-08-30):**
  `check_fixtures --all --require-deny` exits **2** locally — a refusal, not a
  warning — because the private deny-patterns file (`HERMES_EXTRA_DENY_FILE`)
  exists only in CI. Everything else is green locally at 2026-08-30: suite 1686,
  `git diff --check` clean, `ruff` clean, `gitleaks` no leaks / 170 commits,
  `check_fixtures --all` clean. (`gitleaks --all` was never the real
  invocation; corrected above.)

## Implementation Approach

1. Work per phase slice on a short-lived `codex/<phase>` branch from current `b9d3614`; merge via PR with the exact “Mandatory commands at phase completion” from CU1 plan §7.5 + CU2 testing strategy (unit, contract, chaos `SIGKILL`, security, privacy).
2. Isolate secrets: Fly/Nerve/Google values never in argv/DB/log/manifest; use stdin + `SecretSink` inspection only.
3. Keep `landing/` untracked until explicit publishing decision per handoff.
4. After each phase, append evidence to the corresponding validation doc before claiming done.

## Testing Strategy (canon-mandated)

- **Unit:** local-part/IDNA, state machine, OAuth PKCE/state, HMAC/replay, Message-ID normalization, receipt taxonomy.
- **Contract:** one email-identity suite across fake/Nerve managed/Nerve BYO/Gmail; one send suite across SMTP-test/Nerve/Gmail.
- **Integration:** hermetic fake Nerve/Google + synthetic prod canary (explicit `ABROLIA_NERVE_LIVE_CONFIRM`).
- **Chaos:** 8-window SIGKILL (transition→lease→provider→response→Sink→result→claim→activate→cleanup).
- **Security:** CSRF/IDOR/OAuth mix-up, tenant escape, DNS confusables, webhook forgery/replay, secret/PII canaries.
- **Privacy:** export/delete/retention completeness per new table/provider object/attachment.

## References

- Parent canon and gates: `docs/privacy/dpia.md` §5, `lawful-bases.md` §3, `processors.md` §§2–4, `docs/SECURITY.md`, `docs/source-pins.md`
- Live contract: `docs/nerve-phase3-live-contract.md`, `thoughts/shared/implementations/*`
- Rollback/restore: `docs/control-plane-restore.md`, `docs/onboarding-runbook.md`

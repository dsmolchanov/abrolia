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
- [ ] `lawful-bases.md` R7 no longer `high/blocking`; `dpia.md` R7 residual `low` with condition cited.
- [ ] `pytest -k eu_strict` proves `eu-app` boots, `eu-strict` without vertex exits non-zero.

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
- [ ] Live IDs (new org, Machines, volumes, image digests, household, rev/hash) recorded in `thoughts/shared/implementations/2026-08-04-onboarding-foundation-validation.md` addendum.
- [ ] `pytest -p no:cacheprovider -m "not live"` 626+ and `tests/control_plane` 215+ remain green on new org.
- [ ] Cost tag: note monthly Fly cost for synthetic org.

### Phase C — Email Identity Closure (B-01, B-02, B-05, B-06)

**Goal:** Close CU2 2.4–2.9 remaining gates. Split into three slices that can land independently.

#### C1 — Upstream & Durable Receipt (B-01, B-02)

**Files:**
- Upstream `nerve-cloud/internal/cloudapi/handler_keys.go` + `internal/store/*` + `go test ./...`
- Local `control_plane/providers/email/*.py`, `control_plane/provisioning/worker.py`, `control_plane/provisioning/secrets.py`, `control_plane/migrations/0003_*` (if needed), `tests/control_plane/email/*`

**Changes:**
1. **Upstream B-01:** Resolve requested `org_id` against authenticated tenant; reject A→B service-token request with 403; add negative HTTP test `TestServiceToken_CrossOrgRejected` (A billing token requests B's org). Abrolia side adds adapter assertion that tenant never calls `/v1/service-tokens`.
2. **Local B-02:** Add generation-scoped non-secret install receipt for email secrets: `email_secret_installs {job_id, generation, secret_name, installed_at, sink_digest}` derived from `SecretSink` inspection, not secret value. Worker: after `SecretSink.write`, atomically write receipt + transition to `secret_handoff_pending` before marking `verified`. Reconcile path: if `SecretSink` contains generation N (via listing staged secret names) and no receipt exists, create receipt and advance; if lease reclaimed before receipt, stay `secret_handoff_unknown` (already). No generation → no `verified`.
3. Tests: SIGKILL after sink before commit → reclaimed lease still requires receipt; no secret in DB/log/telemetry; revoked/replayed sink behavior.

**Acceptance:**
- [ ] Nerve PR with cross-org test green + `go test ./... -count=1`.
- [ ] Abrolia `pytest tests/control_plane/email` proves crash-after-sink converges without operator, and hard-reclaim without sink stays `secret_handoff_unknown`.

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
- [ ] `tests/test_onboarding.py`, `test_google_oauth.py`, `test_channel_preferences.py`, `test_channel_bindings.py`, `test_web_channel.py` all green per plan criteria.
- [ ] `provision.py --dry-run` on staging lists three steps without writes.
- [ ] Manual pilot onboarding ≤60m per runbook (step1/2/3 checks + primary switch without history loss).

### Phase F — Release Gating & Rollout

**Goal:** Independent kill switches + staged rollout.

**Files:** `control_plane/config.py`, feature-flag table, `docs/onboarding-runbook.md`, `docs/privacy/processors.md` §4

**Changes:**
1. Per-provider flags: `ABROLIA_MANAGED_EMAIL_ENABLED`, `ABROLIA_BYO_EMAIL_ENABLED`, `ABROLIA_GMAIL_ENABLED`, `ABROLIA_WHATSAPP_SHARED_ENABLED`, `ABROLIA_WHATSAPP_DEDICATED_ENABLED`, `ABROLIA_WEB_PUSH_ENABLED` — all `default off`, fail-closed; flag toggle tested in Phase C/D/E suites.
2. Rollout order: synthetic → operator accounts → invited pilot families per provider; each transition requires `Phase A` legal + `Phase C1` receipt + `go test` + `pytest -m "not live"` + one manual live gate.

**Acceptance:**
- [ ] Flag matrix doctested; `git diff --check` + `ruff` + `gitleaks --all` + `check_fixtures --all --require-deny` green.

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

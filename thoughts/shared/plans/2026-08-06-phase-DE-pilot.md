---
title: "Phase D/E/F — Real Actions Sign-off, Pilotization & Release Gating (B-10, B-12)"
status: planning
created_at: "2026-08-06"
base_commit: b9d3614
parent: thoughts/shared/plans/2026-08-06-canon-execution-plan.md
depends_on:
  - thoughts/shared/plans/2026-08-06-phase-C-email-closure.md
  - thoughts/shared/plans/2026-08-02-family-ops-assistant-mvp.md
  - thoughts/shared/plans/2026-08-04-abrolia-phase-1-onboarding-foundation.md
scope: phase-D/E/F
data_policy: synthetic-only-until-explicit-gates
blockers: [B-10, B-12]
gate: "Phase D manual bundle/supersession green + Phase E pilotization + Phase F per-provider kill switches"
---

# Phase D/E/F — Real Actions Sign-off, Pilotization & Release Gating (B-10, B-12)

## Overview

This file covers three canon phases that are planned together because D provides the runtime correctness proof, E builds the pilot surface, and F gates rollout. Each phase lands as a separate PR, but the plan is written as one document to keep the dependency chain visible.

| Phase | Blocker | Severity | Goal |
|-------|---------|----------|------|
| D | B-10 | P1 | Manual e2e bundle/supersession, staged PDF email, inject vs poll identity, WhatsApp staged-only + HMAC reject, kill-switch `email_send=0` |
| E | B-12 | P1 | Pilotization: `channel_preferences` routing, channel binding, shared gateway, minimal Web, cost caps, observability/metrics, upgrade/rollback drills |
| F | — | — | Release gating + rollout: per-provider `ABROLIA_*_ENABLED` kill switches, staged rollout order |

**Dependency:** D reuses the Phase C runtime (Nerve/Gmail email + `email_secret_installs` + History cursor). E reuses D's pipeline plus the durable 3-step onboarding machine from CU1. Do not start E's gateway/Web before D's bundle pipeline is green — the cards and effects are the same objects.

## Current State

### Runtime pipeline at `b9d3614`

- `hermes_cloud/runner/pipeline.py` — event → extraction → card → approval → effect journal. `hermes_cloud/execute/{gcal,email_send,nerve_send,gmail_api_send}.py` — gcal/event, email MIME, and channel executors. `hermes_cloud/ingest/{eml,nerve_webhook,gmail_poll,gmail_api,media,whatsapp_webhook}.py` — ingest paths. `hermes_cloud/core/{events,effects,card,commitments,retention,dsar}.py` — durable primitives.

- `hermes_cloud/ingest/gmail_poll.py` — legacy IMAP poller, now an internal test seam only (`hermes_cloud/core/config.py:99-110` gates `has_gmail` on `HERMES_LEGACY_IMAP_TEST_ONLY=1 && manifest_path is None`). `hermes_cloud/email/*` — Nerve-managed vs Gmail composition paths. `hermes_cloud/execute/gcal.py:264-282` previously wrote OAuth JSON plaintext to volume — Gmail adapter must not copy this; runtime token handling for Google Calendar is separate from the new agent-Gmail refresh-token-via-`SecretSink` path.

- `tests/test_bundle.py`, `tests/test_supersession.py`, `tests/test_gcal.py`, `tests/test_whatsapp.py` (currently dirty per `git status` at `b9d3614`), `tests/test_media_ingest.py`, `tests/test_scheduler.py`, `tests/test_email_send.py`, `tests/test_eml.py`, `tests/test_effects.py` exist. `hermes_cloud/channels/*` and `hermes_cloud/onboarding/*` stubs exist but lack Phase E routing/binding.

- `control_plane/onboarding/{state,provision}.py`, `control_plane/providers/email/google_oauth.py`, `gateway/whatsapp_router.py`, `channels/{router,web}.py`, `web/` PWA, `control_plane/observability.py`, `deploy/*` are the E/F targets — most are missing or skeletal at `b9d3614`.

- `docs/onboarding-runbook.md` documents the synthetic 3-step onboarding; `docs/control-plane-restore.md` documents backup/restore (already rehearsed in Phase B).

### What Phase D/E/F must fix

1. `tests/test_whatsapp.py` dirty diff (staged-only reply enforcement) must be cleaned and proven.
2. Manual wedge battery for bundle/supersession/staged PDF/inject-vs-poll/WhatsApp HMAC/kill-switch has not been recorded.
3. Pilotization missing: `channel_preferences` table + routing, channel binding once-owner-authorized, shared gateway narrow multi-tenant, minimal Web, observability `/health` + alerts, cost caps with degradation, release tag + migrate-on-start with backup-before-migrate, rollback drill.
4. Release kill switches `ABROLIA_MANAGED_EMAIL_ENABLED` etc. not yet per-provider independent.

### Non-goals

- No real family data until Phase A + C gates are met — D's manual wedge battery runs on the `abrolia-synthetic` staging synthetic household with synthetic content (or a cheap dedicated test domain/Gmail from Phase C) — never a real family's inbox.
- No offline archive / vault; no second runtime replica.

---

## Phase D — Real Actions Sign-off (B-10)

**Status: closed 2026-08-06.** The operator-approved live scope passed on the dedicated
`abrolia-phase4-live-synthetic` Fly Machine. Evidence:
`thoughts/shared/implementations/2026-08-06-phase-D-real-actions-validation.md`. Gmail OAuth /
History wedges D2.4–D2.5 are intentionally moved to Phase E2; they are not represented as a
legacy SMTP/IMAP production pass.

### Desired End State

After `codex/phase-D-real-actions` merges:

- `tests/test_whatsapp.py` diff is clean and the suite proves: household message creates a card/dialog, any outbound (WhatsApp/email/gcal) only after staged `✅`, wrong per-household relay-HMAC is rejected.
- Manual wedge battery on the `abrolia-synthetic` staging synthetic household is recorded with event IDs, `gcal` IDs, `effect_ids`, RFC `Message-ID`s, card screenshots, and per-wedge evidence — demonstrating bundle, supersession, staged PDF attachment, inject-vs-poll identity, WhatsApp HMAC, and the `HERMES_EMAIL_SEND=0` kill switch.
- Legacy IMAP poller is asserted unreachable from onboarding (test-seam only).
- All Phase 4 suites are green.

### Detailed Steps

#### Step D0 — Branch and clean dirty state

- Branch: `git checkout -b codex/phase-D-real-actions b9d3614` (or from merged C if C landed).
- Resolve `tests/test_whatsapp.py` dirty diff at `b9d3614`:

```bash
git diff tests/test_whatsapp.py
# Expect: staged-only reply enforcement changes that were not committed
# Action: commit the intended change or revert to intended enforcement — do not leave dirty
git status  # must be clean after
```

- Verify:

```bash
pytest tests/test_whatsapp.py -q
```

#### Step D1 — Implement / tighten staged-only guards

**Files:** `hermes_cloud/runner/pipeline.py`, `hermes_cloud/execute/{email_send,nerve_send,gmail_api_send,whatsapp}.py`, `hermes_cloud/ingest/whatsapp_webhook.py`, `hermes_cloud/ingest/nerve_webhook.py`, `hermes_cloud/core/card.py`, `tests/test_whatsapp.py`.

**Rules:**

- Any inbound on any channel (WhatsApp, Web, email reply) that arrives in an unbound or unknown household produces a card/dialog but no tool call and no outbound. Outbound is only via `effects` journal after `effect.approved_by == actor_id` and `approval.payload_sha` matches the staged payload.
- WhatsApp HMAC: shared gateway relay-HMAC is per-household; `whatsapp_webhook.py` / `gateway/whatsapp_router.py` must reject a message with a wrong HMAC with explicit `403 hmac_rejected` (not silent drop) and emit no card. Test: `test_whatsapp_hmac_reject`.

#### Step D2 — Manual wedge battery on `abrolia-synthetic` synthetic household

Run on the `abrolia-synthetic` staging synthetic household from Phase B. Each wedge must record: input identifier, card screenshot, approval `payload_sha`, `effect_id`, external provider ID (`gcal event id`, `gmail_message_id`, `nerve message id`, `whatsapp message id`), and the verification query that proves the side effect.

| # | Wedge | Input | Steps | Evidence |
|---|-------|-------|-------|----------|
| 1 | Bundle (event+task) | Email: `15€ excursion 12.09 — collect by 10.09` (synthetic) | Ingest → extraction → card shows bundle (calendar event 12.09 + task "collect 15€ by 10.09") → `✅` → verify shared gcal event created + task list entry | `gcal event id`, task `id`, card screenshot, `effect_id` with `payload_sha` |
| 2 | Supersession (same event ID) | Second email: letter `"moved to 19.09"` referencing same excursion | Ingest → extraction → reconcile-card showing supersession of event 12.09 → 19.09 → `✅` → verify same deterministic `gcal event id` updated (not duplicated) | `gcal event id` same before/after, `updated` timestamp, card screenshot |
| 3 | Staged PDF email | Synthetic email with staged PDF attachment (`Kому: schule@example.de` in card) | Card shows MIME preview; attachment not sent until `✅`; after `✅` verify delivered email has PDF MIME part | `Message-ID`, MIME structure dump, delivery receipt |
| 4 | Inject vs Gmail API identity | Dedicated Gmail test-user path: one event ingested via synthetic `inject` (offline EML), same event via Gmail History poll (from Phase C3) | Compare canonical `Message-ID` / `thread_id` — identical; only `effect_id` / `channel` / `ingestion_source` differ | Two `events` rows with same canonical key, different `ingestion_source` |
| 5 | Dedicated Gmail disconnect revokes grant | From Phase C3 wedge, now disconnect | `POST /api/email-identities/:id/disconnect` → `https://oauth2.googleapis.com/revoke` called; token removed from `SecretSink`; re-send fails until reconnect | `revoked_at` timestamp, `SecretSink` list proof, failed send error |
| 6 | WhatsApp staged-only | Household WhatsApp message via `whatsapp_webhook` fake (or real Evolution on `ams` if enabled for synthetic) | Inbound → card/dialog; any outbound `whatsapp.send` only after `✅`; wrong per-household relay-HMAC rejected | `whatsapp message id` after approval, `hmac_rejected` log for wrong HMAC, no premature send |
| 7 | Kill-switch `HERMES_EMAIL_SEND=0` | Set `HERMES_EMAIL_SEND=0` on runtime, attempt an email send effect | Pipeline blocks with honest message (`test_email_send.py::test_kill_switch` expectation); no provider call, effect stays `blocked` | Effect `status=blocked`, provider call count 0, UI message text |

**Kill-switch details:** `hermes_cloud/execute/email_send.py` (or `hermes_cloud/core/effects.py`) must check `HERMES_EMAIL_SEND` env at effect execution time; `0/false/no` → return `EffectResult(status='blocked', reason='email_send disabled by operator')` without calling Nerve/Gmail. `tests/test_email_send.py::test_kill_switch` proves it.

**Legacy IMAP seam:** keep `hermes_cloud/ingest/gmail_poll.py` as importable but assert it is not reachable from `control_plane/onboarding` or `control_plane/api`:

```python
def test_legacy_imap_not_reachable_from_onboarding():
    rg = subprocess.run(["rg", "-n", "gmail_poll|HERMES_GMAIL_APP_PASSWORD",
                         "control_plane/onboarding", "control_plane/api"], capture_output=True, text=True)
    assert rg.stdout == ""
```

#### Step D3 — Suite + screenshots + PR

**Screenshots:** capture each card/dialog in the staging UI (or `hermes_cloud/cli.py` card render) for the addendum. Each screenshot in `thoughts/shared/implementations/2026-08-05-abrolia-phase-2-email-identity-validation.md` (or a new `2026-08-06-phase-D-real-actions-validation.md`) must be captioned with the `effect_id` it corresponds to.

**PR:** `codex/phase-D-real-actions: bundle/supersession/kill-switch/WhatsApp HMAC + manual battery`. Must include the manual transcript table (IDs, message IDs, event IDs) and attach screenshots.

### Phase D Acceptance Criteria

- [x] `tests/test_whatsapp.py` clean and green; staged-only + HMAC-reject proven:

```bash
pytest tests/test_whatsapp.py -q
pytest tests/test_bundle.py tests/test_supersession.py tests/test_gcal.py tests/test_email_send.py tests/test_media_ingest.py tests/test_scheduler.py -q
```

- [x] PII-safe live wedge transcript recorded with event, gcal, effect, approval, and RFC
  Message-IDs. Headless CLI evidence is accepted for Phase D; Gmail OAuth and Web screenshots are
  deferred to Phase E.
- [x] `HERMES_EMAIL_SEND=0` kill-switch blocks with honest message:

```bash
pytest tests/test_email_send.py -k kill_switch -q
```

- [x] Legacy IMAP remains a test-only seam; Gmail production OAuth/History begins in Phase E2.
- [x] `pytest -p no:cacheprovider -m "not live" -q` and `ruff check .` are green in the isolated
  deployment worktree.

---

## Phase E — Pilotization (B-12)

### Desired End State

After `codex/phase-E-pilotization` merges, the system can onboard a pilot household through the full `email → WhatsApp → primary` machine with preferences/routing, verified bindings, a narrow shared gateway, a minimal Web chat, cost caps, and observability — all on `abrolia-synthetic` with synthetic data, rehearsed for pilot.

### Detailed Steps

#### Step E1 — Durable 3-step onboarding machine

**Files:** `control_plane/onboarding/state.py`, `control_plane/onboarding/provision.py`, `control_plane/onboarding/transitions.py`, `control_plane/migrations/0001_control_plane.sql` (if new columns needed), `docs/onboarding-runbook.md`, `control_plane/container.py`, `control_plane/db.py`, `control_plane/provisioning/fakes.py`, `control_plane/repositories/jobs.py`, `control_plane/privacy/consent.py`, `control_plane/provisioning/worker.py`, `tests/control_plane/test_provision_dry_run.py`, `tests/control_plane/test_plan_inventory.py`.

**Report narrowed 2026-08-20.** The rehearsal grew a second job beyond the
no-mutation guarantee: predicting which job the worker would take next, and
summarising the household's state in derived booleans. That prediction had to
agree with `JobsRepository.lease` in every edge case — a future `not_before`, a
held lease, paused workers, a provider the configuration no longer registers, a
kind dispatched without one — and eleven review rounds went into reconciling it
while the finding count rose rather than fell.

The command now reports durable FACTS and one label for what it rehearsed. The
fields `lease` reads are all present, so an operator can see the answer; the
report no longer asserts it. `−207` lines of implementation and `−379` of tests
went with the claims. The no-mutation half — the read-only snapshot, the
refusals, the `committed` proof — is untouched, and it is what Step E1 asked
for.

**Scope corrected 2026-08-20**, on the same reading that corrected Step E9: an
inventory listing only the modules a step was expected to touch cannot detect
the ones it turned out to need, and a changed file outside the plan is a
blocker under the repository rules. Four were missing rather than unnecessary:

- `control_plane/container.py` and `control_plane/db.py` — `apply_migrations`
  and `preserve_journal_mode`, the two flags that let the rehearsal open a
  database without migrating it or rewriting its journal mode. The dry-run's
  no-mutation guarantee is enforced there, not in `provision.py`.
- `control_plane/provisioning/fakes.py` — the `plan` method by which a provider
  states what it will create, so the report describes the CONFIGURED provider
  rather than assuming Fly.
- `tests/control_plane/test_provision_dry_run.py` — the step's own suite.
- `control_plane/repositories/jobs.py` — **added 2026-08-20.** `LEASABLE_SQL`,
  the condition `JobsRepository.lease` selects on. The rehearsal reports whether
  the worker can pick a job up next, and it did that by restating those rules in
  Python — where they disagreed three ways. A report about what the worker will
  do has to ask the worker's own question, so the predicate is shared from where
  the worker asks it.
- `control_plane/privacy/consent.py` and `control_plane/provisioning/worker.py`
  — **added 2026-08-20**, for the same reason and by the same remedy.
  `_run_once` checks the special-category content restriction BEFORE dispatching
  any provider: stale or revoked, it fails the queued runtime job, revokes the
  revision and the bootstrap tokens, and returns the workflow to email.
  Reporting the ordinary success path over that describes an operation whose
  first act is to undo itself, so the rehearsal asks the worker's question —
  `CURRENT_RESTRICTION_RECEIPT_SQL`, which now lives beside the consent rules it
  is about and is read by `_holds_current_restriction` in `provision.py` and by
  `ProvisioningWorker._has_current_email_content_restriction` alike.

Three corrections in three rounds is a mechanism failing, not three oversights.
`tests/control_plane/test_plan_inventory.py` now compares this branch's changed
implementation paths against the inventories above and fails on an undeclared
one, so the next omission is caught before a review generation is spent on it.

**Durable 3-step machine:** `email → WhatsApp → primary`.

- Each step has `step_id ∈ {email, whatsapp, primary}`, `status ∈ {pending, provisioning, waiting_user, verified, failed, skipped}`, and `version` for idempotency.
- `provision.py --dry-run` reports what the next real onboarding will do, mutating nothing and calling no provider — used by the operator before every real pilot onboarding. It reports the household `config_revision` diff (key paths, never values), the resource names **of the pending job's own provider** (Fly names only where that job is a Fly one), the secret names, and the tables the pending operation writes **where durable state determines them**; where it does not, it says so and points at the command that would settle it.

  **Amended 2026-08-19.** The criterion previously read "lists the exact writes it would make". Six review rounds established that exactness is not achievable for every state without executing the operation, which is the one thing the command must never do:

  - A pending runtime job in `outcome_unknown` is reconciled next, and `ProvisioningWorker._reconcile` branches on `provider.inspect()` — settle, fail, or re-run preparation. The branch depends on an answer only the provider holds.
  - `_finish_runtime` itself branches on workflow state, and the two branches write very different sets (nine tables from `runtime_provisioning`, two from `activating`), so a single advertised set was wrong for one of them.

  Naming one branch as "exact" is false precision, and an operator acting on it is worse off than one told to go and look. So the promise is now: exact where it is knowable, explicit uncertainty where it is not, and never a guess presented as a fact.

**The guarantee, stated exactly.** The database file and its `-wal` are left
byte-identical. The `-shm` shared-memory index is not covered: SQLite must map
it to read a WAL database at all, so a read-only open creates one where a
crashed writer left none, and refreshes one that exists. That file holds no
durable data and is rebuilt from the WAL, but it IS a change to the directory,
and a fingerprint over `/data` will see it.

Two consequences an operator should know. A rehearsal needs permission to create
`-shm` beside the database, and will fail without it rather than silently
reading stale pages. And "mutates nothing" is a claim about the data, not about
every inode — the earlier unconditional wording promised more than any
implementation that opens the file through SQLite can deliver, and promising it
is how the guarantee stops being checkable.

  The no-mutation half is unchanged and was **strengthened** in the same rounds, because it turned out to be doing less than it claimed. The command must not migrate the database (`ControlPlaneContainer.build` did, before its own transaction opened), must not create one that is absent (`sqlite3.connect` did), must not rewrite the journal mode (`PRAGMA journal_mode=WAL` is persistent), must roll its rehearsal back, and must attribute only rows it minted itself — a count taken across the rollback blamed the API worker's concurrent commits on the dry-run.
- Fix-before-effect: advancing a step is a durable `onboarding_transitions` append before any provider effect; stale `version` → `409 conflict`.
- Downstream invalidation: if `email` step is reset (delete/reconnect), `whatsapp` and `primary` steps that depended on that `config_revision` are invalidated (`needs_revalidation`) and must be re-confirmed.
- Idempotency: re-POSTing the same `Idempotency-Key` for the same `step_id+version` returns the same response snapshot without re-calling the provider.

#### Step E2 — Google OAuth prod path (reuse C3)

**Files:** `control_plane/providers/email/google_oauth.py` (reuse C3 PKCE/state/prompt/scopes/revoke), `control_plane/onboarding/state.py`.

Gate behind Phase A (counsel) — no real Gmail until DPA/SCC + Art 9(2) + verification/CASA are done. Legacy env path (`HERMES_GMAIL_ADDRESS`/`APP_PASSWORD`) remains deprecated and unreachable (same `rg` check as D).

#### Step E3 — Preferences / routing (`channel_preferences`)

**Files:** `hermes_cloud/core/channel_preferences.py`, `hermes_cloud/core/migrations/0008_channel_preferences.sql`, `hermes_cloud/runner/pipeline.py`, `control_plane/api/*`, `hermes_cloud/channels/*`.

**Table:** `channel_preferences` (household-row now, per-member schema ready):

```sql
-- 0008_channel_preferences.sql (next free number; verify against hermes_cloud/core/migrations/)
CREATE TABLE channel_preferences (
    household_id TEXT PRIMARY KEY REFERENCES households (id) ON DELETE CASCADE,
    -- household-row for pilot; per-member extension adds (household_id, member_id) PK later
    primary_channel TEXT NOT NULL CHECK (primary_channel IN ('telegram','whatsapp_shared','whatsapp_dedicated','web')),
    fallback_channel TEXT NOT NULL CHECK (fallback_channel IN ('email_fallback','none')),
    subject TEXT NOT NULL DEFAULT 'household', -- placeholder for per-member: 'member:<member_id>'
    updated_at REAL NOT NULL,
    updated_by_actor_id TEXT,
    -- fallback email is the verified owner recovery email, never the agent inbox
    CHECK (primary_channel != fallback_channel)
);
-- Per-member ready: add column member_id TEXT NULL and partial unique index when extended
```

**Routing rules:**

- `primary_channel` is household-row for pilot; schema supports per-member `subject` extension without migration rewrite (nullable `member_id` / `subject` column).
- `fallback_channel` is the verified owner contact email (recovery email), **never** the agent inbox (`@abrolia.com` / BYO / agent Gmail) — enforced by check `fallback_address != agent_inbox`.
- Source-channel replies: if inbound arrives on WhatsApp, the proactive reply for that card's turn goes to the same channel if reachable.
- Permanent failure on primary (e.g., `410 Gone`, revoked binding) → short email fallback to the verified owner recovery email (no sensitive message content in fallback; just "new card waiting" with link).
- `outcome_unknown` on primary (e.g., `HERMES_EMAIL_SEND=0` or provider timeout) → **no duplicate** fallback — stay `unknown` and surface to UI; operator retries explicitly.

#### Step E4 — Channel binding (once-owner-authorized)

**Files:** `hermes_cloud/onboarding/channel_bindings.py` or `control_plane/api/channel_bindings.py`, `hermes_cloud/core/bindings.py`, `hermes_cloud/core/runcontext.py`, `control_plane/migrations/*` if binding table needed.

**Binding table (new or existing S1 mirror):**

```sql
CREATE TABLE channel_bindings (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL,
    channel TEXT NOT NULL CHECK (channel IN ('telegram','whatsapp_shared','whatsapp_dedicated','web')),
    external_id TEXT NOT NULL, -- Telegram chat_id, WA sender, Web push subscription endpoint hash
    actor_id TEXT NOT NULL,
    role TEXT NOT NULL,
    verified_at REAL NOT NULL,
    verified_by_actor_id TEXT NOT NULL, -- owner-authorized
    UNIQUE (household_id, channel, external_id)
);
```

**Rules:**

- Once-owner-authorized challenge: binding a new `actor_id` to a channel requires an active owner session to approve the challenge; the challenge is one-time, short-lived, and bound to `(owner_session, household, channel)`.
- `RunContext {channel}` is built from the verified binding — client payload (`chat_id`, `sender`, `household_id`, `role`) never chooses household/role; unknown `external_id` → zero capabilities.
- Second adult: separate binding row with distinct `actor_id` / `external_id`; both adults independently verified; no shared binding.
- Control-plane `household_profiles` / `channel_preferences` are the authoritative source; runtime S1 binding mirror is enforcement-only.

#### Step E5 — Shared gateway (narrow multi-tenant, no model/tools/secrets)

**Files:** `gateway/whatsapp_router.py`, `channels/router.py`, `control_plane/provisioning/secrets.py` (relay-HMAC), `hermes_cloud/core/config.py`.

**Design:**

- Shared gateway is a narrow multi-tenant relay on Fly `ams` (future `S15`), not a model host: it has no `HERMES_EXTRACTION_MODEL`, no tool definitions, no household Fly secrets.
- Exact sender→household mapping: gateway looks up `channel_bindings` by `(sender, household)` via keyed HMAC; `unknown` or `ambiguous` (sender maps to 0 or >1 household) → `deny` with `unknown_sender` / `ambiguous_sender` (not 404 leak).
- Durable webhook before ACK: gateway persists the incoming webhook payload to `S15` durable ingress (SQLite WAL) before returning `200 OK` to Telegram/WhatsApp; only after confirmed delivery to the household runtime's `ingest` queue does it delete the ingress row — crash before ACK → provider retries; crash after ACK but before relay → reconcile re-delivers exactly once.
- Per-household relay-HMAC: gateway → runtime request is signed with `HMAC-SHA256(household_key, body || timestamp)`; runtime `whatsapp_webhook.py` verifies with the household's `relay_hmac_key` from `SecretSink`; wrong HMAC → `403`.
- No gateway fan-out to multiple households for one sender.

#### Step E6 — Minimal Web

**Files:** `channels/web.py`, `web/*`, `control_plane/web/*`, `control_plane/api/web_channel.py`.

**Scope:**

- Authenticated chat over `runner/model.py` (same pipeline as other channels); PWA `manifest.json` (installable, offline shell not required).
- Opt-in push: Web Push subscription stored as `channel_binding` (`channel='web'`, `external_id = endpoint_hash`); push provider is `P11` — remains `TBD/disabled` for real families until `processors.md` P11 is `✅` (so Web chat works without push in Phase E; push is fake-only).
- No vault/offline archive in Phase 5.

#### Step E7 — Observability

**Files:** `control_plane/observability.py`, `hermes_cloud/core/observability.py`, `deploy/control-plane/Dockerfile` (log config), `docs/onboarding-runbook.md` (alert runbook).

**Structured logs:**

- Every log line is JSON with `timestamp, level, household_id_hash (HMAC), request_id, route, status, latency_ms` — **no message content, no raw email, no prompt, no secret**.

**Health:**

- `GET /health` (public) and `GET /healthz` / `GET /readyz` (control plane) report: `nerve_key_ok`, `telegram_ok`, `wa_instance/gateway_ok`, `google_grant_ok` (if Gmail option), `db_ok`, `backup_age_hours`. `backup_age_hours > 30*24` → `needs_attention`.

**Alerts (operator-visible, not auto-page in pilot):**

- `DLQ > 0` (provisioning jobs `failed` without reconcile),
- `sticky executing` (job `running` > 10 min without lease renewal),
- `primary unavailable` (routing fallback triggered),
- `backup stale` (`backup_age_hours > 26h`),
- `budget exceeded` (per-household/day cost cap hit).

#### Step E8 — Cost caps

**Files:** `hermes_cloud/runner/extraction.py`, `hermes_cloud/core/usage.py` (new), `hermes_cloud/core/migrations/*`.

**Mechanism:**

- Per-household/day token counter: `usage {household_id, day, prompt_tokens, completion_tokens, usd_estimate}` incremented after each `extraction` call from the model usage reply (Anthropic `usage` field).
- Soft limit (configurable, e.g., `$5/household/day` for pilot): when exceeded, degrade to `extraction-only` + reply `"дневной бюджет исчерпан — показываю только извлечённые карточки без AI-доработки"` — still durable, still staged approval, but no model re-extraction until next day.
- No hard drop of in-flight card; cap check happens before the next model invocation.

#### Step E9 — Release tag, migrate-on-start, restore drill

**Files:** `deploy/control-plane/Dockerfile`, `control_plane/db.py`, `control_plane/migrations/*`, `docs/control-plane-restore.md`.

**Tag:**

- `git tag pilot-YYYY-MM-DD` on the Phase E merge commit; tag message lists included phases and base `b9d3614`.

**Migrate-on-start with backup-before-migrate:**

- Container entrypoint runs `python -m control_plane.db migrate --backup-first` — creates a timestamped `/data/control-plane.db.pre-migrate-<rev>.bak` before applying any new `control_plane/migrations/*.sql`; migration failure → container exits non-zero, no partial schema.

**Restore drill (reuse Phase B procedure):**

- Perform the isolated backup/restore smoke again on the Phase E schema (now including `channel_preferences` / `channel_bindings` / usage tables); record `integrity_check`, `foreign_key_check`, pause-marker mode, smoke without leasing, resume, new onboarding through rev 1, destroy temp app.

### Phase E Acceptance Criteria

- [x] Durable 3-step machine with `provision.py --dry-run` listing the pending operation's writes **where durable state determines them** and saying so where it does not, without mutation; fix-before-effect; idempotency; downstream invalidation. ("Exact writes" was the original wording and it was false — see the amendment below.) The machine, its version checks, idempotent replay and `reset_from` downstream invalidation were already durable in `control_plane/onboarding/{state,service}.py`; `control_plane/onboarding/provision.py` adds the rehearsal and `tests/control_plane/test_provision_dry_run.py` proves it commits nothing (4 tests).

```bash
python -m control_plane.onboarding.provision --dry-run --household <test-uuid> 2>&1 | head -n 80
# expect: no DB write, and a report of durable state.
# `rehearsal` says what THIS RUN rehearsed, and `table_writes` belongs to that —
# the planning pass before a revision is issued, the SUCCESS PATH of the pending
# runtime operation after, nothing when the next step is a provider-dependent
# reconcile or a precondition is unmet. `pending_step_jobs` and
# `unresolved_runtime_jobs` list every unsettled job with its status, error
# code, not_before, lease_until, provider and — where the work is internal and
# deterministic — its own table_writes. `workers_paused` says whether the queue
# is moving. Resources and secrets come from the pending job's own provider.
```

**Criterion amended 2026-08-20.** It originally read "listing exact writes" and
"Fly resource names" unconditionally, and the runbook promised the same. Neither
was true: a revision that is already issued means no planning pass runs, a
provider-dependent reconcile has no knowable write set, the default
`dry-run-runtime` provider creates no Fly resources, and a terminal or complete
workflow has no pending operation to describe. An operator following the
documented verification could reject correct output, or rely on precision the
implementation deliberately does not claim. `docs/onboarding-runbook.md` now
carries the state-by-state contract.

- [ ] `channel_preferences` table exists; household-row primary/fallback; fallback is verified owner email not agent inbox; source-channel reply + permanent-failure fallback + `outcome_unknown` no-duplicate:

```bash
pytest tests/test_channel_preferences.py -q
```

- [ ] Channel binding once-owner-authorized; `RunContext` never trusts client payload; second adult separate binding:

```bash
pytest tests/test_channel_bindings.py -q
```

- [ ] Shared gateway narrow multi-tenant: exact mapping, unknown/ambiguous deny, durable before ACK, per-household HMAC:

```bash
pytest tests/test_gateway_routing.py tests/test_whatsapp.py -q
```

- [ ] Minimal Web authenticated chat + PWA manifest; push stays fake-only / `P11` `⏳+disabled`:

```bash
pytest tests/test_web_channel.py -q
```

- [ ] Observability: structured logs without content, `/health` fields, alerts defined:

```bash
pytest tests/test_observability.py -q
rg -n "content|prompt|secret|token" control_plane/observability.py  # expect: no content logging
```

- [ ] Cost caps: per-household/day counter + soft-limit degradation message:

```bash
pytest tests/test_cost_caps.py -q
```

- [ ] Full pilot onboarding on `abrolia-synthetic` synthetic household `≤ 60 min` per `docs/onboarding-runbook.md` (step1 `email` + step2 `whatsapp` + step3 `primary` checks + primary switch without history loss), recorded with `household_id`, `config_revision`/`hash`, binding IDs, `channel_preferences` row.

```bash
pytest tests/test_onboarding.py tests/test_google_oauth.py -q
pytest -p no:cacheprovider -m "not live" -q
```

- [ ] Release tag + backup-before-migrate + restore drill evidenced.

---

## Phase F — Release Gating & Rollout

### Desired End State

After `codex/phase-F-release-gating` merges, every provider is independently kill-switched, default-off, and fail-closed, and the rollout order is documented and enforced by gates.

### Detailed Steps

#### Step F1 — Per-provider flags

**Files:** `control_plane/config.py`, `control_plane/providers/*`, `hermes_cloud/core/config.py` (`HERMES_EMAIL_SEND`, `HERMES_VERTEX_EU_ENABLED`), new `feature_flag` table or env-driven flags (choose one — env flags are sufficient for pilot, documented below).

**Flags (all `default off`, fail-closed):**

| Flag | Controls | Default |
|------|----------|---------|
| `ABROLIA_MANAGED_EMAIL_ENABLED` | Nerve `@abrolia.com` provisioning | `0` |
| `ABROLIA_BYO_EMAIL_ENABLED` | Nerve BYO domain provisioning | `0` |
| `ABROLIA_GMAIL_ENABLED` | Gmail agent via OAuth (implies `ABROLIA_REAL_EMAIL_ENABLED` + CASA) | `0` |
| `ABROLIA_WHATSAPP_SHARED_ENABLED` | Shared WA gateway relay | `0` |
| `ABROLIA_WHATSAPP_DEDICATED_ENABLED` | Dedicated Evolution WA | `0` |
| `ABROLIA_WEB_PUSH_ENABLED` | Web Push (P11) | `0` |

Alternative short form if the codebase consolidates: `ABROLIA_REAL_EMAIL_ENABLED` covers managed+BYO (with per-option sub-check), plus `ABROLIA_GMAIL_ENABLED` separately, plus `ABROLIA_WHATSAPP_{SHARED,DEDICATED}_ENABLED`, `ABROLIA_WEB_PUSH_ENABLED`. Either form is acceptable if the matrix below is covered and default-off.

**Enforcement:** every provider code path reads its flag at call time (not only at startup) and returns `blocked(flag_disabled)` without calling the provider when off. Flag toggle is tested: flipping a flag from `1→0` mid-run blocks the next call; `0→1` allows it after a health-check pass.

#### Step F2 — Flag matrix doctest

**Files:** `docs/onboarding-runbook.md`, `docs/privacy/processors.md` §4 (admission criteria cross-ref), `tests/test_config.py` (add flag matrix test).

```python
def test_flag_matrix_blocks_without_flag():
    # For each flag off, assert the corresponding provisioner/adapter returns blocked
    # For each flag on + missing DPA/SCC/counsel gate, assert still blocked (Phase A dependency)
```

#### Step F3 — Rollout order (synthetic → operator accounts → invited pilot families per provider)

**Files:** `docs/onboarding-runbook.md`, `thoughts/shared/plans/*` (rollout section).

Order, each transition gated:

1. **Synthetic** (`abrolia-synthetic`, `owner@abrolia.test`, fake adapters) — requires `go test ./...` + `pytest -m "not live"` + no `TODO` in notices (Phase A docs).
2. **Operator accounts** (owner's real mailbox on allowlist, `ABROLIA_REAL_EMAIL_HOUSEHOLD_ALLOWLIST` populated, `ABROLIA_REAL_EMAIL_ENABLED=1` in a separate PR that references Phase A+C evidence) — requires Phase A + C1 receipt + one manual live gate per provider (BYO DNS or Gmail History).
3. **Invited pilot families** per provider (managed `@abrolia.com` first, then BYO, then Gmail — each provider's `ABROLIA_*_ENABLED` flipped independently after operator-account soak and `go test` + `pytest -m "not live"` green).

Each transition requires: Phase A legal merged + Phase C1 receipt green + `go test ./... -count=1` (nerve-cloud) + `pytest -p no:cacheprovider -m "not live" -q` (abrolia) + one manual live gate on `abrolia-synthetic`.

#### Step F4 — Global pre-release gates

Validate before any flag flip PR:

```bash
git diff --check
ruff check .
gitleaks detect --no-git --source . 2>&1 | head
python -m check_fixtures --all --require-deny  # private deny-list in CI
```

### Phase F Acceptance Criteria

- [ ] Six per-provider flags exist, `default off`, fail-closed, independently togglable; flag toggle tested mid-run.

```bash
pytest tests/test_config.py -k flag -q
pytest tests/control_plane/test_config.py -q
```

- [ ] Rollout order documented in `docs/onboarding-runbook.md` with per-transition prerequisites (Phase A + C1 + `go test` + `pytest -m "not live"` + one manual live gate).

- [ ] Flag matrix doctested; `git diff --check` + `ruff` + `gitleaks --all` + `check_fixtures --all --require-deny` green on the F branch.

```bash
pytest -p no:cacheprovider -m "not live" -q
ruff check .
gitleaks detect --no-git --source . 2>&1 | head -n 20
```

- [ ] `eu-strict` fail-closed still green (Phase A test):

```bash
pytest -k eu_strict -q
```

## Cross-Phase Verification Commands (for E/F PRs)

```bash
# Full non-live suite (record counts in PR body — expect 626+ in control_plane + 215+ hermetic)
pytest -p no:cacheprovider -m "not live" -q

# Phase D suites
pytest tests/test_bundle.py tests/test_supersession.py tests/test_gcal.py tests/test_email_send.py tests/test_whatsapp.py tests/test_media_ingest.py tests/test_scheduler.py -q

# Phase E pilotization
pytest tests/test_onboarding.py tests/test_google_oauth.py tests/test_channel_preferences.py tests/test_channel_bindings.py tests/test_web_channel.py -q

# Provision dry-run smoke (operator)
python -m control_plane.onboarding.provision --dry-run --household 00000000-0000-4000-a000-000000000001 2>&1 | head -n 80

# Phase F gates
pytest tests/test_config.py -k flag -q
pytest -k eu_strict -q
ruff check .
gitleaks detect --no-git --source . 2>&1 | head -n 20
```

## Synthetic-Only Policy

All four phases (D/E/F) run synthetic-only until Phase A's Gate -1 and Phase C's live gates are evidenced. No `ABROLIA_REAL_*=1`, no real family inbox, no real DNS mutation, no real provider enablement outside the explicit allowlisted staging household. The pilot `abrolia-synthetic` org and its dedicated Gmail/test domain are the only live targets, and even there every real-provider call requires an allowlisted household and a versioned notice receipt.

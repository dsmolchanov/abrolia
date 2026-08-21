---
title: "Phase C — Email Identity Closure (B-01, B-02, B-05, B-06)"
status: planning
created_at: "2026-08-06"
base_commit: b9d3614
parent: thoughts/shared/plans/2026-08-06-canon-execution-plan.md
depends_on:
  - thoughts/shared/plans/2026-08-06-phase-B-foundation-migration.md
  - thoughts/shared/plans/2026-08-04-abrolia-phase-2-email-identity.md
scope: phase-C
data_policy: synthetic-only-until-explicit-gates
blockers: [B-01, B-02, B-05, B-06]
gate: "ABROLIA_REAL_EMAIL_ENABLED stays off until C1–C3 all green + Phase A done"
---

# Phase C — Email Identity Closure (B-01, B-02, B-05, B-06)

## Overview

Phase C closes the four remaining email-identity blockers from CU2/CU1 validation. It is split into three independently landable slices (C1–C3) that share no schema conflict but share a single release gate. B-01 is upstream; B-02 is local durable-receipt; B-05/B-06 are live-battery slices.

| Slice | Blocker | Severity | Summary |
|-------|---------|----------|---------|
| C1 | B-01 | P0 upstream | Nerve `handler_keys.go` cross-org service-token delegation: `org_id` not resolved against authenticated tenant, `nerve:admin.billing` delegable by tenant |
| C1 | B-02 | P0 local | `secret_handoff_unknown` convergence missing durable non-secret receipt; crash after `SecretSink` write before durable projection stays quarantined indefinitely |
| C2 | B-05 | P0 | Real DNS/BYO live gates not run: reload/login resume, verified DNS advance, domain race, delete/reconnect with outage/lost-response, Nerve bootstrap hard-delete (PR #64) |
| C3 | B-06 | P0 | Gmail OAuth / Gmail History / UI activation not live-validated: verification/CASA, auto revocation, bounded resync, `gmail_real_enabled` gate |

**B-01 note:** the task prompt states `B-01 already fixed via nerve-cloud 9909268`. C1 still verifies that fix with a negative HTTP test and an Abrolia adapter assertion — a fixed upstream without a local regression test is not closure.

**B-03/B-04:** already fixed locally (provider secret shapes, TTL ownership fail-closed) — kept as guards; no work in C, but suites must stay green.

## Current State

### Control-plane email model at `b9d3614`

- `control_plane/migrations/0002_email_identity.sql`: `email_identities` (household FK, `option ∈ {managed_abrolia,gmail,own_domain}`, `status ∈ {selected,provisioning,waiting_user,verified,activating,active,needs_attention,disconnecting,deleted,outcome_unknown}`, `address_ciphertext/lookup_hmac/masked`, `provider_subject_ciphertext`, `provider_resource_refs_json`, `secret_binding_ref`, `granted_scopes_json`, `encryption_key_version`, `verified_at/activated_at`, `version`), `email_address_reservations` (held/consumed/released/expired, unique on `(normalized_domain, normalized_local_part)`), `oauth_transactions` (state hash, `pkce_verifier_ciphertext`, `requested_scopes_json`, `workflow_version`, `expires_at`), `email_activation_receipts` (PK `(email_identity_id, desired_revision)`, `runtime_ref/provider/inbound_check/outbound_check/checked_at/receipt_digest/status`).

- `control_plane/migrations/0003_email_domain_claims.sql`: adds `domain_lookup_hmac` + unique index `email_identities_live_owned_domain` on `domain_lookup_hmac` where `option='own_domain' and status!='deleted'`, plus trigger requiring HMAC.

- `control_plane/migrations/0004_google_oauth.sql`: extends `oauth_transactions` with `email_identity_id/provider_subject_ciphertext/address_ciphertext/granted_scopes_json/secret_binding_ref/credential_digest/callback_at/confirmed_at/revoked_at` and unique index `oauth_transactions_one_live_identity` on `email_identity_id` where `failed_at IS NULL AND revoked_at IS NULL`.

- `control_plane/email/models.py` + `repository.py` + `domain_policy.py` + `local_part.py` + `service.py` implement the typed state machine, HMAC-lookup reservation, and provider projection. `control_plane/providers/email/*` hosts per-option provisioners. `control_plane/provisioning/worker.py` drives the job machine with `SecretSink` (`control_plane/provisioning/secrets.py` + `fakes.py` for hermetic). `control_plane/provisioning/manifest.py` / `manifest_toml.py` build the immutable `config_revision`.

- `control_plane/config.py:118-164` gates `real_email_enabled` (requires `nerve_base_url + nerve_admin_key + nerve_platform_org_id + nerve_platform_domain_id + allowlist`) and `gmail_real_enabled` (requires `real_email_enabled + google_oauth_client_id/secret + app_verified + gmail_scope_approved + casa_current + limited_use_disclosed`).

- `hermes_cloud/core/migrations/0006_email_identity.sql` and `0007_nerve_runtime.sql` wire the runtime side: `HERMES_EMAIL_PROVIDER/ADDRESS/IDENTITY_ID/BINDING_REVISION/SECRET_NAMES`, Nerve inbox/threads/attachments, `residency_mode` / `HERMES_VERTEX_EU_ENABLED`.

- `hermes_cloud/ingest/gmail_api.py`, `hermes_cloud/execute/gmail_api_send.py`, `hermes_cloud/ingest/nerve_webhook.py`, `hermes_cloud/email/`, `hermes_cloud/execute/email_send.py`, `hermes_cloud/runner/pipeline.py` implement ingest/poll/send.

- `tests/control_plane/email/*`, `tests/control_plane/test_provisioning_jobs.py`, `tests/test_gmail_api_*.py`, `tests/test_email_send.py` cover hermetic paths. Live suite `tests/live/test_nerve_phase3_contract.py` (pinned `nerve-email==0.2.0`, `c6f5ed03`) covers Nerve contract but not the new B-01/B-02 paths.

### What is open

1. **B-01 upstream:** `nerve-cloud/internal/cloudapi/handler_keys.go` does not resolve requested `org_id` against authenticated tenant for service-token creation; a tenant with `nerve:admin.billing` can mint a service token for another org. Fix commit `9909268` exists but needs a negative HTTP test `TestServiceToken_CrossOrgRejected` and an Abrolia adapter assertion that the tenant never calls `/v1/service-tokens`.
2. **B-02 durable receipt:** after `SecretSink.write` commits to Fly secrets, the worker must durably project a non-secret receipt before marking `verified`. Today a crash between Sink write and DB commit leaves `secret_handoff_unknown` without convergence — the next reconcile has no evidence to advance, and the job stays quarantined. No `email_secret_installs` table exists yet.
3. **B-05 BYO live battery:** reload/login resume, wrong DNS stays `waiting_user`, verified DNS advances once, two-connection writer race → one owner (HMAC uniqueness), delete/reconnect matrix (DNS present vs provider unavailable vs lost-response webhook/key/inbox/domain/org, `outcome_unknown` explicit, Nerve bootstrap PR #64 hard-delete path) — not evidenced.
4. **B-06 Gmail live battery:** History cursor (`historyId` on connect, `history.list` polling, INBOX filter, cursor advance only after durable append, 404 expired history → bounded resync with overlap+dedup, `needs_attention` on gap), deterministic RFC Message-ID + `rfc822msgid:` send reconciliation, PKCE + `state` binding + `prompt=select_account` + exact scopes + address confirmation + refresh-token-direct-to-`SecretSink` + disconnect revoke — not evidenced.

## Desired End State

After Phase C (C1+C2+C3) merges on `codex/phase-C-email` (or three PRs `codex/phase-C1`, `C2`, `C3`):

1. **Upstream B-01 proven closed.** Nerve PR with cross-org rejection test green (`go test ./... -count=1`), and Abrolia `tests/control_plane/email` contains an adapter assertion that `NerveManagedEmailProvisioner` / `NerveBYODomainProvisioner` never invoke `/v1/service-tokens` (they use bootstrap admin + domain-grant + runtime key).

2. **Local B-02 converging.** New table `email_secret_installs` exists; worker path after `SecretSink.write` atomically writes a generation-scoped non-secret install receipt + transitions to `secret_handoff_pending` before marking `verified`; reconcile path inspects staged secret names (generation N) and converges without operator; hard-reclaim without sink stays `secret_handoff_unknown`; no secret value in DB/log/telemetry.

3. **BYO domain (B-05) live-proven.** On the `abrolia-synthetic` staging org, the full live battery passes with operator-observed DNS IDs + `nerve domain` HMACs in the Phase 2 validation addendum. Bounded backoff `30/60/120/300/600` + manual `CHECK` preserved; no blind retry on `outcome_unknown`.

4. **Gmail (B-06) live-proven.** On an allowlisted dedicated Gmail test account, connect → receive → approve/send → revoke/delete each manual, with History cursor, Message-ID reconciliation, and OAuth PKCE/state/scope/address checks green; `rg` proves no `HERMES_GMAIL_ADDRESS`/`APP_PASSWORD` in prod path; `gmail_real_enabled` gate still fail-closed without verification/CASA.

5. **Release gate respected.** `ABROLIA_REAL_EMAIL_ENABLED` and `gmail_real_enabled` remain `0` in repo fixtures until the combined C1–C3 + Phase A evidence is reviewed. The Phase 2 validation addendum `thoughts/shared/implementations/2026-08-05-abrolia-phase-2-email-identity-validation.md` records the live evidence.

## Detailed Steps

### Slice C1 — Upstream & Durable Receipt (B-01, B-02)

#### C1.1 — Verify upstream B-01 fix (nerve-cloud 9909268)

**Upstream files:** `nerve-cloud/internal/cloudapi/handler_keys.go`, `internal/store/*`, `internal/cloudapi/handler_keys_test.go` (or equivalent `*_test.go`).

**Steps:**

1. Pull `nerve-cloud` at or after `9909268` (on `main` ≥ `c6f5ed03`). Confirm commit `9909268` diff: `org_id` from request body is resolved against `authenticated_tenant.org_id` (or session org), not trusted blindly; `nerve:admin.billing` delegation is scoped to the authenticated org.
2. Add/verify negative HTTP test `TestServiceToken_CrossOrgRejected`:

   ```go
   // handler_keys_test.go
   func TestServiceToken_CrossOrgRejected(t *testing.T) {
       // Org A billing token (nerve:admin.billing) requests service token for Org B's org_id
       // Expect 403 Forbidden, no token minted, audit log records attempt
   }
   ```

   The test must create two orgs (A, B), mint a billing-scoped token for A, and attempt `POST /v1/service-tokens {org_id: B}` — assert `403` and zero `service_tokens` row for B.

3. Run upstream:

   ```bash
   go test ./... -count=1
   ```

   All packages green; no `go vet` regression.

**Abrolia adapter assertion:**

**Files:** `control_plane/providers/email/*`, `control_plane/provisioning/fly.py`, `tests/control_plane/email/test_nerve_managed.py` (or `test_nerve_byo.py`).

Add a contract test that the Abrolia provisioner never calls `POST /v1/service-tokens`:

```python
def test_managed_provisioner_never_calls_service_tokens(http_capture):
    # Drive NerveManagedEmailProvisioner through reservation→org→domain-grant→inbox→key→webhook→Sink
    # Assert http_capture.requests has no POST /v1/service-tokens
    # Assert it uses POST /v1/orgs, POST /v1/domain-grants, POST /v1/inboxes, POST /v1/keys, POST /v1/webhooks
    # Assert bootstrap admin key is the only admin credential
```

This is the local evidence that even a tenant escape in Nerve would not be triggerable by Abrolia.

#### C1.2 — Add `email_secret_installs` + worker convergence (B-02)

**New table:** `email_secret_installs` — generation-scoped non-secret install receipt derived from `SecretSink` inspection, not secret value.

**Schema:** `control_plane/migrations/0005_email_secret_installs.sql` (next free number; check `control_plane/migrations/` — after `0004_google_oauth.sql`):

```sql
-- 0005_email_secret_installs.sql
CREATE TABLE email_secret_installs (
    job_id TEXT NOT NULL REFERENCES provisioning_jobs (id) ON DELETE CASCADE,
    generation INTEGER NOT NULL CHECK (generation > 0),
    secret_name TEXT NOT NULL,
    installed_at REAL NOT NULL,
    sink_digest TEXT NOT NULL CHECK (length(sink_digest) = 64),
    -- sink_digest = hex(sha256(secret_name || generation || job_id)) or HMAC of staged secret name
    -- Never stores secret value, only a non-secret digest of the installed envelope
    PRIMARY KEY (job_id, generation, secret_name)
);
CREATE INDEX email_secret_installs_job_generation
    ON email_secret_installs (job_id, generation);
```

**Alternative if `generation` already exists on `provisioning_jobs`:** reuse that column; the receipt `generation` must match the job's provisioning generation (the `config_revision` or `email_binding_revision` that triggered the job). Document the mapping in the migration header comment.

**Worker changes:**

**Files:** `control_plane/provisioning/worker.py`, `control_plane/provisioning/secrets.py`, `control_plane/provisioning/contracts.py`, `control_plane/email/repository.py`, `control_plane/provisioning/fakes.py` (test Sink).

**Current job machine (simplified):** `transition → lease → provider (Nerve/Google) → response → SecretSink.write → result → claim → activate → cleanup`. The bug is between `SecretSink.write` and `result` durable commit.

**Required change in `worker.py`:**

1. After `SecretSink.write(secret_name, envelope)` succeeds (Fly secrets API returned success), **before** marking the email identity `verified` or the job `done`, atomically in one SQLite transaction:
   - `INSERT INTO email_secret_installs (job_id, generation, secret_name, installed_at, sink_digest) VALUES (... ) ON CONFLICT DO NOTHING`
   - where `sink_digest = hex(sha256(secret_name + generation + job_id))` computed from the secret name + generation, and `installed_at = now`.
   - Transition `provisioning_jobs.status` to `secret_handoff_pending` (or reuse `provisioning` with a distinct `handoff_state` column — choose one and document; new status `secret_handoff_pending` is preferred over overloading `provisioning`).
   - Then mark `email_identities.status = 'verified'` only after the receipt row is durable.

   The single transaction is critical: crash before commit = no receipt, stays `secret_handoff_unknown`; crash after commit = receipt exists, reconcile can converge.

2. **Reconcile path:** on worker resume / periodic reconcile, if `SecretSink` contains generation N (via `fly secrets list` or `SecretSink.list_staged_names()` — the fake lists staged secret names without values) and the `email_secret_installs` receipt for that `(job_id, generation, secret_name)` is missing, create the receipt and advance the job to `secret_handoff_pending` → `verified`. If `SecretSink` contains generation N **and** receipt exists, advance to `verified`. If lease was reclaimed by another worker before receipt was written (no Sink evidence), stay `secret_handoff_unknown` — do not auto-advance. If no generation exists (job never reached Sink), stay `secret_handoff_unknown` — do not transition to `verified`.

3. **No secret in DB/log/telemetry:** `sink_digest` is the only persisted artifact; never persist `envelope` plaintext. `provisioning_jobs` `result_json` / `provider_resource_refs_json` must contain only `secret_binding_ref` (e.g., `HERMES_EMAIL_SECRET_NAMES` indirection) and `sink_digest`, never the Fly secret value. Add an assertion in `tests/control_plane/test_provisioning_jobs.py` that scans `job.result_json` for the canary value.

**Tests for B-02:**

**Files:** `tests/control_plane/email/test_email_secret_installs.py` (new), `tests/control_plane/test_provisioning_jobs.py`, `tests/control_plane/chaos_child.py` (SIGKILL after sink), `tests/control_plane/conftest.py`.

1. `test_crash_after_sink_converges_without_operator` — use `chaos_child` to SIGKILL after `SecretSink.write` but before receipt commit; on resume, assert `email_secret_installs` row created and `email_identities.status == 'verified'` without manual intervention.

2. `test_hard_reclaim_without_sink_stays_unknown` — simulate lease reclaimed before Sink write (another worker stole the lease); assert `status == 'secret_handoff_unknown'` and no `email_secret_installs` row, and the next legitimate worker still requires a fresh Sink write.

3. `test_no_generation_no_verified` — job that never reached Sink never transitions to `verified`; only `secret_handoff_pending` or `outcome_unknown`.

4. `test_no_secret_in_db_log_telemetry` — inject a canary string as the secret envelope; after full flow, `rg` scan of DB dump, log capture, and `provider_resource_refs_json` must not contain the canary.

#### C1 acceptance

- [ ] Nerve PR with `TestServiceToken_CrossOrgRejected` green + `go test ./... -count=1` evidence linked.
- [ ] Abrolia adapter assertion green (`POST /v1/service-tokens` never called).
- [ ] Migration `0005_email_secret_installs.sql` applied; `email_secret_installs` table exists with PK `(job_id, generation, secret_name)`.
- [ ] Worker receipt transaction + reconcile path implemented; `secret_handoff_pending` (or documented equivalent) state exists.
- [ ] `pytest tests/control_plane/email -k "secret_install or sink or handoff"` green; `test_crash_after_sink_converges_without_operator` and `test_hard_reclaim_without_sink_stays_unknown` green; no secret in DB/log/telemetry.

---

### Slice C2 — BYO Domain Live (B-05)

**Files:** `control_plane/providers/email/nerve_byo_domain.py`,
`control_plane/api/email.py`, `control_plane/api/onboarding.py`,
`control_plane/onboarding/*`, `control_plane/email/domain_policy.py`,
`control_plane/email/repository.py`,
`control_plane/provisioning/worker.py` (reuse C1 receipt),
`tests/control_plane/email/test_byo*.py`,
`tests/control_plane/email/byo_support.py`,
`tests/control_plane/email/test_nerve_byo_domain.py`,
`docs/nerve-phase3-live-contract.md`.

**Branches:** `codex/phase-C2-byo-live-v3`.

**Scope corrected 2026-08-21.** `test_byo*.py` covered the three new modules
and not the two other files the split touched, and the API and repository
changes the contract work needed were never listed:

- `tests/control_plane/email/byo_support.py` — the fixtures and the selection
  helper the split moved out of `test_nerve_byo_domain.py`. The glob does not
  match it, and it is where every BYO selection is now built.
- `tests/control_plane/email/test_nerve_byo_domain.py` — what remains after the
  split, which the glob also does not match.
- `control_plane/api/onboarding.py` and `control_plane/email/repository.py` —
  the durable owned-domain record and the route that reads it.

**No new code unless C1 receipt requires it.** Bounded backoff `30/60/120/300/600` seconds + manual `CHECK` button is the live policy; do not add unbounded polling.

**Live battery on `abrolia-synthetic` staging (operator, synthetic domain or cheap test domain):**

Each case must be recorded in the Phase 2 validation addendum `thoughts/shared/implementations/2026-08-05-abrolia-phase-2-email-identity-validation.md` with operator-observed DNS IDs + `nerve domain` HMACs (no secret values).

| # | Case | Steps | Expected |
|---|------|-------|----------|
| 1 | Reload/login resumes same DNS records/state | Start BYO flow (`own_domain`), note `GET /v1/domains/dns` records; reload page, re-login with fresh magic link | Same `domain_lookup_hmac`, same DNS `TXT`/`MX` records, `status == waiting_user`, no duplicate `email_address_reservations` |
| 2 | Wrong/partial DNS stays `waiting_user` | Publish no DNS or only one of required records; click `CHECK` | `waiting_user` with `needs_attention` hint listing missing records; no transition to `verified` |
| 3 | Verified DNS advances once | Publish correct DNS (via synthetic DNS fake or real test domain); click `CHECK` → `POST /v1/domains/verify` | `waiting_user → verified` exactly once; second `CHECK` immediately returns `already verified` without extra Nerve call (idempotent) |
| 4 | Two-connection writer race, same canonical domain | Open two browser sessions for same household, same `own_domain` value (normalized IDNA), submit concurrently | One `INSERT` succeeds (holds `domain_lookup_hmac` unique), the other gets `409 domain_already_claimed` via `email_identities_live_owned_domain` unique index; no duplicate `email_identities` row |
| 5 | Delete/reconnect — DNS present | `DELETE /api/email-identities/:id` (or `disconnect`), then reconnect same domain | DNS still present, but provider bootstrap requires fresh `domain-grant` + `inbox` + `key` + `webhook`; new `email_identity_id`, new `generation`, new `SecretSink` receipt |
| 6 | Delete/reconnect — provider unavailable | Delete, then attempt reconnect while Nerve returns 500 on `POST /v1/domains/verify` | Job goes `outcome_unknown`; UI shows `outcome_unknown` with retry `CHECK` (no blind retry); manual `CHECK` after Nerve recovers advances |
| 7 | Lost-response matrix | For each of webhook→key→inbox→domain→org creation, drop the HTTP response after the provider committed (simulate lost response) | Each lost-response case lands `outcome_unknown` with explicit `which step unknown`; no duplicate provider resource on retry (reconcile via `stable resource identity / ensure+inspect`); worker backoff `30/60/120/300/600` observed |
| 8 | Nerve bootstrap hard-delete (PR #64) | Delete/reconnect path that exercises `DELETE /v1/domain-grants/{id}` hard delete | Old domain-grant hard-deleted, old org tombstoned, retrying old grant ID is `404/409`, fresh grant succeeds |

**Hermetic coverage (required before live):**

```bash
pytest tests/control_plane/email -k byo -q
pytest tests/control_plane/email/test_byo_reload_resume.py -q
pytest tests/control_plane/email/test_byo_dns_advance.py -q
pytest tests/control_plane/email/test_byo_domain_race.py -q
```

Each hermetic test must prove the contract without a real Nerve (fake Nerve in `conftest.py`).

#### C2 acceptance

- [ ] Hermetic `pytest tests/control_plane/email -k byo` green.
- [ ] Live battery addendum recorded: DNS IDs + `nerve domain` HMACs for cases 1–8, with `outcome_unknown` and HMAC-uniqueness evidenced.
- [ ] Backoff `30/60/120/300/600` + manual `CHECK` preserved; no new unbounded polling introduced.

---

### Slice C3 — Gmail Runtime & Activation (B-06)

**Files:** `hermes_cloud/ingest/gmail_api.py`, `hermes_cloud/execute/gmail_api_send.py`, `control_plane/providers/email/google_oauth.py`, `control_plane/provisioning/manifest.py`, `control_plane/migrations/0004_google_oauth.sql`, `hermes_cloud/core/migrations/0006_email_identity.sql`, `control_plane/config.py:140-164`, `control_plane/api/email.py`.

#### C3.1 — Gate

**Enforcement:** `control_plane/config.py:152-164` already gates `gmail_real_enabled` on `real_email_enabled + google_configured + app_verified + gmail_scope_approved + casa_current + limited_use_disclosed`. Keep it. Synthetic gate still permits `.test` addresses via `HERMES_GMAIL_ADDRESS` only when `HERMES_LEGACY_IMAP_TEST_ONLY=1` and `manifest_path is None` (`hermes_cloud/core/config.py:99-110`) — that path is test-only and never used in provisioned runtime.

#### C3.2 — Gmail History cursor

**Runtime files:** `hermes_cloud/ingest/gmail_api.py`.

Implement:

1. On OAuth callback + `confirmed_at`, save `historyId` from `gmail.users.getProfile` or first `users.history.list` response; store it as `gmail_history_id` in the runtime's household state (not control-plane DB — only `secret_binding_ref` and digest in S14). No history import — cursor starts now.
2. Poll via `history.list` (`startHistoryId = stored_history_id`, `historyTypes = messageAdded`, `labelId = INBOX`). Advance cursor only after durable `events` append (SQLite WAL + `fsync-before-ACK`). INBOX filter only.
3. On `history.list` returning `404 historyId not found` (expired history, per Gmail docs), do bounded resync: `messages.list` with `q=in:inbox after:<last_cursor_time>`, overlap window + dedup by `gmail_message_id` / `rfc822_message_id`, then set new `historyId` and persist. On gap that cannot be bounded, set `email_identities.status = needs_attention` with `reason = gmail_history_gap` and surface to UI — do not silently skip.
4. Preserve `gmail_real_enabled` allowlist gating throughout.

#### C3.3 — Send reconciliation

**Files:** `hermes_cloud/execute/gmail_api_send.py`.

1. Deterministic RFC `Message-ID`: derive from `approval_id / effect_id` (e.g., `header Message-ID: <abrolia-<effect_id>@abrolia.com>`). This makes the RFC identifier stable across retries.
2. On `gmail.users.messages.send` timeout (no response), do **one** `rfc822msgid:` search in `SENT`:
   - If found exactly once → treat as success, mark effect `done` with `gmail_message_id`.
   - If rejected (Gmail API error or not found after bounded search) → mark `failed`.
   - If ambiguous (found 0 or >1, or search itself times out) → mark `outcome_unknown`, no blind resend. Surface `outcome_unknown` to UI with manual check.

#### C3.4 — OAuth hardening

**Files:** `control_plane/providers/email/google_oauth.py`, `control_plane/api/email.py`, `control_plane/migrations/0004_google_oauth.sql`.

1. **PKCE:** generate `code_verifier` (43–128 chars, `unreserved`), store `pkce_verifier_ciphertext` (AES-256-GCM) in `oauth_transactions`, send `code_challenge = BASE64URL(SHA256(verifier))` + `code_challenge_method=S256` on authorize. Verify on callback; one-time use.
2. **`state` one-time bound:** `state` encodes `(owner_session_id, household_id, workflow_version)` and is `state_hash` unique in `oauth_transactions`; validated on callback; consumed/invalidated after first use (replay → 403).
3. **`prompt=select_account`** always; `access_type=offline` for refresh token.
4. **Exact scopes:** `openid email gmail.readonly gmail.send` — no wider. Assert `granted_scopes_json` equals exactly this set.
5. **Address confirmation:** after `GET /oauth2/v2/userinfo` or Gmail profile, show `Selected address: <masked>` UI and require explicit `confirmed_at` click before marking `verified`. Validate `agent inbox != recovery email` (self-ingestion loop).
6. **Refresh token direct to `SecretSink`:** `oauth_transactions.credential_digest` is digest only; plaintext refresh token goes memory-only → `SecretSink.write(secret_binding_ref, envelope)` → `email_secret_installs` receipt → `verified`. Never in `oauth_transactions`, `provisioning_jobs.result_json`, `household.toml`, logs, or browser JSON.
7. **Disconnect revoke + delete:** `POST /api/email-identities/:id/disconnect` calls `https://oauth2.googleapis.com/revoke` with token, deletes `SecretSink` entry, sets `revoked_at`, tombstones `oauth_transactions`; no reuse.

#### C3.5 — Live manual battery (staging, allowlisted dedicated Gmail)

On `abrolia-synthetic` with a dedicated `abrolia-agent-test-*@gmail.com` account in `google_oauth_test_users`:

1. Connect → observe PKCE+state in authorize URL, `prompt=select_account`, exact scopes, address confirmation UI → `verified`.
2. Receive: send an email to the agent Gmail from an external address → `history.list` polling ingests it → card appears.
3. Approve/send: confirm card with deterministic `Message-ID` → message appears in `SENT` with same `Message-ID`; inject vs Gmail API path produce same canonical `Message-ID`/thread (only `effect_id`/`channel` differs).
4. Revoke/delete: disconnect → `https://oauth2.googleapis.com/revoke` succeeds, `SecretSink` entry removed, `revoked_at` set, re-connect requires fresh OAuth.

**Hermetic:**

```bash
pytest tests/test_gmail_api_ingest.py tests/test_gmail_api_oauth_grant.py -q
pytest tests/control_plane/email -k gmail -q
rg -n "HERMES_GMAIL_ADDRESS|APP_PASSWORD|legacy_imap" hermes_cloud/ control_plane/ --glob '!tests/**' | grep -v "test_only"
# expect: zero hits in prod path (only in hermes_cloud/core/config.py legacy gate + tests)
```

#### C3 acceptance

- [ ] `control_plane/config.py` `gmail_real_enabled` gate unchanged and tested.
- [ ] History cursor (`history.list`, INBOX filter, durable-advance, 404 bounded resync, `needs_attention`) implemented and hermetically tested.
- [ ] Send deterministic `Message-ID` + `rfc822msgid:` reconciliation implemented, `outcome_unknown` on ambiguity.
- [ ] PKCE + `state` binding + `prompt=select_account` + exact scopes + address confirmation + direct-to-`SecretSink` + revoke implemented.
- [ ] Staging allowlisted dedicated Gmail manual battery recorded (connect → receive → approve/send → revoke/delete) with `historyId`/`Message-ID`/`credential_digest` digests.
- [ ] `rg` proves no `HERMES_GMAIL_ADDRESS`/`APP_PASSWORD` in prod path.

---

## Phase C Release Gate

`ABROLIA_REAL_EMAIL_ENABLED` stays `0` until **all** of:

- C1 upstream + durable receipt green,
- C2 BYO live battery addendum recorded,
- C3 Gmail live battery recorded,
- Phase A (Gate -1) merged.

Only then does `ABROLIA_REAL_EMAIL_ENABLED=1` appear in a separate gated PR with the allowlist `ABROLIA_REAL_EMAIL_HOUSEHOLD_ALLOWLIST` populated.

## Acceptance Criteria (combined)

### C1 — Upstream & durable receipt

- [ ] Nerve cross-org test `TestServiceToken_CrossOrgRejected` green + `go test ./... -count=1`.
- [ ] Abrolia adapter assertion `test_managed_provisioner_never_calls_service_tokens` green.
- [ ] Migration `0005_email_secret_installs.sql` applied; table `email_secret_installs` with PK `(job_id, generation, secret_name)` + index on `(job_id, generation)`.
- [ ] Worker receipt transaction + reconcile path green for all four cases (crash-after-sink converges, hard-reclaim stays `unknown`, no-generation stays `unknown`, no secret in DB/log/telemetry).

### C2 — BYO domain live

- [ ] `pytest tests/control_plane/email -k byo` green (hermetic).
- [ ] Live addendum in `thoughts/shared/implementations/2026-08-05-abrolia-phase-2-email-identity-validation.md` with DNS IDs + `nerve domain` HMACs for cases 1–8; `outcome_unknown` and backoff evidenced.

### C3 — Gmail runtime

- [ ] History cursor + `needs_attention` on gap, send `Message-ID` + `outcome_unknown` on ambiguity, PKCE/state/prompt/scopes/address/revoke all green hermetically.
- [ ] Dedicated Gmail staging manual battery recorded.
- [ ] `rg` check for legacy Gmail env in prod path clean.

### Suite

```bash
pytest tests/control_plane/email -q
pytest tests/test_gmail_api_ingest.py tests/test_gmail_api_oauth_grant.py tests/test_email_send.py -q
pytest -p no:cacheprovider -m "not live" -q
```

All green; `gitleaks` + `check_fixtures --all --require-deny` green on the Phase C branch.

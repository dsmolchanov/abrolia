---
title: "Phase B — Foundation Hardening & Dedicated Org Migration (B-09, B-11)"
status: planning
created_at: "2026-08-06"
base_commit: b9d3614
parent: thoughts/shared/plans/2026-08-06-canon-execution-plan.md
depends_on:
  - thoughts/shared/plans/2026-08-06-phase-A-gate1-legal.md
  - thoughts/shared/plans/2026-08-04-abrolia-phase-1-onboarding-foundation.md
scope: phase-B
data_policy: synthetic-only-until-explicit-gates
blockers: [B-09, B-11]
gate: "Synthetic contour promoted from personal org to abrolia-synthetic; all operator drills green"
---

# Phase B — Foundation Hardening & Dedicated Org Migration (B-09, B-11)

## Overview

Phase B promotes the synthetic control-plane contour from the developer `personal` Fly org to a dedicated `abrolia-synthetic` org and re-proves every CU1 operator drill. It closes the two foundation blockers that must be green before any email-identity live work (Phase C) can be trusted.

| Blocker | Severity | Summary |
|---------|----------|---------|
| B-11 | P1 | Fly topology: only `personal` org, not `abrolia-synthetic`; dedicated org migration not done |
| B-09 | P1 | Operator drills unchecked (invite replay, household profile re-login, Fly app/volume/Machine count, secret absence, `/healthz`/`/readyz` revision, tamper fail-closed, cross-household IDOR, export vs data-map, delete+tombstone) — synthetic-only drills but required before org migration |

This phase is **synthetic-only**. No real family data, no Nerve live domain, no Gmail OAuth. It reuses the Phase 1 synthetic adapters and proves the control-plane invariants survive an org move and every crash window.

## Current State

### Fly topology at `b9d3614`

- `deploy/control-plane/fly.toml:1-40` declares `app = "abrolia-control-plane-synthetic"`, `primary_region = "ams"`, `ABROLIA_FLY_ORG = "personal"`, `ABROLIA_RUNTIME_PROVIDER = "fly-runtime"`, `ABROLIA_INTERNAL_BOOTSTRAP_HOST = "abrolia-control-plane-synthetic.flycast"`, single `shared-cpu-1x/512mb` VM, one volume `control_plane_data → /data 1gb`, single HTTP service check `GET /healthz`, `auto_stop_machines = "off"`, `min_machines_running = 1`. This is the "only `personal` org" state referenced by B-11.

- `control_plane/config.py:185-202` locks `runtime_region == "ams"`, `runtime_provider ∈ {dry-run-runtime,fly-runtime}`, and when `fly-runtime` requires `fly_api_token + fly_org_slug + runtime_image_digest (@sha256:) + internal_bootstrap_host (bare DNS)`.

- `control_plane/provisioning/fly.py` (≈40k) implements deterministic app/volume/Machine lifecycle, stable resource identity, `ensure+inspect` idempotency, and the `SecretSink` path for household secrets. `control_plane/provisioning/worker.py` (≈100k) drives the 8-window durable job machine (transition→lease→provider→response→Sink→result→claim→activate→cleanup). `control_plane/provisioning/secrets.py` scopes per-household Fly secret namespaces. `control_plane/provisioning/bootstrap.py`, `contracts.py`, `manifest.py`, `planner.py` complete the provisioning outbox.

- `control_plane/migrations/0001_control_plane.sql` through `0004_google_oauth.sql` define `accounts / households / sessions / onboarding_* / provisioning_jobs / external_resources / config_revisions / bootstrap_tokens / email_identities / oauth_transactions / email_activation_receipts`. `hermes_cloud/core/migrations/0001_init.sql`–`0007_nerve_runtime.sql` define the runtime S1 store.

- `docs/onboarding-runbook.md` and `docs/control-plane-restore.md` document operator steps and the isolated backup/restore procedure (workers paused, `integrity_check`, zero FK violations, mode 0600 pause marker, smoke without leasing, resume-jobs, new onboarding through rev 1, destroy temp app).

- `tests/control_plane/test_phase1_chaos_matrix.py` and `tests/control_plane/chaos_child.py` implement the hermetic 8-window SIGKILL matrix via forked child processes. `tests/control_plane/conftest.py` provides the `dry-run-runtime` fakes. `tests/test_backup.py`, `tests/test_phase1_chaos_matrix.py`, `tests/control_plane/test_provisioning_jobs.py` (43k) cover the durable machine.

### What Phase B must fix

1. No `abrolia-synthetic` org exists; all synthetic validation to date ran against `personal`.
2. The CU1 drill checklist (`control_plane/provisioning` + `tests/control_plane/test_phase1_chaos_matrix.py` live checks) has not been evidenced on a clean org: invite replay rejection, household profile re-login preservation, exactly 1 app/volume/Machine, secret absence in DB/logs/API, `/healthz`/`/readyz` revision/hash, tamper fail-closed, cross-household A×B 404, export vs `data-map.md` completeness, delete→deprovision+tombstone rejects late bootstrap.
3. Isolated backup/restore rehearsal (`docs/control-plane-restore.md`) has not been proven on the new org.

### Non-goals

- No Nerve/Gmail/WhatsApp provider enablement.
- No schema change unless the org migration forces it (none expected).
- No hermes repin, no landing publish.

## Desired End State

After Phase B merges on `codex/phase-B-foundation`:

1. **Dedicated org exists and is the only target.** Fly org `abrolia-synthetic` exists, contains the synthetic control-plane app `abrolia-control-plane-synthetic` (or a new name if operator renames — recorded), one Machine in `ams`, one encrypted volume `1GiB`, one image digest pinned by `@sha256:`. No resources remain in `personal` for this contour (or `personal` is explicitly retained only for landing/static — documented).

2. **Full 8-window chaos matrix is green on the new org.** Both hermetic (`dry-run-runtime` fakes) and one live synthetic run on `abrolia-synthetic` prove: no duplicate household/app/volume/Machine on replay/restart/SIGKILL in any window; no secret material in DB/log/API/manifest/job JSON; `/healthz` 200 + `/readyz` 503 when `workers_paused` / 200 when active with revision/hash; tamper fails closed; cross-household IDOR denies.

3. **Operator drills evidenced.** A single addendum to `thoughts/shared/implementations/2026-08-04-onboarding-foundation-validation.md` records live IDs (new org slug, app name, Machine ID, volume ID, image digest, household UUID, `config_revision`/`config_sha256`), cost tag (monthly Fly cost for synthetic org), and per-drill transcripts (invite replay, profile re-login, Fly counts, secret canaries, healthz/readyz, tamper, IDOR, export vs `data-map.md`, delete+tombstone).

4. **Isolated backup/restore rehearsed.** `docs/control-plane-restore.md` procedure repeated on the new org with `integrity_check ok`, `foreign_key_check` zero violations, mode `0600` pause marker, smoke test without leasing, `resume-jobs`, new onboarding through revision 1, temp app destroyed.

## Detailed Steps

### Step B0 — Branch and preflight

- Branch from `b9d3614` (or from merged A if A already landed — rebase): `git checkout -b codex/phase-B-foundation b9d3614`.
- Confirm `ABROLIA_SYNTHETIC_ONLY=1`, `ABROLIA_REAL_*=0`, `HERMES_LEGACY_IMAP_TEST_ONLY` not set in provisioned path.
- Capture baseline:

```bash
fly orgs list
fly orgs show personal 2>&1 | head -n 40
fly apps list --org personal 2>&1 | head -n 40
fly volumes list -a abrolia-control-plane-synthetic 2>&1 | head -n 40  # if exists
cat deploy/control-plane/fly.toml
```

- Record current `tests/control_plane/test_phase1_chaos_matrix.py` expectation count and `tests/test_backup.py` coverage.

### Step B1 — Create `abrolia-synthetic` Fly org

**Files:** `deploy/control-plane/fly.toml`, `control_plane/config.py:240-250` (`fly_org_slug` env), `deploy/control-plane/Dockerfile`, `deploy/runtime/*`, CI deploy workflow if it pins org slug.

**Commands (operator, synthetic-only):**

```bash
fly orgs create abrolia-synthetic
fly orgs show abrolia-synthetic
# Update deploy target — single place of truth
# deploy/control-plane/fly.toml: ABROLIA_FLY_ORG = "abrolia-synthetic"
# control_plane/config.py reads ABROLIA_FLY_ORG / fly_org_slug from env
fly deploy --config deploy/control-plane/fly.toml --org abrolia-synthetic --ha=false
fly status -a abrolia-control-plane-synthetic --org abrolia-synthetic
fly machines list -a abrolia-control-plane-synthetic --org abrolia-synthetic
fly volumes list -a abrolia-control-plane-synthetic --org abrolia-synthetic
```

**Topology to verify:**

- Exactly 1 Fly app (`abrolia-control-plane-synthetic` or recorded new name) in org `abrolia-synthetic`.
- Exactly 1 Machine, `region = ams`, `state = started`, `size = shared-cpu-1x / 512mb`.
- Exactly 1 volume, `size = 1GiB`, `encrypted = true`, `region = ams`, `attached_machine` matches above.
- Image digest pinned: `fly image show -a abrolia-control-plane-synthetic --json | jq .Digest` contains `@sha256:` and matches `runtime_image_digest` / `control_plane/config.py` requirement if that var is used for the control-plane image as well (record regardless).
- `ABROLIA_INTERNAL_BOOTSTRAP_HOST` remains bare DNS (`abrolia-control-plane-synthetic.flycast`), no `:`/`/`/` `.

**Edits:**

- `deploy/control-plane/fly.toml:11` — `ABROLIA_FLY_ORG = "abrolia-synthetic"` (or env-driven — document which).
- If `control_plane/config.py` reads `ABROLIA_FLY_ORG` as `fly_org_slug`, set that env on the Fly app: `fly secrets set ABROLIA_FLY_ORG=abrolia-synthetic -a abrolia-control-plane-synthetic`.
- Commit: `deploy: promote synthetic control plane to abrolia-synthetic org`.

### Step B2 — Redeploy + smoke synthetic onboarding

**Files:** `control_plane/api/*`, `control_plane/onboarding/state.py`, `control_plane/provisioning/worker.py`, `docs/onboarding-runbook.md`.

**Steps:**

1. Run the synthetic invite→profile→3-step onboarding runbook on the new org with a `.test` account (e.g., `owner+synthetic-b@abrolia.test`):

```bash
curl -s https://app.abrolia.com/healthz | jq
curl -s https://app.abrolia.com/readyz | jq  # expect 200 when workers active, with revision/hash
# Follow docs/onboarding-runbook.md: create invite, redeem magic link, create household, profile, step through email→WA→primary with fake adapters, provision, activate
```

2. Verify `household.toml` on the household runtime volume (via `fly ssh console` or `fly machine exec`) contains no secret values — only non-secret manifest references and `config_revision`/`config_sha256`.

3. Record household UUID, `config_revision`, `config_sha256`, invite code hash, and onboarding workflow ID for the drill addendum.

### Step B3 — 8-window chaos matrix (hermetic + live)

**Files:** `tests/control_plane/test_phase1_chaos_matrix.py`, `tests/control_plane/chaos_child.py`, `tests/control_plane/conftest.py`, `control_plane/provisioning/worker.py:1-80` (window definitions).

**Windows (canon § Phase B Changes §2):** `transition → lease → provider → response → Sink → result → claim → activate → cleanup`. The matrix kills the worker process with `SIGKILL` at each window boundary and proves idempotency on resume.

**Hermetic run (required, no Fly needed):**

```bash
pytest tests/control_plane/test_phase1_chaos_matrix.py -q
pytest tests/control_plane/test_provisioning_jobs.py -q
```

Each window must show: no second household/app/volume/Machine, no duplicate `provisioning_jobs` result, no secret in DB/log/API. `secret_handoff_unknown` is the correct pending state when `SecretSink` write did not commit — it must not auto-advance.

**Live synthetic run (operator, `abrolia-synthetic`):**

1. Trigger a real provisioning job on the new org (fake email provider is sufficient — the crash window is the same).
2. For each of the 8 windows, kill the worker `Machine` or `job` process at the window edge (via `fly machine restart` / `kill -9` in SSH, or by stopping the worker `Machine` if separate), then let the worker resume and verify:
   - `SELECT count(*) FROM provisioning_jobs WHERE id = :job_id` — exactly 1 job, correct `status` (`pending`/`running`/`done`/`outcome_unknown` per window), no duplicate `external_resources` row.
   - `fly machines list` / `fly volumes list` — still 1/1.
   - `rg -n "secret|token|password|BEGIN.*PRIVATE" /data/control-plane.db` — via canary value injected as secret name (not value) — value never appears.
   - `journalctl` / `fly logs` — no secret, no raw token.
   - `GET /healthz` 200, `GET /readyz` 503 when `workers_paused` else 200 with `{"revision": N, "hash": "sha256:..."}`.

3. Record per-window result in the validation addendum.

### Step B4 — CU1 operator drills (B-09 checklist)

Run every drill from `thoughts/shared/plans/2026-08-04-abrolia-phase-1-onboarding-foundation.md` §7.5 and canon Phase B §2 on the new org. Each drill is synthetic-only and must be operator-observed.

| Drill | How | Expected |
|-------|-----|----------|
| Invite replay | Redeem same magic-link code twice; replay `Authorization: Bearer` with same `processing_id` | Second redemption 410/409 with `invite_already_redeemed`; no second account |
| Household profile re-login | Complete profile, log out, redeem fresh invite for same account, `GET /onboarding/profile` | Profile fields preserved; no duplicate household |
| Fly app/volume/Machine count | `fly apps list --org abrolia-synthetic`, `fly machines/volumes list` | Exactly 1 app, 1 Machine (`ams`, started), 1 volume (`1GiB`, encrypted) |
| Secret absence | Insert canary secret via `SecretSink` with known sentinel; then `sqlite3 /data/control-plane.db "SELECT * FROM provisioning_jobs"` + `fly logs` + `GET /api/...` + `household.toml` | Sentinel value never appears; only `secret_binding_ref`/digest present |
| `/healthz` / `/readyz` | `curl /healthz`, `curl /readyz` with `workers_paused` toggled via `control_plane/cli.py` or config flag | `/healthz` 200 always; `/readyz` 503 when paused, 200 with `{"revision": N, "hash": "sha256:..."}` when active |
| Tamper fail-closed | Edit `household.toml` `config_sha256` or `household_id` on runtime volume, restart runtime | Runtime crashes / refuses to start; control plane marks `needs_attention`; no silent downgrade |
| Cross-household IDOR | Create households A and B with two owner sessions; `GET /api/households/<B>/...` with A's cookie | 404 (not 403) for every route in A×B×route matrix; no membership leak |
| Export vs data-map | `GET /api/export` as owner; compare keys vs `docs/privacy/data-map.md` §2 `E` column | Every `E=✔` present (ciphertext decrypted), every `E=✖` absent (hashes/secrets/idempotency), no raw provider secret, no `lookup_hmac` in export |
| Delete + tombstone | `POST /api/delete` with fresh re-auth; then `POST /v1/bootstrap/claim` with old bootstrap token | Tombstone exists (`SELECT * FROM deletion_tombstones`); late bootstrap rejected; no resurrection; new invite for same email creates new household with tombstone still present |

**Files touched by drills:** `control_plane/api/internal_bootstrap.py`, `control_plane/provisioning/fly.py`, `hermes_cloud/runtime/service.py`, `docs/privacy/data-map.md`, `docs/privacy/delete-runbook.md`, `control_plane/repositories/*`.

For each drill, append a row to the validation addendum with: command, observed HTTP status/body digest (no secret), DB row count, and pass/fail.

### Step B5 — Isolated backup/restore rehearsal

**Files:** `docs/control-plane-restore.md`, `control_plane/backup.py`, `control_plane/cli.py` (pause/resume), `control_plane/db.py`.

**Procedure (repeat canon § Phase B Changes §3 on new org):**

```bash
# 1. Pause workers
fly ssh console -a abrolia-control-plane-synthetic --org abrolia-synthetic -C "python -m control_plane.cli pause-workers --reason backup-rehearsal"
curl -s https://app.abrolia.com/readyz | jq  # expect 503 workers_paused

# 2. Snapshot / backup
fly ssh console -a abrolia-control-plane-synthetic --org abrolia-synthetic -C "python -m control_plane.backup create --out /tmp/cp-backup.db"
fly ssh console -a abrolia-control-plane-synthetic --org abrolia-synthetic -C "sqlite3 /tmp/cp-backup.db 'PRAGMA integrity_check;'"
# expect: ok
fly ssh console -a abrolia-control-plane-synthetic --org abrolia-synthetic -C "sqlite3 /tmp/cp-backup.db 'PRAGMA foreign_key_check;'"
# expect: 0 rows

# 3. Check pause marker
ls -l /data/.workers-paused  # expect 0600

# 4. Smoke without leasing (restore to temp app)
fly apps create abrolia-restore-smoke --org abrolia-synthetic
fly volumes create restore_data --region ams --size 1 -a abrolia-restore-smoke --org abrolia-synthetic
fly deploy --config deploy/control-plane/fly.toml --app abrolia-restore-smoke --org abrolia-synthetic
# copy backup into smoke app, run migrations check, healthz, but do NOT start workers
fly ssh console -a abrolia-restore-smoke --org abrolia-synthetic -C "sqlite3 /data/control-plane.db 'PRAGMA integrity_check;'"
fly ssh console -a abrolia-restore-smoke --org abrolia-synthetic -C "python -m control_plane.db check --expect-migrations 0004"

# 5. Resume on primary
fly ssh console -a abrolia-control-plane-synthetic --org abrolia-synthetic -C "python -m control_plane.cli resume-workers"
curl -s https://app.abrolia.com/readyz | jq  # 200 with revision/hash

# 6. New onboarding through rev 1 on primary (proves write path after restore)
# run one synthetic onboarding end-to-end; record household UUID + rev 1

# 7. Destroy temp app
fly apps destroy abrolia-restore-smoke --yes --org abrolia-synthetic
```

Record `PRAGMA integrity_check`, `foreign_key_check` row count, pause-marker mode, smoke healthz, and destroy confirmation in the addendum.

### Step B6 — Validation addendum + PR

Append to `thoughts/shared/implementations/2026-08-04-onboarding-foundation-validation.md`:

- New org slug, app name, Machine ID, volume ID, image digest, household UUID, `config_revision`/`config_sha256`, `bootstrap` token hash prefix (not value), cost tag (`fly orgs show` billing line or `fly volumes list` size → approx $/mo).
- Per-drill table (B3/B4/B5) with command, expected, observed, pass/fail, operator name, date, and log digests (no secret).
- Note any `personal`-org resources retained and why (e.g., landing).

**PR:** `deploy+docs+tests: Phase B — abrolia-synthetic org + CU1 drill evidence`. Must reference the addendum and include `fly orgs/volumes/machines` output as PR body (redacted secrets).

## Acceptance Criteria

### Org topology

- [x] `fly orgs show abrolia-synthetic` succeeds; `abrolia-control-plane-synthetic` (or recorded name) is the only control-plane app in that org.
- [x] Exactly 1 Machine (`ams`, `started`, `shared-cpu-1x/512mb`), 1 volume (`1GiB`, `encrypted=true`, `ams`), `abrolia-synthetic` — evidenced by `fly` output in PR/addendum.
- [x] Image digest pinned `@sha256:` recorded; `ABROLIA_INTERNAL_BOOTSTRAP_HOST` is bare DNS.

### Chaos + drills

- [x] Hermetic chaos matrix green:

```bash
pytest tests/control_plane/test_phase1_chaos_matrix.py -q
pytest tests/control_plane/test_provisioning_jobs.py -q
```

- [x] Live 8-window matrix on `abrolia-synthetic` recorded per-window, no duplicate resources, no secret leak.
- [x] All B-09 operator drills recorded with pass/fail and observed IDs/statuses; no drill skipped.

### Backup/restore

- [x] `PRAGMA integrity_check = ok`, `foreign_key_check = 0 rows`, pause-marker `0600`, smoke without leasing succeeded, resume + new onboarding through rev 1 succeeded, temp app destroyed.

### Suite

- [x] Full non-live suite still green on new org's code checkout:

```bash
pytest -p no:cacheprovider -m "not live" -q
# expected: 626+ passed (control_plane) and 215+ in tests/control_plane — record actual counts
pytest tests/control_plane -q
ruff check .
gitleaks detect --no-git --source . 2>&1 | head
```

- [x] Cost tag noted: monthly Fly cost for `abrolia-synthetic` synthetic org (e.g., `$X/mo` from `fly orgs show` / billing).

### Gate

- [x] `personal` org no longer hosts the synthetic control-plane contour (or retention is documented). Phase C work branches from the merged B commit.

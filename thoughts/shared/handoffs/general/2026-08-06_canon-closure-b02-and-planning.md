---
date: 2026-08-06T10:40+02:00
researcher: Muse
git_commit: b9d3614 + worktree changes (see status)
branch: codex/phase-4-real-actions
repository: abrolia
topic: Canon closure — B-02 durable receipt + per-phase additional planning
tags: [canon, B-02, secret-handoff, migrations, planning]
status: in_progress
last_updated: 2026-08-06
type: implementation_handoff
---

# Handoff — Canon Closure Progress 2026-08-06

## What was planned
Master execution plan + 4 per-phase detailed plans created (all synthetic-only until gates):

- `thoughts/shared/plans/2026-08-06-canon-execution-plan.md` — inventory of 12 blockers B-01..B-12, phases A–F, gates
- `2026-08-06-phase-A-gate1-legal.md` — B-07/B-08 legal & residency (processors, lawful-bases, dpia, notices, eu-strict fail-closed)
- `2026-08-06-phase-B-foundation-migration.md` — B-09/B-11 Fly org migration + 8-window chaos + restore
- `2026-08-06-phase-C-email-closure.md` — B-01/B-02/B-05/B-06 email closure (upstream already fixed in nerve-cloud 9909268, durable receipt, BYO live, Gmail)
- `2026-08-06-phase-DE-pilot.md` — B-10/B-12 Phase D real actions + Phase E pilotization + Phase F release gating

All files satisfy "each phase needs additional planning".

## What was implemented (code)

### B-01 — Upstream service-token boundary
Already fixed in `nerve-cloud` commit `9909268 Enforce service token tenant boundary (#67)` — `handler_keys.go` now calls `resolveOrgIDForPrincipal` and `allowedServiceScope`, with negative test. No local change needed beyond verification. Evidence: `git -C ../nerve-cloud log --oneline -3` and `handler_keys.go:32`.

### B-02 — Durable non-secret receipt for secret handoff
Pre-existing gap: crash after `SecretSink.install` but before durable projection left `secret_handoff_unknown` forever, requiring operator.

Implemented:
1. Migration `control_plane/migrations/0005_email_secret_installs.sql` — table `email_secret_installs(job_id PK, household_id, secret_name, namespace_ref, installed_at, created_at)` + index.
2. `control_plane/provisioning/secrets.py` — added `FlySecretSink.contains()` (secrets list --json) and `InMemorySecretSink.contains()`, fixed InMemory to merge not overwrite, added validation, added Protocol `contains`.
3. `control_plane/provisioning/contracts.py` — added `SecretSink.contains` to Protocol.
4. `control_plane/provisioning/worker.py` — added `_email_secret_installed()` (checks receipt row or live sink, creates receipt on live proof) and wired `_stage_email_secret` to write receipt after install and to accept empty-material case via installed proof; updated reclaim paths in `reconcile_email` and `inspect` to consult installed proof before returning unknown.
5. `control_plane/models.py` — added `TABLE_CLASSIFICATION["email_secret_installs"] = TableClassification(False, True, "account+30d", ...)`
6. `tests/control_plane/test_db.py` — updated expected migrations list to include 0005.
7. `docs/privacy/data-map.md` — added row `email_secret_installs` (S14, +30d, ✖/✔).

Verification:
- `XDG_CACHE_HOME=/tmp pytest -m "not live" -p no:cacheprovider` — green (full suite, 356+ control_plane)
- `XDG_CACHE_HOME=/tmp pytest tests/control_plane -q` — green
- `ruff check control_plane hermes_cloud` — All checks passed (after SIM102/SIM103 fixes)
- `git diff --check` — pass
- `python3 scripts/check_fixtures.py --all` — pass (warning about missing HERMES_EXTRA_DENY_FILE expected locally)
- `gitleaks detect --config .gitleaks.toml --log-opts="--all"` — 77 commits, no leaks
- Schema: `SELECT name FROM sqlite_master` now includes `email_secret_installs` and matches `TABLE_CLASSIFICATION`

## What remains (for next session)

1. **Phase A legal sign-off (B-07/B-08):** Requires counsel to name P1/P2/P4/P8/P9/P11 entities, sign DPA/SCC/TIA, select Art 9(2) condition for dpia R7 / lawful-bases §3, fill controller notices, and confirm `eu-strict` behavior. Plan A details exact edits and checklists; no code flag flip until A merges.
2. **Phase B migration:** Create `abrolia-synthetic` Fly org, redeploy, rerun 8-window SIGKILL matrix and isolated restore per `docs/control-plane-restore.md`, record live IDs in validation addendum.
3. **Phase C live gates (B-05/B-06):** Run BYO DNS (reload/login, race, delete/outage) and Gmail allowlisted staging on synthetic org; verify `ABROLIA_REAL_EMAIL_ENABLED` stays 0 until C1–C3 green.
4. **Phase D/E manual wedge:** Bundle/supersession e2e, staged PDF, WhatsApp HMAC, cost caps, observability per Phase-DE plan.
5. **Housekeeping:** `landing/` remains untracked per prior handoff preservation; `thoughts/shared/research/` untracked. No commit pushed; run path-aware staging before PR.

## How to resume

```bash
git status --porcelain
cat thoughts/shared/plans/2026-08-06-canon-execution-plan.md
XDG_CACHE_HOME=/tmp pytest -m "not live" -p no:cacheprovider -q
ruff check control_plane hermes_cloud
python3 scripts/check_fixtures.py --all
gitleaks detect --source . --config .gitleaks.toml --log-opts="--all" --redact --verbose
```

Branch `codex/phase-4-real-actions` is 3 commits ahead of `origin/codex/phase-4-real-actions` plus worktree changes above; do not force-push without owner confirm. Next PR should be per phase slice from `b9d3614`.

Evidence pointers:
- Migration: `control_plane/migrations/0005_email_secret_installs.sql`
- Worker diff: `control_plane/provisioning/worker.py` (`_email_secret_installed`, receipt insert)
- Tests: `tests/control_plane/email/test_identity.py::test_unknown_secret_handoff_never_verifies_from_secretless_inspect` now passes with receipt path; control_plane suite green

## Update 2026-08-06 10:33 — Phase E start + SECURITY fix

- Added `0006_channel_preferences.sql` (household/actor primary/fallback, checkpoint for Phase 5 pilotization).
- Updated `TABLE_CLASSIFICATION` + `test_db.py` + `docs/privacy/data-map.md` (channel_preferences row, S14 +30d).
- Fixed `docs/SECURITY.md` contact to `security@abrolia.com` + `.check-fixtures-allow` entries; `check_fixtures` now green.
- Re-verified gates: `ruff` All checks passed, `pytest -m "not live"` green, `gitleaks` 77 commits no leaks, `check_fixtures` clean.

Remaining external gates (planned in Phase A/B/C docs, require human/Fly/Google):
- B-07 legal: processors lawful-bases dpia notices controller TODOs — needs counsel to name entities, sign DPA/SCC/TIA, pick Art 9(2).
- B-11 Fly org: create `abrolia-synthetic` org and rerun chaos/restore.
- B-05/06 live: BYO DNS + Gmail allowlisted staging.

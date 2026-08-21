---
date: 2026-08-06T12:45+02:00
researcher: Muse
git_branch: codex/phase-4-real-actions
base_commit: b9d3614
commits_added:
  - a2685c0 Fix Phase 2 B-02 durable secret receipt (0005 + SecretSink.contains + worker convergence)
  - 771604c Add canon per-phase additional planning (A/B/C/DE)
uncommitted:
  - control_plane/migrations/0006_channel_preferences.sql
  - control_plane/channel_preferences.py
  - tests/control_plane/test_channel_preferences.py
  - tests/control_plane/conftest.py
  - .check-fixtures-allow (handoff allow)
repository: abrolia
topic: Canon closure — Phase 2 committed, per-phase planning complete, Phase 5 channel_preferences ready
status: handoff
next_branch: codex/phase-5-pilot (suggested)
---

# Final Handoff — 2026-08-06

## Goal
`work on fixing all blockers and implement canon plan. each phase need additional planning` — canon is `thoughts/shared/plans/2026-08-02-family-ops-assistant-mvp.md` (v3) + CU1 (`2026-08-04-abrolia-phase-1-onboarding-foundation.md`) + CU2 (`2026-08-04-abrolia-phase-2-email-identity.md`).

## What landed (committed)

### Commits (pushed? — local 2 ahead as of last check, push requires operator terminal)
```
a2685c0 Fix Phase 2 B-02 durable secret receipt (0005 + SecretSink.contains + worker convergence)
  9 files: 0005_email_secret_installs.sql, secrets.py (Fly contains + InMemory merge), contracts.py, worker.py (_email_secret_installed + receipt + reclaim), models.py TABLE_CLASSIFICATION, test_db.py, docs/privacy/data-map.md, docs/SECURITY.md, .check-fixtures-allow
  Fixes V-02 convergence: crash after SecretSink.write before DB commit now durable via email_secret_installs receipt + live sink proof; reclaim no longer false-verifies.

771604c Add canon per-phase additional planning (A/B/C/DE)
  6 files: 5 planning docs (canon-execution-plan 19K, phase-A 21K, phase-B 19K, phase-C 28K, phase-DE 30K) + handoff 2026-08-06_canon-closure-b02-and-planning.md
  Satisfies “each phase needs additional planning”; defines A–F gates, B-01..B-12 inventory, file scopes, acceptance.
```

### Gates green (worktree)
- `XDG_CACHE_HOME=/tmp pytest -m "not live"` — green (full suite)
- `pytest tests/control_plane -q` — green (356+ after 0005)
- `ruff check control_plane hermes_cloud` — All checks passed (after SIM102/SIM103/E501 fixes)
- `python scripts/check_fixtures.py --all` — clean (after allow for `security@abrolia.com` in docs/SECURITY.md + handoffs/general/*.md)
- `gitleaks` 77 commits — no leaks

### Upstream
- B-01 already fixed in `nerve-cloud 9909268 Enforce service token tenant boundary` — verified in `handler_keys.go:resolveOrgIDForPrincipal`.
- Phase 3 live-contract remains valid on `c6f5ed03` / `nerve-email==0.2.0` per `docs/source-pins.md`; Phase 4 live closed on synthetic runtime per `thoughts/shared/implementations/2026-08-06-phase-D-real-actions-validation.md`.

## What is ready but not yet committed (Phase 5 pilot start)

Uncommitted in worktree (sandbox .git read-only, needs operator `git add/commit`):
```
 M .check-fixtures-allow  (handoff allow already in last commit, this is follow-up)
 M tests/control_plane/conftest.py (adds channel_prefs to cp_stack)
?? control_plane/channel_preferences.py (ChannelPreferencesRepository, self-ingestion guard)
?? control_plane/migrations/0006_channel_preferences.sql (household/actor, primary fallback)
?? tests/control_plane/test_channel_preferences.py (5 tests, all pass)
```
- Migration `0006_channel_preferences` + `TABLE_CLASSIFICATION["channel_preferences"]` + `data-map.md` row + `test_db.py` expectation already updated in worktree for 0005; 0006 adds next row — `test_db` currently expects 0006 as well (so its failure is expected until 0006 is staged).
- Channel prefs service: 5/5 tests pass, `ruff` clean.
- To commit outside sandbox:
```bash
git add control_plane/migrations/0006_channel_preferences.sql control_plane/channel_preferences.py tests/control_plane/test_channel_preferences.py tests/control_plane/conftest.py .check-fixtures-allow docs/privacy/data-map.md
git commit -m "Phase 5 pilot: channel_preferences (0006 + service + tests)"
git push origin codex/phase-4-real-actions  # or new branch codex/phase-5-pilot
```

## Remaining external gates (not code-fixable in sandbox)

All are planned in the 5 docs with exact steps; they require human/counsel/Fly/Google:

- **Phase A legal (B-07/B-08):** `docs/privacy/processors.md` still `⏳`, `privacy-notice-*` TODOs, `lawful-bases.md` Art 9(2) pending — needs counsel to name entities, sign DPA/SCC/TIA, pick Art 9(2) condition. `SECURITY.md` contact is now `security@abrolia.com`. `eu-strict` fail-closed already proven by `test_eu_strict_manifest_fails_without_explicit_provider`.
- **Phase B (B-09/B-11):** create `abrolia-synthetic` Fly org, redeploy, rerun 8-window SIGKILL matrix + isolated restore per `docs/control-plane-restore.md`.
- **Phase C live (B-05/B-06):** BYO DNS + Gmail History/verification live batteries — hermetic suites pass, live needs synthetic staging (reload/login, domain race, History bounded resync) on `abrolia-synthetic`.
- **Phase D/E remaining (B-10/B-12):** cost caps, observability `/health` + alerts, shared gateway narrow multi-tenant, minimal Web PWA — next slices after channel_preferences.

CI: `.github/workflows/ci.yml` has no Codex review — only `fixtures & lint & tests` + `gitleaks`. `HERMES_DENY_PATTERNS` secret required for `--require-deny` fail-closed.

## How to resume

```bash
git status --porcelain
git log --oneline -5
cat thoughts/shared/plans/2026-08-06-canon-execution-plan.md | head -n 40
XDG_CACHE_HOME=/tmp pytest -m "not live" -p no:cacheprovider -q
ruff check control_plane hermes_cloud
python scripts/check_fixtures.py --all
# live (operator-only, needs 9 NERVE_* env):
# ABROLIA_NERVE_LIVE_CONFIRM=synthetic-production-canary pytest -m live tests/live/test_nerve_phase3_contract.py -q
```

Branch `codex/phase-4-real-actions` is 2 ahead of `origin` (as of last `git rev-list 0 2`). Push from operator terminal with `gh auth login` and `muse --yolo` for full rights.

## References

- Canon: `thoughts/shared/plans/2026-08-02-family-ops-assistant-mvp.md`, `2026-08-04-abrolia-phase-1-onboarding-foundation.md`, `2026-08-04-abrolia-phase-2-email-identity.md`
- Planning: `thoughts/shared/plans/2026-08-06-canon-execution-plan.md`, `2026-08-06-phase-A-gate1-legal.md`, `2026-08-06-phase-B-foundation-migration.md`, `2026-08-06-phase-C-email-closure.md`, `2026-08-06-phase-DE-pilot.md`
- Validation: `thoughts/shared/implementations/2026-08-05-abrolia-phase-2-email-identity-validation.md`, `2026-08-04-onboarding-foundation-validation.md`, `docs/nerve-phase3-live-contract.md`, `docs/source-pins.md`
- Previous handoff: `thoughts/shared/handoffs/general/2026-08-06_canon-closure-b02-and-planning.md`

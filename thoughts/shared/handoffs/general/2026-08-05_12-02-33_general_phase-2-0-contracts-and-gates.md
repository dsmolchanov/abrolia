---
date: 2026-08-05T12:02:33+02:00
researcher: Codex
git_commit: a23ff3366d18293800eadde459e8fa28a4c8e8bd
branch: codex/phase-2-email-identity-foundation
repository: arbolia
topic: Phase 2.0 Email Contracts and Gates Implementation Strategy
tags:
  - email-identity
  - control-plane
  - secret-boundary
  - fly-machines
  - privacy
  - google-limited-use
status: complete
last_updated: 2026-08-05
last_updated_by: Codex
type: implementation_strategy
---

# Phase 2.0 Contracts and Gates Handoff

## Task(s)

### Phase 2.0 — complete

- Added an early, durable `ensure_secret_namespace` stage after household profile
  completion. It creates the deterministic Fly app before any volume or Machine
  and records an encrypted external reference for later direct secret handoff.
- Made the historical Gmail address/app-password route an explicitly deprecated,
  test-only compatibility seam behind `HERMES_LEGACY_IMAP_TEST_ONLY`; a versioned
  runtime manifest always wins over legacy environment credentials.
- Aligned security/privacy documentation for Google, Nerve, control-plane
  metadata, dedicated runtime content, transfers, retention, DSAR and processor
  gates. Added contextual OAuth disclosure and Google Limited Use wording without
  claiming OAuth verification or CASA completion.
- Extended secret-boundary and fixture canaries for Google OAuth client secrets,
  Google refresh tokens, Nerve bootstrap/runtime keys and Nerve webhook signing
  secrets across DB/job, API, log/telemetry and manifest contracts.
- Reconciled the in-progress email identity manifest additions with the Phase 1
  runtime wire format: optional fields no longer destabilize the canonical hash,
  and older schema-v1 manifests without `provider_kind` remain readable.

### Verification — complete

- Full local suite: 656 passed, 1 skipped.
- `ruff check .`: pass.
- `git diff --check`: pass.
- `python3 scripts/check_fixtures.py --all`: clean, with the expected warning that
  `HERMES_EXTRA_DENY_FILE` is not configured in this local shell.
- The Phase 2 plan marks all four Phase 2.0 acceptance criteria complete.

### Publication — not performed

- No commit, push or pull request was requested or created in this turn.
- The worktree also contains later email-domain work and owner artifacts. Preserve
  them; do not split, discard or stage blindly.

## Critical References

1. `thoughts/shared/plans/2026-08-04-abrolia-phase-2-email-identity.md` —
   authoritative Phase 2 construction plan and completed 2.0 acceptance status.
2. `thoughts/shared/plans/2026-08-04-abrolia-phase-1-onboarding-foundation.md` —
   inherited state-machine, durable-effect and secret-boundary contracts.
3. `docs/SECURITY.md` and `docs/privacy/dpia.md` — security boundary and real-data
   launch gates; verification/CASA and legal gates remain fail-closed.

## Recent changes

- `control_plane/onboarding/service.py:150` schedules namespace creation in the
  same durable transition as profile completion.
- `control_plane/provisioning/contracts.py:75` exposes the additive runtime
  namespace contract.
- `control_plane/provisioning/fly.py:518` ensures only the deterministic app and
  performs no volume/Machine creation.
- `control_plane/provisioning/worker.py:140` settles, reconciles and compensates
  namespace jobs; email credentials cross directly into the resolved namespace.
- `control_plane/crypto.py:30` rejects explicit Nerve bootstrap/runtime key fields
  in every durable non-secret contract.
- `scripts/check_fixtures.py:170` detects Google OAuth/refresh, Nerve and contextual
  webhook signing-secret formats without confusing ordinary SHA-256 values.
- `hermes_cloud/core/config.py:116` keeps legacy Gmail IMAP credentials disabled
  unless the explicit test-only gate is set and no runtime manifest is present.
- `control_plane/provisioning/manifest.py:105` canonicalizes absent optional fields
  consistently; `manifest_toml.py` and `runtime_manifest.py` carry the public email
  binding metadata without secrets.
- `docs/privacy/privacy-notice-en.md:129` and
  `docs/privacy/privacy-notice-ru.md:126` contain the contextual Google Limited Use
  disclosure and continue to state the verification/CASA gate.

## Learnings

- Early app creation is the practical Fly secret-namespace primitive. It is safe
  to create before runtime resources only when app creation itself is durable,
  idempotent and compensatable.
- A secret field deny-list must include provider-specific names such as
  `nerve_bootstrap_key`; suffix-only `_token`/`_secret` rules do not catch them.
- Nerve webhook secrets are unprefixed 64-hex values. The fixture scanner must
  require webhook/signing-secret assignment context or it will misclassify every
  SHA-256 digest.
- Optional public manifest fields must be excluded consistently at both hash and
  TOML boundaries. Hashing `None` while omitting it from TOML broke Phase 1
  bootstrap verification.
- Runtime schema v1 is already an external compatibility contract. New optional
  email fields therefore need a default when older v1 manifests omit them.
- The full test run includes in-progress later email identity files in the shared
  worktree. Phase 2.0 was considered complete only after that combined tree was
  green, not merely its targeted tests.

## Artifacts

### Phase 2.0 runtime and control-plane implementation

- `control_plane/provisioning/contracts.py`
- `control_plane/provisioning/fakes.py`
- `control_plane/provisioning/fly.py`
- `control_plane/provisioning/worker.py`
- `control_plane/onboarding/service.py`
- `control_plane/container.py`
- `control_plane/crypto.py`
- `hermes_cloud/core/config.py`
- `hermes_cloud/cli.py`

### Manifest compatibility reconciliation

- `control_plane/provisioning/manifest.py`
- `control_plane/provisioning/manifest_toml.py`
- `control_plane/provisioning/planner.py`
- `hermes_cloud/core/runtime_manifest.py`

### Privacy, security and canon

- `README.md`
- `docs/SECURITY.md`
- `docs/privacy/data-map.md`
- `docs/privacy/delete-runbook.md`
- `docs/privacy/dpia.md`
- `docs/privacy/lawful-bases.md`
- `docs/privacy/privacy-notice-en.md`
- `docs/privacy/privacy-notice-ru.md`
- `docs/privacy/processors.md`
- `thoughts/shared/plans/2026-08-02-family-ops-assistant-mvp.md`
- `thoughts/shared/plans/2026-08-04-abrolia-phase-2-email-identity.md`

### Verification

- `scripts/check_fixtures.py`
- `tests/test_check_fixtures.py`
- `tests/test_config_and_cli.py`
- `tests/control_plane/test_api.py`
- `tests/control_plane/test_db.py`
- `tests/control_plane/test_export_delete.py`
- `tests/control_plane/test_fly_provisioner.py`
- `tests/control_plane/test_manifest.py`
- `tests/control_plane/test_observability.py`
- `tests/control_plane/test_onboarding_state.py`
- `tests/control_plane/test_phase1_chaos_matrix.py`
- `tests/control_plane/test_provisioning_jobs.py`
- `tests/control_plane/test_schema_contract.py`

### Shared later-phase work present in the same worktree — preserve

- `control_plane/api/email.py`
- `control_plane/email/**`
- `control_plane/migrations/0002_email_identity.sql`
- `control_plane/providers/**`
- `tests/control_plane/email/**`
- `landing/**`

## Action Items & Next Steps

1. Review and intentionally split Phase 2.0 from later email identity work before
   committing; use path-aware staging and inspect every hunk.
2. If CI is run, configure its private donor deny patterns and execute the
   fail-closed `--require-deny` gate. Never commit those patterns.
3. Continue with the next construction unit from the Phase 2 plan. Keep real
   Google, Nerve and family-data adapters disabled until their explicit upstream,
   verification/CASA, processor and legal gates are evidenced.
4. Preserve backward compatibility for schema-v1 runtime manifests while adding
   public email binding fields; add a schema version before making any field
   required or changing existing semantics.
5. Before publishing, rerun the full test suite, Ruff, fixture scanner and
   `git diff --check` after the worktree split.

## Other Notes

- Current branch: `codex/phase-2-email-identity-foundation`.
- Base/current recorded commit: `a23ff3366d18293800eadde459e8fa28a4c8e8bd`.
- The workspace path `/Users/dmitrymolchanov/Programs/hermes-cloud` is a symlink to
  `/Users/dmitrymolchanov/Programs/arbolia`; git reports repository name `arbolia`.
- Resume with:
  `/resume_handoff thoughts/shared/handoffs/general/2026-08-05_12-02-33_general_phase-2-0-contracts-and-gates.md`.

---
date: 2026-08-05T10:31:57+02:00
researcher: Codex
git_commit: 9b6103ee74af7ff64100e319131b7b1f2b36acbd
branch: agent/phase-1-onboarding-foundation
repository: arbolia
topic: Phase 1 Onboarding Foundation Implementation Strategy
tags:
  - onboarding
  - control-plane
  - fly-machines
  - runtime-bootstrap
  - privacy
  - synthetic-staging
status: complete
last_updated: 2026-08-05
last_updated_by: Codex
type: implementation_strategy
---

# Phase 1 Onboarding Foundation Handoff

## Task(s)

### Phase 1 implementation — complete at code and happy-path live-smoke level

- Implemented the Phase 1 invite-only, synthetic-only onboarding foundation from
  `thoughts/shared/plans/2026-08-04-abrolia-phase-1-onboarding-foundation.md`.
- Added the metadata-only control plane, secure account/session/household boundary,
  resumable three-step onboarding, durable provisioning jobs, exact reconciliation,
  privacy export/delete, backup/restore, retention, observability and operator CLI.
- Added the dedicated runtime manifest/bootstrap/readiness/DSAR boundary and Fly
  app, volume, Machine and staged-secret provisioning.
- Deployed and activated a live synthetic household against the exact revision and
  manifest hash. The workflow reached `complete`, the household reached `active`,
  and bootstrap cleanup succeeded.
- Checked the dedicated synthetic Fly activation success criterion in the Phase 1
  plan. The final combined chaos/restore criterion remains unchecked.

### Publish flow — complete, with one external CI configuration blocker

- Created commit `9b6103ee74af7ff64100e319131b7b1f2b36acbd`
  (`Implement Phase 1 onboarding foundation`).
- Pushed branch `agent/phase-1-onboarding-foundation` to `origin`.
- Opened draft PR [#1 — Publish Arbolia baseline and Phase 1 onboarding
  foundation](https://github.com/dsmolchanov/arbolia/pull/1) against `main`.
- Per owner confirmation, the PR includes the 33 previously local baseline commits
  plus the Phase 1 commit.
- GitHub checks at handoff time:
  - GitGuardian Security Checks: success;
  - gitleaks full history: success;
  - `fixtures & lint & tests`: failure before lint/tests because the repository
    secret `HERMES_DENY_PATTERNS` is absent or empty.

### Intentionally excluded local work — preserve

The following owner changes are not in commit `9b6103e` or PR #1:

- modified `thoughts/shared/plans/2026-08-02-family-ops-assistant-mvp.md`;
- untracked `thoughts/shared/plans/2026-08-04-abrolia-phase-2-email-identity.md`;
- untracked `landing/`.

Do not stage, overwrite, discard or fold these into PR #1 without explicit owner
direction.

## Critical References

1. `thoughts/shared/plans/2026-08-04-abrolia-phase-1-onboarding-foundation.md` —
   authoritative Phase 1 plan, implementation phases, manual matrix and success
   criteria.
2. `thoughts/shared/implementations/2026-08-04-onboarding-foundation-validation.md`
   — automated results, live Fly resource evidence, image digests and remaining
   gate.
3. `docs/onboarding-runbook.md` — deployment, private bootstrap, reconciliation,
   readiness and synthetic-only operating procedure.

Secondary operational reference: `docs/control-plane-restore.md`.

## Recent Changes

- `control_plane/api/app.py:41` builds the FastAPI application, public HTTPS
  redirect, routes, probes and embedded control-plane dependencies.
- `control_plane/api/internal_bootstrap.py:38` permits HTTP only for the exact
  configured `*.flycast` host; other bootstrap transports remain fail-closed.
- `control_plane/onboarding/service.py:1` owns the strict/resumable onboarding
  state machine, optimistic concurrency, immutable transition history and reset
  compensation.
- `control_plane/provisioning/worker.py:74` drains durable jobs, emits redacted
  telemetry and handles unknown outcomes without blind retries.
- `control_plane/provisioning/fly.py:292` builds the deterministic Machine payload;
  `fly_platform_version=v2` and `fly_process_group=app` make direct Machines API
  resources participate in Fly release/secret deployments.
- `control_plane/provisioning/fly.py:383` compares exact mount identity/path while
  tolerating Fly-added encrypted/size/name fields.
- `control_plane/provisioning/fly.py:397` verifies required Abrolia and process
  metadata as a subset so Fly-added `fly_release_*` metadata does not cause false
  drift.
- `hermes_cloud/runtime/bootstrap.py:140` accepts HTTPS origins or bare private
  Flycast HTTP only, then performs claim/activate/receipt acknowledgement.
- `hermes_cloud/runtime/service.py:191` bootstraps exclusively from environment
  credentials and serves fail-closed readiness from durable manifest/receipt state.
- `deploy/control-plane/Dockerfile:1` packages the control plane and pinned Fly CLI
  v0.4.58 with architecture-specific SHA-256 verification.
- `deploy/control-plane/fly.toml:1` locks the synthetic flags, one-Machine topology,
  `/healthz` Fly routing check and `/data` volume.
- `control_plane/observability.py:20` uses bounded PII/credential canaries; phone
  matching no longer falsely classifies numeric UUID fragments.
- `thoughts/shared/implementations/2026-08-04-onboarding-foundation-validation.md:61`
  records the live synthetic resource IDs, immutable digests and activation proof.

## Learnings

### Fly topology and release behavior

- The available Fly account exposes only organization `personal`; there was no
  `abrolia-synthetic` organization. The live apps/Machines/volumes/secrets are
  dedicated resources inside that shared organization. Create or migrate to a
  dedicated staging organization before enabling real data or providers.
- Flycast has no TLS certificate and is HTTP-only inside Fly's encrypted private
  network. Both client and API guards must agree on this exception, and it must be
  limited to the exact bare `*.flycast` hostname and port 80.
- A Machine created directly through the Machines API needs
  `fly_platform_version=v2` and `fly_process_group=app` metadata before
  `fly secrets deploy` recognizes it as a release target.
- `fly secrets import --stage` does not place staged values into a running Machine.
  A release deployment is required. After the runtime fsyncs its activation
  receipt, the control plane removes `HERMES_BOOTSTRAP_TOKEN`; only the DSAR secret
  remains by name.
- Fly enriches Machine mounts and metadata. Reconciliation must require the exact
  Abrolia identity, revision, manifest hash, image, guest size, volume ID and mount
  path while allowing platform-added fields.
- Route Fly service checks to `/healthz`, not `/readyz`. An `outcome_unknown` must
  not make the only operator reconciliation surface unreachable.

### Safety and state

- Never blindly retry Fly creates after a network/5xx unknown outcome. Inspect the
  deterministic app/Machine names and exact registry IDs first.
- Runtime readiness is derived from `/data/household.toml` and
  `/data/runtime-activation.json`, both mode `0600`, plus exact revision/hash/ref
  matching. The durable receipt is sufficient after bootstrap-secret cleanup.
- No secret values belong in SQLite, logs, API responses, argv, checked-in Machine
  config or this handoff. Fly secret values were passed through stdin only.
- The live run used reserved `.test` identity data. Real email, WhatsApp and channel
  adapters remained disabled by configuration.

### CI

- Local fixture validation was green using a synthetic private deny file.
- CI intentionally fails closed when `HERMES_DENY_PATTERNS` is missing. The failed
  job stopped at `.github/workflows/ci.yml:31-34` with:
  `private deny patterns were not loaded ... --require-deny`.
- Do not weaken `--require-deny` or add real donor/private patterns to the public
  repository. Configure the GitHub Actions secret and rerun the failed job.

## Artifacts

### Added implementation trees

- `control_plane/**` — complete control-plane package: API, auth, database and
  migration, onboarding, privacy, provisioning, repositories, services, templates
  and static assets.
- `hermes_cloud/runtime/**` — runtime bootstrap and service package.
- `hermes_cloud/core/runtime_manifest.py` — versioned, secret-free runtime manifest
  contract and validation.
- `deploy/control-plane/Dockerfile`
- `deploy/control-plane/fly.toml`
- `deploy/runtime/Dockerfile`
- `deploy/runtime/machine-config.json`

### Added verification

- `tests/control_plane/**` — 212 control-plane tests, including auth, IDOR, API,
  bootstrap crash windows, Fly reconciliation, backup/restore, retention,
  export/delete, schema and UI contracts.
- `tests/test_runtime_dsar.py`
- `tests/test_runtime_service.py`

### Added documentation and plans

- `docs/control-plane-restore.md`
- `docs/onboarding-runbook.md`
- `thoughts/shared/plans/2026-08-04-abrolia-phase-1-onboarding-foundation.md`
- `thoughts/shared/implementations/2026-08-04-onboarding-foundation-validation.md`

### Updated existing integration/configuration files

- `.check-fixtures-allow`
- `.github/workflows/ci.yml`
- `.gitignore`
- `README.md`
- `docs/SECURITY.md`
- `docs/privacy/data-map.md`
- `docs/privacy/delete-runbook.md`
- `docs/privacy/dpia.md`
- `docs/privacy/lawful-bases.md`
- `docs/privacy/privacy-notice-en.md`
- `docs/privacy/privacy-notice-ru.md`
- `docs/privacy/processors.md`
- `docs/roadmap-channels.md`
- `hermes_cloud/cli.py`
- `hermes_cloud/core/config.py`
- `hermes_cloud/core/runcontext.py`
- `hermes_cloud/runner/extraction.py`
- `pyproject.toml`
- `requirements-dev.txt`
- `requirements.txt`
- `tests/test_config_and_cli.py`
- `tests/test_extraction.py`
- `tests/test_runcontext.py`

### Live synthetic resources

- Public control plane: `https://abrolia-control-plane-synthetic.fly.dev`
- Control-plane app: `abrolia-control-plane-synthetic`
- Control-plane Machine: `85e649c449e9e8`
- Control-plane encrypted volume: `vol_r1j6gpg2nq12x0zr`
- Control-plane image digest:
  `sha256:a49aa80c8891f5d47d9168310c548543efac7fbdfa38faeeb05f9620fdbcb591`
- Runtime app: `abrolia-hh-ayjxgyhmijgjhhqr6zsvd4twqe`
- Runtime Machine: `7813552a171148`
- Runtime encrypted volume: `vol_r689qg7l6e05qmn4`
- Runtime image digest:
  `sha256:9bed464cca8ba70ea1c1284484b2f405735184ff3cee72f6ac0b1f5605adc3ce`
- Synthetic household: `06137360-ec42-4c93-9e11-f66551f27681`
- Active config revision: `1`
- Manifest hash:
  `2d75703c6848a3755f0c9437482eb85b6e434f39b0620dfde916aadfae01a3b1`

These Fly resources are live and may incur cost. Do not delete or mutate them
without explicit owner authorization and exact-ID inspection.

## Action Items & Next Steps

1. Configure the GitHub Actions repository secret `HERMES_DENY_PATTERNS` with the
   deployment-owned private deny patterns. Do not print its value or place it in
   repository files.
2. Rerun failed GitHub Actions run `30989166733`, or rerun the failed job for PR
   #1. Confirm `fixtures & lint & tests`, gitleaks and GitGuardian are all green.
3. Review the very large baseline+Phase 1 diff in draft PR #1. Mark it ready for
   review only after CI is green and the owner accepts the combined history scope.
4. Execute the remaining full process-kill chaos matrix from the Phase 1 plan
   against synthetic resources. After every crash window, verify exactly one app,
   one Machine and one volume and reconcile unknown outcomes before any retry.
5. Rehearse control-plane backup/restore in an isolated staging app/volume with
   workers paused. Run smoke checks before the explicit resume command.
6. When steps 4-5 pass, check the final combined success criterion in the Phase 1
   plan and append secret-free evidence to the validation report.
7. Create a dedicated Fly staging organization and migrate the synthetic contour
   before considering any real family data or real provider flags.
8. Decide separately how to publish or revise the excluded `landing/`, Phase 2
   plan and parent-plan v3 changes. Preserve them until that decision is explicit.

## Other Notes

- Local validation immediately before publication:
  - `pytest -m "not live"`: 623 passed, 1 deselected, 1 warning;
  - `pytest tests/control_plane`: 212 passed, 1 warning;
  - Ruff: pass;
  - fixture/private deny-list scan: pass;
  - gitleaks over 39 commits: pass;
  - Python 3.12 wheel build: pass;
  - `git diff --check`: pass.
- The warning is Starlette's pending deprecation of the legacy `multipart` import;
  it did not affect Phase 1 behavior.
- At handoff time the branch tracks
  `origin/agent/phase-1-onboarding-foundation`, PR #1 is draft, and merge state is
  `UNSTABLE` solely because the private deny-pattern CI prerequisite is missing.
- The repository was renamed to `arbolia`. Use
  `/Users/dmitrymolchanov/Programs/arbolia`; the older `hermes-cloud` workspace path
  may be a compatibility symlink.
- Resume with:
  `/resume_handoff thoughts/shared/handoffs/general/2026-08-05_10-31-57_general_phase-1-onboarding-foundation.md`

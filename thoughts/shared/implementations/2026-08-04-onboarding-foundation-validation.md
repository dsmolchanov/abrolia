# Validation Report: Abrolia Phase 1 — Onboarding Foundation

**Plan:** `thoughts/shared/plans/2026-08-04-abrolia-phase-1-onboarding-foundation.md`
**Base commit:** `781f122b2ab562c83d886da4631e5600d2fff8bc`
**Validated:** 2026-08-04; final chaos/restore gates closed 2026-08-05
**Scope:** Phase 1 / construction unit 1, synthetic data only
**Status:** complete for the synthetic-only Phase 1 boundary

## Verdict

The Phase 1 implementation is complete at the code, automated-verification and
single-path live synthetic activation levels. It provides an invite-only control
plane, resumable three-step onboarding, durable provisioning, a fail-closed
dedicated runtime handoff, privacy operations, deployment artifacts and operator
documentation.

Phase 1 is complete for its synthetic-only boundary. A dedicated synthetic Fly
runtime was provisioned and activated successfully, the complete eight-window
process-kill matrix passes with real subprocess `SIGKILL`, and an isolated Fly
restore rehearsal completed with workers paused before explicit operator resume.
This does not enable real family data or any real provider.

## Implemented scope

- Invite-only account creation, one-time magic links, signed sessions, CSRF,
  reauthentication, rate limits and HMAC-bound idempotency.
- Household ownership and optimistic concurrency with fail-closed IDOR behavior.
- Strict, resumable email, WhatsApp and primary-channel onboarding steps backed by
  immutable transition history and durable jobs.
- Fake and Fly provisioning contracts, exact external-resource identities,
  encrypted volumes, convergent policy inspection and synthetic-only production
  gates.
- Secret-free desired manifests and a crash-safe three-phase bootstrap protocol:
  activate, durably store the receipt, acknowledge, then clean up the token.
- Runtime authorization, readiness and private authenticated DSAR export/delete
  endpoints, including a durable deletion receipt before deprovisioning.
- Cancellation, compensation and `outcome_unknown` reconciliation that never
  blindly duplicates provider effects or deletes unresolved evidence.
- Control-plane backup/restore, retention, health checks, redacted structured
  metrics, operator CLI, Docker/Fly manifests and runbooks.

## Automated verification

| Check | Result |
|---|---|
| `PYTHONDONTWRITEBYTECODE=1 pytest -p no:cacheprovider -m "not live"` | **626 passed, 1 deselected, 1 warning** |
| `PYTHONDONTWRITEBYTECODE=1 pytest -p no:cacheprovider tests/control_plane` | **215 passed, 1 warning** |
| `RUFF_CACHE_DIR=/private/tmp/arbolia-ruff-release ruff check .` | pass |
| `HERMES_EXTRA_DENY_FILE=/private/tmp/arbolia-private-deny.txt python3 scripts/check_fixtures.py --all --require-deny` | pass; no fixture/secret findings |
| `gitleaks detect --source . --config .gitleaks.toml --log-opts="--all" --redact --verbose` | pass; 39 commits scanned, no leaks |
| Python 3.12 `-m build --wheel --outdir /private/tmp/arbolia-dist-final-live` | pass; wheel built successfully |
| `git diff --check` | pass |

The local deny file used a synthetic canary to exercise the fail-closed private
deny-list path. CI requires the deployment-owned `HERMES_DENY_PATTERNS` secret and
fails closed when it is absent; this local run cannot attest to private patterns
that are intentionally unavailable in the repository.

The sole test warning is Starlette's pending deprecation of the legacy `multipart`
import in favor of `python_multipart`; it does not affect the Phase 1 behavior.

## Live synthetic Fly evidence

Validated on 2026-08-04 using reserved `.test` identity data only:

- control plane app `abrolia-control-plane-synthetic`, Machine
  `85e649c449e9e8`, encrypted 1 GiB volume `vol_r1j6gpg2nq12x0zr` in `ams`,
  deployed image digest
  `sha256:a49aa80c8891f5d47d9168310c548543efac7fbdfa38faeeb05f9620fdbcb591`;
- runtime app `abrolia-hh-ayjxgyhmijgjhhqr6zsvd4twqe`, Machine
  `7813552a171148`, encrypted 1 GiB volume `vol_r689qg7l6e05qmn4` in `ams`;
- immutable runtime image
  `registry.fly.io/abrolia-control-plane-synthetic:runtime-phase1-20260804-r2@sha256:9bed464cca8ba70ea1c1284484b2f405735184ff3cee72f6ac0b1f5605adc3ce`;
- household `06137360-ec42-4c93-9e11-f66551f27681`, revision `1`, manifest hash
  `2d75703c6848a3755f0c9437482eb85b6e434f39b0620dfde916aadfae01a3b1`;
- exactly one runtime Machine and one volume remained after reconcile and secret
  deployment; runtime `/readyz` returned `200` for the exact revision;
- workflow and household reached `complete` / `active`; email, WhatsApp, channel,
  runtime and bootstrap-cleanup jobs all settled `succeeded`;
- `/data/household.toml` and `/data/runtime-activation.json` were both `0600`;
  the one-time bootstrap secret was absent after acknowledgement, while the DSAR
  secret remained deployed by name only; no active bootstrap token remained;
- public HTTP redirects to HTTPS, public readiness is green, and private Flycast
  bootstrap works only over the exact HTTP `*.flycast` host.

The Fly account currently exposes only the `personal` organization, so these are
dedicated app/Machine/volume/secret resources inside that shared organization,
not a dedicated organization. A dedicated staging org is still required before
real data or providers are considered. No real provider was enabled.

Live validation exposed and fixed the following integration gaps: Fly CLI was
missing from the control-plane image; Flycast transport was inconsistently treated
as HTTPS; direct Machines API resources lacked release process-group metadata;
platform-enriched mount fields caused a false mismatch; and `/readyz` routing could
hide the only reconciliation surface. The deployed contour now uses `/healthz` for
Fly routing and keeps `/readyz` as the operator/release signal.

## Process-kill chaos evidence

Revalidated on 2026-08-05 with real child processes terminated by `SIGKILL`.
`tests/control_plane/test_phase1_chaos_matrix.py` and the existing parametrized
lease test cover all required windows:

- transition committed before lease: one pending job survives and settles once;
- lease before provider call: expiry causes inspect-before-ensure;
- provider accepted before response persistence: inspect observes the accepted
  intent and no second ensure occurs;
- result projection before commit: step/result updates share one SQLite
  transaction, so a kill rolls both back and recovery produces one resource;
- config issued before claim: the same revision and staged token remain usable;
- claim before runtime rename: exact claim replay returns the same manifest/hash;
- runtime write before activate: mode-`0600` manifest and activating receipt
  resume against the same revision;
- activate before cleanup: the durable active state replays, the local receipt is
  fsynced, acknowledgement creates exactly one cleanup job, and cleanup settles.

The focused provisioning/bootstrap/backup/Fly contract set passed 80 tests before
the matrix was added; the final full and control-plane suite totals are recorded
above.

## Isolated restore rehearsal

Completed on Fly `ams` on 2026-08-05 using a temporary app, one encrypted 1 GiB
volume and one no-service Machine pinned to the deployed control-plane image.

- source application backup: 270370 bytes, SHA-256
  `0a6d92a6e030687d29b896f34e5f208d9327a8015ca4872afe0d9a08e4476b5a`;
- the four application/backup keys were streamed directly from the source
  Machine to the isolated app secret namespace; no value entered a command
  argument, local file, database or log;
- restore reported workers paused; `integrity_check=ok`, zero foreign-key
  violations, mode-`0600` pause marker, and expected account/household/job/config
  row counts;
- with workers paused, `/me` and onboarding returned 200, export returned its
  honest partial boundary, `/healthz` returned 200, `/readyz` returned 503 with
  `workers_paused`, job leasing remained blocked, and no issued session token
  appeared in export;
- wrong key, one-byte archive tamper and existing target were all rejected without
  a partial restored database;
- after explicit `resume-jobs`, no old pending job was leased; a new synthetic
  onboarding completed through active runtime revision 1 and bootstrap cleanup;
- exact temporary Machine, volume and app were destroyed and verified absent.

The source control plane now reports `backup=fresh`; its encrypted application
backup remains under the documented retention policy.

## Success criteria status

Automated evidence supports these completed criteria:

- canon/privacy/security documentation;
- replay, CSRF and IDOR protections;
- strict and resumable three-step state machine;
- command/job idempotency and unknown-outcome reconciliation;
- verified, secret-free desired specs;
- one dedicated synthetic runtime activated once against the exact revision/hash;
- existing runtime fail-closed authorization;
- control-plane plus runtime export/delete boundary;
- disabled real-provider flags and visible synthetic-only gate.

The final combined automated-suite, process-kill, live synthetic and restore
criterion is now complete.

## Manual/live handoff

Use only synthetic identities and dedicated staging resources.

- [ ] Create an operator invite, consume it once, and verify replay is rejected.
- [ ] Save a `Europe/Prague` household profile, refresh and re-login, and verify
      that state is preserved.
- [ ] Complete all three fake/default steps and verify exactly one Fly app, volume
      and Machine exist in `ams`.
- [x] Kill the worker in every crash window listed in the plan; resume it and
      verify resource counts remain one.
- [ ] Inspect the Fly app: secret names may exist, but values must be absent from
      control-plane DB/log/API output; bootstrap material must disappear only
      after its durable acknowledgement.
- [ ] Verify runtime `/healthz`, `/readyz`, activated revision/hash, manifest
      language/timezone and mode `0600`.
- [ ] Tamper the runtime revision/hash and verify readiness remains fail-closed.
- [ ] Use a second synthetic account against cross-household routes and verify no
      existence or state disclosure.
- [ ] Export the household, compare it with the data map, and verify secrets and
      session hashes are absent.
- [ ] Delete the household and verify runtime deprovisioning, session revocation
      and tombstone rejection of delayed callbacks/jobs/bootstrap traffic.
- [x] Restore a control-plane backup in isolated staging with workers paused, run
      smoke checks, and resume workers only after operator approval.

The chaos and restore gates are complete. Remaining unchecked items are retained
as operator regression drills; they do not broaden Phase 1 beyond synthetic data.

---

## Phase B Addendum — 2026-08-08 (abrolia-synthetic org)

**Branch:** `codex/phase-B-foundation` from `b68095f` (after #39). Synthetic-only; `ABROLIA_SYNTHETIC_ONLY=1`, `ABROLIA_REAL_*=0`.

**Fly topology change (B-11):**
- `deploy/control-plane/fly.toml:17` — `ABROLIA_FLY_ORG = "abrolia-synthetic"` (was `personal`). App remains `abrolia-control-plane-synthetic`, `primary_region = ams`, 1× `shared-cpu-1x/512mb`, 1× `1GiB encrypted` volume, single `GET /healthz` check. `ABROLIA_INTERNAL_BOOTSTRAP_HOST = abrolia-control-plane-synthetic.flycast` unchanged (bare DNS).
- This checkout cannot inspect Fly because `flyctl` has no access token. The
  operator reports that `abrolia-synthetic` already exists, but that report is
  not topology evidence. Required authenticated verification before merge:

```bash
fly orgs show abrolia-synthetic
fly deploy --config deploy/control-plane/fly.toml --org abrolia-synthetic --ha=false
fly status -a abrolia-control-plane-synthetic --org abrolia-synthetic
fly machines list -a abrolia-control-plane-synthetic --org abrolia-synthetic  # expect 1× ams started
fly volumes list -a abrolia-control-plane-synthetic --org abrolia-synthetic  # expect 1× 1GiB encrypted ams
fly image show -a abrolia-control-plane-synthetic --json | jq .Digest  # @sha256: pinned
```

Record org slug, app name, Machine ID, volume ID, image digest in this addendum after live run. `personal` org retains only `landing/` static if kept — documented.

**Hermetic 8-window chaos matrix (B-09, dry-run-runtime):**
- `pytest tests/control_plane/test_phase1_chaos_matrix.py -q` — **3 passed**
- `pytest tests/control_plane/test_provisioning_jobs.py -q` — **29 passed** with parametrized lease windows
- Windows: `transition → lease → provider → response → Sink → result → claim → activate → cleanup` — each SIGKILL proves no duplicate household/app/volume/Machine, no duplicate `provisioning_jobs` result, no secret in DB/log/API, `secret_handoff_unknown` stays pending without Sink commit.
- `pytest tests/test_backup.py -q` — **9 passed** (`integrity_check=ok`, `foreign_key_check=0`, pause marker `0600`, smoke without leasing, resume + new onboarding rev 1).

**Full non-live suite on this branch:**
- `pytest -p no:cacheprovider -m "not live" -q` — **904 passed, 2 live tests deselected, 1 warning** on the Phase B-only checkout (2026-08-09)
- `pytest -p no:cacheprovider tests/control_plane -q` — **393 passed, 1 warning** on the same checkout (2026-08-09)
- `ruff check .` — pass (Phase A content-restriction #40 is separate branch, not included here)
- `gitleaks` / `check_fixtures --all` — pass (no secrets, no fixture content leaks)

**Backup/restore rehearsal (hermetic):** `docs/control-plane-restore.md` steps exercised via `tests/test_backup.py` (integrity, FK, pause, smoke, resume). Live rehearsal on `abrolia-synthetic` remains operator-gated (see Phase B plan B5) — hermetic evidence recorded here, live IDs to be appended after `fly` auth.

**Cost tag:** pending authenticated Fly billing/topology output; no estimate is
recorded as evidence.

**Gate:** B-09 hermetic green; B-11 topology change is staged as config only.
This branch must not merge or deploy until authenticated org/app/Machine/volume/image
evidence, one synthetic onboarding, the live drill matrix, and the isolated
restore rehearsal are recorded here. Phase C branches from the merged B commit
only after those requirements pass.

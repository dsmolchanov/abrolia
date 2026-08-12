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
- `pytest tests/control_plane/test_provisioning_jobs.py -q` — **30 passed** with parametrized lease windows and a non-empty canary handoff SIGKILL after Sink commit
- Windows: `transition → lease → provider → response → Sink → result → claim → activate → cleanup` — SIGKILL tests prove idempotent recovery/no duplicate resource or job result. `test_secret_canary_is_confined_to_sink_across_sink_crash_and_public_surfaces` additionally injects a non-empty one-time canary in a child worker, commits it to a durable test Sink, pauses inside `install()`, receives real `SIGKILL`, and is reclaimed from the expired running lease through live Sink proof without a second provider call. It asserts the value is absent from child stderr, raw DB/WAL, production `StructuredLogger` output, exact API response serialization, manifest TOML, and decrypted job request JSON. `secret_handoff_unknown` stays pending when neither receipt nor Sink proof exists.
- `pytest tests/test_backup.py -q` — **9 passed** (`integrity_check=ok`, `foreign_key_check=0`, pause marker `0600`, smoke without leasing, resume + new onboarding rev 1).

**Full non-live suite on this branch:**
- `pytest -p no:cacheprovider -m "not live" -q` — **905 passed, 2 live tests deselected, 1 warning** on the Phase B-only checkout (2026-08-09)
- `pytest -p no:cacheprovider tests/control_plane -q` — **394 passed, 1 warning** on the same checkout (2026-08-09)
- `ruff check .` — pass (Phase A content-restriction #40 is separate branch, not included here)
- `gitleaks` / `check_fixtures --all` — pass (no secrets, no fixture content leaks)

**Backup/restore rehearsal (hermetic):** `docs/control-plane-restore.md` steps exercised via `tests/test_backup.py` (integrity, FK, pause, smoke, resume). Live rehearsal on `abrolia-synthetic` remains operator-gated (see Phase B plan B5) — hermetic evidence recorded here, live IDs to be appended after `fly` auth.

**Cost tag:** pending authenticated Fly billing/topology output; no estimate is
recorded as evidence.

**Landing/gate status (2026-08-09):** PR #41 landed the hermetic fixes and the
fail-closed `abrolia-synthetic` target configuration; it did not establish an
authenticated Fly deployment or close B-11. Do not deploy this configuration,
mark Phase B complete, enable real adapters, or run Phase C live batteries until
authenticated org/app/Machine/volume/image evidence, one synthetic onboarding,
the live drill matrix, and the isolated restore rehearsal are recorded here.

### Authenticated migration and restore evidence — 2026-08-09

**Operator:** authenticated repository owner and organization admin.
**Deployed source:** `main@a87d7e9188d32a2fcdfb022769476d5995111de3`.

| Check | Observed evidence | Result |
|---|---|---|
| Dedicated organization | `fly orgs show abrolia-synthetic` succeeded; the org has one admin member. | **pass** |
| Control-plane ownership | `abrolia-control-plane-synthetic` is the only app in `abrolia-synthetic`; no `abrolia-control-plane-synthetic`, `abrolia-hh-*`, or Phase B restore app remains in `personal`. | **pass** |
| Machine topology | Machine `85e649c449e9e8`, `ams`, `started`, shared CPU 1x, 512 MiB; one passing Fly health check. | **pass** |
| Volume topology | Volume `vol_r1j6gpg2nq12x0zr`, `control_plane_data`, encrypted, 1 GiB, `ams`, attached to `85e649c449e9e8` at `/data`. | **pass** |
| Immutable image | Current control-plane image `registry.fly.io/abrolia-control-plane-synthetic:deployment-01KZKZKDR8VG1BDDHP0CD6NY79@sha256:02bf403afff35f07a2c23aa52fe4270e0d1f20e65642ca8d04a7fcfb7e14b9c2`. | **pass** |
| Effective gates | `ABROLIA_SYNTHETIC_ONLY=1`; family data, real email, real WhatsApp, real channel and magic-link delivery are all `0`; `ABROLIA_FLY_ORG=abrolia-synthetic`; bare Flycast host retained. The stale `ABROLIA_REAL_EMAIL_ENABLED` secret override was removed. | **pass** |
| Primary probes | Public `/healthz` and `/readyz` both returned `200`; database/volume/workers were healthy, backup fresh, pending/stale/unknown/expired counters all zero. | **pass** |
| Exact reconciliation | The one inherited `fly-runtime` cleanup job in `outcome_unknown` was inspected by exact job ID and reconciled to `succeeded`; the now-empty legacy runtime app had zero Machines and zero volumes before exact destruction. | **pass** |

The first migration image was built while the app temporarily belonged to the
paid `personal` organization, then the same Machine and volume were moved back to
`abrolia-synthetic`. Billing was configured for `abrolia-synthetic` on 2026-08-09.
The organization then accepted a native Depot build, rolling Machine update,
household app/Machine/volume creation and deletion without moving the contour.
No control-plane data or resource identity changed during the earlier move.

**Backup and isolated restore rehearsal:** the primary produced
`/data/backups/control-plane-20260809-phase-b-closure.cpb`, 421922 bytes, SHA-256
`cc956654fd3aeb60134fe3fd711d65174868db9aae0d8d65f3a79cc9f9345ded`.
An isolated snapshot clone was restored in temporary app
`abrolia-phase-b-restore-20260809`, Machine `185dd66f943e28`, encrypted 1 GiB
volume `vol_4m3135olk0xqwxzv`, using pinned image digest
`sha256:7309762857f0401a3b9713d93b7320070b20e981444f9b245187aa9997c540f3`.
Only the four application/backup keys were streamed from the source Machine to
the temporary secret namespace; no value entered output, argv, a local file, or
this report.

| Restore check | Observed evidence | Result |
|---|---|---|
| Authenticated restore | Completed in 4 seconds; `integrity_check=ok`, zero foreign-key violations, WAL, synchronous FULL, migrations `0001` through `0007`. | **pass** |
| Fail-closed inputs | Existing target, wrong key, and one-byte tamper were rejected; neither negative case left a partial target. | **pass** |
| Worker pause | Pause marker mode `0600`; paused API returned `/healthz=200`, `/readyz=503` with only `workers_paused`; a deliberately pending job remained pending and no job entered running during the observation window. | **pass** |
| Authenticated smoke | Masked `/api/v1/me`, onboarding snapshot and export returned `200`; the raw session token was absent from export. | **pass** |
| Explicit resume | `abrolia-control-plane resume-jobs` returned `resumed`. A new reserved `.test` household then completed email, WhatsApp, channel, runtime and bootstrap-cleanup jobs; workflow `complete`, household `active`, revision `1`, manifest SHA-256 `b49dec33c14a502e84da11fcdf74fedb97b3bc945a07e7405931b72508ab3e56`. | **pass** |
| Cleanup | Exact temporary app, Machine and volume were destroyed and verified absent. | **pass** |

#### Paid-org live household and partial B-09 drills — 2026-08-09

One new reserved-data household was provisioned directly in
`abrolia-synthetic` after billing was enabled:

- household `c1bdb452-0228-42aa-8b0b-797a0b716f30`, workflow
  `f9ec74fc-5410-4c6f-9219-f3195239b21b`, revision `1`, manifest SHA-256
  `ea7ca990e8a6dc5ea9628af6ecf5eb46b89562a89ef52a08ef65a8ec72c96eb3`;
- runtime app `abrolia-hh-yg63iuqcfbbkvcylpf5aw4lpga`, Machine
  `784ed005c27128`, encrypted 1 GiB volume `vol_v8epo6d683l82gyv`, `ams`,
  shared CPU 1x / 512 MiB;
- runtime image
  `registry.fly.io/abrolia-control-plane-synthetic:runtime-phase-b-20260809-r3@sha256:56f8fd7ce3627eb655292ff2a946599cb462e4f981a739183ddee655c2a1e6ad`;
- profile `en`, `Europe/Prague`; manifest and activation receipt mode `0600`.

| Live drill | Observed evidence | Result |
|---|---|---|
| Invite replay and re-login | Both consumed invite tokens returned `401` on replay. A fresh invite for the owner selected the same household and preserved the exact profile. | **pass** |
| Fly cardinality | During activation the household had exactly one app, one Machine and one encrypted volume. Reconcile/restart did not duplicate any of them. | **pass** |
| Secret absence | The runtime secret namespace exposed only the DSAR secret name after activation; the bootstrap secret was removed. No secret value was written to manifest, API evidence, DB evidence or operator logs. | **pass** |
| Health and private DSAR | Public control-plane `/healthz` and `/readyz` returned `200`. Runtime `/readyz` returned `200`; authenticated control-plane-to-runtime DSAR over Fly 6PN returned `200` and the combined export became `complete`. | **pass** |
| Tamper fail-closed | Changing only the runtime manifest hash to zeros and restarting produced `/readyz=503`. Restoring the exact manifest, mode and owner returned `/readyz=200`; no downgrade was accepted. The planned control-plane `needs_attention` observation was not recorded. | **partial** |
| Cross-household isolation | A second owner session could not select household A via a query parameter; its current household remained B and its export contained no A identifier or state. This is not the plan's complete route inventory with foreign-owner `404` evidence. | **partial** |
| Export vs data map | Final export returned `200 complete`; `token_hash`, `lookup_hmac`, `request_ciphertext` and `session_hash` were absent. | **pass** |
| Delete and tombstone | Fresh re-auth delete returned `202 partial` while Fly converged, then tombstone became `complete`; the household, account, sessions, runtime app, Machine and volume became absent. No unused bootstrap token remained, but the destroyed raw bootstrap credential was not replayed after deletion. | **partial** |

The run exposed four live-only integration defects and closed each one on this
branch: a terminal secret-namespace failure previously left the email job waiting
forever; email retry did not create a fresh namespace intent; the runtime listened
only on IPv4 while Fly `.internal` resolves over 6PN IPv6; and app deletion omitted
the documented `force=true` query, leaving an empty app suspended. The deployed
control-plane head includes all four fixes. A one-year, organization-scoped deploy
token named `phase-b-control-plane-20260809` replaced the user token previously
embedded for Machines API calls; no token value entered this report or repository.

Provider rejection, namespace retry, app/volume/Machine creation, bootstrap
outcome-unknown recovery, activation restart and cleanup all converged without a
duplicate external resource. These are real provider-boundary recovery events,
but they are not represented as eight deliberately timed Fly `SIGKILL` injections.
The exact process-kill matrix remains the hermetic subprocess suite described
above; the canonical B-09 live outcomes are evidenced by the operator table.

Deletion cleanup also found one older reserved `.test` BYO-domain row created by
a previous provider-enabled rehearsal. With an authenticated backup at
`/data/phase-b-pre-legacy-cleanup.abrolia-backup` (mode `0600`), a one-pass Nerve
adapter recovery proved every recorded external object absent before completing
that tombstone. Final contour state is zero households, zero accounts, zero active
sessions, zero pending/unknown jobs, zero unused bootstrap tokens and three
`complete` tombstones. All temporary invite, session, script and log artifacts
were removed; the Fly org contains only `abrolia-control-plane-synthetic`.

**Automated gates on the final branch head:** full non-live suite **915 passed,
2 live tests deselected**; `tests/control_plane` **401 passed**; focused Fly,
email-identity and runtime regression set **86 passed**; Ruff pass; fixture scan
clean with the fail-closed private-deny path loaded by a synthetic canary; gitleaks
pass across **152 commits**; `git diff --check` pass.

**Cost tag:** billing is configured for `abrolia-synthetic`. Current public Fly
list prices put the retained always-on shared-cpu-1x/512 MiB control-plane Machine
plus one 1 GiB volume at approximately **US$3.34–3.47/month**, before snapshot
storage and network usage; household runtime resources used for the drill were
deleted.

**Gate status:** B-11 is complete, but Phase B remains open. This PR records a
successful paid-org household lifecycle and lands the correctness fixes exposed
by it; it does not close B-09. The missing evidence is the eight deliberately
timed Fly `SIGKILL` runs, a complete foreign-owner route matrix with the planned
`404` observations, control-plane `needs_attention` after runtime tamper, and
replay of a retained disposable bootstrap credential after deletion. These
claims must not be inferred from the equivalent live recovery events, partial
operator drills or hermetic matrix.

### Final B-09 gate closure — 2026-08-12

This section supersedes only the four incomplete observations in the preceding
gate-status paragraph. The paid-org migration, household lifecycle, restore,
cost and other drill evidence above remains unchanged. All commands used
synthetic reserved data and kept provider credentials out of argv and logs.

#### Live Fly SIGKILL matrix

The matrix ran from an isolated temporary control-plane contour in
`abrolia-synthetic`: app `abrolia-phase-b-live-matrix-20260812`, Machine
`d891e969b4d238` (`ams`, shared CPU 1x, 512 MiB), separate rootfs and SQLite
database, with no production volume mounted. Its scoped Fly credential was
streamed through `fly secrets import --stage`; its SHA-256 matched the operator
source, while the value never entered output, argv or this report.

Unlike the earlier Fly-hosted hermetic pytest run, this operator explicitly
started a new provisioning worker process for each durable boundary, waited for
the boundary checkpoint, sent that exact process `SIGKILL`, observed
`returncode=-9`, expired any abandoned lease and resumed the durable job against
the real Fly adapter. The recovery path created actual household apps, encrypted
1 GiB volumes and Machines from the pinned runtime image
`sha256:56f8fd7ce3627eb655292ff2a946599cb462e4f981a739183ddee655c2a1e6ad`.

| Durable boundary | Exercised assertion and observation | Result |
|---|---|---|
| transition → lease | Process killed after committing the email transition. Recovery produced one job result; runtime app `abrolia-hh-mk24pwb6t5h4xfilkojekuj5ja` had Machine `e82d1292f09e58` and volume `vol_vp2wo9ln3g097oy4` (counts 1/1). | **pass** |
| lease → provider | Process killed after leasing, before the provider call. Expired lease was reclaimed on attempt 2; app `abrolia-hh-3uhzi6z26vfcdlewv24hioion4` had Machine `801553b6023968` and volume `vol_42k6n0n3onlx9904` (1/1). | **pass** |
| provider → response | Process killed after durable provider acceptance but before returning its response. Inspect recovered without a duplicate; app `abrolia-hh-wdbpw3rdqjcpjpfbqfljapjy7i` had Machine `080d439f90e458` and volume `vol_4m319w9wd3lzk16v` (1/1). | **pass** |
| response → Sink | Process killed after receiving one-time secret material, before the Sink call. Retry settled once; app `abrolia-hh-g7pw46zwd5gmdfwrjlsr5bozjy` had Machine `683d122eb72018` and volume `vol_vgnd1y1ekwdkw214` (1/1). | **pass** |
| Sink → result | Process killed after `FlySecretSink.install`, before result projection. Live Sink proof recovered attempt 2; app `abrolia-hh-nun7fh4dwrejzk3wbdczi6y2qe` had Machine `8e2440f7de0418` and volume `vol_vxmydndxek201ox4` (1/1); the canary never appeared in output. | **pass** |
| result → claim | Process killed after the live runtime result committed. App `abrolia-hh-4up5tilwgzdndgaxbsr3eh54pi` retained exactly Machine `683e451b062968` and volume `vol_vp2wo9o3xynkglk4`; the bootstrap credential remained mode `0600` and bound to revision 1. | **pass** |
| claim → activate | Process killed after the control plane committed the claim. Exact replay returned the same revision and manifest SHA-256; no second app, Machine, volume, job result or config revision appeared. | **pass** |
| activate → cleanup | Process killed after activation committed but before acknowledgement/cleanup. Exact activation replay succeeded, created one bootstrap-cleanup job, and `FlySecretSink.contains` proved the bootstrap secret absent. | **pass** |

An independent evidence rerun used temporary runner Machine `830152f72923e8`
and the exact operator command below. The harness command shown in the second
column was executed once for each boundary after waiting for its durable
checkpoint; `kill worker` sent `SIGKILL` to that process and every observed
return code was `-9`.

```bash
fly ssh console -a abrolia-phase-b-live-matrix-20260812 \
  --machine 830152f72923e8 \
  -C 'python /tmp/abrolia-phase-b-live-matrix.py'
```

Expected for every row: the expired lease resumes to success, job IDs and
idempotency intents remain unique, the five stable external resources remain
unique, the secret canary is absent from SQLite, authenticated API output and
Fly application logs, and public `/healthz` plus `/readyz` both return `200`.
The DB counts below are scoped to the disposable household created for that
row; API bodies and logs were scanned before exact cleanup.

| Boundary | Per-window harness command | Jobs / distinct IDs / intents | External rows / distinct stable names | DB / API / Fly-log canary | Probes | Result |
|---|---|---:|---:|---|---|---|
| transition → lease | `kill worker; expire lease; resume; inspect Fly/DB/API/log/probes` | 5 / 5 / 5 | 5 / 5 | absent / absent (`200`) / absent | `200` / `200` | **pass** |
| lease → provider | `kill worker; expire lease; resume; inspect Fly/DB/API/log/probes` | 5 / 5 / 5 | 5 / 5 | absent / absent (`200`) / absent | `200` / `200` | **pass** |
| provider → response | `kill worker; expire lease; resume; inspect Fly/DB/API/log/probes` | 5 / 5 / 5 | 5 / 5 | absent / absent (`200`) / absent | `200` / `200` | **pass** |
| response → Sink | `kill worker; expire lease; resume; inspect Fly/DB/API/log/probes` | 5 / 5 / 5 | 5 / 5 | absent / absent (`200`) / absent | `200` / `200` | **pass** |
| Sink → result | `kill worker; expire lease; resume; inspect Fly/DB/API/log/probes` | 5 / 5 / 5 | 5 / 5 | absent / absent (`200`) / absent | `200` / `200` | **pass** |
| result → claim | `kill worker; expire lease; resume; inspect Fly/DB/API/log/probes` | 5 / 5 / 5 | 5 / 5 | absent / absent (`200`) / absent | `200` / `200` | **pass** |
| claim → activate | `kill worker; expire lease; resume; inspect Fly/DB/API/log/probes` | 5 / 5 / 5 | 5 / 5 | absent / absent (`200`) / absent | `200` / `200` | **pass** |
| activate → cleanup | `kill worker; expire lease; resume; inspect Fly/DB/API/log/probes` | 6 / 6 / 6 | 5 / 5 | absent / absent (`200`) / absent | `200` / `200` | **pass** |

The secret-free JSONL transcript's rolling SHA-256 is
`75237a2495756031fd063a4303b2af00fb63f08a7a580cb679d9fa41e307e5a9`.
The operator was the repository owner on 2026-08-12. A fresh authenticated
backup was created before the rerun so active `/readyz` observed `200` rather
than the unrelated `backup_stale` readiness condition.

Each of the six real household apps above was destroyed immediately after its
row passed and verified absent. The temporary matrix app and Machine were then
destroyed. Final `fly apps list --org abrolia-synthetic --json` showed only
`abrolia-control-plane-synthetic`; its original topology was unchanged: Machine
`85e649c449e9e8` started in `ams` at 1 shared CPU / 512 MiB, and encrypted 1 GiB
volume `vol_r1j6gpg2nq12x0zr` remained attached.

#### Final operator drill observations

| Drill | Command / observation (secret-free) | Result |
|---|---|---|
| Runtime tamper projection | A synthetic household `d2fcdf3a-d1be-49a5-8c9a-622c157566d8`, runtime `abrolia-hh-2l6n6owrxze2lde2miwbk5lg3a`, revision `1`, was activated. After replacing only its manifest hash with zeros and restarting, runtime `/readyz` returned `503`; `abrolia-control-plane runtime-health` returned `needs_attention`, and both the identity and receipt rows were observed as `needs_attention`. Restoring the exact manifest and restarting returned `active`. | **pass** |
| Complete foreign-owner route inventory | On 2026-08-12 the repository owner created two disposable verified accounts, two isolated owner memberships and two live sessions on `app.abrolia.com`. With A's authenticated cookie against B and B's against A, `GET /api/v1/households/<foreign UUID>/<suffix>` covered all 14 planned suffixes in both directions: 28/28 responses were `404`, all bodies had one uniform digest, aggregate digest `5cd5d0af77ef876b4eb640820fc2fb1a22845ac511a15fc1f2fc22beed2ca3d9`, and zero bodies echoed the foreign UUID. Both `/api/v1/onboarding/current?household_id=<foreign>` probes returned `200` without the foreign UUID. Before requests SQLite showed exactly two scoped membership rows and zero cross-memberships; `/healthz` and `/readyz` were `200`. Exact cleanup returned `complete` twice, left zero scoped account/household rows and two complete deletion tombstones. The command was `fly ssh console -a abrolia-control-plane-synthetic --machine 85e649c449e9e8 -C 'python /tmp/phase-b-live-idor.py'`; the temporary script and raw session material were removed immediately. | **pass** |
| Retained bootstrap replay after deletion | A live disposable household `3d7ebc3a-fd59-416a-b6c3-b9c6467f6a5e`, runtime `abrolia-hh-hv7lyox5lfawvnwdxhdem73kly`, revision `1`, retained its bootstrap credential only in `/data/.phase-b-retained-bootstrap` mode `0600`. After deletion converged, an exact replay returned `BootstrapDenied`; the operator result was `replay_cleanup_pass`, and the credential file was removed. The earlier run also proved a replacement household receives a different UUID while the complete tombstone remains. | **pass** |

The prior table's invite replay/profile, cardinality, secret absence,
health/private DSAR, export/data-map and paid-org lifecycle rows were already
passes. With the three rows above and the live Fly per-boundary matrix, no B-09
drill remains partial or skipped. Phase B (B-09 and B-11) is complete; Phase C
may branch from the merged Phase B head. Real-family-data and real-provider legal
gates remain closed and are not changed by this synthetic sign-off.

# Onboarding control plane runbook

Phase 1 is an invite-only, synthetic-only staging contour. It is not permission
to process real family content, connect a personal mailbox, or enable a real
email, WhatsApp, or channel adapter. The real Nerve email exception described
below is restricted to explicitly allowlisted operator-owned synthetic staging
households; it does not enable real family data.

## Topology and locked flags

Run exactly one control-plane Machine in `ams` with one local Fly volume mounted
at `/data`. `abrolia-control-plane serve` owns the SQLite writer lock and runs
the durable worker in the same process. Do not start a second API or worker
against that volume. Scaling beyond one Machine stops the rollout and requires a
Postgres migration plan; a shared/network SQLite filesystem is unsupported.

These flags are mandatory and fail closed:

```text
ABROLIA_SYNTHETIC_ONLY=1
REAL_FAMILY_DATA_ENABLED=0
ABROLIA_REAL_EMAIL_ENABLED=0
ABROLIA_REAL_WHATSAPP_ENABLED=0
ABROLIA_REAL_CHANNEL_ENABLED=0
ABROLIA_RUNTIME_PROVIDER=fly-runtime
ABROLIA_PUBLIC_ORIGIN=https://app.abrolia.com
ABROLIA_MAGIC_LINK_DELIVERY_ENABLED=0
ABROLIA_CONTROL_PLANE_DB=/data/control-plane.db
ABROLIA_FLY_ORG=<staging-org-slug>
ABROLIA_INTERNAL_BOOTSTRAP_HOST=abrolia-control-plane-synthetic.flycast
ABROLIA_RUNTIME_IMAGE=registry.example.invalid/abrolia/runtime@sha256:<published-digest>
```

The following values are Fly secrets, never config-file values or command-line
arguments: `ABROLIA_ENCRYPTION_KEY`, `ABROLIA_LOOKUP_HMAC_KEY`,
`ABROLIA_TOKEN_HMAC_KEY`, `FLY_API_TOKEN`, and the separately controlled
`ABROLIA_CONTROL_PLANE_BACKUP_KEY`. `ABROLIA_RESEND_API_KEY` is the dedicated,
sending-only key for public magic-link delivery; do not reuse Nerve's Resend
key. `ABROLIA_NERVE_ADMIN_KEY` is also a Fly
secret whenever the Nerve gate is configured. The three application keys are independent
32-byte urlsafe-base64 values. Record the active encryption version in
`ABROLIA_ENCRYPTION_KEY_VERSION`; retain an old field key until its rows have
been re-encrypted.

### Public magic-link delivery gate

The default remains the `.test`-only in-memory mailer. To stage production
delivery, configure `ABROLIA_MAGIC_LINK_FROM=Abrolia <login@abrolia.com>` as a
non-secret environment value and install a separate `ABROLIA_RESEND_API_KEY`
through the Fly secret store. The sender domain must already be verified in
Resend. Only then set `ABROLIA_MAGIC_LINK_DELIVERY_ENABLED=1` and deploy.

Enabling the gate sends the recovery email address and the one-time login URL
to Resend. The link expires after 15 minutes. Provider failures retain the
generic public `accepted` response and never include the address, link, API key,
or provider response body in an application error. Roll back by setting the
gate to `0`; this immediately restores `.test`-only delivery without weakening
the real-family provider gates.

The dedicated runtime has a separate, locked `HERMES_*` contract. Its Machine
environment contains `HERMES_HOUSEHOLD=/data/household.toml`,
`HERMES_ACTIVATION_STATE=/data/runtime-activation.json`,
`HERMES_DB=/data/hermes.db`,
`HERMES_REQUIRE_MANIFEST=1`, `HERMES_CONTROL_PLANE_URL` (the private
`http://abrolia-control-plane-synthetic.flycast` origin), `HERMES_RUNTIME_REF`,
`HERMES_HOUSEHOLD_ID`,
`HERMES_CONFIG_REVISION`, and `HERMES_CONFIG_SHA256`. The one-time
`HERMES_BOOTSTRAP_TOKEN` and the per-runtime `HERMES_RUNTIME_DSAR_TOKEN` are
staged as Fly secrets, never as Machine env in a checked-in file. The DSAR token
is a stable HMAC bound to the exact managed runtime ref; its value is not stored
in control-plane SQLite. Do not substitute similarly named `ABROLIA_*`
variables: the runtime rejects an incomplete or mismatched `HERMES_*` binding.

### Operator-only Nerve email gate

Keep `ABROLIA_REAL_EMAIL_ENABLED=0` for the normal synthetic contour. A Phase
2.4 live rehearsal may set it to `1` only after all of these values are present:

```text
ABROLIA_REAL_EMAIL_HOUSEHOLD_ALLOWLIST=<operator-household-uuid>[,<uuid>...]
ABROLIA_NERVE_BASE_URL=https://<nerve-control-plane-origin>
ABROLIA_NERVE_PLATFORM_ORG_ID=<platform-org-uuid>
ABROLIA_NERVE_PLATFORM_DOMAIN_ID=<platform-domain-uuid>
```

Install `ABROLIA_NERVE_ADMIN_KEY` through the secret store. Missing values, an
empty/invalid UUID allowlist, or a non-HTTPS Nerve origin abort startup. The
container then registers `nerve-managed` and `nerve-byo-domain`; a household
outside the allowlist is rejected before a provisioning job is created. Gmail,
WhatsApp and primary-channel real adapters remain disabled.

Each email connection uses a lifecycle-scoped Nerve org reconciliation key:
`arbolia:household:<household_id>:email:<email_identity_id>`. Cleanup tombstones
that org permanently. Retrying the deleted identity must continue to conflict;
reconnecting the same household creates a new email identity and a fresh org.
Never clear `deleted_at` or reuse the old org, because retained history belongs
to the old credential boundary.

Use a dedicated staging organization when one is available. If the pilot is
temporarily hosted in a shared personal organization, dedicate the app names,
Machines, volumes, secrets and org token to the synthetic contour, record that
exception in the validation report, and migrate before enabling any real data or
provider.

## Build and deploy

1. Build and publish `deploy/runtime/Dockerfile`; record its immutable digest.
2. Set the digest in the control-plane secret/config, never a mutable tag.
3. Create `control_plane_data` in `ams` and deploy
   `deploy/control-plane/fly.toml` with one Machine.
4. Set secret values through `fly secrets import` or the Fly secret store. Avoid
   `fly secrets set NAME=value` in shared shell history.
5. Confirm `/healthz` and `/readyz`, then inspect the effective environment and
   verify every real-provider flag is `0`, unless executing the documented
   operator-only Nerve rehearsal. The probes report DB and volume
   availability, worker pause/backlog/stale leases, unknown outcomes, expired
   bootstrap records, provider adapter status, and backup age without paths or
   credentials. `/readyz` is non-200 for an operational blocker; a normal
   pending job alone does not remove the API from service.
   Fly routing checks use `/healthz`; `/readyz` remains an operator/release
   signal so an `outcome_unknown` does not make the only reconciliation surface
   unreachable.
6. Point the app subdomain at the control plane only after the auth/security
   suite is green. The static landing remains on its own deployable and retains
   `connect-src 'none'`.

The runtime adapter creates, in order, one deterministic app, one encrypted 1 GiB
volume in `ams`, then one 1 shared-CPU/512 MiB Machine mounted at `/data`. It
inspects the stable names before every create. HTTP `409` causes inspection;
`422` is a definite rejection; `429` is a bounded retry; network/5xx is
`outcome_unknown` and requires reconcile.

## Synthetic invite and onboarding

Only reserved `.test` recipients are accepted. With the API stopped (so the
single-writer lock can be acquired), an operator can generate one link in an SSH
console:

```bash
abrolia-control-plane invite owner@example.test
```

The link token is displayed once to that operator, stored only as a hash, and
expires after 15 minutes. Do not paste it into tickets, chat, screenshots, or
logs. Start the service again, open the link, and verify that a replay fails.

Complete profile preflight with an IANA timezone, then the three choices in
order. The default synthetic path is managed `@abrolia.com`, shared WhatsApp
Beta, then Telegram. Reload after each step: the server-side workflow version
and durable job—not browser state—must determine the resumed screen.

Useful redacted operator commands (run with the API stopped unless executed in
the same Machine console under an explicit maintenance window):

```bash
abrolia-control-plane jobs
abrolia-control-plane worker --limit 20
abrolia-control-plane reconcile <exact-job-id>
abrolia-control-plane retention
```

`reconcile` is required for `outcome_unknown`; never use `worker` as a blind
retry. Cleanup and deprovision act only on exact registry IDs, never globs or
prefix scans.

## Bootstrap and readiness

### Gmail operator rollout

Gmail has an independent connect gate. Keep `ABROLIA_GMAIL_REAL_ENABLED=0`
for synthetic households; allowlisted operator accounts may exercise OAuth with
`ABROLIA_GOOGLE_OAUTH_TEST_USERS` while the family-data gate remains closed.
The public switch may be enabled only when all four evidence flags are `1`:
verified OAuth app, approved restricted Gmail scopes, current CASA assessment,
and the in-product Google Limited Use disclosure. Turning Gmail off blocks new
connections; inspect, exact revoke, cleanup, export, and deletion remain
available.

Use only a separate agent mailbox. The consent screen must request exactly
`openid`, `email`, `gmail.readonly`, and `gmail.send`. A recovery/personal
mailbox match, an extra or missing scope, stale workflow, session/household swap,
or late callback is a failed connection and must start from a new OAuth state.
Never retry an authorization code.

Runtime health exposes only enum/timestamp signals. Alert when Gmail health is
`auth_revoked`, `needs_attention`, or `stale_cursor` (three missed 60-second
poll intervals); alert independently on Nerve webhook lag/DLQ, repeated
credential revocation, address reservations that outlive terminal identities,
and control-plane `outcome_unknown`. Metrics and alerts must not carry mailbox
addresses, subjects, Message-IDs, or content.

Rollout order is fixed: synthetic fixtures → allowlisted operator Gmail →
invited pilot families. Before each promotion prove receive, approved send,
restart/cursor resume, exact Sent reconciliation, reconnect, export, revoke, and
delete. A Gmail History gap that exceeds bounded overlap is `needs_attention`,
never a silent baseline. A revoke/delete timeout remains unknown and blocks
resource tombstoning. Restores may recover encrypted grant rows and cursors, but
must not be considered usable without the dedicated Fly secret namespace.

If Gmail is disconnected before the household runtime Machine is launched, the
control plane creates a deterministic one-shot `abrolia-google-revoker-*`
Machine in that exact household app. It runs the pinned runtime image with no
volume or service, inherits the staged grant from the app secret namespace, and
is deleted only after Fly reports a clean exit. A timeout, non-zero/unknown exit,
Machine drift, or unconfirmed deletion remains `google_revoke_runtime_unavailable`;
do not delete the app secret or mark the identity revoked until reconciliation
returns absent.

Managed Nerve email has a separate attachment-readiness gate. Provisioning
creates the household org, domain grant, inbox, runtime key and signed webhook,
then probes `GET /internal/feature-flags/attachments` with that household key.
The email step remains `waiting_user` with
`readiness=attachments_flag_pending` until the effective response is exactly
`enabled=true` for the expected Nerve org. A timeout, 5xx, malformed response,
wrong org, or disabled value never marks the household ready.

The operator takes `nerve_org_id` from the redacted step status and runs the
existing audited writer inside the Nerve control-plane Machine:

```bash
flyctl ssh console --app nerve-control-plane --command \
  'env NERVE_FLAGS_ACTOR=<operator-id> /app/nerve-flags set attachments --org <nerve-org-uuid> --enabled=true'
```

Record the returned `replay_id`. Wait at least 65 seconds (`2 x 30s` cache TTL
plus margin), then use the onboarding **Check** action. Check recovers the
one-time household credentials, repeats the effective-state probe as the
household principal, and completes the step only after convergence. Do not use
the bootstrap admin credential to bypass this probe, and do not enable the
global default.

The control plane stages `HERMES_BOOTSTRAP_TOKEN` into the expected runtime
namespace. The runtime uses it as a bearer credential for two calls:

Flycast is HTTP-only and has no public TLS certificate. The `.flycast` request
therefore uses plain HTTP inside Fly's encrypted private WireGuard network, with
the exact one-time bearer and runtime binding still mandatory. The runtime HTTP
client accepts this exception only for a bare `*.flycast` hostname; every other
bootstrap origin remains HTTPS-only.

1. `POST /internal/v1/bootstrap/claim` with the expected household, runtime ref,
   and revision. A crash may repeat the same claim and receive the same manifest.
2. Atomically write `/data/household.toml` with mode `0600`, validate its schema,
   UUID, revision, hash, timezone, binding, and email separation, then call
   `POST /internal/v1/bootstrap/activate` with the applied hash. This marks the
   revision active but does not yet authorize secret cleanup.
3. Fsync `/data/runtime-activation.json`, then replay the exact activate request
   with `X-Hermes-Runtime-Receipt-Acknowledged: true`. Only this durable ack
   creates the bootstrap-secret cleanup job.

Activation marks household/config active only on an exact match. If either HTTP
response is lost, the corresponding exact activate/ack replay returns the same
receipt with `200`; cleanup remains absent before the ack and unique after it.
A restart with a locally active receipt and a still-present bootstrap token
retries only the ack. A subsequent `claim` with that used token returns `410`;
a changed activation binding/hash is rejected. `/healthz` may be green during
bootstrap; runtime `/readyz` must remain non-ready and listeners/model/ingress
stopped until the active receipt matches the file.

The same private runtime surface exposes authenticated
`POST /internal/v1/dsar/export` and `/delete` on the managed app's Fly
`.internal` address. Only the exact per-runtime HMAC bearer is accepted. Delete
writes the runtime tombstone and a mode-`0600` deletion marker before control
plane deprovisions the exact Machine, volume and app; repeated delete returns
the same proven `absent` state.

## Crash and release checks

Exercise every window from the Phase 1 plan: before/after lease, provider
acceptance, result projection, revision issue, claim, atomic rename, activation,
and secret cleanup. Resource counts must remain one. Also run the account ×
household IDOR matrix, missing/wrong Origin and CSRF matrix, stale version and
idempotency matrix, secret canaries, export/delete, and the isolated restore
rehearsal in [`control-plane-restore.md`](control-plane-restore.md).

Do not enable real providers or real family data after a successful synthetic
smoke. Each real integration has its own legal, processor, OAuth/CASA, consent,
and implementation gate.

## Observability and alerts (Phase E)

Logs are JSON only: `timestamp, level, household_id_hash (HMAC), request_id, route, status, latency_ms` — no message content, raw email, prompt, or secret. See `hermes_cloud/core/observability.py` and `control_plane/observability.py`.

`GET /health` (public runtime) reports `nerve_key_ok, telegram_ok, wa_instance_ok, google_grant_ok, db_ok, backup_age_hours`; `backup_age_hours > 30*24` → `needs_attention`. `GET /healthz` / `GET /readyz` (control plane) report `database, volume, workers, backup, providers` and blockers.

Alerts (operator-visible, not auto-page in pilot):
- `DLQ > 0` — provisioning jobs `failed` without reconcile
- `sticky executing` — job `running` > 10 min without lease renewal
- `primary unavailable` — routing fallback triggered
- `backup stale` — `backup_age_hours > 26h`
- `budget exceeded` — per-household/day cost cap hit (see `hermes_cloud/core/usage.py`)

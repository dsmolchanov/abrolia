# Validation and Remediation Report: Abrolia Phase 2 — Family Email Identity

**Plan:** `thoughts/shared/plans/2026-08-04-abrolia-phase-2-email-identity.md`
**Validated and remediated:** 2026-08-05
**Original Abrolia baseline:** `a5dd303c6a2170d09bb5baa9c1e570e27fbd967f`
**Phase 2.4 baseline:** `bc1b3cf`
**Original Nerve baseline:** `2b8608f486f8117ded53f49e0551a058e4ebe0d4`
**Latest deployed Nerve main:** `99092688c3213af3cf7dc8e72cc28bd89983f6a1`
**Original remediation worktree base:** `ff93f31`
**PR integration base:** `f3d8c8f` (`origin/main` after Nerve attachment readiness)
**Scope:** Phase 2.0–2.4; Phase 2.5 runtime-email work is explicitly excluded
**Current status:** the production wiring, generation-safe cleanup/reconnect,
bounded BYO-domain polling, V-01 tenant isolation, and operator-owned live-DNS
gates are **accepted and deployed**. Phase 2.4 is complete.

> The path in the original request used `Programs/arbolia`; the active repository
> is `Programs/abrolia`. Phase 2.5 advanced the shared checkout while this work
> was running. No `hermes_cloud/**`, runtime-email, landing, or Phase 2.5 plan
> files were edited by this remediation.

The production rehearsals used only the allowlisted synthetic household
`06137360-ec42-4c93-9e11-f66551f27681` and reserved recovery address
`owner@example.test`. It mutated live Nerve and Abrolia provider state, but used
no real family data. The final BYO rehearsal published records only below the
operator-owned disposable subdomain `test.axiomatlas.llc`.

## Operator-owned live-DNS closure — 2026-08-05

The remaining Phase 2.4 gate passed against production Nerve and Abrolia:

- Namecheap BasicDNS published the exact six provider records below
  `test.axiomatlas.llc`: inbound MX and SPF, `send` MX and SPF, DKIM, and DMARC.
  Google Public DNS returned all six authoritative values after propagation.
- First identity `ca113cc4-538c-42bf-9c3b-8d7b97a25e4a` used durable inspect job
  `c219963d-bc64-4bd6-ab91-b77e886f96d9`. It stayed `waiting_user` through four
  checks, then the same job reached `succeeded` on attempt five at
  `2026-08-05T22:39:28Z`; the identity advanced once to `verified`.
- The verified graph was org `494ae0a0-93a6-4557-a154-45200b4e2d62`, domain
  `7a859589-8739-4487-abd1-a87f94b38021`, inbox
  `f05e056b-33e5-418c-8abe-41440a8287f5`, key
  `cf903550-77cb-4948-a113-1e9b8f0c01fa`, and webhook
  `b56e59f3-d8df-4584-b8fb-29c63bca87c3`.
- A maintenance login and two consecutive onboarding reads returned the same
  verified snapshot. No raw magic-link token or session value was logged.
- Reset left the DNS records in place. Cleanup job
  `f85095e5-dae0-4325-b3a8-f76465f3a182` succeeded on its first attempt at
  `2026-08-05T22:41:06Z`, and the first identity became `deleted`.
- Reconnecting the same household and domain created identity
  `6b73a64c-b595-44b1-9852-53e01b6a8a73` and durable inspect job
  `43b658e5-8c45-41cc-beaf-5b73cef3ff21`. The job succeeded on attempt three at
  `2026-08-05T22:43:33Z` and the identity became `verified`.
- The reconnect graph is wholly generation-distinct: org
  `82062f74-8a2c-4bd9-b42c-ac4b9cbda19d`, domain
  `4c8d9265-a253-4e77-aff5-3b132f8d19be`, inbox
  `bf978c48-5cc7-49f3-b7e9-2a16affae82f`, key
  `77545070-fc67-4888-9426-03f22d0ad587`, and webhook
  `b489ac1b-0c91-4cc8-aed9-c01972d6fd28`.
- Abrolia release 18 restored the normal API on the unchanged immutable image
  digest `sha256:2aa6100eda56f3cce124e4cf928519b3ca1fee74d6b8a66d317b71868604c92f`.
  Its Fly service check and `/healthz` pass with workers running.

The live run observed the real provider remain pending while records were
missing or not yet recognized, followed by the complete record set. It did not
retain a separate snapshot of one exact partial-record combination; partial
record-level behavior remains covered by the hermetic matrix exercising the
same pending projection. This evidence limitation does not weaken the live
provider, persistence, cleanup, or reconnect results.

## Production wiring and reconnect closure — 2026-08-05

The two blockers discovered after Nerve PR #64 are closed in production:

- Abrolia PRs #15, #17 and #18 register the Nerve providers behind the
  fail-closed allowlist, preserve runtime cleanup references, and converge
  resets that originated on the legacy synthetic provider. Abrolia release 12
  is healthy on image digest
  `sha256:d2762fdba1e272257966adfb71b9813e8ebb8a768feb4dc1ffa0003d041394cc`.
- Nerve PR #66 retains each generation-scoped org `external_ref` on the deleted
  org tombstone. Production release 68 is healthy on image digest
  `sha256:693aefba5d6fa319523bf2d8215b9c568f0054cc1fb5da9d706493edd46ea6a4`.
- Generation A (`a51cb145-63fb-4277-96eb-ef2e28e4907f`, org
  `70d15c36-3ab4-40d6-9c58-ae35744d8db5`) provisioned, verified and cleaned
  up. Its webhook, key, inbox and domain grant were removed/revoked in order.
- A diagnostic replay made before PR #66 exposed the old release behavior and
  created empty orphan org `b32fea34-bb3d-4527-acc5-9ab6805d59f9`. After release
  68, the rehearsal verified that all five child collections were empty,
  tombstoned the orphan, observed the retained generation-A reference, and
  received HTTP 409 from a delayed ensure using that reference.
- Generation B (`2265aaea-83a3-4138-bf54-2af0a4909bb9`) created fresh org
  `d9509937-6ac0-4e6f-9788-981d3e8c0cd0` and inbox local-part
  `phase24-reconnect-b` on the `abrolia.com` platform domain; it did not revive
  generation A. The
  org-scoped attachment flag was enabled with audit replay
  `bd4ccb08-9a94-4fae-88be-b67647f27673`, after which the identity reached
  `verified` at workflow version 17.
- The persistence rehearsal logged out the original session, issued a bounded
  maintenance login link without printing its token, consumed it through the
  public API, loaded the onboarding state twice, and observed the same verified
  generation-B org. It then logged out and confirmed the session returned 401.
- Legacy job `b9260b08-27b8-42dc-80e2-34df52690d1b` was changed from
  `outcome_unknown` to `cancelled` only after replacement job
  `36d86b13-1987-47d0-80f8-6470be02a55f` was `succeeded` and its resource was
  `deleted`.

This closes the production composition and same-household reconnect blockers.
It does not substitute a managed-domain rehearsal for the Phase 2.4 BYO-domain
DNS acceptance matrix.

## Bounded polling and tenant-isolation closure — 2026-08-05

- Abrolia PR #21 turns the original durable BYO provisioning job into the
  inspection job, leases it only when `not_before` is due, polls at
  30/60/120/240-second intervals, and stops after five total attempts while
  retaining manual `CHECK`. Main `261a35f0d71b66159aea2036501d6cad9381e104`
  passed the complete non-live CI matrix and is deployed as production release
  15 on image digest
  `sha256:82134117d06cbd53841850e7511c08385b97d872e55fed2874fb59f7b9cc699c`.
- Nerve PR #67 resolves `/v1/tokens/service` through the authenticated tenant
  before issuance. Its negative cross-org and positive own-org tests passed in
  the complete Go suite. Main `99092688c3213af3cf7dc8e72cc28bd89983f6a1`
  is deployed as production release 69 on image digest
  `sha256:7406baea1ea524f0b9de43ff4e5d40ae9d407f3e316f70f10dafa44851229d08`.
- PR #22 makes runtime cleanup ignore Fly's historical `destroyed` Machine
  records so exact volume deletion can proceed. Current main
  `168d25c20c27b80bcda031210c40f41751a2c7b4` is deployed as production
  release 16 on image digest
  `sha256:2aa6100eda56f3cce124e4cf928519b3ca1fee74d6b8a66d317b71868604c92f`.
- Post-deploy `/healthz` and the Fly service check pass. Generation B remains
  `verified`. `/readyz` remains 503 solely because the previously documented
  cleanup job `6a0d040d-bfb9-4671-b2c5-e6ee681bdc6b` is still
  `outcome_unknown`; the fix is deployed, but this quarantined job requires an
  explicit operator reconcile and predates the email release.
- No authoritative DNS credential is present in the local environment,
  repository secrets, Abrolia Fly secrets, or the available Route 53 account.
  The visible operator-owned domains use third-party authoritative name
  servers. A domain must therefore be nominated and DNS access supplied before
  the live mutation matrix can be run safely.

## Executive verdict

| Item | Current result | Release meaning |
|---|---|---|
| V-01 — cross-org Nerve service-token issuance | **Fixed, tested and deployed** | Nerve PR #67 binds requested org IDs to the authenticated tenant; production release 69 is healthy |
| V-02 — false verification after one-time-secret loss | **Safety fixed / convergence partial** | False verification is eliminated; crash-after-install still needs a durable non-secret receipt/inspection contract |
| V-03 — provider secret in allowed values | **Fixed locally for durable/public email paths** | Provider outputs are typed, credential-shaped values are rejected, and external error codes are normalized |
| V-04 — expired hold reported claimable while identity is live | **Fixed fail-closed locally** | Availability stays false until the owning identity is terminal; automatic TTL ownership transfer remains deferred |
| Phase 2.4 — family-owned domain | **Accepted and deployed** | Operator-owned DNS, bounded polling, persistence, cleanup with DNS retained, and same-household reconnect pass in production |

The fixes and live rehearsals restore the cleanup/reconnect safety boundary and
close the BYO-domain Phase 2.4 gate. They do not make the Gmail or broader
real-family-data rollout ready.

## Remediation applied

### V-01 — fixed and deployed in `nerve-cloud`

Nerve PR #67 routes `/v1/tokens/service` through the same authenticated-tenant
organization resolver as the other tenant-scoped handlers. The regression
matrix proves that organization A receives a denial for organization B and can
still issue for its own organization. The full Go suite passed and production
release 69 is healthy on the immutable digest recorded above.

### V-02 — false success is fixed; receipt recovery remains open

The common worker now enforces all of the following before durable verification:

- a result that declares `secret_binding_ref` but has no recoverable material
  remains `secret_handoff_unknown`;
- non-empty `SecretMaterial` must contain exactly the declared binding name;
- typed public binding and selected mailbox identity are validated before the
  external secret sink is called;
- a hard lease reclaim cannot turn a consumed one-time secret into
  `succeeded`/`verified`;
- cancellation during secret installation compensates both the provider
  resource and the staged email secret;
- a failed compensation creates a durable late-secret cleanup, and successful
  retry terminalizes the parent job, identity, and address reservation;
- provider cleanup cannot terminalize the identity while that secret cleanup is
  pending, so reconnect cannot expose a replacement binding to a stale delete;
- disconnect completion and reservation release are scoped to the original
  identity rather than every identity/reservation in the household;
- a temporarily missing secret namespace still produces a durable cleanup
  intent instead of silently dropping the required deletion;
- confirmed namespace/app absence is accepted as proof that a binding secret is
  absent, while an unproved missing namespace remains `outcome_unknown`;
- identity release waits for every durable email provider resource, not merely
  the first cleanup job, and terminalizes the original cancelled job regardless
  of whether provider cleanup or secret cleanup finishes first;
- release also waits for every unresolved email job and binding-secret cleanup;
  cancellation before the provider call and definitive rejection without a
  resource can release, while ambiguous/malformed/late outcomes stay quarantined;
- a late `waiting_user` response that arrives during cancel/reset persists its
  exact provider reference and compensation job instead of losing the cleanup;
- late rate-limit, safe-retry, and provider-degraded signals cannot turn a
  cancel/reset-quarantined `outcome_unknown` job back into leaseable `pending`;
- runtime and namespace cleanup are ordered and execution-gated behind email
  provider and binding-secret cleanup, preventing an app delete from making the
  required secret unset permanently uninspectable;
- reset removes the runtime Machine/volume through a dedicated idempotent
  operation while preserving the Fly app secret namespace; full cancellation
  then deletes that namespace as the final cleanup step;
- reconcile-side provider errors and repeated transport failures settle or stay
  explicitly reconcilable instead of escaping and leaving the job leased.

Regression coverage includes a real subprocess `SIGKILL` after the simulated
one-time provider response, binding/material mismatch, cancellation during
installation, failed-then-retried compensation, reversed provider/secret
cleanup ordering, and all six `EmailFailureKind` categories on normal execution
plus reconciliation.

This closes the original false-success defect, but not the full crash-convergence
criterion. A crash after a successful sink write but before durable projection
still has no generation-specific non-secret install receipt or sink-inspection
proof. It therefore remains `secret_handoff_unknown` and requires operator
reconciliation; it never becomes unproved `verified`.

### V-03 — durable/public provider output is constrained

The remediation adds layered protection:

- known Anthropic, OpenAI, Nerve, Google, Telegram, GitHub, AWS, Slack, and
  private-key shapes are rejected in durable values, not only forbidden keys;
- opaque high-entropy values are rejected in error/reference/subject/scope
  channels;
- JSON-encoded reference strings are recursively scanned rather than treated as
  opaque scalar text;
- `secret_binding_ref` is an uppercase secret name, not an arbitrary value;
- provider adapters declare their public provider identity; the worker rejects
  a Nerve adapter claiming a synthetic result (and the reverse);
- each provider has an exact binding/scopes contract, so an arbitrary reserved
  runtime secret name cannot be overwritten or later deleted by email cleanup;
- email public results use an `extra="forbid"` typed binding with
  provider-discriminated reference models: synthetic identity refs and both
  Nerve graphs have exact key sets, while real Nerve resource IDs must be
  canonical UUIDs;
- successful email external references must match the selected identity,
  household, stable intent, address and typed provider refs;
- JSON external references must use the exact canonical encoding, preventing
  duplicate-key or lexical payloads from disappearing during validation and
  then surviving in durable state;
- BYO DNS public status and records are typed separately, so legitimate long
  public DNS values remain representable without opening arbitrary result keys;
- initial wait, manual inspect and reconciliation all accept only an empty
  synthetic wait or the typed BYO DNS result for the selected domain;
- unknown provider error codes become the fixed local `provider_rejected` code
  before DB/API/log projection;
- malformed or ambiguous provider results become `outcome_unknown` and block
  reconnect, while a definitive provider rejection remains terminal;
- telemetry rejection falls back to fixed safe fields and cannot change a
  durable command result.

The global gitleaks stopword exception was removed. The three historical
synthetic findings are now suppressed only by exact commit/path/rule/line
fingerprints in `.gitleaksignore`; the full-history scan remains green.

### V-04 — availability and claim state now agree

`address_available` now checks the live identity HMAC before considering an
expired reservation. An expired hold is therefore not advertised as claimable
while the original identity still owns the unique address.

This is deliberately fail-closed: TTL expiry does not silently cancel a
provisioning or ambiguous provider operation. A future lifecycle operation may
atomically terminalize a provably unstarted identity and release its hold, but
that is not inferred from time alone.

## Additional Phase 2.2 corrections

- `EmailProvisionIntent.selection` is parsed through the discriminated
  `EmailSelection` union and must agree with `EmailOption`.
- The six-category `EmailProviderError` taxonomy is handled by both normal
  execution and reconciliation.
- Clean `import control_plane.models` no longer enters the email
  model/repository import cycle; package exports are lazy and covered in a fresh
  interpreter.
- Real-domain validation rejects `.test`, malformed labels, whitespace,
  underscores, and other provider-unsupported characters while the synthetic
  gate still permits reserved `.test` fixtures.

Configuration-backed operational aliases are still not implemented. The fixed
canon aliases remain enforced; extending aliases is a non-blocking follow-up,
not a reason to weaken the current reserved set.

## Phase 2.4 validation

### Planned changes

| Planned change | Result | Evidence and remaining gap |
|---|---|---|
| 1. IDNA/domain policy | **Pass locally** | Unicode labels are IDNA-canonicalized; provider-compatible ASCII labels, public suffixes, Abrolia/reserved names and real-vs-synthetic `.test` gates are covered. IDN TLDs are rejected locally because current Nerve rejects their punycode form |
| 2. Subdomain recommendation and MX warning | **Partial** | UI calls the guidance endpoint, shows `recommended_domain`, and requires apex acknowledgement; it conservatively warns for every apex rather than querying existing MX state |
| 3. Durable typed DNS result | **Pass locally** | A non-empty typed record set retains type, host, value, priority, purpose and required flag; server and JS render exact records and record-level status after reload |
| 4. Bounded poll/inspect backoff | **Pass locally** | The durable job becomes an `inspect` intent, leases only when `not_before` is due, backs off at 30/60/120/240 seconds, stops after five total attempts, and retains manual `CHECK` |
| 5. Create inbox/key/webhook only after verification | **Partial** | Hermetic path advances once and never creates an inbox while DNS is pending; lost original-and-recovery key response remains unproved |
| 6. Ordered cleanup with explicit unknown | **Pass live for managed and BYO paths** | Nerve PR #64 made the graph deletable and PR #66 retained generation tombstones; managed and operator-owned BYO cleanup/reconnect passed in production |

### Acceptance criteria

| Criterion | Result |
|---|---|
| Reload/login resumes the same DNS records and state | **Pass live** — production login and two reloads retained the same verified `test.axiomatlas.llc` identity and DNS state |
| Wrong/partial DNS waits; verified DNS advances once | **Pass live with noted evidence limit** — the provider stayed pending while records were missing/unrecognized and advanced once after the complete set; a specific partial combination is covered hermetically but was not separately snapshotted live |
| One canonical domain cannot be claimed by two households | **Pass locally** — domain HMAC uniqueness, legacy-row fallback, address/domain binding, and different-local-part collision tests pass; there is no claim-specific two-connection concurrent-writer test |
| Delete/reconnect covers DNS present, provider unavailable and lost response | **Pass** — BYO cleanup and reconnect passed with DNS still published; provider-unavailable and lost-response branches remain covered by the hermetic failure matrix |

### Phase 2.4 issues fixed during this review

- malformed labels and real-gate `.test` acceptance;
- direct root-module import cycle;
- missing UI rendering for DNS priority/purpose and record-level status;
- missing UI use of domain guidance/recommended subdomain;
- full-domain ownership rather than full-mailbox-only uniqueness;
- provider-returned mailbox drift outside the selected/claimed domain;
- reconnect while prior cleanup is unknown;
- repeated provider outage during reconcile escaping as an exception;
- malformed DNS provider payload stranding a leased job;
- provider/secret cleanup ordering that could otherwise release an identity
  before a late fixed-name secret deletion completed;
- IDN-TLD compatibility drift between local validation and Nerve;
- empty DNS instructions that left the owner with no actionable record;
- manual-inspect response loss reconciling with the inspect job key rather than
  the original provider stable reference;
- provider provenance, exact binding/scope enforcement, and canonical
  duplicate-key-safe external references;
- runtime/app cleanup overtaking email provider and binding-secret cleanup;
- multiple historical email resources allowing the first cleanup to release
  the identity before all provider obligations were deleted;
- late rate-limit/safe-retry responses reopening a cancel/reset-quarantined job;
- runtime reset deleting the shared Fly secret namespace and leaving its durable
  row falsely `ready`; reset now preserves the app, while cancel removes it last.

### Remaining Phase 2.4 blockers

None. Public delivery of repeated login links remains a later production-mailer
rollout concern; the bounded maintenance login used here is sufficient for the
Phase 2.4 persistence criterion.

## Automated verification

### Phase 2.4 production-wiring/reconnect remediation (2026-08-05)

- `ABROLIA_REAL_EMAIL_ENABLED=1` now requires an HTTPS Nerve origin, bootstrap
  admin secret, canonical platform org/domain UUIDs, and a non-empty canonical
  household UUID allowlist. Real family data, Gmail, WhatsApp and primary-channel
  gates remain closed.
- Production composition registers `nerve-managed` and `nerve-byo-domain` and
  routes only allowlisted households to them.
- Nerve org identity is now scoped to the immutable email identity generation:
  `arbolia:household:<household_id>:email:<email_identity_id>`. The worker
  validates that exact value before persisting any provider result or wait state.
- Hermetic lifecycle coverage proves that cleanup tombstones generation A,
  replaying A remains rejected, and reconnecting the same household as generation
  B receives a distinct org. No Nerve tombstone is restored or reused.
- The managed Nerve cleanup/reconnect and logout/login/reload rehearsal passed
  in production. The operator-owned BYO DNS rehearsal subsequently passed on
  `test.axiomatlas.llc`.

| Check | Current result |
|---|---|
| Nerve PR #66 CI | **pass** — cloud-e2e, go-checks, unit, integration, lint, coverage, SDK, dashboard, exact mirror and vulnerability checks |
| Nerve production release 68 | **pass** — digest-pinned image, service check healthy |
| Nerve PR #67 and production release 69 | **pass** — cross-org service-token request denied, own-org request preserved; immutable digest healthy |
| Abrolia PR #21 CI | **pass** — complete non-live suite, lint, fixtures, contracts and secret scan |
| Abrolia production release 18 | **pass** — current main is deployed on the unchanged immutable digest; Fly service check and `/healthz` are healthy; the BYO reconnect identity is verified |
| Managed generation A cleanup and old replay | **pass** — empty diagnostic orphan tombstoned; delayed generation-A ensure returned 409 |
| Managed generation B reconnect | **pass** — fresh identity/org/inbox verified at workflow version 17 |
| Logout/login/two reloads/logout | **pass** — 200, stable verified state, 204, then 401 |
| `python3 -m pytest -q -p no:cacheprovider tests/control_plane` | **pass** — 349 tests |
| `python3 -m pytest -m "not live" -q -p no:cacheprovider` | **pass** — 824 tests after rebasing production-wiring/reconnect remediation over Gmail lifecycle main |
| `python3 -m pytest -q -p no:cacheprovider tests/control_plane/email` | **pass** — 93 tests |
| Phase 2.4 focused matrix (domain policy, BYO adapter/API, UI and DB) | **pass** — 47 tests; 10 + 15 + 2 + 15 + 5 |
| Production wiring + Nerve managed/BYO + Google OAuth regression matrix | **pass** — 60 tests |
| Final lifecycle/security regression matrix | **pass** — 124 tests; no additional local P0/P1 finding remained in the final read-only audit (V-01 remains upstream) |
| `ruff check control_plane tests/control_plane` | **pass** |
| `python3 scripts/check_fixtures.py --all` | **pass**; private deny patterns are unavailable locally, as expected |
| `gitleaks detect --source . --config .gitleaks.toml --log-opts=--all --redact --verbose` | **pass** — 60 commits scanned, no leaks |
| `git diff --check` on remediation paths | **pass** |
| CI for immutable Phase 2.4 commit `bc1b3cf` | **pass**, but predates these uncommitted remediation regressions |

The repository has no configured coverage backend, so this report does not
claim a coverage percentage.

## Historical findings retained for audit

At original baseline `a5dd303`, the isolated probes demonstrated:

- **V-01:** organization A received HTTP 200 when requesting a delegable billing
  token for organization B;
- **V-02:** an expired running lease reclaimed a ready provider resource,
  produced `succeeded`, and marked the email identity `verified` although the
  one-time secret had never reached the sink;
- **V-03:** a synthetic Google-shaped secret value survived telemetry,
  manifest, provisioning-job, and onboarding API paths when placed in allowed
  fields;
- **V-04:** availability returned true after TTL expiry, followed by a SQLite
  live-address uniqueness failure on the advertised claim.

The normal-path baseline suites were green, which is why the new negative,
hard-process-loss, cross-household, malformed-provider, and cancellation tests
are part of the remediation rather than relying on the previous suite alone.

## Manual release gate result

The operator-owned live-DNS gate passed on `test.axiomatlas.llc`. Broader
real-family-data, Gmail, WhatsApp and primary-channel gates remain independently
closed; Phase 2.4 acceptance does not activate them.

## Phase C3 Addendum — 2026-08-08 (Gmail History/Send/OAuth, B-06)

**Branch:** `codex/phase-C3-gmail` stacked on `codex/phase-C2-byo-live`.
Synthetic-only unless the Phase B/C2 live gates and Google verification/CASA
are complete. Phase A remains required before any real-family enablement.

**Hermetic coverage (no live Gmail):**
- `pytest tests/test_gmail_api_send.py -q` — **4 passed** (base64url RAW, `rfc822msgid:` single-match success, timeout→`outcome_unknown` no blind resend)
- `pytest tests/test_gmail*.py -q` — **34 passed** (History cursor, bounded resync, INBOX filter, OAuth PKCE/state, `gmail_real_enabled` gate)
- `hermes_cloud/ingest/gmail_api.py` + migration
  `0009_gmail_cursor_overlap.sql` — History cursor from `profile.historyId`,
  `history.list` with `INBOX` filter, cursor advance only after durable WAL
  `fsync-before-ACK`; expired history uses
  `q=in:inbox after:<cursor_observed_at-overlap>` with a bounded result cap and
  provider-ID dedup, otherwise `needs_attention`.
- `hermes_cloud/execute/gmail_api_send.py` — deterministic
  `Message-ID: <abrolia-<effect_id>@abrolia.com>`, timeout→ one
  `rfc822msgid:` SENT search (1→success; 0 or >1/search failure→`outcome_unknown`,
  never a blind resend).
- `control_plane/providers/email/google_oauth.py:222` — `gmail_real_enabled` gated on `real_email_enabled + google_configured + app_verified + gmail_scope_approved + casa_current + limited_use_disclosed`; refresh token direct to `SecretSink` memory-only, never in DB/job/logs.

**Live battery on `abrolia-synthetic` + allowlisted dedicated Gmail (operator, gated by Phase A + B + verification/CASA):**
Pending-operator until verification/CASA and an allowlisted dedicated real
account matching `abrolia-agent-test-*@gmail.com` is provisioned. The account
address is operator evidence and must not be replaced by a `.test` placeholder.
Cases record `gmail_message_id`, `historyId`, and RFC `Message-ID` without tokens:

| # | Case | Expected | Status |
|---|------|----------|--------|
| 1 | OAuth connect (PKCE/state/select_account, exact scopes `openid email gmail.readonly gmail.send`) | `state` one-time, PKCE verified, scopes exact, address confirmed, `agent inbox != recovery` | pending |
| 2 | History receive (INBOX message → durable append → cursor advance) | `needs_attention` only on gap, no silent skip | pending |
| 3 | Approve/send → revoke/delete | `Message-ID` deterministic, SENT reconciled, revoke calls `oauth2.googleapis.com/revoke`, tombstone | pending |

The legacy `.test` fake path remains useful for regression tests but is not C3
acceptance evidence. C3 must not merge until the dedicated Gmail staging battery
is complete and its non-secret identifiers are recorded.

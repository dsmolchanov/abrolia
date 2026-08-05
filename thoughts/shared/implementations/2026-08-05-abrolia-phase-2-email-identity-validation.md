# Validation and Remediation Report: Abrolia Phase 2 — Family Email Identity

**Plan:** `thoughts/shared/plans/2026-08-04-abrolia-phase-2-email-identity.md`
**Validated and remediated:** 2026-08-05
**Original Abrolia baseline:** `a5dd303c6a2170d09bb5baa9c1e570e27fbd967f`
**Phase 2.4 baseline:** `bc1b3cf`
**Original Nerve baseline:** `2b8608f486f8117ded53f49e0551a058e4ebe0d4`
**Latest deployed Nerve main:** `56826658a69423136b45f3bd575744e7e1511699`
**Original remediation worktree base:** `ff93f31`
**PR integration base:** `f3d8c8f` (`origin/main` after Nerve attachment readiness)
**Scope:** Phase 2.0–2.4; Phase 2.5 runtime-email work is explicitly excluded
**Current status:** the production wiring and generation-safe cleanup/reconnect
gate is **accepted**, and bounded BYO-domain polling is implemented and green
locally. Full Phase 2.4 remains **partial / not accepted** until the live-DNS
matrix runs and the unrelated V-01 tenant token fix is deployed.

> The path in the original request used `Programs/arbolia`; the active repository
> is `Programs/abrolia`. Phase 2.5 advanced the shared checkout while this work
> was running. No `hermes_cloud/**`, runtime-email, landing, or Phase 2.5 plan
> files were edited by this remediation.

The production rehearsal used only the allowlisted synthetic household
`06137360-ec42-4c93-9e11-f66551f27681` and reserved recovery address
`owner@example.test`. It mutated live Nerve and Abrolia provider state, but used
no real family data and made no live DNS change.

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

## Executive verdict

| Item | Current result | Release meaning |
|---|---|---|
| V-01 — cross-org Nerve service-token issuance | **Open upstream / P0** | Blocks real-provider rollout; no affected Abrolia call site was found |
| V-02 — false verification after one-time-secret loss | **Safety fixed / convergence partial** | False verification is eliminated; crash-after-install still needs a durable non-secret receipt/inspection contract |
| V-03 — provider secret in allowed values | **Fixed locally for durable/public email paths** | Provider outputs are typed, credential-shaped values are rejected, and external error codes are normalized |
| V-04 — expired hold reported claimable while identity is live | **Fixed fail-closed locally** | Availability stays false until the owning identity is terminal; automatic TTL ownership transfer remains deferred |
| Phase 2.4 — family-owned domain | **Partial / not accepted** | Production composition, managed cleanup/reconnect and bounded BYO polling pass; the live DNS matrix remains |

The fixes and managed-path rehearsal restore the cleanup/reconnect safety
boundary. They do not make the full BYO-domain or Gmail rollout ready.

## Remediation applied

### V-01 — still open in `nerve-cloud`

The finding remains present on deployed Nerve main `5682665`:

- `internal/cloudapi/handler_keys.go` passes the requested `org_id` to service
  token issuance without resolving it against the authenticated tenant;
- `nerve:admin.billing` remains delegable by a tenant billing principal;
- the isolated A-to-B request still requires an upstream negative HTTP test and
  authorization fix.

Abrolia currently creates tenant keys through `/v1/keys` and does not call
`/v1/service-tokens`, so there is no safe local patch for this upstream defect.
It remains a real-provider release blocker rather than an Abrolia runtime
regression.

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
| 6. Ordered cleanup with explicit unknown | **Pass for managed live path / BYO live pending** | Nerve PR #64 made the graph deletable and PR #66 retained generation tombstones; production generation A cleanup and generation B reconnect passed |

### Acceptance criteria

| Criterion | Result |
|---|---|
| Reload/login resumes the same DNS records and state | **Pass hermetically; managed live pass / BYO live pending** — production login and two reloads retained generation B, but no live DNS records were created |
| Wrong/partial DNS waits; verified DNS advances once | **Pass hermetically** — partial checks remain waiting and inbox creation occurs once |
| One canonical domain cannot be claimed by two households | **Pass locally** — domain HMAC uniqueness, legacy-row fallback, address/domain binding, and different-local-part collision tests pass; there is no claim-specific two-connection concurrent-writer test |
| Delete/reconnect covers DNS present, provider unavailable and lost response | **Partial** — managed production cleanup/reconnect now passes; BYO live DNS-present and post-delete response-loss cases remain unexercised |

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

1. Merge and deploy the bounded BYO-domain DNS polling change.
2. Run the live BYO-domain DNS matrix with an operator-owned test domain,
   including wrong/partial records, verified advance, persisted DNS state,
   cleanup while DNS remains present, and reconnect. The managed-domain live
   rehearsal above does not exercise DNS mutation.
3. Fix and negatively test the unrelated cross-org service-token issue V-01 in
   Nerve before declaring the whole real-provider release surface accepted.
4. Wire a production mailer if repeated login must be validated through public
   delivery rather than the documented maintenance-window operator link.

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
  in production. The live BYO DNS portion remains a separate manual gate.

| Check | Current result |
|---|---|
| Nerve PR #66 CI | **pass** — cloud-e2e, go-checks, unit, integration, lint, coverage, SDK, dashboard, exact mirror and vulnerability checks |
| Nerve production release 68 | **pass** — digest-pinned image, service check healthy |
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

## Upstream and manual release gate

Before Phase 2.4 or any real email provider is accepted:

1. fix V-01 and add the A-to-B service-token negative test in `nerve-cloud`;
2. make verified inbox/domain/org cleanup convergent in Nerve;
3. implement bounded DNS polling/backoff and lost-recovery-response handling;
4. rerun both repositories' integration and chaos suites;
5. run the staged manual/live matrix with synthetic households and no real
   family data;
6. keep `ABROLIA_REAL_EMAIL_ENABLED` and the broader real-family-data gates off
   until all items above pass.

Phase 2.5 may continue independently, but it must not be used as evidence that
Phase 2.4 is accepted.

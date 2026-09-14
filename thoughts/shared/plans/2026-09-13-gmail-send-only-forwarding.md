---
title: "Agent Gmail without CASA — send-only scope, inbound through forwarding"
status: active
created_at: "2026-09-13"
repository: abrolia
parent_plans:
  - thoughts/shared/plans/2026-08-23-go-live-checklist.md
scope: gmail-send-only-forwarding
data_policy: production; Gmail option limited to ABROLIA_GOOGLE_OAUTH_TEST_USERS until Google sensitive-scope verification
---

# Agent Gmail without CASA — Implementation Plan

## Overview

The "separate agent Gmail" email option requests the restricted scope
`gmail.readonly`, which makes Google's restricted-scope verification and an
annual CASA security assessment mandatory before the option can open beyond
test users. Owner decision 2026-09-13: drop `gmail.readonly`, keep only
`gmail.send` (sensitive) with `openid` and `email`. Mail still reaches Abrolia:
the family turns on Gmail **automatic forwarding** from the agent Gmail account
to a hidden, per-household Nerve inbox, which the runtime already knows how to
ingest. Outbound mail keeps leaving from the agent Gmail address through
`users.messages.send`. The result needs only Google's sensitive-scope
verification — no CASA.

## Current State Analysis

**Scopes and their two copies.** The control plane requests
`GMAIL_EMAIL_SCOPES` = openid, email, gmail.readonly, gmail.send
(`control_plane/email/models.py:34-39`) and the runtime requires an exact match
against its own copy `GMAIL_REQUIRED_SCOPES`
(`hermes_cloud/email/google_client.py:30-35`, check at `:75`). A callback whose
granted set differs is revoked and rejected (`control_plane/providers/email/google_oauth.py:386-393`);
the binding validator rejects a mismatch too (`control_plane/email/models.py:282-283`).
Hardcoded four-scope copies live in `tests/test_runtime_service.py:685-690`,
`tests/test_config_and_cli.py:236-241` and `tests/test_gmail_api_oauth_grant.py:92-97`.
`tests/control_plane/email/test_google_oauth.py:261-283`
(`test_scope_downgrade_revokes_and_fails_closed`) drops `GMAIL_EMAIL_SCOPES[:-1]`,
which after sorting is `openid`, so it never exercises losing a Gmail scope.

**Everything the runtime does with Gmail except sending needs read access.**
`GmailHttpClient.profile/history/message/list_inbox/search_sent`
(`hermes_cloud/email/google_client.py:243-290`) back the inbox poller
`GmailHistorySource` (`hermes_cloud/ingest/gmail_api.py:110-189`), which a
background loop runs for `provider == "gmail"` (`hermes_cloud/runtime/service.py:603-630`,
thread at `:1236-1256`); the activation health probe calls `profile()`
(`hermes_cloud/runtime/service.py:505-526`); and `GmailSendProvider.reconcile`
lists SENT mail to settle a timed-out send (`hermes_cloud/execute/gmail_api_send.py:20,44-59`),
which `EmailSender` calls when `supports_idempotent_reconcile` is true
(`hermes_cloud/execute/email_send.py:397,488`). Per Google's reference pages,
`users.getProfile` does not accept `gmail.send`; `users.messages.send` does and
returns the sent `Message` (`id`, `threadId`, `labelIds`) synchronously.

**One email identity, one provider kind.** A household has exactly one live
email identity (unique index `email_identities_one_live_household`,
`control_plane/migrations/0002_email_identity.sql:26-31`). The planner projects
one `EmailV1 {agent_inbox, fallback, provider_kind, provider_binding_ref,
secret_binding_ref}` (`control_plane/provisioning/manifest.py:32-38`,
`control_plane/provisioning/planner.py:284-296`); the runtime parses the same
shape (`hermes_cloud/core/runtime_manifest.py:388-401`) and derives a single
`EmailBinding` from it (`hermes_cloud/runtime/service.py:428-440`). Ingest
source and send backend are both chosen from that one string: Nerve config
refuses non-`nerve*` (`hermes_cloud/runtime/service.py:533-535`), the Gmail loop
refuses non-`gmail` (`:603-608`), and the sender is picked by
`binding.provider` (`hermes_cloud/cli.py:153-208`).

**Gmail provisioning today.** `gmail_agent` routes to `google-oauth`
(`control_plane/onboarding/service.py:297-298`). `GoogleOAuthService.callback`
installs the grant bundle under `ABROLIA_GMAIL_OAUTH_GRANT` before the user
confirms (`control_plane/providers/email/google_oauth.py:418-446`);
`GoogleOAuthProvisioner.ensure` waits in `oauth_required` /
`dedicated_account_confirmation` and returns a result with no secret material,
accepted through `pre_staged_secret_verified`
(`google_oauth.py:563-630`, `control_plane/provisioning/worker.py:1210-1216`).
The waiting status carries a fixed disclosure literal duplicated at
`google_oauth.py:42-45` and `control_plane/email/models.py:290-297`, re-validated
at `worker.py:1406`.

**Nerve inbox provisioning and ingest.** `NerveManagedEmailProvisioner.ensure`
creates org → platform-domain grant → inbox → runtime key
(`nerve:email.read`, `nerve:email.send`) → `email.received` webhook pointing at
the household runtime, and returns one secret
`ABROLIA_NERVE_EMAIL_CREDENTIALS` (`control_plane/providers/email/nerve_managed.py:113-209`,
`control_plane/providers/email/nerve_client.py:101-240`). Teardown deletes
webhook, keys, inbox, grant and org, and accepts a computed
`nerve-org:<org_external_ref>` (`nerve_managed.py:324-372`,
`nerve_client.py:27-31`). The worker requires result secret names to equal
`[binding_ref]` (`worker.py:1220-1245`). In the runtime,
`NerveWebhookStore.append` verifies signature, event type and org/inbox tenancy
only (`hermes_cloud/ingest/nerve_webhook.py:179-275`); the attachment worker
**rebuilds** an RFC822 message from Nerve's JSON with a synthetic Message-ID and
no original headers (`nerve_webhook.py:517-545`) before `ingest_rfc822`
(`hermes_cloud/ingest/rfc822.py:29-91`). Nerve receives Resend's header map
(`~/Programs/nerve-cloud/internal/emailtransport/providers/resend/resend_receiving.go:31`)
but does not persist or expose it (the `raw_headers` column from
`nerve-cloud/internal/store/migrations/core/0011_inbound_receiving.sql:17` has no reader).

**Gates and texts.** Real Gmail boot requires verified-app, scope-approved,
**CASA** and Limited-Use flags (`control_plane/config.py:307-319`,
`google_casa_current` at `:203,494`). Readonly semantics are described in the
privacy notices, `docs/google-verification.md`, `docs/onboarding-runbook.md:408`,
`docs/privacy/{data-map.md,lawful-bases.md,dpia.md}` and the draft PR #165.

**Production data.** Read-only count on 2026-09-13
(`fly ssh console -a abrolia-control-plane-synthetic`, `email_identities GROUP BY option,status`):
`[('managed_abrolia','active',2)]` — no Gmail identity exists, so no issued
four-scope grant needs migrating.

## Desired End State

- The Gmail consent screen lists only "Send email on your behalf" plus sign-in;
  no code path calls a Gmail read method.
- A Gmail household has one email identity (the Gmail address) whose binding
  carries both the Google grant and a hidden Nerve forwarding inbox. The
  runtime ingests that inbox through the existing Nerve path and sends through
  Gmail.
- After activation, Web chat walks the family through enabling forwarding,
  shows the Gmail confirmation link it received (validated, never auto-opened),
  and reports forwarding as `pending`, `active` or `stale` on `/readyz` and in
  chat. A daily check message proves forwarding still works.
- A timed-out Gmail send becomes `outcome_unknown` and is never retried.
- Real-Gmail boot requires verified-app, sensitive-scope-approved and
  Limited-Use evidence — not CASA.
- Privacy Policy, Terms, onboarding copy and `docs/google-verification.md`
  describe the send-only design and the sensitive-scope verification path.

Recognise it by: the live battery in Phase 6 passing on a synthetic Gmail
account, and `grep -rn "gmail.readonly" control_plane hermes_cloud landing docs`
returning only historical/changelog mentions.

### Key Discoveries

- The disclosure literal is part of a persisted model
  (`control_plane/email/models.py:294-297`); zero Gmail rows in production make
  changing it safe now, and only now.
- `GmailAuthRevoked` is raised for both 401 and 403
  (`hermes_cloud/email/google_client.py:222-224`), so an insufficient-scope
  call would masquerade as a revoked grant — removing read calls is required,
  not cosmetic.
- Nerve's rebuilt message loses `X-Forwarded-For`; authenticity of forwarded
  mail cannot be proven from headers without a nerve-cloud change.
- The public `@abrolia.com` inbox already accepts mail from anyone and treats
  it as untrusted content that only yields proposals
  (`hermes_cloud/runner/extraction.py:44-45`); a hidden forwarding inbox has the
  same threat model, so an unguessable address is hygiene, not the security
  boundary.
- Gmail forwards new incoming mail except spam, does not forward mail the
  account sends to itself, and needs the web UI to enable forwarding; the
  confirmation arrives from `forwarding-noreply@google.com` with a link
  (support.google.com/mail/answer/10957; sender and format per the Phase 0 spike).
- Invariants that bind this work: teardown only through references the control
  plane can name (`AGENTS.repo-invariants.md:172-215`), preconditions enforced
  where the provider is called (`:216-240`).

## What We're NOT Doing

- Restricted-scope verification or CASA for Gmail.
- Configuring forwarding through the Gmail API (`gmail.settings.sharing` is
  restricted and domain-wide-delegation only).
- Cryptographic verification of forwarded mail (ARC/DKIM) — needs Nerve to
  expose received headers; recorded as a follow-up in nerve-cloud.
- An in-app "Disconnect Gmail" control after setup (separate change).
- Changing the managed `@abrolia.com` or family-domain options.
- Opening Gmail beyond test users — that follows Google's approval (Phase 6).

## Implementation Approach

Keep **one email identity and one `provider_kind = "gmail"`**, and extend the
Gmail binding with an optional inbound relay: `inbound_provider_kind = "nerve"`,
`inbound_binding_ref`, `inbound_secret_binding_ref`. The runtime chooses the
send backend from `provider_kind` and the ingest source from the inbound
fields, so managed and domain households are untouched. The Gmail provisioner
becomes a composite: it ensures the hidden Nerve inbox first (reusing
`NerveAdminClient` and the managed provisioner's idempotent steps), then runs
the existing OAuth wait states with the reduced scope set.

Forwarding is finished **after activation, inside the runtime**, because the
confirmation email is inbound content and the control plane must stay
metadata-only. The Gmail step therefore verifies once OAuth and the inbox are
ready; forwarding readiness is a runtime health state.

Order by real dependency: spike facts → scope reduction (removes read paths,
independent of forwarding) → runtime can consume the relay fields (must ship
in a runtime image before the control plane emits them) → control plane
provisions and emits the relay → forwarding health → texts → rollout.

Each phase is its own branch and PR; the `**Branches:**`/`**Files:**` lines
are what `tests/control_plane/test_plan_inventory.py` enforces.

## Phase 0: Live spike on synthetic accounts

### Overview

Pin the external facts later phases depend on, with a synthetic agent Gmail
account and a synthetic Nerve canary inbox. No production household is used.

### Changes Required

#### 1. Spike record and synthetic fixtures

**Branches:** `docs/gmail-send-only-plan`, `docs/gmail-forwarding-spike`.

**Files:** `tests/fixtures/email/gmail_forwarding/confirmation.nerve.json`,
`tests/fixtures/email/gmail_forwarding/forwarded_letter.nerve.json`,
`tests/fixtures/email/gmail_forwarding/canary_return.nerve.json`,
`.check-fixtures-allow`.

**Changes**:

- Record in `thoughts/shared/research/2026-09-XX-gmail-forwarding-spike.md`
  (new; `thoughts/` is inventory-exempt), each with date and observed evidence:
  - S1 Confirmation email as Nerve delivers it: `from`, `subject`, whether a
    code is present, the confirmation URL host and path shape.
  - S2 An external letter forwarded by Gmail, as Nerve JSON: `from` (original
    sender?), `to`/`cc` (agent address?), `subject`, threading fields.
  - S3 A message from the hidden Nerve inbox to the agent Gmail: forwarded back
    or not, and whether it lands in spam.
  - S4 A token with exactly openid+email+gmail.send: `messages.send` succeeds
    with `id`/`threadId`/`labelIds`; `getProfile` returns 403.
- Save sanitised, synthetic reconstructions of S1–S3 as fixtures (reserved
  documentation domains; Google's automated sender allowlisted in
  `.check-fixtures-allow`). They are text, so `tests/fixtures/PROVENANCE.md`
  needs no entry.

**Deterministic outcomes used later**:

- Confirmation-link allowlist (Phase 2) = the exact https host(s) observed in
  S1, each required to end in `.google.com`.
- Health strategy (Phase 4) = daily check message if S3 shows forwarding back;
  otherwise the inactivity strategy defined in Phase 4.
- If S2 shows Nerve's `from` is the Gmail account instead of the original
  sender, stop and revise this plan before Phase 2: reply targeting would
  change.

### Outcomes (2026-09-14)

Recorded in `thoughts/shared/research/2026-09-14-gmail-forwarding-spike.md`.

- **S1:** sender `forwarding-noreply@google.com`; subject
  `(Gmail Forwarding confirmation – Receive mail from <agent>`; **no code**, only
  links on host `mail-settings.google.com`: `/mail/vf-…` confirms, `/mail/uf-…`
  cancels. Gmail shows "Verify <address>" until a human opens the vf link **and
  presses Confirm**. Mail that arrived before the forwarding radio was saved was
  not forwarded.
- **S2:** a letter forwarded by Gmail arrives in about 6 s. Nerve `from` = the
  **original sender**; Nerve `to` = **the relay inbox address, not the Gmail
  address**; subject unchanged, no "Fwd:"; no header fields. Stop condition
  not met.
- **S3:** a message from the relay inbox to the agent Gmail is forwarded back in
  about 11 s (inbound, from = to = relay address). **Strategy A applies.**
- **S4:** granted exactly openid, userinfo.email, gmail.send.
  `users.messages.send` → 200 with `id`, `threadId`, `labelIds`. `getProfile` and
  `messages.list` → 403 `PERMISSION_DENIED` reason `insufficientPermissions`.
  Two preconditions surfaced: Google's consent screen shows `gmail.send` as a
  checkbox the user must tick (unticked → only openid+email granted), and the
  Cloud project must have the Gmail API enabled.
- **Nerve side:** the runtime MCP `list_threads` tool timed out after the MCP 2026
  transition, while REST `GET /v1/inboxes/{id}/threads` with
  `X-Nerve-Cloud-Key` worked. The runtime already uses REST for ingest.

Consequences carried into later phases:
- Phase 1: distinguish 403 `insufficientPermissions` from a revoked grant.
- Phase 2: link allowlist = `mail-settings.google.com`, path prefix
  `/mail/vf-`. The chat copy says "open the link, press Confirm, then choose
  'Forward a copy of incoming mail' and Save".
- Phase 5: the onboarding copy tells the family to tick "Send email on your behalf".
- Phase 6: verify the Gmail API is enabled before the live battery.

### Success Criteria

#### Automated Verification

- [x] `python3 scripts/check_fixtures.py --all` passes with the new fixtures.

#### Manual Verification

- [x] Operator performs S1–S4 on synthetic accounts and commits the spike record (2026-09-14).

---

## Phase 1: Send-only Gmail scope

### Overview

Reduce the scope set everywhere at once and delete every Gmail read path, so
nothing can issue an insufficient-scope call.

### Changes Required

#### 1. Scope constants and consistency

**Branches:** `feat/gmail-send-only-scope`.

**Files:** `control_plane/email/models.py`, `control_plane/providers/email/google_oauth.py`,
`control_plane/provisioning/worker.py`, `control_plane/config.py`,
`hermes_cloud/email/google_client.py`, `hermes_cloud/email/google_grant.py`,
`hermes_cloud/ingest/gmail_api.py`, `hermes_cloud/execute/gmail_api_send.py`,
`hermes_cloud/runtime/service.py`, `hermes_cloud/cli.py`,
`hermes_cloud/email/service.py`,
`tests/control_plane/email/test_google_oauth.py`, `tests/test_runtime_service.py`,
`tests/test_config_and_cli.py`, `tests/test_gmail_api_oauth_grant.py`,
`tests/test_gmail_api_send.py`, `tests/test_gmail_api_ingest.py`,
`tests/test_gmail_scope_consistency.py`, `tests/control_plane/test_required_config.py`,
`tests/control_plane/test_real_email_wiring.py`, `deploy/control-plane/fly.toml`.

**Changes**:

- `GMAIL_EMAIL_SCOPES` and `GMAIL_REQUIRED_SCOPES` → openid, email,
  gmail.send. New `tests/test_gmail_scope_consistency.py` imports both and
  asserts equality, so the two copies cannot drift.
- **Found while implementing (2026-09-15):** the code requests the short scope
  name `email`, and Google writes the grant back as
  `https://www.googleapis.com/auth/userinfo.email` (spike S4 read exactly
  `gmail.send userinfo.email openid`). `GoogleOAuthClient.exchange` compared
  the two verbatim, so every real consent would have been revoked as "an
  unexpected scope set" — unnoticed because production holds no Gmail identity
  and the spike used its own script. `exchange` now folds the alias before the
  comparison; the bundle keeps the short name; a test replays the spike's scope
  string.
- Fix `test_scope_downgrade_revokes_and_fails_closed` to drop `gmail.send`
  explicitly; add a case granting the old four-scope set (a wider grant) and
  assert it is revoked and rejected.
- Replace the disclosure literal in both places with: "Abrolia sends mail from
  this dedicated agent mailbox only after you confirm; incoming mail reaches
  Abrolia through forwarding you turn on." Production has no persisted Gmail
  status rows (see Current State), so no compatibility literal is kept.
- Delete `GmailHttpClient.profile/history/message/list_inbox/search_sent`,
  `GmailHistorySource`, the Gmail poll loop in `runtime/service.py` and
  `ABROLIA_GMAIL_WORKER_SECONDS`. Gmail activation health = outbound only
  (bundle has gmail.send and a token refresh succeeds); inbound health comes
  from Phase 4.
- `GmailHttpClient._request`: a 403 whose error reason is `insufficientPermissions`
  raises a new `GmailScopeInsufficient`, not `GmailAuthRevoked`; 401 and other
  403s keep today's behaviour. Test both. (The `needs_reconnect` health label
  named earlier had no writer once the poller's `email_sync_state` rows were
  gone; `/readyz` reports `email_health.status = send_only` for a live grant
  and stays `not_ready` for a revoked one, until Phase 4 adds forwarding
  health.)
- `GmailSendProvider.supports_idempotent_reconcile = False` and remove
  `reconcile`; a send timeout or connection error stays `EmailOutcomeUnknown`
  and `EmailSender` records `outcome_unknown` without retry. Test that path.
- `config.validate`: real Gmail requires `google_oauth_app_verified`,
  `google_gmail_scope_approved` and `google_limited_use_disclosed`; delete
  `google_casa_current` and `ABROLIA_GOOGLE_CASA_CURRENT`. Update the error
  text and its test.
- Until Phase 2–3 land, a Gmail household has no inbound source: the Gmail card
  remains offered only to test users (unchanged gate) and production has none.

### Success Criteria

#### Automated Verification

- [ ] `python3 -m pytest -m "not live"` passes.
- [ ] `python3 -m pytest tests/control_plane -q` passes.
- [ ] `ruff check .` passes.
- [ ] `grep -rn "gmail.readonly" control_plane hermes_cloud` returns nothing.
- [ ] `tests/test_gmail_scope_consistency.py` fails when either constant is edited alone (checked by temporarily editing one).

#### Manual Verification

- [ ] None — behaviour is covered by unit tests; live checks are in Phase 6.

---

## Phase 2: Runtime consumes a forwarding relay

### Overview

Teach the runtime to read optional inbound-relay fields, ingest the hidden
Nerve inbox for a Gmail household, and recognise Gmail's forwarding
confirmation. Ships before the control plane emits the fields.

### Changes Required

#### 1. Manifest and binding

**Branches:** `feat/gmail-forwarding-runtime`.

**Files:** `hermes_cloud/core/runtime_manifest.py`, `hermes_cloud/core/config.py`,
`hermes_cloud/runtime/service.py`, `hermes_cloud/email/receipts.py`,
`hermes_cloud/ingest/nerve_webhook.py`, `hermes_cloud/ingest/forwarding.py`,
`hermes_cloud/runner/pipeline.py`, `hermes_cloud/channels/web.py`,
`hermes_cloud/cli.py`, `tests/test_gmail_forwarding_runtime.py`,
`tests/test_nerve_runtime.py`, `tests/test_runtime_service.py`.

**Changes**:

- `EmailRouting` gains optional `inbound_provider_kind`, `inbound_binding_ref`,
  `inbound_secret_binding_ref`. Parsing rule: allowed only with
  `provider_kind == "gmail"`, and then all three are required with
  `inbound_provider_kind == "nerve"`; any other combination is a
  `ManifestError`.
- Runtime Nerve wiring (`_nerve_config`, the webhook route, `run_nerve_once`)
  reads refs and secret from the inbound fields when present; the ingest binding
  records `source="gmail-forward"` so receipts distinguish it from managed mail.
  Outbound for the household stays `GmailSendProvider`.
- New `hermes_cloud/ingest/forwarding.py`:
  - `classify(message, *, agent_address, relay_address, allowed_link_hosts)` →
    `confirmation(link)` | `check(token)` | `letter`.
  - `confirmation` requires `from == forwarding-noreply@google.com` (per S1)
    and exactly one https link with host `mail-settings.google.com` and path
    prefix `/mail/vf-`; the `/mail/uf-` cancel link and other links are never
    surfaced. Otherwise the message is a `letter`.
  - `check` requires `from == relay_address` and the Phase 4 token format.
  - A `letter` continues to `ingest_rfc822` unchanged; the original sender is
    Nerve's `from` (per S2), so replies go to the original sender.
- A `confirmation` never enters extraction. It posts one Web chat message to
  the owner with the link and the instruction "Open this link and press
  Confirm, then in Gmail on a computer choose 'Forward a copy of incoming mail
  to …' and Save Changes". Abrolia never requests the URL.
- Forwarding state table (runtime DB migration): `pending` on first activation
  of a relay binding; `active` on the first `letter` or `check` received
  through the relay. Exposed in `/readyz` `email.forwarding`.

### Success Criteria

#### Automated Verification

- [ ] `python3 -m pytest tests/test_gmail_forwarding_runtime.py tests/test_nerve_runtime.py -q` passes, covering:
  - manifest accept and reject combinations;
  - the confirmation fixture producing a chat message, with no extraction and no HTTP fetch;
  - a confirmation from a foreign sender or link host treated as a letter;
  - the forwarded-letter fixture producing a proposal whose reply target is the original sender and whose send backend is Gmail;
  - state moving `pending → active`.
- [ ] `python3 -m pytest -m "not live"` passes; managed and domain households are unchanged (existing `tests/test_nerve_runtime.py` parametrisation still green).

#### Manual Verification

- [ ] None beyond Phase 6.

---

## Phase 3: Control plane provisions the relay

### Overview

Make the Gmail provisioner create the hidden inbox, carry both secrets, emit
the relay fields, and tear everything down by name.

### Changes Required

#### 1. Composite Gmail provisioner

**Branches:** `feat/gmail-forwarding-inbox`.

**Files:** `control_plane/providers/email/gmail_forwarding.py`,
`control_plane/providers/email/google_oauth.py`,
`control_plane/providers/email/nerve_managed.py`,
`control_plane/email/models.py`, `control_plane/email/service.py`,
`control_plane/provisioning/worker.py`, `control_plane/provisioning/planner.py`,
`control_plane/provisioning/manifest.py`, `control_plane/container.py`,
`control_plane/feature_flags.py`,
`tests/control_plane/email/test_gmail_forwarding_provisioner.py`,
`tests/control_plane/email/test_google_oauth.py`,
`tests/control_plane/test_provisioning_jobs.py`,
`tests/control_plane/test_email_option_flags.py`,
`tests/control_plane/test_assistant_address_per_household.py`.

**Changes**:

- New `GmailForwardingProvisioner` (registered as `google-oauth`, so selection
  routing and `CUT_EMAIL_OPTIONS` stay valid):
  - `ensure` first ensures the relay through `NerveAdminClient`, reusing the
    managed provisioner's idempotent org, grant, inbox, key and webhook steps,
    `AttachmentFlagPending` handling and lost-secret recovery. The inbox
    address is `fwd-<26 lowercase base32 of 128 random bits>@<platform domain>`,
    generated once and persisted in the job's `external_ref_ciphertext` before
    the inbox call.
  - Then it runs the existing OAuth wait states.
  - On confirm it returns `provider="gmail"`, `external_ref` =
    `{google: "google-oauth:<identity_id>", nerve: <managed _Refs>}`, and secret
    material `{ABROLIA_NERVE_EMAIL_CREDENTIALS: …}`.
- `EmailPublicBinding` for gmail adds `inbound_binding_ref` (JSON org and inbox
  ids, like managed) and `inbound_secret_binding_ref =
  ABROLIA_NERVE_EMAIL_CREDENTIALS`. Scope validation uses the new
  `GMAIL_EMAIL_SCOPES`.
- Worker `_stage_email_secret`: for gmail, the result's material names must
  equal `[ABROLIA_NERVE_EMAIL_CREDENTIALS]` **and**
  `pre_staged_secret_verified` must hold for `ABROLIA_GMAIL_OAUTH_GRANT`.
  `_validate_email_external_ref` accepts exactly the `{google, nerve}` key set
  for gmail.
- `deprovision` for gmail runs Google revoke and secret delete, then the managed
  Nerve teardown. The computed shutdown reference for a gmail identity yields
  both `google-oauth:<identity_id>` and `nerve-org:<email_org_external_ref(hh, identity)>`;
  a provisioner-level test asserts both are **accepted**, per
  `AGENTS.repo-invariants.md:172-215`.
- The kill switches (`ABROLIA_GMAIL_ENABLED` and `ABROLIA_REAL_EMAIL_ENABLED`,
  since the relay is a real Nerve inbox) are asserted at the provider call in
  `ensure`, `inspect` and `reconcile`, extending
  `test_disabling_an_option_stops_work_that_is_already_queued` to the relay step.
- Planner `EmailV1` gains the three inbound fields, set only for gmail.
- The local-part validator refuses user-chosen managed local parts starting
  with `fwd-`.

### Success Criteria

#### Automated Verification

- [ ] `python3 -m pytest tests/control_plane -q` passes, including new tests for:
  - relay created before OAuth, and idempotent across a crash between the inbox and key calls;
  - lost webhook secret recovered;
  - both secrets required to verify;
  - the manifest carrying the inbound fields;
  - teardown by computed references accepted by both provisioners;
  - a disabled option or real-email brake stopping queued relay work;
  - a `fwd-` local part refused.
- [ ] `python3 -m pytest -m "not live"` passes.
- [ ] `ruff check .` passes.

#### Manual Verification

- [ ] None beyond Phase 6.

---

## Phase 4: Forwarding health

### Overview

Detect forwarding that was never finished or later stopped, and tell the family.

### Changes Required

#### 1. Check messages, state and alerts

**Branches:** `feat/gmail-forwarding-health`.

**Files:** `hermes_cloud/ingest/forwarding.py`, `hermes_cloud/runtime/service.py`,
`hermes_cloud/execute/nerve_send.py`, `hermes_cloud/core/observability.py`,
`hermes_cloud/channels/web.py`, `control_plane/web/templates/onboarding.html`,
`tests/test_gmail_forwarding_health.py`, `tests/test_observability_health.py`.

**Changes**:

- **Strategy A (S3 shows the message is forwarded back):**
  - Every `ABROLIA_GMAIL_FORWARD_CHECK_HOURS` (default 24), the runtime sends
    one message from the relay inbox to the agent Gmail address through
    `nerve_send`, with subject `Abrolia forwarding check <token>`. The token is
    `HMAC(household key, UTC date)`.
  - A matching `check` received within 2 hours keeps the state `active`. Two
    consecutive misses set `stale`.
  - Sends go through the outgoing-mail kill switch, like `send_notice`.
- **Strategy B (S3 negative):** no messages are sent. The state becomes `stale`
  after 7 days without any relay traffic; the chat copy asks the family to
  confirm forwarding.
- `stale` raises alert `gmail_forwarding_stale` (registered name) and posts one
  Web chat message with setup steps. `/readyz` shows `email.forwarding`.
- Chat action "I've turned forwarding on" triggers an immediate check
  (Strategy A) or re-arms the 7-day window (Strategy B).
- Onboarding Gmail card copy discloses the daily check message (Strategy A),
  that forwarding is set up in Gmail on a computer after setup, and that the
  "Send email on your behalf" checkbox on Google's consent screen must be ticked.

### Success Criteria

#### Automated Verification

- [ ] `python3 -m pytest tests/test_gmail_forwarding_health.py tests/test_observability_health.py -q` passes, covering:
  - check scheduled, returned and missed twice → `stale` → alert and chat message once, not repeated;
  - check blocked by the kill switch;
  - an unknown alert name still raises.
- [ ] `python3 -m pytest -m "not live"` passes.

#### Manual Verification

- [ ] None beyond Phase 6.

---

## Phase 5: Texts and verification docs

### Overview

Describe the send-only design everywhere Google, families and operators read it.
Builds on PR #165; if #165 is unmerged when this starts, merge it first.

### Changes Required

#### 1. Policy, terms, product copy, docs

**Branches:** `docs/gmail-send-only-privacy`.

**Files:** `docs/privacy/privacy-notice-en.md`, `docs/privacy/privacy-notice-ru.md`,
`landing/privacy.html`, `landing/terms.html`, `landing/index.html`,
`control_plane/web/templates/onboarding.html`, `docs/google-verification.md`,
`docs/privacy/data-map.md`, `docs/privacy/processors.md`,
`docs/privacy/lawful-bases.md`, `docs/privacy/dpia.md`, `docs/SECURITY.md`,
`docs/onboarding-runbook.md`, `docs/canon-closure-runbook.md`,
`tests/control_plane/test_ui_contract.py`.

**Changes**:

- Google user data section:
  - the scopes table lists `gmail.send`, `openid` and `email` only;
  - inbound mail arrives through forwarding the family turns on, to an Abrolia
    inbox operated with Nerve and Resend;
  - the daily check message;
  - Nerve and Resend added as recipients for this option;
  - the Limited Use statements are kept.
- Terms §6 and the homepage card: "sends only emails you confirm; you forward
  incoming mail".
- `docs/google-verification.md`:
  - sensitive-scope path only, with the CASA sections removed;
  - justification for `gmail.send`;
  - the video shows forwarding setup and a confirmed send;
  - the evidence flags reduced to three.
- `data-map.md` rows for the relay inbox, the check messages and the
  forwarding-state table; `processors.md` P3/P4 now also serve the Gmail option.
- `SECURITY.md` threat entry: relay address exposure (same model as the public
  inbox), a spoofed confirmation email (sender and host allowlist, never
  fetched), a forged `check` (only affects health).
- `test_ui_contract.py` disclosure test updated to the new copy.

### Success Criteria

#### Automated Verification

- [ ] `python3 -m pytest tests/control_plane/test_ui_contract.py -q` passes.
- [ ] `python3 scripts/check_fixtures.py --all` passes.
- [ ] `grep -rn "gmail.readonly" landing docs/google-verification.md docs/privacy/privacy-notice-*.md` returns nothing.

#### Manual Verification

- [ ] Owner reviews the policy and terms wording before the PR leaves draft.

---

## Phase 6: Rollout and Google submission

### Overview

Deploy in the order the manifest contract requires, prove the path live, then
submit sensitive-scope verification.

### Changes Required

No code. Operator steps recorded in
`thoughts/shared/implementations/2026-09-XX-gmail-send-only-forwarding-validation.md`.

0. Confirm the Gmail API is enabled in the OAuth client's Cloud project (spike S4).
1. Re-run the read-only production count. If any Gmail identity exists, the
   operator resets that household's email step (revokes the grant) before
   deploying Phase 1.
2. After Phase 2 merges: build and pin the runtime image, then
   `abrolia-control-plane roll-runtime` for existing households. Only after
   that, deploy Phase 3.
3. Live battery on a synthetic Gmail test user:
   - connect with send-only consent;
   - the Web chat forwarding guide;
   - the confirmation link shown and opened by the human;
   - an external letter → proposal → confirmed reply delivered from the Gmail address;
   - health `active`;
   - forwarding disabled in Gmail → `stale`, alert and chat message;
   - Google access removed → send fails as revoked;
   - account deletion → Google revoke and Nerve teardown.
4. Owner submits sensitive-scope verification per `docs/google-verification.md`.
   After approval, set the three evidence flags and `ABROLIA_GMAIL_REAL_ENABLED=1`.

### Success Criteria

#### Automated Verification

- [ ] `curl -s https://app.abrolia.com/readyz` reports `ready` after each deploy.

#### Manual Verification

- [ ] Live battery steps 1–8 recorded with evidence.
- [ ] Google approval received and the flags deployed.

---

## Testing Strategy

### Unit Tests

- Scope constants equal across packages; narrower and wider grants both revoked.
- Manifest relay-field combinations accepted or rejected.
- `forwarding.classify` for confirmation, spoofed confirmation, check, forged
  check and ordinary letter.
- Send timeout → `outcome_unknown`, never retried.
- Relay provisioning idempotency, secret staging, teardown by computed refs,
  kill switches at the provider call.
- Health state transitions and a single alert per stale episode.

### Integration Tests

- Runtime: a Nerve webhook fixture for a Gmail household drives
  confirmation → chat message, letter → proposal → Gmail send.
- Control plane: `cp_stack` from Gmail selection through the worker to a
  manifest with relay fields.

### Manual Testing

- Phase 0 spike and Phase 6 live battery on synthetic accounts; both need real
  Google and Nerve behaviour that fakes cannot establish.

## Performance Considerations

At most one check message per Gmail household per day, plus one Nerve inbox per
Gmail household (same unit cost as a managed inbox). No new hot-path work in
the control plane.

## Migration and Rollback

- **Data:** none in production (zero Gmail identities on 2026-09-13; re-checked
  in Phase 6 step 1). Runtime DB gains a forwarding-state table through the
  existing migration mechanism.
- **Ordering:** a runtime image that parses the relay fields must be pinned and
  rolled before the control plane emits them (Phase 6 step 2); otherwise an
  older runtime rejects the manifest.
- **Rollback:**
  - `ABROLIA_GMAIL_ENABLED=0` stops new Gmail selections and queued relay work
    at the provider call.
  - Existing Gmail households are torn down through account deletion or an
    email-step reset.
  - Reverting Phase 3 alone is safe once no Gmail identity is live.
  - Reverting Phase 1 would reintroduce the restricted scope and must not be
    deployed with `ABROLIA_GMAIL_REAL_ENABLED=1`.

## References

- Request: owner decision 2026-09-13 in this session (option 1 of the CASA
  alternatives).
- PR #165 (privacy texts, draft); `docs/google-verification.md`.
- Google:
  - [Gmail API scopes](https://developers.google.com/workspace/gmail/api/auth/scopes)
  - [users.getProfile](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users/getProfile)
  - [users.messages.send](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/send)
  - [Gmail forwarding help](https://support.google.com/mail/answer/10957)
  - [sensitive scope verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/sensitive-scope-verification)
  - all accessed 2026-09-13.
- Code:
  - `control_plane/providers/email/google_oauth.py:386-446,563-683`
  - `control_plane/providers/email/nerve_managed.py:113-372`
  - `control_plane/provisioning/worker.py:1210-1278,1336-1406`
  - `hermes_cloud/email/google_client.py:30-99,222-290`
  - `hermes_cloud/ingest/nerve_webhook.py:179-275,517-545`
  - `hermes_cloud/runtime/service.py:428-630`
  - `hermes_cloud/core/runtime_manifest.py:388-401`
- Invariants: `AGENTS.repo-invariants.md:172-240`.

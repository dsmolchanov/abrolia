---
date: 2026-08-05T23:38:46+02:00
researcher: Codex
git_commit: 47c8013b57ece10ca6ce3e9384ee15e64557f8a4
branch: codex/phase-2-4-live-validation
repository: abrolia
topic: Phase 2.4 Production Wiring and Reconnect Implementation Strategy
tags: [phase-2-4, nerve, email, production, cleanup, reconnect, tombstone]
status: complete
last_updated: 2026-08-06
last_updated_by: Codex
type: implementation_strategy
---

# Phase 2.4 production wiring and reconnect handoff

## Task(s)

- **Complete — production Nerve provider wiring.** Abrolia validates the full
  real-email configuration, registers `nerve-managed` and `nerve-byo-domain`,
  and routes only the allowlisted synthetic household to them.
- **Complete — verified graph cleanup.** Generation A provisioned and cleaned up
  through webhook, key, inbox, grant and org teardown.
- **Complete — generation-safe replay boundary.** Nerve retains the deleted org
  generation reference; a delayed generation-A ensure returns HTTP 409.
- **Complete — same-household reconnect.** Generation B received a new email
  identity, org, inbox, key and webhook and reached `verified`.
- **Complete — persistence rehearsal.** Logout, maintenance login, two reloads,
  logout and post-logout denial all passed without printing tokens.
- **Complete — bounded BYO polling.** The durable inspection job now polls at
  bounded backoff intervals and stops after five total attempts.
- **Complete — Nerve V-01.** Cross-org service-token issuance is denied and the
  fix is deployed in production release 69.
- **Complete — live BYO DNS.** `test.axiomatlas.llc` passed production DNS
  publication, bounded inspection, persisted reload, cleanup with DNS retained,
  and same-household reconnect.

## Critical References

1. `thoughts/shared/plans/2026-08-04-abrolia-phase-2-email-identity.md`
2. `thoughts/shared/implementations/2026-08-05-abrolia-phase-2-email-identity-validation.md`
3. Nerve `thoughts/shared/plans/2026-08-05-abrolia-phase-2-4-cleanup-contract.md`

## Recent changes

- Production composition and generation-scoped Nerve org identity are in
  `control_plane/config.py`, `control_plane/container.py` and
  `control_plane/providers/email/nerve_client.py`.
- Runtime cleanup reference sanitization is in
  `control_plane/provisioning/fly.py` and its worker integration.
- Legacy reset convergence is in `control_plane/onboarding/service.py` and
  `control_plane/provisioning/worker.py`.
- Nerve org deletion now retains its generation reference in
  `internal/cloudapi/handler_orgs.go`; the store release operation was removed
  from `internal/store/store_orgs.go`.
- The complete live evidence and remaining gates are recorded in
  `thoughts/shared/implementations/2026-08-05-abrolia-phase-2-email-identity-validation.md`.
- Bounded polling is implemented in `control_plane/repositories/jobs.py` and
  `control_plane/provisioning/worker.py`; the automatic and bounded-stop
  regressions live in `tests/control_plane/email/test_nerve_byo_domain.py`.
- Nerve tenant isolation is enforced in `internal/cloudapi/handler_keys.go` and
  covered by the cross-org tests in `internal/cloudapi/email_tenancy_test.go`.

## Learnings

- Reconnect safety requires a generation-scoped org idempotency reference:
  `arbolia:household:<household_id>:email:<email_identity_id>`. Releasing that
  reference on org deletion permits delayed retries to create an orphan tenant.
- A cleanup endpoint may be idempotent without releasing replay protection:
  repeated DELETE returns 204 while the tombstone continues to own its external
  reference.
- Abrolia reset can encounter resources created by the pre-Nerve `fake-email`
  provider. Such cleanup is safe only with an explicit fixed legacy binding;
  unknown identity/provider combinations must remain fail-closed.
- Runtime `config_sha256` is credential-shaped metadata under Abrolia's scanner.
  It must be stripped from the durable cleanup reference while Fly app, Machine
  and volume identifiers remain available for cleanup.
- The first replay probe ran against the old Nerve contract and created one
  empty orphan org. It was not hidden: after release 68, all child collections
  were checked empty, the org was tombstoned, and its retained reference was
  verified by an HTTP 409 replay.

## Artifacts

- Abrolia PR #15, merge `1dac0d516fd94c2cc2f43c624e3ccbf4a12e688c`
- Abrolia PR #17, merge `098bbcf`
- Abrolia PR #18, merge `47c8013`
- Nerve PR #64, merge `8945d3bf43589c51995c6a68ae9e34e62ca37e67`
- Nerve PR #66, merge `56826658a69423136b45f3bd575744e7e1511699`
- Nerve PR #67, merge `99092688c3213af3cf7dc8e72cc28bd89983f6a1`
- Abrolia PR #21, merge `261a35f0d71b66159aea2036501d6cad9381e104`
- Abrolia PR #22, merge `246c1008be46907c42a227dbe1aca4e95c126c41`
- Abrolia PR #23, merge `168d25c20c27b80bcda031210c40f41751a2c7b4`
- Abrolia release 12 image digest
  `sha256:d2762fdba1e272257966adfb71b9813e8ebb8a768feb4dc1ffa0003d041394cc`
- Nerve release 68 image digest
  `sha256:693aefba5d6fa319523bf2d8215b9c568f0054cc1fb5da9d706493edd46ea6a4`
- Nerve release 69 image digest
  `sha256:7406baea1ea524f0b9de43ff4e5d40ae9d407f3e316f70f10dafa44851229d08`
- Abrolia release 15 image digest
  `sha256:82134117d06cbd53841850e7511c08385b97d872e55fed2874fb59f7b9cc699c`
- Abrolia releases 16–18 image digest
  `sha256:2aa6100eda56f3cce124e4cf928519b3ca1fee74d6b8a66d317b71868604c92f`
- Live BYO domain: `test.axiomatlas.llc`; all six supplied MX/TXT records were
  visible through Google Public DNS.
- First live BYO identity/job/org/domain/inbox/key/webhook:
  `ca113cc4-538c-42bf-9c3b-8d7b97a25e4a` /
  `c219963d-bc64-4bd6-ab91-b77e886f96d9` /
  `494ae0a0-93a6-4557-a154-45200b4e2d62` /
  `7a859589-8739-4487-abd1-a87f94b38021` /
  `f05e056b-33e5-418c-8abe-41440a8287f5` /
  `cf903550-77cb-4948-a113-1e9b8f0c01fa` /
  `b56e59f3-d8df-4584-b8fb-29c63bca87c3`. The job succeeded on attempt five.
- Live cleanup job `f85095e5-dae0-4325-b3a8-f76465f3a182` succeeded while DNS
  remained published; the first identity is `deleted`.
- Reconnect identity/job/org/domain/inbox/key/webhook:
  `6b73a64c-b595-44b1-9852-53e01b6a8a73` /
  `43b658e5-8c45-41cc-beaf-5b73cef3ff21` /
  `82062f74-8a2c-4bd9-b42c-ac4b9cbda19d` /
  `4c8d9265-a253-4e77-aff5-3b132f8d19be` /
  `bf978c48-5cc7-49f3-b7e9-2a16affae82f` /
  `77545070-fc67-4888-9426-03f22d0ad587` /
  `b489ac1b-0c91-4cc8-aed9-c01972d6fd28`. The job succeeded on attempt three.
- Abrolia release 18 restored the standard API on the same immutable release-16
  digest; Fly service check and `/healthz` pass.
- Generation A identity/org:
  `a51cb145-63fb-4277-96eb-ef2e28e4907f` /
  `70d15c36-3ab4-40d6-9c58-ae35744d8db5`
- Diagnostic orphan tombstone:
  `b32fea34-bb3d-4527-acc5-9ab6805d59f9`
- Generation B identity/org/inbox:
  `2265aaea-83a3-4138-bf54-2af0a4909bb9` /
  `d9509937-6ac0-4e6f-9788-981d3e8c0cd0` /
  local-part `phase24-reconnect-b` on platform domain `abrolia.com`
- Generation B attachment-flag audit replay:
  `bd4ccb08-9a94-4fae-88be-b67647f27673`

## Action Items & Next Steps

1. Wire a production mailer if login must be verified through public delivery;
   this rehearsal used the documented maintenance operator path and immediately
   revoked the test session.
2. Review the older runtime cleanup job
   `6a0d040d-bfb9-4671-b2c5-e6ee681bdc6b` separately if it remains
   `outcome_unknown`; its Machine and volume are already absent and it did not
   block the email lifecycle.

## Other Notes

- Real family data, Gmail, WhatsApp and primary-channel gates remained disabled.
- Only synthetic household `06137360-ec42-4c93-9e11-f66551f27681` was allowed
  onto the production Nerve email path.
- The live BYO reconnect generation is the active canary; its predecessor is
  deleted and its five Nerve resource IDs are distinct from the replacement.
- Abrolia release 18 passed its Fly service check and `/healthz`; the reconnect
  identity remained `verified`. `/readyz` remains 503 only because the
  older cleanup job above is still `outcome_unknown`; PR #22's fix is deployed,
  but the quarantined job still needs one explicit operator reconcile.
- Resume with:
  `/resume_handoff thoughts/shared/handoffs/general/2026-08-05_23-38-46_general_phase-2-4-production-reconnect.md`

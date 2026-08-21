---
date: 2026-08-06T10:24:45+02:00
validator: Codex
git_commit: 41235b8a1a59faf9477b7afedc0493d6c430cda4
branch: main
repository: abrolia
plan: thoughts/shared/plans/2026-08-04-abrolia-phase-2-email-identity.md
status: runtime_deployed_pending_live_google_acceptance
---

# Validation Report: Abrolia Phase 2 — Email Identity

## Verdict

Phase 2 is **not fully accepted**, but its Gmail runtime implementation blocker
is remediated and merged through PR #28. The production Nerve paths are
accepted; Gmail now has an executing History worker, OAuth API sender selection,
restart-safe cursor handling and exact Sent reconciliation under process-level
tests. The immutable runtime image is published and pinned; the live Google
test-user lifecycle remains outstanding.

Ten of the twelve final Phase 2 success criteria are now supported without
qualification. Two remain partial: all-provider live lifecycle and opt-in live
staging. Nerve managed/BYO and the Phase 3 attachment contract passed live; the
dedicated Gmail lifecycle still requires an authorized Google Cloud operator,
runtime image deployment and end-to-end execution.

The Gmail path remains correctly fail-closed in production, so the incomplete
live acceptance does not expose real family data.

## Validated baseline

- Gmail runtime merge: `41235b8a1a59faf9477b7afedc0493d6c430cda4`
- Remediation PR: #28
- Published runtime image digest:
  `sha256:ed3794ecf26756b2b95da54d2155f5576bd4e66f88f8d0c7e4186347d095f1dd`
- Synthetic control-plane Machine: version 22, Fly service check passing
- Phase 2.4 live-DNS evidence: merged Abrolia PR #25
- Phase 3 Nerve live consumer contract: merged Abrolia PR #26
- Abrolia production: release 18, Machine `85e649c449e9e8`, service check passing
- Nerve production: control-plane release 69 and runtime release 16 healthy

## Final success-criteria audit

| # | Canonical criterion | Result | Evidence |
|---|---|---|---|
| 1 | Exact `@abrolia.com` addresses are household-isolated in Nerve | **Pass** | Generation-scoped org references, tenant-bound runtime keys, cross-org denial tests, and managed production cleanup/reconnect evidence |
| 2 | Managed, Gmail and own-domain choices share one durable state contract | **Pass** | `EmailOption`/typed selection and common identity/job state machine are exercised across managed, Gmail and BYO tests |
| 3 | One-time secrets go directly to household secret namespace | **Pass with safety caveat** | `SecretSink` precedes durable verification; one-time-loss/SIGKILL tests never falsely verify. Crash after sink success but before projection remains explicit `outcome_unknown` and operator-reconcilable rather than converging automatically |
| 4 | Nerve inbound/attachments and lifecycle blockers are implemented/tested | **Pass live** | Phase 3 production canary verified signed inbound, retry/dedup, PDF download with runtime scope, SDK 0.2.0 compose, delivery and exact cleanup |
| 5 | Gmail OAuth uses dedicated account confirmation and minimum scopes | **Pass automated** | PKCE/state binding, `prompt=select_account consent`, dedicated-mailbox confirmation and exact `openid email gmail.readonly gmail.send` scope contract |
| 6 | Gmail policy/CASA gates remain fail-closed until evidence is current | **Pass** | Config refuses Gmail unless OAuth verification, restricted-scope approval, CASA and Limited Use flags are all true. Production has no Gmail policy-gate secrets and reports `synthetic-only` |
| 7 | Nerve/Gmail ingress converge on one canonical RFC822 pipeline | **Pass automated** | `serve_runtime()` starts a bounded Gmail History worker that feeds `GmailHistorySource` through `EmailRuntimeService`; process tests prove baseline, append-before-cursor, restart continuity and dedup |
| 8 | Nerve/Gmail send preserve approvals, deterministic identity and unknown-outcome safety | **Pass automated** | The provisioned sender factory selects `GmailSendProvider`; immediate timeout and crash-left-pending paths reconcile exact RFC Message-ID in Sent, while absent/ambiguous results persist `outcome_unknown` without resend |
| 9 | Resume/reset/disconnect/export/delete pass for every provider | **Partial** | Runtime restart/cursor and idempotent revoke pass process tests, and managed/BYO cleanup passed live; the complete Gmail connect/receive/send/revoke/delete lifecycle is not yet live-accepted |
| 10 | Tenant escape, OAuth mix-up, webhook replay and prompt-injection tests pass | **Pass** | Full non-live and control-plane security matrices pass; Phase 3 live suite also verifies webhook signature windows and replay/dedup |
| 11 | Synthetic and opt-in live staging suites pass without secret leakage | **Partial** | Synthetic suite, managed/BYO production canaries and Nerve Phase 3 live contract pass. Dedicated Gmail connect/receive/approve-send/revoke/delete has not run live |
| 12 | No production path requests a Gmail password/app password | **Pass** | Production UI/config uses OAuth; legacy IMAP/app-password support remains only a test/migration seam and is not offered by onboarding |

## Automated verification

| Check | Result |
|---|---|
| `uv run --python 3.12 --no-project --with-requirements requirements-dev.txt pytest -m "not live" --import-mode=importlib -o addopts='' --disable-warnings` | **Pass — 879 passed, 2 deselected** |
| `uv run --no-project --with-requirements requirements-dev.txt pytest tests/control_plane --import-mode=importlib -o addopts='' --disable-warnings` | **Pass — 386 passed** |
| `ruff check .` | **Pass** |
| `python3 scripts/check_fixtures.py --all` | **Pass**; private deny patterns are CI-only and were not available locally |
| `gitleaks detect --source . --config .gitleaks.toml --log-opts=--all --redact --verbose` | **Pass — 77 commits scanned** |
| GitHub CI for `8c5028e` / PR #26 | **Pass** |
| Abrolia `/healthz` | **Pass** — database/volume/workers/providers healthy, no blockers |
| Nerve control-plane and runtime `/healthz` | **Pass** |
| `docker build -f deploy/runtime/Dockerfile -t abrolia-runtime:gmail-phase2 .` | **Pass** — production Python 3.12 image built; runtime Gmail imports pass inside the image |

The first local pytest attempt generated an untracked `uv.lock`; the repository's
fixture scanner correctly rejected four dependency-lock IBAN-like strings. The
lock was deleted and the authoritative rerun used `uv --no-project`, leaving the
worktree clean before this report was added. This was a validation-environment
artifact, not a product failure.

## Manual and external gates still open

1. Add the nominated operator-owned Gmail mailbox as a Google Auth Platform
   test user. The
   currently signed-in browser account lacks `oauthconfig.testusers.get` and
   project access, so this requires an authorized Google Cloud project owner.
2. Then run the dedicated Gmail test-user matrix against the separate Google test
   project: connect, initial History baseline, receive, approve/send, exact Sent
   reconciliation, restart/cursor continuity, revoke, reset and delete.
3. Perform the Phase 2.9 isolated control-plane backup/restore rehearsal with
   workers paused, then one synthetic onboarding smoke and exact cleanup.
4. Record the cross-provider lifecycle rehearsal as one operator artifact. Do
   not infer Gmail live acceptance from the hermetic Google server tests.
5. Parent MVP Phase 2 still asks for a manual unknown-group-member denial check
   and product-owner review of `docs/privacy/delete-runbook.md`. The runbook is
   technically complete and consistent with the implemented DSAR orchestration,
   but owner sign-off was not manufactured by this validation.
6. Google verification/CASA, legal DPIA review, DPA/SCC/TIA and processor gates
   remain mandatory before real family data. Their absence is currently enforced
   rather than bypassed.

## Non-blocking operational findings

- Abrolia health still reports one previously documented unrelated
  `outcome_unknown` cleanup job. It does not block Phase 2 email acceptance, but
  should be reconciled before readiness is used as a pilot gate.
- The parent MVP Phase 3 overview still says the first operator run remains,
  while the adjacent checked criteria and PR #26 record that it passed. This is
  documentation drift.
- The twelve final checkboxes in the Phase 2 plan are all still unchecked. The
  evidence now supports checking ten; all-provider live lifecycle and opt-in
  live staging remain partial.
- The Google web client JSON has the exact production callback URI and was
  restricted locally to mode `0600`. Client ID, client secret and the corrected
  Abrolia operator allowlist (`owner@example.test`) are deployed to the synthetic
  control plane. The separate connected mailbox remains the nominated
  operator-owned Gmail test user; real-family and Gmail policy gates remain off.

## Recommended canonical state

- **Phase 2 implementation:** runtime blocker remediated, merged and deployed as
  an immutable image pin.
- **Phase 2 Nerve paths:** live accepted.
- **Phase 2 Gmail path:** OAuth/core/runtime wiring passes automated gates; live Google lifecycle pending.
- **Real-family rollout:** blocked by explicit policy/legal gates.
- **Next action:** add the Gmail test user with an authorized Google Cloud
  account, then execute Gmail and isolated restore rehearsals before reconciling
  the final two checkboxes.

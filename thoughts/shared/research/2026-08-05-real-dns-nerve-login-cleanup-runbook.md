---
date: 2026-08-05T21:20:05+02:00
author: Codex
git_commit: 6dd422948ec194c8ff437e37e0fa94e49eb1d5de
branch: codex/phase-2-email-validation-remediation
repository: abrolia
topic: "Real DNS/Nerve, repeated login/reload, and cleanup validation"
tags: [research, phase-2.4, nerve, dns, onboarding, cleanup]
status: complete
last_updated: 2026-08-05
last_updated_by: Codex
---

# Real DNS/Nerve, repeated login/reload, and cleanup validation

## Executive summary

The complete Phase 2.4 validation cannot be executed safely by deploying Nerve PR #64 alone.

Two blockers remain:

1. Abrolia currently rejects `ABROLIA_REAL_EMAIL_ENABLED=1` at startup and its production container registers only synthetic email providers. A separate Abrolia rollout-wiring change is required before live Nerve/DNS validation.
2. Nerve PR #64 hard-deletes inboxes for bootstrap callers, but org cleanup is still a tombstone. Reconnecting the same household reuses the same org `external_ref`, and `EnsureOrg` rejects the deleted row as an idempotency conflict. Therefore delete-to-reconnect is not yet a passing Phase 2.4 gate.

Until both blockers are fixed, keep the real-email feature flags disabled and treat the procedure below as the post-fix validation runbook.

## 1. Preconditions and safe test scope

1. Deploy Nerve PR #64.
2. Land and deploy an Abrolia rollout-wiring change that:
   - constructs `NerveAdminSettings` from deployment secrets/configuration;
   - registers `NerveByoDomainProvisioner` as `nerve-byo-domain`;
   - passes that provider to `OnboardingService`;
   - enables real domains only through an explicit rollout gate;
   - wires an actual production mailer if public repeated-login delivery is part of the test.
3. Fix Nerve org tombstone reuse/restore semantics before testing cleanup followed by reconnect for the same household.
4. Use an operator-controlled staging subdomain such as `assistant.staging.example.net`. Do not use a family's apex domain or disturb an existing MX configuration.
5. Keep bootstrap/admin credentials in the deployment secret store. Do not paste API keys, magic links, or decrypted credentials into shell history, tickets, or evidence logs.

Current evidence for the first blocker:

- `ControlPlaneConfig.validate()` rejects any real email provider flag.
- `ControlPlaneContainer.build()` registers only the synthetic provider registry and does not construct a Nerve client/provider.
- The current deployment runbook deliberately keeps all real-email flags at zero.

## 2. Real DNS and Nerve provisioning

1. Sign in with the operator test account and choose **Family domain**.
2. Enter the dedicated staging subdomain. For an apex selection, the UI requires an explicit MX-impact acknowledgement; avoid apex for this validation.
3. Abrolia atomically stores the typed selection, email identity, and durable `ensure` job.
4. The worker calls Nerve in this order:
   - ensure org using `arbolia:household:<household_id>`;
   - ensure domain;
   - fetch the exact DNS record set.
5. While Nerve reports the domain as pending, Abrolia persists and displays the exact DNS records. Inbox, API key, and webhook are not created yet.
6. Add every record exactly as displayed: type, host/name, optional priority, and value. Do not normalize or substitute record values.
7. Use `dig` only as an external sanity check, for example:

   ```sh
   dig +short TXT <record-host>
   dig +short CNAME <record-host>
   dig +short MX <test-domain>
   ```

   Nerve's verification response remains the authoritative result.
8. Abrolia persists automatic checks with delays of 30, 60, 120, 300, and 600 seconds. Partial ownership/MX/SPF/DKIM/DMARC results remain `waiting_user`.
9. After the automatic attempts are exhausted, the step remains recoverable as `dns_manual_check_required`; the DNS instructions stay visible and **Check again** creates another durable inspection job.
10. When Nerve reports `active`, Abrolia creates the inbox, key, and webhook, installs the credential into the Fly namespace, and atomically marks the email identity and onboarding step `verified`.

Expected durable evidence is available through `GET /api/v1/onboarding/current`: it may contain public status, domain, DNS record states, and non-secret Nerve binding metadata, but must not contain the API key secret.

## 3. Reload and repeated login

### Reload while DNS is pending

1. Save the response from `GET /api/v1/onboarding/current`, especially `workflow_id`, domain, step state, and the full DNS record list.
2. Hard-reload `/onboarding`.
3. Confirm that the same records and statuses are rendered. The browser is not the source of truth; the server rebuilds the page from the durable snapshot.
4. If Nerve becomes active while the browser is in `waiting_user`, reload or press **Check again**. The browser does not continuously auto-refresh that state.

### Logout and login again

1. Call `POST /api/v1/auth/logout` from the same origin with the CSRF token. The route revokes the current session and removes the auth cookies; it does not delete the household or onboarding workflow.
2. Because the current production container uses `MemoryMailer`, public magic-link delivery is not yet a real deployment path. Until a production mailer is wired, generate a fresh operator link during a maintenance window with the API stopped:

   ```sh
   abrolia-control-plane invite owner@example.test
   ```

3. Start the API, consume the new link once, and return to `/onboarding`. Never store the link in logs or tickets.
4. Compare the new `GET /api/v1/onboarding/current` response with the saved response:
   - same household and workflow;
   - same selected domain;
   - same full DNS record set while pending;
   - same verified binding after success;
   - no second Nerve org/domain graph.

A new session may change session metadata, but must not restart domain provisioning.

## 4. Cleanup after the Nerve deployment

Start cleanup through Abrolia with `POST /api/v1/onboarding/reset/email_identity` or the **Change email** action. Abrolia quarantines unsettled work, sets the identity to disconnecting, and creates a durable cleanup job.

The provider cleanup order is strict:

1. webhook;
2. key;
3. inbox;
4. domain;
5. org.

For direct Nerve verification, use only the bootstrap `X-API-Key` credential. Bootstrap tenant-scoped routes require `?org_id=<ORG_ID>`.

| Step | Nerve request | Expected first result | Durable verification |
|---|---|---|---|
| Webhook | `DELETE /v1/webhooks/<id>?org_id=<org>` | `204` | webhook list has no matching id/external ref |
| Key | `DELETE /v1/keys/<id>?org_id=<org>` | `200`, `status=revoked` | key may remain, but `revoked_at` is non-null |
| Inbox | `DELETE /v1/inboxes/<id>?org_id=<org>` | `200`, `status=deleted` | inbox list has no matching id/external ref |
| Domain | `DELETE /v1/domains/<id>?org_id=<org>` | `200`, `status=deleted` | domain list has no matching domain |
| Org | `DELETE /v1/orgs/<id>` | `204` | org remains visible with non-null `deleted_at` |

Important details:

- Inbox hard-delete is the behavior added by Nerve PR #64 for bootstrap callers. A tenant JWT/service principal only disables the inbox and is not sufficient for this cleanup proof.
- Key revocation is intentionally soft and repeat deletion returns `absent_or_revoked`.
- Domain deletion returns `409` while inboxes or grants remain. A provider outcome-unknown response retains the durable domain row for reconciliation.
- Org deletion is a tombstone, not physical deletion; expecting a `404` for the org is incorrect.
- On a network error or `5xx`, re-list resources before retrying the same step. In Abrolia, keep `outcome_unknown` blocked and use the explicit reconciliation path rather than blindly replaying the whole worker operation.

After the Abrolia cleanup job succeeds, verify only redacted/durable metadata:

- cleanup job `succeeded`;
- external resource `deleted`;
- email identity `deleted`;
- domain reservation released;
- no unsettled or `outcome_unknown` cleanup work.

## 5. Phase 2.4 reconnect blocker

Do not mark delete-to-reconnect as passed after PR #64.

Nerve keeps the deleted org row and its stable `external_ref`. Abrolia reconnects the same household with the same value, `arbolia:household:<household_id>`. `EnsureOrg` finds the tombstoned row and returns an idempotency conflict instead of restoring it or creating a reusable org. The cleanup proof therefore ends at a valid tombstone; the next same-household connect currently fails.

The Phase 2.4 acceptance sequence becomes valid only after Nerve defines and implements one of these semantics:

- restore the tombstoned org on an idempotent ensure; or
- safely replace/reuse the tombstone while preserving tenant isolation and idempotency.

Then rerun the complete sequence: provision real staging DNS, verify, reload, logout/login, cleanup, reconnect the same household, and verify that exactly one fresh usable Nerve resource graph exists.

## Key code references

- `control_plane/config.py:50-60` — real-provider startup rejection.
- `control_plane/container.py:106-124` — memory mailer and synthetic-only provider wiring.
- `control_plane/providers/email/nerve_byo_domain.py:95-242` — ensure, DNS inspection, and finish flow.
- `control_plane/providers/email/nerve_byo_domain.py:298-307` — cleanup order.
- `control_plane/provisioning/worker.py:212-229,390-432` — persisted DNS polling schedule.
- `control_plane/api/auth.py:128-139` — logout behavior.
- `control_plane/onboarding/service.py:801-930` — email identity reset and cleanup scheduling.
- Nerve `internal/cloudapi/handler_inboxes.go:237-278` — bootstrap hard-delete versus tenant disable.
- Nerve `internal/store/email_tenancy.go:29-62,85-120` — tombstone and `EnsureOrg` conflict.

---
title: "Gmail forwarding spike — send-only scope and forwarded inbound"
date: 2026-09-14
repository: abrolia
plan: thoughts/shared/plans/2026-09-13-gmail-send-only-forwarding.md (Phase 0)
data_policy: synthetic accounts only
---

# Gmail forwarding spike (plan Phase 0, S1–S4)

## Setup

- **Agent Gmail:** a dedicated synthetic Google account, added as a test user of
  the Abrolia OAuth app (Cloud project `abrolia-508516`, publishing status
  Testing) through a Desktop OAuth client created for the spike.
- **Relay:** the synthetic Nerve canary inbox `canary-family-a`. Letters were
  sent from the synthetic peer canary `canary-family-b`. No personal mailbox took
  part.
- **Tooling:** two local scripts outside the repository (`~/abrolia-spike/`,
  `nerve-email==0.2.0`). Temporary Nerve runtime keys were created per run and
  deleted on exit (cleanup 200 every run). Google tokens were revoked at the end
  of each run. No secret was printed or stored in the repository.
- **Fixtures:** `tests/fixtures/email/gmail_forwarding/*.json` are synthetic
  reconstructions of the observed Nerve thread-message objects, with documentation
  domains and synthetic tokens.

## S4 — token with exactly openid + email + gmail.send

| Attempt | Observation |
|---|---|
| 1 | `403 access_denied` with no "Advanced" option. The account was not yet a test user of this project. |
| 2 | Granted `openid userinfo.email` only; every Gmail call 403. **Google's consent screen shows `gmail.send` as a checkbox; left unticked, the scope is not granted.** |
| 3 | Granted all three scopes; `messages.send` still 403. The Gmail API was not yet enabled in the project. |
| 4 (API enabled, box ticked) | Granted `gmail.send userinfo.email openid`. `users.getProfile` → 403 `PERMISSION_DENIED`, reason `insufficientPermissions`, "Request had insufficient authentication scopes". `users.messages.list` → the same. `users.messages.send` → **200**, body keys `id`, `labelIds`, `threadId`; `labelIds` `UNREAD, SENT, INBOX` (sent to itself). |

**Consequences**

- A successful send is confirmed by the send response; no read access is needed.
- 403 `insufficientPermissions` is distinguishable from a revoked grant.
  `hermes_cloud/email/google_client.py:222-224` currently treats every 401/403 as
  revoked.
- Onboarding must tell the family to tick "Send email on your behalf". The
  control plane already rejects and revokes a partial grant
  (`control_plane/providers/email/google_oauth.py:386-393`).
- The Gmail API must be enabled in the production client's project before rollout.

## S1 — forwarding confirmation

1. Gmail web → Settings → Forwarding and POP/IMAP → Add a forwarding address
   → relay address.
2. The confirmation reached the relay about 1 minute later. As delivered by
   Nerve REST (`GET /v1/inboxes/{id}/threads/{thread}`):
   - `direction` inbound; `from` `forwarding-noreply@google.com`; `to` the relay.
   - `subject`: `(Gmail Forwarding confirmation – Receive mail from <agent>`
     (leading parenthesis, **no code**).
   - `text` only (`html` empty), containing three links:
     `https://mail-settings.google.com/mail/vf-%5B…%5D-…` (confirm),
     `https://mail-settings.google.com/mail/uf-%5B…%5D-…` (cancel),
     `http://support.google.com/mail/bin/answer.py?answer=184973`.
3. Gmail showed "Verify <relay>" until the vf link was opened **and Confirm was
   pressed** on that page. Before that, nothing was forwarded. Letters that
   arrived before "Forward a copy of incoming mail to" was selected and saved
   were not forwarded afterwards.

**Consequences**

- Confirmation-link allowlist: host `mail-settings.google.com`, path prefix `/mail/vf-`.
- The Web chat instruction must name all three human steps: open the link,
  press Confirm, select "Forward a copy…" and Save Changes.

## S2 — letter forwarded by Gmail

- A synthetic school letter was sent from the peer canary to the agent Gmail
  (21:37:39 UTC). The forwarded copy was in the relay thread list at 21:37:45 UTC
  (about 6 s; Nerve logged the inbound `POST /v1/webhooks/resend` at 21:37:45).
- Nerve message:
  - `from` = **the original sender** (peer canary);
  - `to` = **the relay address**, not the Gmail address;
  - `cc` empty;
  - `subject` unchanged (no "Fwd:");
  - `text` = the original body;
  - fields: `attachments, attachments_state, cc, created_at, direction, from, html, id, subject, text, to` — no header data.

**Consequences**

- The reply target is the original sender. The plan's stop condition was not met.
- Nothing in Nerve's representation distinguishes a Gmail-forwarded letter from
  mail sent directly to the relay address. Binding to the Gmail account rests on
  the relay address being unguessable, as the plan assumes. ARC/DKIM
  verification would need Nerve to expose received headers.

## S3 — relay → agent Gmail check message

- Sent from the relay inbox to the agent Gmail at 21:38:19 UTC. Nerve listed
  the outbound message and, at 21:38:30 UTC (about 11 s), an **inbound** copy:
  `from` = relay, `to` = relay, same subject and body. It was not classified as
  spam.

**Consequence:** Strategy A (daily check message) is viable.

## Incidental findings

- **MCP 2026:** the Nerve runtime MCP tool `list_threads` timed out ("Max
  retries exceeded") right after the MCP 2026 schema transition. REST with
  `X-Nerve-Cloud-Key` worked. The Abrolia runtime already ingests over REST.
- **Nerve outage:** Nerve production was unavailable from 2026-09-12 17:22 to
  2026-09-14 09:59 UTC during that planned transition (runs 34708051602 →
  34830201738). Abrolia's `/readyz` stayed green throughout, because nothing
  probes Nerve availability. Not in this plan's scope; recorded for the go-live
  checklist.

## Cleanup owed after the spike

- Remove the relay forwarding address from the synthetic Gmail account, or keep
  it for the Phase 6 battery.
- Delete the spike Desktop OAuth client when it is no longer needed.

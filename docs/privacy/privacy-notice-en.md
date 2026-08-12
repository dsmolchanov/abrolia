# Privacy Notice

*Version: pilot draft, 2026-08-12. Controller details and the contact address for
data-subject requests are filled in. The Union representative under Art. 27 GDPR
and the supervisory authority of that representative's member state remain to be
recorded before the first real family.*
*The Russian version ([`privacy-notice-ru.md`](privacy-notice-ru.md)) is the
reference text; both are kept in sync.*

## In short

- We read **only what you send to the assistant's separate inbox or channel**:
  a forwarded email, photo, voice note, or message.
- The account/onboarding control plane stores only profile, setup state, and
  provisioning metadata; family emails and messages never enter it.
- We **never send or create anything** without your explicit confirmation — every
  action appears as a card and waits for your ✅.
- Your data is **not used to train models**.
- The application and database run **in the EU**; some services (the model,
  email delivery) are outside the EU and are listed by name below.
- You can obtain a **complete user-data export** and request account+runtime
  **deletion**; security secrets/hashes are not exported and minimal
  consent/DSAR/tombstone records remain for the stated period.

> **Pilot status.** The service currently runs on test data: provider
> agreements and international-transfer mechanisms are still being put in
> place, so we do not process real family correspondence yet. This text
> describes how processing will work and will be updated with actual details
> before the first family is connected. Real Gmail, WhatsApp, and Web-push
> adapters are disabled; Gmail additionally requires OAuth verification/CASA.

## Who processes your data

Controller — **Axiom Atlas, LLC**, a limited liability company formed under the
laws of the State of Delaware, USA, by Certificate of Formation dated 19 March
2025. Registered office: 131 Continental Dr, Suite 305, Newark, DE 19713, New
Castle County, USA. Registered agent: Legalinc Corporate Services Inc. The
Abrolia service operator is that same legal entity.

Questions, data-subject requests and withdrawal of consent: `support@abrolia.com`.

**No Union representative under Art. 27 GDPR has been designated yet.** The
controller is established outside the EU and has no establishment in the Union.
Until a representative is designated in writing, the service runs on synthetic
data only: no real family data is processed and the real provider adapters are
disabled and fail closed. The representative's name and address, and the
supervisory authority of that representative's member state, are recorded in
this notice before the first family is connected.

No Data Protection Officer has been appointed; whether one is required is
checked before any real family is connected.

You may lodge a complaint with the supervisory authority of your habitual
residence, place of work, or place of the alleged infringement (Art. 77 GDPR).
The authority of the representative's establishment will be named here once the
representative is designated.

## What we process and why

| Data | Source | Purpose |
|---|---|---|
| Abrolia account, verified recovery email, names, language, timezone, country | invite and profile | sign in, recover access, and configure the household |
| Session/security metadata and workflow/step/transition records | browser/control plane | protect the account, resume the exact step, and audit commands |
| Provisioning jobs/resource refs/config revisions/bootstrap lifecycle | control-plane worker | create one dedicated runtime and activate the right revision; browser secrets are excluded |
| Content of emails, messages, photos, voice notes | you send it to the agent inbox/channel | understand the obligation: what, when, how much, to whom |
| Verified channel ID, runtime actor/role, primary/fallback | owner-authorised binding | route cards, enforce rights, and select the proactive channel |
| Tasks, reminders, calendar events | your confirmations | deliver the service |
| Memory (facts about your household) | only via your per-entry confirmation | avoid asking the same thing twice |
| Action journal | system | accountability: who confirmed what |
| Technical logs | system | reliability; **message content is never written to logs** |

Legal bases: performance of our contract (account, onboarding, provisioning,
agent inbox, and channels); separate consent (memory, dedicated-WhatsApp risk,
and push where required); our legitimate interests (security, replay prevention,
and processing sender data in content you provide).

## Other people's data

A school email contains names of teachers, other parents, children. We process
them only as part of your email: no enrichment from external sources, no
profiles, no sharing beyond the providers listed below, and deletion on
schedule. Any of those people may contact us and request erasure of their data
from your instance of the service.

## Children

Children are not users of the service. Their data reaches us only inside your
emails; we build no child profiles and make no automated decisions about
children. See our minors policy for detail.

## Who we share with

| Provider | Role | Where |
|---|---|---|
| Anthropic | the model that reads the email text | US / global |
| Fly.io | metadata-only control plane, dedicated runtime, databases/secrets, and future shared-WA gateway | Netherlands (EU); provider control plane in the US |
| Resend | account magic-link delivery; delivery for Nerve-managed inboxes | US |
| Nerve | `@abrolia.com` or family-domain agent inbox (email a/c) | US |
| Google | calendar; a separate agent Gmail through OAuth only for email option b | global |
| Telegram / WhatsApp | chosen communication channels; WhatsApp shared/dedicated are separate Beta modes | outside the EU |
| Web Push provider | optional Abrolia Web push; not yet selected and disabled | TBD |

Some of these providers are outside the EU. Data processing agreements and
Standard Contractual Clauses with a transfer risk assessment are **still being
put in place**: until they are signed, the service does not process real family
correspondence. You can request a copy of the safeguards in force at the
contact address. We do **not** claim "EU-only processing" — with today's
providers that would be untrue.

## Retention

| Data | Retention |
|---|---|
| Source emails, photos, voice notes, attachments | 30 days |
| Conversation with the assistant | 180 days |
| Tasks and reminders | 90 days after completion |
| Action journal | 365 days |
| Email delivery receipts | 365 days with us; at the mail provider (Resend) per their own retention policy |
| Calendar events (our internal mapping to your calendar) | 365 days; the event itself stays in your Google account |
| Memory | until you delete it; we prompt a review every 90 days |
| Incoming messages on any channel (webhook, WhatsApp) before processing | 30 days — same as other source content |
| Messages that failed processing: content / technical failure reason | 30 days / 90 days |
| Outgoing emails and messages: text and recipient in the journal | 365 days; the copy at the recipient and at the mail provider is outside our control |
| Requests to the language model | not stored separately: the prompt is built at processing time and not retained; at Anthropic, per their terms, with no training on your data |
| Account/profile/membership, onboarding workflow/transitions, resource refs/config revisions | account/household lifetime + 30 days; the active config lives while the household is active |
| Magic/invite/reauth token record | link works for 15 minutes; hash-only record is removed 24 hours after use/expiry |
| Web session record | 24-hour idle and 30-day absolute life; hash/security metadata removed 30 days after revoke/expiry |
| Idempotency record | 24 hours |
| Provisioning/bootstrap encrypted payload / technical metadata | 30 / 90 days after settled/revoked/expired; no plaintext bootstrap token in the database |
| Access keys/OAuth/provider secrets | while the integration is active; never exported; revoked/deleted on disconnect/delete; bootstrap secret after activation |
| Technical logs (identifiers and statuses, no content) | 30 days |
| Backups | rolling 30-day window |
| Consent receipts and deletion tombstone | while consent is in force plus 3 years after withdrawal; tombstone for 3 years |
| Records of your rights requests (what was asked, what was done, when) | 3 years from closing the request |
| Incident records | 3 years from closing the incident |
| Family member identifiers and settings | while the account is active + 30 days |

The new control-plane periods are a provisional/configurable synthetic-pilot
policy, not a production promise; the owner and counsel review them before real
data. The three-year periods are our choice, not a statutory figure: the law requires
us to be able to demonstrate consent and to keep incident records, but names no
period. Three years follows the limitation period and will be confirmed by
counsel before launch.

This table lists every class of data we retain. The technical version with
storage locations and jurisdictions is in `docs/privacy/data-map.md`.

## If you chose a separate agent Gmail

- You first create a separate Google account for the assistant. We do not
  connect personal Gmail and never request a password or app password.
- The OAuth account chooser is always shown; after callback you confirm the
  selected address again. Scopes are only `gmail.readonly` and `gmail.send`.
- When this option is enabled, the refresh token goes directly to the
  household's Fly secret namespace; it is absent from the browser, control-plane
  database, job records, runtime manifest, model, and logs. Disconnect revokes
  the grant and removes token material.
- Immediately before OAuth, we explain what Gmail data Abrolia will access and
  why. Our use and transfer of that data follows the Google API Services User
  Data Policy, including its Limited Use requirements: it is used only to
  provide or improve the user-facing Gmail assistant, never for advertising,
  sale, credit decisions, or generalized model training. Human access is
  limited to consented support, security, legal, or operational cases allowed
  by that policy.
- This option fails closed for real families until Google verification/CASA;
  only a synthetic fake is used now.

## If you connected WhatsApp

- **Shared Abrolia number (Beta)** is a quick start only for family dialogue
  from pre-verified adult numbers. School/external/group chats are not routed.
  It requires a separate channel privacy-notice receipt.
- **Dedicated number (Beta)** uses a separate family SIM/eSIM through an
  unofficial linked-device session. The session can technically see messages
  on that number and there is a real blocking risk. It requires a separate
  informed-risk receipt before QR; disconnect logs the session out.

One receipt cannot replace the other. Every outgoing WhatsApp action waits for
explicit approval. The official Business Platform needs separate eligibility
and legal review and is not promised as a universal GA path.

## Where you talk to Abrolia

Telegram is recommended by default; verified WhatsApp and authenticated Abrolia
Web are alternatives. Primary controls proactive messages; replies stay in the
verified source channel. The owner's recovery email is only a fallback
notification (for Web without push, a link without sensitive content) and is
never the agent inbox. A primary change takes effect after a test receipt.

## Special categories of data

School emails sometimes contain health information (a sick note, an allergy, an
exemption) and occasionally religious information. We do not extract it or use
it in any product logic. Do not send this material, or any other special-category
personal data about any person, to the agent inbox or channel. Before choosing
an email identity, the household owner separately acknowledges this obligation;
the accepted text version is stored as an accountability receipt. If material is
sent by mistake, stop using it and request deletion at `help@abrolia.com`.

This restriction defines the permitted pilot scope, but it does not remove
Abrolia's data-protection obligations or make accidentally received material
"unprocessed". The law requires a specific condition for processing such data;
until that condition is documented, real family inboxes and channels are not
connected.

## Is providing data mandatory

Data required for the service (account/recovery contact, household profile,
verified channel binding, and content you send to the assistant) is provided
under our contract. Agent email identity and primary channel are your choices.
Memory, dedicated-WhatsApp risk consent, and optional push can be withdrawn
without affecting the other available parts of the service.

## Where data about other people comes from

Data about teachers, other parents and children reaches us not from them but
from content you send to the agent inbox/channel, and from messages arriving on connected
channels. Categories: names, contact details, roles, participation in events,
payment details. We do not collect it from external sources and do not enrich it.

## Automated decision-making

There is no automated decision-making producing legal or similarly significant
effects (Art. 22). The assistant only proposes; only what a human confirms is
carried out.

## Your rights

Access, rectification, erasure, restriction, objection, portability, withdrawal
of consent (withdrawal does not affect the lawfulness of processing before it),
and complaint to a supervisory authority. In practice:

- account-level `/export` combines control-plane metadata and the dedicated
  runtime; secrets and token/session/bootstrap hashes are excluded;
- account-level `/delete` requires fresh owner re-auth, revokes sessions/tokens,
  cleans runtime/providers and control-plane data, and never reports a partial
  or unknown cleanup as complete; a minimal three-year tombstone remains;
- what has already left our system (message copies in your Telegram, events in
  your Google Calendar, emails at their recipients) we cannot delete, and we say
  so plainly — data in backups disappears within the 30-day window.

We respond within one month.

## Changes

Material changes to this notice are announced in the assistant chat before they
take effect.

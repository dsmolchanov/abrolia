# Privacy Notice

*Version: 2026-09-13, production pilot. Controller details, the contact address
for data-subject requests, and the legal condition for special-category data are
filled in. A Union representative under Art. 27 GDPR has not yet been
designated.*
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

> **Service status.** Since 8 September 2026 Abrolia runs in production and
> processes real family correspondence. Available today: an `@abrolia.com`
> assistant inbox, an inbox on your family's own domain, a separate agent Gmail
> account, and Abrolia Web chat. While Google completes verification of the
> Gmail integration, Gmail can be connected only by accounts Abrolia has added
> as test users. WhatsApp, Telegram and Web push are not available yet.
> Open compliance items are stated below where they apply.

## Google user data (separate agent Gmail)

This section applies if you connect a separate Google account as your
assistant's mailbox. It describes **all** Google user data Abrolia accesses
through Google APIs; Abrolia accesses no other Google data. The same
information is shown in the app before you connect.

### What we access and why

Abrolia requests only these Google OAuth scopes:

| Scope | What it allows | Why Abrolia needs it |
|---|---|---|
| `gmail.readonly` | Read messages in the agent mailbox | When a new message arrives in the inbox of the agent mailbox, Abrolia reads its sender, recipients, date, subject, body and the names and types of attachments, to work out what your family needs to do and show you a proposal. Abrolia does not import the mailbox history: it follows new inbox messages only (if Gmail can no longer provide that change feed, Abrolia re-checks at most the 100 most recent inbox messages). Drafts, sent mail and other labels are not processed. |
| `gmail.send` | Send email from the agent mailbox | To send an email that an adult in your household has reviewed and confirmed on a proposal card. Abrolia never sends email automatically. |
| `openid`, `email` | Identify the connected Google account | To show you which address was connected and check that it is the separate agent account you intended, not a personal one. |

### How we use it

Google user data is used only to provide and improve the user-facing features
you use in Abrolia: turning incoming letters into proposals (events, tasks,
reminders, replies), answering your questions about those letters, and sending
emails you confirm. We do **not**:

- use Google user data for advertising, including personalized, retargeted or
  interest-based advertising;
- sell it, or transfer it to data brokers or information resellers;
- use it to determine creditworthiness or for lending purposes;
- use it to develop, improve or train generalized or non-personalized AI or
  machine-learning models. Problems we find are reproduced with synthetic
  examples; real messages are not copied into test or training data.

### AI processing

To understand a letter, its content is sent to our AI model provider,
**Anthropic** (Claude API), which processes it on our behalf under a data
processing agreement and commercial terms that do not permit using it to train
models. The result is shown to you as a proposal and is not used to train any
model.

### Who we share it with

We transfer Google user data only:

- to **Anthropic**, for the AI processing described above;
- to **Fly.io**, which hosts the dedicated environment where it is stored
  (Netherlands, EU);
- to the recipients of an email **you** confirm;
- when required by applicable law, or to protect against fraud, abuse or
  security threats;
- as part of a merger, acquisition or sale of assets, only after telling you
  and obtaining your consent.

### Storage, security and retention

- Stored in your household's dedicated environment in the Netherlands (EU),
  on encrypted disks; always transmitted over TLS.
- OAuth tokens are encrypted with AES-256-GCM and are never exposed to your
  browser, our account system, the AI model, or logs.
- Message content is deleted after **30 days**. Details of items you confirm
  follow the retention table below; emails sent on your confirmation are
  journaled for 365 days. Logs contain identifiers and statuses only, never
  message content. Backups roll off within 30 days.

### Human access

No one at Abrolia reads your Google user data, except: with your explicit
permission for specific messages (for example, when you ask for support);
when necessary for security purposes, such as investigating a bug or abuse; to
comply with applicable law; or where the data has been aggregated and
anonymized for internal operations.

### Revoking access and deleting data

- Remove Abrolia's access at any time in your Google Account at
  [myaccount.google.com/permissions](https://myaccount.google.com/permissions);
  Abrolia can no longer read or send from the mailbox after that.
- Ask us at `help@abrolia.com` to disconnect the mailbox: we revoke the grant
  with Google and delete the stored tokens.
- Deleting your Abrolia account revokes the grant, deletes the tokens and
  deletes the household's data, including Google user data; copies in backups
  disappear within 30 days.

### Limited Use

Abrolia's use and transfer to any other app of information received from Google
APIs will adhere to the
[Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
including the Limited Use requirements. The use of information received from
Google Workspace APIs will adhere to the
[Google Workspace API User Data and Developer Policy](https://developers.google.com/workspace/workspace-api-user-data-developer-policy),
including the Limited Use requirements.

Until Google completes verification of the Gmail integration, connecting Gmail
is available only to accounts Abrolia has added as test users.

## Who processes your data

Controller — **Axiom Atlas, LLC**, a limited liability company formed under the
laws of the State of Delaware, USA, by Certificate of Formation dated 19 March
2025. Registered office: 131 Continental Dr, Suite 305, Newark, DE 19713, New
Castle County, USA. Registered agent: Legalinc Corporate Services Inc. The
Abrolia service operator is that same legal entity.

Questions, data-subject requests and withdrawal of consent: `help@abrolia.com`.

**No Union representative under Art. 27 GDPR has been designated yet.** The
controller is established outside the EU and has no establishment in the Union.
The obligation applies to us: the Art. 27(2)(a) derogation is unavailable
because the processing is not occasional and involves special categories of
data, and Art. 27(2)(b) concerns public authorities. Until a representative is
designated in writing, you can reach the controller directly at
`help@abrolia.com`; the representative's name and address will be recorded in
this notice once designated.

**No Data Protection Officer is required** — determined on 2026-08-12 under
Art. 37(1): the controller is not a public authority, and neither regular and
systematic monitoring of data subjects on a large scale nor large-scale
processing of special categories is a core activity (the pilot is limited to
5–20 households, third-party special categories are outside the scope of the
service, and no profiling of data subjects takes place). The determination is
revisited when the pilot ends.

You may lodge a complaint with the supervisory authority of your habitual
residence, place of work, or place of the alleged infringement (Art. 77 GDPR).

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

## Special-category data (health, religion)

School emails often contain health data (a medical certificate, an allergy, a PE
exemption) and, indirectly, religion (an exemption for a religious holiday). For
the content you send to the agent inbox or channel, the legal condition is
**explicit consent, Art. 9(2)(a) GDPR**: your own, and — for your minor children
— given by you as a holder of parental responsibility. Consent is collected as a
separate item, never bundled with the terms, recorded in a versioned receipt, and
withdrawn in one step.

Special-category data **about other people** — other children, teachers, other
parents — is **outside the scope of the service**: no Art. 9(2) condition is
available for it, so such material should not be sent to the assistant. You
confirm this as a separate item before choosing an email identity. If such
material reaches us by mistake, stop using it and request deletion at
`help@abrolia.com`.

We do not extract or index health and religion attributes and build no profiles
from them. There is no server-side "medical filter" either: telling such an email
apart would require reading it, which is itself processing.

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
| Fly.io | metadata-only control plane, dedicated runtime, databases/secrets | Netherlands (EU); provider control plane in the US |
| Resend | account magic-link delivery; delivery for Nerve-managed inboxes | US |
| Nerve | `@abrolia.com` or family-domain agent inbox (email a/c) | US |
| Google | Gmail API for a separate agent Gmail account, if you choose that option — see "Google user data" | global |
| Telegram / WhatsApp | communication channels — not available yet, no data is shared | outside the EU |
| Web Push provider | optional Abrolia Web push — not selected, not used | TBD |

Some of these providers are outside the EU. Data processing agreements with
Standard Contractual Clauses are **in effect with Anthropic and Resend**. The
data processing agreement with Fly.io and the transfer impact assessments are
**still being put in place**. You can request a copy of the safeguards in force
at the contact address. We do **not** claim "EU-only processing" — with today's
providers that would be untrue.

## Retention

| Data | Retention |
|---|---|
| Source emails, photos, voice notes, attachments | 30 days |
| Conversation with the assistant | 180 days |
| Tasks and reminders | 90 days after completion |
| Action journal | 365 days |
| Email delivery receipts | 365 days with us; at the mail provider (Resend) per their own retention policy |
| Calendar events, where a family calendar is connected (our internal mapping) | 365 days; the event itself stays in that calendar |
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

The control-plane periods are a provisional, configurable pilot policy and are
reviewed as the pilot grows. The three-year periods are our choice, not a statutory figure: the law requires
us to be able to demonstrate consent and to keep incident records, but names no
period. Three years follows the limitation period and will be confirmed by
counsel before launch.

This table lists every class of data we retain. The technical version with
storage locations and jurisdictions is in `docs/privacy/data-map.md`.

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

Today you talk to Abrolia in authenticated Abrolia Web chat; Telegram and
WhatsApp are not available yet. The owner's recovery email is only a fallback
notification (a link without sensitive content) and is never the agent inbox.

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
"unprocessed". The condition we rely on for special-category data in content you
send is your explicit consent, described above.

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

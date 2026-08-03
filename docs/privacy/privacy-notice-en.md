# Privacy Notice

*Version: pilot draft, 2026-08-02. Controller details and the contact address for
data-subject requests are filled in before the first real family (marked TODO).*
*The Russian version ([`privacy-notice-ru.md`](privacy-notice-ru.md)) is the
reference text; both are kept in sync.*

## In short

- We read **only what you hand over**: a forwarded email, an email you tag
  `Hermes`, a photo, a voice note, a message to the assistant.
- We **never send or create anything** without your explicit confirmation — every
  action appears as a card and waits for your ✅.
- Your data is **not used to train models**.
- The application and database run **in the EU**; some services (the model,
  email delivery) are outside the EU and are listed by name below.
- You can **export everything** and **delete everything** at any time.

> **Pilot status.** The service currently runs on test data: provider
> agreements and international-transfer mechanisms are still being put in
> place, so we do not process real family correspondence yet. This text
> describes how processing will work and will be updated with actual details
> before the first family is connected.

## Who processes your data

Controller — Hermes Cloud service operator: TODO: legal name, registered
address, registration details, EU representative (if required under Art. 27).
Questions, requests and withdrawal of consent: TODO: contact address.
No Data Protection Officer has been appointed; whether one is required is
checked before any real family is connected.
You may lodge a complaint with the supervisory authority of your habitual
residence or place of work, or of the operator's establishment: TODO: authority.

## What we process and why

| Data | Source | Purpose |
|---|---|---|
| Content of emails, messages, photos, voice notes | you forward it / tag it | understand the obligation: what, when, how much, to whom |
| Your messenger ID, family role, language | onboarding | route cards and check permissions |
| Tasks, reminders, calendar events | your confirmations | deliver the service |
| Memory (facts about your household) | only via your per-entry confirmation | avoid asking the same thing twice |
| Action journal | system | accountability: who confirmed what |
| Technical logs | system | reliability; **message content is never written to logs** |

Legal bases: performance of our contract with you; your consent (memory, mailbox
access, WhatsApp channel); our legitimate interests (security, resilience, and
processing the data of senders that appears inside your email).

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
| Fly.io | hosting of the application and database | Netherlands (EU) |
| Nerve + Resend | mailbox and email delivery (managed-subdomain and own-domain options) | US |
| Google | your calendar; your mailbox only in the Gmail-access option | global |
| Telegram / WhatsApp | the channels you chose | outside the EU |

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
| Passwords and access keys (app password, channel tokens) | while the connection is active; deleted when the channel is disconnected or the account closed — no later than 30 days |
| Technical logs (identifiers and statuses, no content) | 30 days |
| Backups | rolling 30-day window |
| Consent receipts | for as long as the consent is in force, plus 3 years after withdrawal |
| Records of your rights requests (what was asked, what was done, when) | 3 years from closing the request |
| Incident records | 3 years from closing the incident |
| Family member identifiers and settings | while the account is active + 30 days |

The three-year periods are our choice, not a statutory figure: the law requires
us to be able to demonstrate consent and to keep incident records, but names no
period. Three years follows the limitation period and will be confirmed by
counsel before launch.

This table lists every class of data we retain. The technical version with
storage locations and jurisdictions is in `docs/privacy/data-map.md`.

## If you chose Gmail access

- We query **only messages labelled `Hermes`**. An unlabelled message never
  enters the system.
- Technically an app password grants access to the whole mailbox — we constrain
  ourselves to the label contractually and in code, and that constraint is
  covered by tests.
- You can revoke access without us: delete the app password in your Google
  account settings.

## If you connected WhatsApp

This channel is unofficial: the assistant operates through a WhatsApp Web
session on **your** number. That is outside WhatsApp's official terms, and there
is a **real risk that WhatsApp blocks the number**. We recommend a dedicated
number/SIM. The channel is only enabled after you confirm you accept this. You
can disconnect at any time by ending the session.

## Special categories of data

School emails sometimes contain health information (a sick note, an allergy, an
exemption) and occasionally religious information. We do not extract it and use
it in no logic at all. The law requires a specific condition for processing
such data; that condition is determined before any real family is connected,
and until then such emails are not processed by the system.

## Is providing data mandatory

The data required for the service (your messenger ID, the content you forward)
is provided under our contract: without it the service cannot work. Everything
else is your choice — memory, mailbox access and the WhatsApp channel are
enabled by consent and disabled by withdrawal, with no effect on the rest of
the service.

## Where data about other people comes from

Data about teachers, other parents and children reaches us not from them but
from the content you forward or label, and from messages arriving on connected
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

- `/export` — a full export of your data;
- `/delete` — erasure, initiated by the account owner with a confirmation code;
- what has already left our system (message copies in your Telegram, events in
  your Google Calendar, emails at their recipients) we cannot delete, and we say
  so plainly — data in backups disappears within the 30-day window.

We respond within one month.

## Changes

Material changes to this notice are announced in the assistant chat before they
take effect.

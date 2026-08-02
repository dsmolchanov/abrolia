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

## Who processes your data

Hermes Cloud service operator — TODO: legal name, address, registration details.
Questions and requests: TODO: contact address.

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

Transfers outside the EU rely on Standard Contractual Clauses with a transfer
risk assessment. We do **not** claim "EU-only processing" — with today's
providers that would be untrue.

## Retention

| Data | Retention |
|---|---|
| Source emails, photos, voice notes, attachments | 30 days |
| Conversation with the assistant | 180 days |
| Tasks and reminders | 90 days after completion |
| Action journal and delivery receipts | 365 days |
| Memory | until you delete it; we prompt a review every 90 days |
| Backups | rolling 30-day window |

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

## Your rights

Access, rectification, erasure, restriction, objection, portability, withdrawal
of consent, and complaint to a supervisory authority. In practice:

- `/export` — a full export of your data;
- `/delete` — erasure, initiated by the account owner with a confirmation code;
- what has already left our system (message copies in your Telegram, events in
  your Google Calendar, emails at their recipients) we cannot delete, and we say
  so plainly — data in backups disappears within the 30-day window.

We respond within one month.

## Changes

Material changes to this notice are announced in the assistant chat before they
take effect.

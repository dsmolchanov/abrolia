# Hermes Cloud

Family operations assistant for households living in a foreign language. Forward a school email, a photo of a notice, or a voice note — get back a structured proposal in your family's language (what it is, what's due, how much, by when). One tap turns it into a calendar event, a task, a reminder, or an approved outgoing email. Nothing outward-facing ever happens without explicit human confirmation.

- **Email layer**: [Nerve](https://github.com/dsmolchanov) — managed inbox on our domain, or bring your own domain (DNS verification).
- **Model layer**: Anthropic Claude API (commercial terms) — typed tools only, no shell, no direct send capability for the model.
- **Channels**: Telegram for confirmations and notifications (WhatsApp planned post-pilot via the official WhatsApp Business Platform).
- **Calendar**: family Google Calendar shared to a dedicated assistant account.
- **Privacy**: EU-hosted application with documented international subprocessors (model API, email delivery) under DPA/SCC; no training on your data; per-household isolation; full export and delete.

Status: pilot MVP under construction. See `thoughts/shared/plans/2026-08-02-family-ops-assistant-mvp.md` for the implementation plan.

## License

TBD

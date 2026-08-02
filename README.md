# Hermes Cloud

Family operations assistant for households living in a foreign language. Forward a school email, a photo of a notice, or a voice note — get back a structured proposal in your family's language (what it is, what's due, how much, by when). One tap turns it into a calendar event, a task, a reminder, or an approved outgoing email. Nothing outward-facing ever happens without explicit human confirmation.

- **Email layer**: three options per family — a managed inbox on our domain ([Nerve](https://github.com/dsmolchanov)), your own domain (DNS verification), or direct access to your existing Gmail (label-scoped: only messages you label `Hermes` are ever read).
- **Model layer**: Anthropic Claude API (commercial terms) — typed tools only, no shell, no direct send capability for the model.
- **Channels**: Telegram for confirmations and notifications; WhatsApp on the family's own number (QR-paired browser session — unofficial automation, risks disclosed at onboarding; official Business Platform is the GA path).
- **Calendar**: family Google Calendar shared to a dedicated assistant account.
- **Privacy**: EU-hosted application with documented international subprocessors (model API, email delivery); no training on your data; per-household isolation; full export and delete. DPAs and transfer mechanisms are still being put in place — see [`docs/privacy/processors.md`](docs/privacy/processors.md) for the actual, unfinished status.

Status: pilot MVP under construction. **Gate −1 (right-to-build) is open**: the engineering and privacy drafts exist and are under review, and the system runs on synthetic data only. Real data — including the owner's own mailbox — is not connected before Phase 2 and counsel sign-off.
See `thoughts/shared/plans/2026-08-02-family-ops-assistant-mvp.md` for the implementation plan and `thoughts/shared/implementations/2026-08-02-family-ops-assistant-mvp-validation.md` for the Gate −1 validation report.

## Documentation

- [`docs/SECURITY.md`](docs/SECURITY.md) — threat model, trust boundaries, vulnerability reporting
- [`docs/privacy/`](docs/privacy/README.md) — data map and retention, lawful bases, DPIA, processor register, privacy notice (RU/EN), minors policy, incident response
- [`docs/source-pins.md`](docs/source-pins.md) — pinned donor revisions
- [`docs/roadmap-channels.md`](docs/roadmap-channels.md) — post-MVP channels and voice
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — fixture sanitisation rules, secrets policy, working order

## License

TBD

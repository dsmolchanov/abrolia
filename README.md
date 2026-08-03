# Hermes Cloud

Family operations assistant for households living in a foreign language. Forward a school email, a photo of a notice, or a voice note — get back a structured proposal in your family's language (what it is, what's due, how much, by when). One tap turns it into a calendar event, a task, a reminder, or an approved outgoing email. Nothing outward-facing ever happens without explicit human confirmation.

- **Email layer**: three options per family — a managed inbox on our domain ([Nerve](https://github.com/dsmolchanov)), your own domain (DNS verification), or direct access to your existing Gmail (label-scoped: only messages you label `Hermes` are ever read).
- **Model layer**: Anthropic Claude API (commercial terms) — typed tools only, no shell, no direct send capability for the model.
- **Channels**: Telegram for confirmations and notifications; WhatsApp on the family's own number (QR-paired browser session — unofficial automation, risks disclosed at onboarding; official Business Platform is the GA path).
- **Calendar**: family Google Calendar shared to a dedicated assistant account.
- **Privacy**: EU-hosted application with documented international subprocessors (model API, email delivery); no training on your data; per-household isolation; full export and delete. DPAs and transfer mechanisms are still being put in place — see [`docs/privacy/processors.md`](docs/privacy/processors.md) for the actual, unfinished status.

Status: pilot MVP under construction. **Gate −1 (right-to-build) is open**: the engineering and privacy drafts exist and are under review, and the system runs on synthetic data only. Real data — including the owner's own mailbox — is not connected before Phase 2 and counsel sign-off.
See `thoughts/shared/plans/2026-08-02-family-ops-assistant-mvp.md` for the implementation plan and `thoughts/shared/implementations/2026-08-02-family-ops-assistant-mvp-validation.md` for the Gate −1 validation report.

## Running the Phase 1 slice (synthetic data only)

```bash
pip install -r requirements-dev.txt
export ANTHROPIC_API_KEY=...          # or put it in .env (gitignored)
export HERMES_CHAT=-100990000101      # Telegram chat the cards go to
export HERMES_OWNER=990000001         # who may export/delete household data
export HERMES_FAMILY_ACTORS=990000002 # who may confirm actions (comma-separated)
export HERMES_GUEST_ACTORS=990000003  # optional: read-only actors (nanny, grandparent)
export TELEGRAM_BOT_TOKEN=...         # optional: without it messages print to the console

python3 -m hermes_cloud.cli inject-eml tests/fixtures/email/forwarded_school_de.eml
python3 -m hermes_cloud.cli worker     # extraction → card with ✅ / ✏️ / ❌
python3 -m hermes_cloud.cli listen     # long-poll the channel, handle button presses
python3 -m hermes_cloud.cli status     # queue counters
python3 -m hermes_cloud.cli tick       # deliver reminders that came due
python3 -m hermes_cloud.cli reconcile  # settle effects left hanging by a crash
python3 -m hermes_cloud.cli dlq        # events that exhausted their attempts
python3 -m hermes_cloud.cli replay <event_id>
```

Without a bot (or with `--console`) the cards are printed to the terminal and
confirmed from there — same approval gate, different transport:

```bash
python3 -m hermes_cloud.cli --console worker
python3 -m hermes_cloud.cli --console pending
python3 -m hermes_cloud.cli --console confirm <approval_id>
python3 -m hermes_cloud.cli --console tick
```

Nothing leaves the machine without a human pressing ✅: the worker only stages a
proposal, and execution happens after `claim` (see [`docs/SECURITY.md`](docs/SECURITY.md)).

An ordinary message in the chat is a dialogue turn: the model sees only the
tools the sender's role allows, every tool call is journalled before it happens,
and a tool that changes anything only stages a proposal — the confirmation is
still a human pressing ✅.

Actor roles decide who may press it. The mapping comes from `household.toml`
when the file exists and from the variables above otherwise; anyone not listed —
and anyone writing from a chat outside `HERMES_CHAT` — gets zero capabilities,
not reduced ones.

Model benchmark and the extraction-model decision: [`bench/README.md`](bench/README.md).

## Documentation

- [`docs/SECURITY.md`](docs/SECURITY.md) — threat model, trust boundaries, vulnerability reporting
- [`docs/privacy/`](docs/privacy/README.md) — data map and retention, lawful bases, DPIA, processor register, privacy notice (RU/EN), minors policy, incident response
- [`docs/ontology.md`](docs/ontology.md) — the operational vocabulary: journals vs. statements, status model, competency questions
- [`docs/source-pins.md`](docs/source-pins.md) — pinned donor revisions
- [`docs/roadmap-channels.md`](docs/roadmap-channels.md) — post-MVP channels and voice
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — fixture sanitisation rules, secrets policy, working order

## License

TBD

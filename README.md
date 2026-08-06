# Abrolia

Family operations assistant for households living in a foreign language. Forward a school email, a photo of a notice, or a voice note — get back a structured proposal in your family's language (what it is, what's due, how much, by when). One tap turns it into a calendar event, a task, a reminder, or an approved outgoing email. Nothing outward-facing ever happens without explicit human confirmation.

- **Onboarding**: an invite-only Abrolia account leads through three resumable steps: email identity, WhatsApp identity, then the preferred communication channel. Account identity, runtime actor identity, and the assistant's mailbox are separate credentials.
- **Email identity**: three options in a fixed order — a recommended `@abrolia.com` assistant inbox ([Nerve](https://github.com/dsmolchanov)), a separate Gmail account created for the assistant and connected through Google OAuth, or the family's own domain after DNS verification. Abrolia never asks for a Gmail password or app password, and connecting a personal Gmail mailbox is not a product path.
- **Model layer**: Anthropic Claude API (commercial terms) — typed tools only, no shell, no direct send capability for the model.
- **WhatsApp identity**: a shared Abrolia number provides a Beta quick start for verified adults only; a dedicated family SIM/eSIM provides the full Beta WhatsApp contour through an explicitly consented QR-linked session. Eligibility and legal review for the official Business Platform is a separate post-MVP workstream, not an assumed GA migration.
- **Communication channel**: Telegram is recommended by default; verified WhatsApp and a minimal authenticated Abrolia Web surface are alternatives. The verified Abrolia-account recovery email remains the notification fallback and can never be the assistant mailbox.
- **Calendar**: family Google Calendar shared to a dedicated assistant account.
- **Privacy**: a metadata-only control plane on `app.abrolia.com` provisions a dedicated runtime per household. Family content stays out of the control plane; no training on your data; full export and delete span both control plane and runtime. The application is EU-hosted with documented international subprocessors. DPAs and transfer mechanisms are still being put in place — see [`docs/privacy/processors.md`](docs/privacy/processors.md) for the actual, unfinished status.

Status: pilot MVP under construction. **Engineering Gate −1 closed on 2026-08-03 for synthetic-only work.** It did not authorize real-family processing. Real data — including the owner's own correspondence — remains blocked until the legal basis for special-category data, counsel review, processor agreements and transfer safeguards are complete; the separate-agent Gmail option also remains blocked until Google OAuth verification/CASA prerequisites are complete.
See `thoughts/shared/plans/2026-08-02-family-ops-assistant-mvp.md` for the implementation plan and `thoughts/shared/implementations/2026-08-02-family-ops-assistant-mvp-validation.md` for the Gate −1 validation report.

## Running the onboarding control plane (synthetic data only)

The control plane requires independent urlsafe-base64 encryption, lookup-HMAC,
and token-HMAC keys and fails closed if they or the synthetic-only gates are
missing. See [`docs/onboarding-runbook.md`](docs/onboarding-runbook.md) for the
complete deployment and bootstrap procedure.

After the household profile is saved, the control plane durably ensures the
household's deterministic Fly app as an empty secret namespace. This early step
does not create a volume or Machine. Runtime storage, bootstrap material and the
Machine remain gated on all three verified onboarding choices.

```bash
export ABROLIA_ENCRYPTION_KEY_VERSION=v1
export ABROLIA_ENCRYPTION_KEY='<32-byte urlsafe-base64 test key>'
export ABROLIA_LOOKUP_HMAC_KEY='<independent 32-byte urlsafe-base64 test key>'
export ABROLIA_TOKEN_HMAC_KEY='<independent 32-byte urlsafe-base64 test key>'
export ABROLIA_PUBLIC_ORIGIN=https://app.example.test
export ABROLIA_SYNTHETIC_ONLY=1 REAL_FAMILY_DATA_ENABLED=0
abrolia-control-plane serve --host 127.0.0.1 --port 8080
```

## Running the existing runtime slice (synthetic data only)

The commands below exercise the dedicated household runtime, whose Python package
retains the historical `hermes_cloud` name. They do not create an Abrolia account
or run the new control plane. The IMAP poller is a legacy test seam retained for
pipeline compatibility; it is not offered by production onboarding.

```bash
pip install -r requirements-dev.txt
export ANTHROPIC_API_KEY=...          # or put it in .env (gitignored)
export HERMES_CHAT=-100990000101      # Telegram chat the cards go to
export HERMES_OWNER=990000001         # who may export/delete household data
export HERMES_FAMILY_ACTORS=990000002 # who may confirm actions (comma-separated)
export HERMES_GUEST_ACTORS=990000003  # optional: read-only actors (nanny, grandparent)
export TELEGRAM_BOT_TOKEN=...         # optional: without it messages print to the console
export HERMES_LEGACY_IMAP_TEST_ONLY=1 # required only for the deprecated fixture seam below

python3 -m hermes_cloud.cli inject-eml tests/fixtures/email/forwarded_school_de.eml
python3 -m hermes_cloud.cli gmail-poll --baseline  # synthetic legacy-IMAP seam: initialise cursor
python3 -m hermes_cloud.cli gmail-poll   # synthetic legacy-IMAP seam only
python3 -m hermes_cloud.cli worker     # extraction → card with ✅ / ✏️ / ❌
python3 -m hermes_cloud.cli listen     # long-poll the channel, handle button presses
python3 -m hermes_cloud.cli status     # queue counters
python3 -m hermes_cloud.cli tick       # deliver reminders that came due
python3 -m hermes_cloud.cli reconcile  # settle effects left hanging by a crash
python3 -m hermes_cloud.cli retention  # delete what the retention matrix says is due
python3 -m hermes_cloud.cli export     # runtime owner-actor: export this runtime only
python3 -m hermes_cloud.cli delete     # runtime owner-actor: wipe this runtime only
python3 -m hermes_cloud.cli backup     # encrypted snapshot (HERMES_BACKUP_KEY)
python3 -m hermes_cloud.cli restore <archive> --target /data/restored.db
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

Outgoing email is the one action with no way back, so it carries three locks:
the recipient is shown on its own line and bound to the confirmation by
payload_sha, `HERMES_EMAIL_SEND=1` is re-read immediately before transport, and
a dropped connection is reported as an unknown outcome rather than retried.

WhatsApp uses the same gate. A relay POSTs to `/v1/whatsapp/webhook` with
`X-Relay-Signature` (HMAC-SHA256 from `HERMES_WHATSAPP_RELAY_SECRET`); the
runtime accepts only its configured `HERMES_WHATSAPP_INSTANCE`. Delivery uses
that household's `HERMES_WHATSAPP_API_URL` and `HERMES_WHATSAPP_API_KEY`, and
stays disabled until `HERMES_WHATSAPP_SEND=1`. Every message—including a reply
to a known family actor—is staged and shows its number and full text before ✅.

Actor roles decide who may press it. The mapping comes from `household.toml`
when the file exists and from the variables above otherwise; anyone not listed —
and anyone writing from a chat outside `HERMES_CHAT` — gets zero capabilities,
not reduced ones.

`HERMES_OWNER` is a channel actor inside one dedicated runtime, not the owner of
an Abrolia Web account. Likewise, the runtime `export` and `delete` commands are
only one side of the complete data-subject workflow: the control plane must also
authenticate the account owner, orchestrate runtime cleanup and remove its own
account/onboarding metadata before deletion is reported complete.

Model benchmark and the extraction-model decision: [`bench/README.md`](bench/README.md).

## Documentation

- [`docs/SECURITY.md`](docs/SECURITY.md) — threat model, trust boundaries, vulnerability reporting
- [`docs/privacy/`](docs/privacy/README.md) — data map and retention, lawful bases, DPIA, processor register, privacy notice (RU/EN), minors policy, incident response
- [`docs/ontology.md`](docs/ontology.md) — the operational vocabulary: journals vs. statements, status model, competency questions
- [`docs/restore.md`](docs/restore.md) — backup format, restore procedure, what a restore cannot bring back
- [`docs/source-pins.md`](docs/source-pins.md) — pinned donor revisions
- [`docs/nerve-phase3-live-contract.md`](docs/nerve-phase3-live-contract.md) — operator runbook for the synthetic Nerve Phase 3 live contract
- [`docs/roadmap-channels.md`](docs/roadmap-channels.md) — post-MVP channels and voice
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — fixture sanitisation rules, secrets policy, working order

## License

TBD

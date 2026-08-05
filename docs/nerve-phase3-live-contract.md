# Nerve Phase 3 live consumer contract

This suite closes Abrolia's consumer-owned part of MVP Phase 3. There is no
staging Nerve environment: it uses two clearly synthetic, org-scoped production
canaries while the global `attachments` feature flag remains off. It never
writes feature flags and therefore cannot enable the feature globally.

The test proves, as one end-to-end contract:

- the real `email.received` body verifies with `X-Nerve-Signature` at both
  edges of the ±5 minute window and fails immediately outside it;
- an exact replay is rejected and a provider-style retry with a fresh signed
  timestamp maps to the same single durable Abrolia event;
- a synthetic PDF is discovered in the REST thread response and downloaded
  byte-for-byte with the household runtime key (`nerve:email.read`);
- `nerve-email==0.2.0` composes a synthetic PDF and Nerve confirms delivery to
  a separate external test mailbox;
- temporary runtime keys, the org webhook and the webhook.site receiver are
  removed in `finally`, including after a failed assertion.

## Preconditions

- Use only synthetic canary and peer organizations/inboxes. Never point the
  suite at a real family's mailbox.
- The canary and peer orgs must have the org-scoped `attachments=true` flag.
  The global default must remain off; verify this in the Nerve operator surface
  before and after the run. The suite deliberately has no feature-flag writer.
- `NERVE_EXTERNAL_MAILBOX` must be a designated test mailbox different from
  `NERVE_CANARY_ADDRESS`.
- Install the exact promoted SDK with `pip install -r requirements-dev.txt`.

## Required environment names

Secret values must be supplied through the operator's secret manager or
ephemeral shell environment and must never be committed, pasted into reports,
or written to `.env` in this repository.

Secrets:

- `NERVE_ADMIN_KEY` — bootstrap/admin credential used only to create and delete
  temporary scoped keys and the temporary webhook.

Non-secret identifiers and endpoints:

- `NERVE_CONTROL_PLANE_ORIGIN`
- `NERVE_RUNTIME_ORIGIN`
- `NERVE_CANARY_ORG_ID`
- `NERVE_CANARY_INBOX_ID`
- `NERVE_CANARY_ADDRESS`
- `NERVE_CANARY_PEER_ORG_ID`
- `NERVE_CANARY_PEER_INBOX_ID`
- `NERVE_EXTERNAL_MAILBOX`

The webhook signing secret and both runtime keys are generated for the run,
kept in memory, and deleted during cleanup.

## Operator command

For the existing production canaries, the non-secret identifiers are recorded
in the completed Nerve Phase 7 handoff. The local Nerve dashboard environment
contains the admin credential but does not export it automatically. Load it and
set the canary contract explicitly:

```bash
set -a
source ../nerve-cloud/dashboard/.env.local
set +a

export NERVE_CONTROL_PLANE_ORIGIN=https://nerve-control-plane.fly.dev
export NERVE_RUNTIME_ORIGIN=https://nerve-runtime.fly.dev
export NERVE_CANARY_ORG_ID=326a4ad5-27c9-4d89-a034-ebcd796c1f76
export NERVE_CANARY_INBOX_ID=5260272d-e8cb-470c-b9e1-9697c3945a65
export NERVE_CANARY_ADDRESS='canary-family-a'@'abrolia.com'
export NERVE_CANARY_PEER_ORG_ID=1d5b28b5-0717-46bc-a05a-66a18503fc7d
export NERVE_CANARY_PEER_INBOX_ID=ec151dac-7d87-4fc4-af8b-96c129f758a9
export NERVE_EXTERNAL_MAILBOX='<a real mailbox you control outside Abrolia/Nerve>'
```

Do not use either canary address, an `abrolia.com` address, `example.com`, or
another reserved/test domain as `NERVE_EXTERNAL_MAILBOX`; that criterion must
exercise delivery to a real mailbox outside Nerve. Keep the actual address in
your local environment and do not paste it into an issue or test report. Then
run:

```bash
ABROLIA_NERVE_LIVE_CONFIRM=synthetic-production-canary \
pytest -m live tests/live/test_nerve_phase3_contract.py -q -s
```

Ordinary CI continues to use `pytest -m "not live"`; the suite is never run by
a pull request or a normal push. A successful run may record only the test
result and generated non-secret resource IDs. Do not record request bodies,
headers, runtime keys, the admin key, or the one-time webhook secret.

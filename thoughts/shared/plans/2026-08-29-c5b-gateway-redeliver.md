---
title: "C5b — the ingress WAL is read back, and what can never be delivered stops being kept"
status: active
created_at: "2026-08-29"
repository: abrolia
parent_plans:
  - thoughts/shared/plans/2026-08-23-go-live-checklist.md
scope: c5b
data_policy: synthetic-only-until-explicit-gates
---

# C5b — the ingress WAL is read back, and what can never be delivered stops being kept

First of the three slices C5 has left after C5a reconciled the signature
(`#85`). The other two — the relay-key provisioning path (C5c) and the HTTP
entrypoint plus its deploy unit (C5d) — are separate branches and are named
here only where this slice's boundaries touch them.

## The defect

`GatewayStore` exists to make ingress durable before the webhook is ACKed, and
it half works: `persist_before_ack` writes the row, `mark_delivered` deletes it
once the runtime confirms. **Nothing ever reads a row back.** The write-ahead
log has no reader, so every row a delivery failure leaves behind stays there
forever — the durability it was built for was never spent.

`tests/test_gateway_routing.py:104` already says so, in a comment describing a
mechanism that does not exist:

```python
# Missing key — not delivered, WAL kept for reconcile
```

Kept for a reconcile nobody wrote. This slice writes it.

### The four outcomes the code cannot currently tell apart

`handle_webhook` returns `hmac_rejected` for four different situations and
leaves the WAL row in three of them, two by intent and one by accident:

| situation | today's code | today's row | can retrying ever succeed? |
| --- | --- | --- | --- |
| route denied (unknown/ambiguous sender) | `unknown_sender` / `ambiguous_sender` | deleted | no — correctly dropped |
| **relay key absent** for the household | `hmac_rejected` | kept | **yes** — C5c provisions the key later |
| **signature does not verify** against a present key | `hmac_rejected` | kept | **no** — the bytes will never verify |
| **runtime delivery raised** | `hmac_rejected` | kept | **yes** — the runtime may come back |

The third row is the leak. A payload whose signature is wrong under a key that
is present cannot become right, and the row holding it is family message
content sitting at rest indefinitely — the retention problem the rest of this
codebase is careful about, arrived at by accident through a `return` that
skipped the delete.

The second and fourth rows are the ones the WAL is *for*, and they are
indistinguishable from the third at the only place a reader could look.

## Design

### D1. The outcome says what to do with the row

`handle_webhook` stops answering `hmac_rejected` to mean four things. The
distinction it needs is not "was the HMAC bad" but **"can this ever be
delivered"**, because that is the only question the WAL reader has to ask.

* `hmac_rejected` — a present key rejected these bytes. Terminal. The row is
  deleted, exactly as a denied route is: keeping it stores message content that
  no future state of the system can act on.
* `relay_key_absent` — no key for the household. Retryable, and the reason it
  is retryable is C5c: the key does not exist yet and is expected to. This is
  the case the existing comment meant.
* `runtime_unavailable` — the delivery call raised. Retryable.

The two new codes are the honest names for cases that were already happening;
none of them changes what the gateway ACKs, only what it records about why.

### D2. The row carries what a reader needs, and no more

`gateway_ingress` gains `attempts`, `next_attempt_at` and `last_error`. The
vestigial `delivered` column stays as it is — `mark_delivered` deletes the row
rather than setting it, so the column has never held a 1 — because changing a
column no reader consults is churn this slice does not need. It is called out
in "Deliberately not here" rather than left for the next reader to wonder about.

Backfill is not a question: the table is created by `CREATE TABLE IF NOT
EXISTS` in a SQLite file that belongs to a gateway process, not to the control
plane's migration chain, and no deployed gateway exists to hold rows (C5d is
what deploys one). The store adds the columns with `ALTER TABLE` when it opens
a file that predates them, so a developer's local WAL survives.

### D3. The reader is a worker, and it gives up in a bounded way

`GatewayRedeliverWorker.run_once` claims rows whose `next_attempt_at` has
passed, re-routes each one, and re-delivers. Re-routing rather than trusting
the stored sender's old answer is the point: a row kept because no key existed
is retried *after* C5c provisions one, and a row whose household has since gone
away must not be delivered to it.

Backoff is exponential from a base of 30s. After `MAX_ATTEMPTS` the row is
dropped and counted, because a gateway WAL is not a dead-letter store: it holds
message content, and the alternative to dropping is keeping family messages
indefinitely for an operator who has no interface to read them. **What is
retained is the fact, not the payload** — the drop is counted and logged with
the ingress id and the last error code, never the body or the sender.

`MAX_AGE_SECONDS` bounds it in the other direction, so a row whose attempts are
still low cannot outlive the window in which delivering it would mean anything.
A WhatsApp message delivered three days late is not a repair.

## The worker's invariants

Written down because not writing them down is what went wrong: four review
generations found these one at a time, each fix locally correct and incomplete
because the rule was being inferred from the last defect rather than stated.
`GatewayRedeliverWorker._process` is ordered to satisfy them in this order, and
each has a test named for it.

1. **Classification is never gated.** A row's outcome is either TERMINAL —
   too old, no provenance, nobody to deliver to, a signature that does not
   verify — and deletes the row, or RETRYABLE and keeps it. Terminal outcomes
   are retention and security decisions that hold whether or not traffic is
   flowing. The kill switch sits exactly on that seam: it stops sending and
   retrying, never classification, so an incident does not also suspend
   cleanup.
2. **One clock per row, read when that row is processed.** A batch is up to
   `limit` rows with a blocking call in each. A batch-start stamp goes stale
   inside the batch, and a backoff scheduled after a delivery attempt is
   measured from after it, because the attempt consumed real time.
3. **An attempt is spent only on a genuine try that could succeed later.** No
   key yet, and the runtime refusing. A hold spends none — an operator brake
   must not exhaust the work it was pulled to protect. A terminal drop spends
   none, because nothing is left to try.
4. **Re-routing decides whether to deliver, never to whom.** On the channel
   the row arrived on, and the answer must agree with the household the row
   was accepted for.
5. **Nothing is signed that was not proven to come from the relay.** The
   stored timestamp and signature are verified against the key that later
   appears before anything is re-signed.

Structurally: every row yields exactly one outcome, and a row that fails
unexpectedly is deferred rather than abandoning the batch.

## Risks

**A retained payload may never have been authenticated.** The
`relay_key_absent` path keeps a row without verifying it, because there is no
key to verify with. Signing that body with the real key when one appears would
launder a forged payload into the runtime's trusted ingest — the gateway's
signature is precisely the runtime's reason to trust it. So the row keeps the
timestamp and signature it arrived with, and redelivery PROVES authenticity
against the now-present key before signing anything. A row that cannot answer
— written before these columns, or retained by the earlier implementation after
a rejected HMAC — is dropped rather than blessed.

**A redelivery is a second delivery if the first only appeared to fail.**
`runtime_deliver` raising does not prove the runtime did not receive the
payload. The runtime's own ingest is the layer that has to be idempotent, and
it already is — `hermes_cloud/ingest/whatsapp_webhook.py` keys the durable
event queue by the relay's provenance. This slice therefore does not add a
second idempotency mechanism, and the acceptance below pins that the redelivery
carries the same identity the first attempt did rather than a fresh one.

**Dropping at `MAX_ATTEMPTS` loses a message.** It does, and the alternative
loses privacy instead. The count is the operator's signal that it happened.

## Acceptance

* A delivery failure leaves a row, and the worker delivers it on the next run.
* A payload retained while no key existed is verified against the key that
  later appears, and dropped if it does not verify.
* A row with no provenance is dropped, never signed.
* A sender rebound from household A to B does not carry A's message into B.
* A row retained on a non-default channel is re-routed on THAT channel.
* The kill switch stops the worker too, holding the backlog rather than
  dropping it and without spending its attempts.
* The worker reads the same WAL the router writes on the default
  configuration, where no `ingress_path` is given.
* A row is never on disk without the household it was accepted for, so a
  process killed mid-delivery restarts into a row it can still redeliver.
* The brake stops the REST of a batch, not only the next one, and holds a
  keyless row without spending its attempts — an incident must not exhaust the
  work the brake was pulled to protect.
* A row kept because the relay key was absent is delivered once a key appears,
  without the payload being re-signed — the C5c seam, tested here with the key
  simply arriving.
* A signature rejected by a present key leaves NO row.
* A row past `MAX_ATTEMPTS` is dropped and counted; a row past `MAX_AGE_SECONDS`
  is dropped and counted.
* Backoff grows, so a permanently unavailable runtime is not retried hot.
* `check_fixtures --all` clean, ruff clean, full suite green.

## Deliberately not here

* **The HTTP entrypoint and the deploy unit (C5d).** The worker is a class with
  a `run_once`, called by tests. Nothing schedules it in production yet because
  nothing deploys the gateway yet, and inventing a scheduler for a process that
  does not exist would be built against a guess.
* **The relay-key provisioning path (C5c).** This slice makes the absence of a
  key a retryable outcome and proves the retry works when one appears. It does
  not create keys.
* **Retiring the `delivered` column.** Vestigial, harmless, and not this
  slice's subject — see D2.

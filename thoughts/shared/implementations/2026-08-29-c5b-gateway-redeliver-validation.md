---
title: "C5b validation — the ingress WAL is read back"
status: complete
created_at: "2026-08-29"
repository: abrolia
plan: thoughts/shared/plans/2026-08-29-c5b-gateway-redeliver.md
scope: c5b
---

# C5b validation

Compares what landed against
`thoughts/shared/plans/2026-08-29-c5b-gateway-redeliver.md`.

## Acceptance, line by line

| plan says | landed | evidence |
| --- | --- | --- |
| A delivery failure leaves a row, and the worker delivers it on the next run | yes | `test_a_delivery_failure_is_redelivered_on_the_next_run` |
| A row kept because the key was absent is delivered once a key appears | yes | `test_a_row_kept_for_a_missing_key_is_delivered_once_the_key_arrives` |
| A signature rejected by a present key leaves NO row | yes | `test_per_household_relay_hmac_and_durable_before_ack`, `_ingress_count(...) == 0` |
| Past `MAX_ATTEMPTS` → dropped and counted | yes | `test_a_row_is_dropped_and_counted_once_it_can_no_longer_be_repaired` |
| Past `MAX_AGE_SECONDS` → dropped and counted | yes | same test, second half |
| Backoff grows | yes | same test — the loop only reaches the drop because each pass is deferred further out |
| sanitizer / ruff / suite | yes | `check_fixtures --all` clean, `ruff check .` clean, suite green excluding `tests/live` |

`tests/live/test_nerve_phase3_contract.py` fails on any machine without
`ABROLIA_NERVE_LIVE_CONFIRM`; it is excluded from CI and fails identically on a
clean tree.

## Two things the plan did not anticipate

**The store and the router kept different clocks.** `persist_before_ack` read
`time.time()` directly while the router took an injectable `now_fn`, and
`MAX_AGE_SECONDS` compares one against the other. Under any injected clock the
age check silently never fires — the expiry test found this by failing, not by
being written for it. `persist_before_ack` now takes `now` from the caller, so
the two ends of the comparison share a clock.

**Re-routing needs a fresh timestamp, not the stored one.** `route` refuses
`None` outright in strict mode and refuses anything outside
`REPLAY_WINDOW_SECONDS` in either mode. Passing the message's original stamp
would have made every redelivery answer `timestamp_replay` and drop the whole
backlog as undeliberable — the exact opposite of the slice's purpose. The
window guards an inbound webhook's freshness; a redelivery's own freshness is
now. Pinned by `test_a_redelivery_is_re_routed_so_a_revoked_sender_does_not_land`,
which would fail on the drop count if the re-route were denied for the wrong
reason.

## Red-phase honesty

The four worker tests were written against code that did not exist, so they
were red for the trivial reason that `GatewayRedeliverWorker` was unimportable.
That is weaker evidence than a test that fails against a complete
implementation, and it is worth saying rather than implying otherwise.

The one test here that IS red against working code is the WAL assertion added
to `test_per_household_relay_hmac_and_durable_before_ack`: before this slice
that path returned `hmac_rejected` and kept the row, so
`_ingress_count(...) == 0` fails on the prior implementation.

## Deviations from the plan

None in scope. The plan's "Deliberately not here" list is unchanged: no HTTP
entrypoint, no deploy unit, no scheduler, no relay-key creation, and the
vestigial `delivered` column is left alone.

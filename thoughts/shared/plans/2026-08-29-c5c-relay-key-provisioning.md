---
title: "C5c — the relay key exists, and the gateway can see a binding"
status: active
created_at: "2026-08-29"
repository: abrolia
parent_plans:
  - thoughts/shared/plans/2026-08-23-go-live-checklist.md
scope: c5c
data_policy: synthetic-only-until-explicit-gates
---

# C5c — the relay key exists, and the gateway can see a binding

Second of the three slices C5 had left. C5a reconciled the signature (#85),
C5b gave the ingress WAL its reader (#87). This one creates the key both of
them assume.

## The defect

Two things are missing, and the checklist named neither of them correctly
until #87 fixed the text. `provisioning/secrets.py` has been on disk
throughout — what is absent is the KEY:

* **`WhatsAppGatewayRouter.relay_keys` is populated only by tests.** The
  gateway signs each delivery with a per-household relay key and the runtime
  verifies it with `HERMES_WHATSAPP_RELAY_SECRET`
  (`hermes_cloud/runtime/service.py:100`). Nothing generates that value and
  nothing installs it, so `_whatsapp_config` raises `RuntimeNotReady` and no
  WhatsApp message can reach any household. C5b's `relay_key_absent` is the
  outcome that waits for this.
* **`channel_bindings.external_id_hmac` is always NULL**
  (`repositories/bindings.py:826`). A gateway in strict mode looks a sender up
  by that digest, so every binding the control plane writes is invisible to it.
  `test_hmac_column_stays_null_until_c5_provisions_the_key` is the test that
  should start failing here.

## The invariants

Written down before implementing, because C5b's four review generations were
one function whose rules were never stated.

**K1 — The gateway holds roots, never household data.** It is constructed with
the database, a sender key and a relay root, and no field cipher. Its two
secrets let it compute a lookup digest and sign a delivery. Neither lets it
read anything a household owns.

**K2 — A per-household relay key is DERIVED, never stored.**

    relay_key(household) = HMAC-SHA256(relay_root, "abrolia-relay-key-v1:" + household_id)

Not a design preference — a consequence of two facts. `FlySecretSink` can
`install`, `contains` and `delete`, and cannot read: Fly secrets are
write-only, which is what makes them safe. And the gateway has no field
cipher, so a key stored encrypted in `channel_bindings` would need a read path
that does not exist and should not be built for the routing layer.

Derivation gives both ends the same value from a root each already has to
hold: the control plane derives it to install into the runtime, the gateway
derives it to sign. No storage, no new read path, and rotation is one root
change rather than a per-household migration. The version prefix is in the
label so a future scheme can coexist with this one during a rotation.

**K3 — The lookup digest is a projection, written at one funnel.**
`external_id_hmac` is `sender_hmac(external_id)` and nothing else. Both
writers — `ensure_owner_binding` and `verify_challenge` — reach
`ChannelBindingsRepository._insert`, so the digest is computed there and every
writer gets it. Being a pure projection is also what makes a repair possible:
anything that can write it can recompute it for a row that predates the key.

**K4 — The gateway's sender key is NOT the control plane's lookup key.** The
repository already holds a `LookupHasher`, and reusing it would be the short
path. It is the wrong one: that key digests email addresses and every other
equality lookup in the system, and handing it to the routing layer would let
the layer that resolves senders correlate identifiers it has no business
seeing. A separate `ABROLIA_GATEWAY_SENDER_HMAC_KEY`, held by exactly the two
sides that need it.

**K5 — Absent keys degrade to today's behaviour, they do not crash.** Both
values are optional. Without them the digest stays NULL and the gateway keeps
running the plaintext lookup it runs now, which is exactly the state before
this slice. A deployment that has not provisioned keys yet is not a broken
deployment; it is an unconfigured one, and `ControlPlaneConfig.from_env`
already distinguishes those.

## Design

The relay secret joins the runtime's material where every other runtime secret
already travels — the `merged` mapping in `ProvisioningWorker._finish_runtime`
that carries `HERMES_BOOTSTRAP_TOKEN` and the DSAR token, installed through the
sink into the runtime's own namespace, never through argv, the manifest or a
log.

It is derived per household rather than per revision, so a re-provisioning
installs the same value and a rollout does not invalidate deliveries the
gateway is mid-flight on.

The repair for rows written before the key existed is a repository method, not
a migration: SQL cannot compute a keyed digest, and the key belongs to the
application. It is idempotent and touches only rows whose digest is NULL.

## Acceptance

* A provisioned runtime receives `HERMES_WHATSAPP_RELAY_SECRET`, and the value
  is the one the gateway derives for that household — asserted by deriving on
  both sides and comparing, not by reading one side's constant.
* A binding written by either writer carries `external_id_hmac`, and a strict
  gateway resolves the sender to that household.
* The repair fills a NULL digest and leaves a populated one alone.
* With no keys configured, the digest is NULL and the gateway behaves exactly
  as it does today.
* `test_hmac_column_stays_null_until_c5_provisions_the_key` is replaced by its
  opposite, because the gap it pinned is closed.
* Full suite green, ruff and sanitizer clean.

## Deliberately not here

* **The HTTP entrypoint and the deploy unit (C5d).** Still nothing calls
  `handle_webhook` outside tests, and there is no `deploy/gateway/`. This slice
  makes the keys exist; it does not stand a gateway up.
* **Key rotation as an operation.** The scheme admits it — a labelled version
  and a single root — but the runbook step, the re-install sweep and the
  overlap window are their own work, and writing them against a gateway nobody
  deploys yet would be built against a guess.

---
title: "C5e — the gateway asks the control plane, and holds nothing"
status: active
created_at: "2026-08-29"
repository: abrolia
parent_plans:
  - thoughts/shared/plans/2026-08-23-go-live-checklist.md
scope: c5e
data_policy: synthetic-only-until-explicit-gates
---

# C5e — the gateway asks the control plane, and holds nothing

C5d shipped the entrypoint, the scheduler and the deliverer, and deliberately
shipped no deploy unit: a gateway on its own Fly volume opens a database the
control plane never wrote to, so it starts, passes its health check and routes
nobody. This is the slice that decides where the gateway's answers come from.

## The decision, and why it is not the other one

Two candidates were named when C5d descoped this: **an authenticated lookup
endpoint**, or **a replicated read projection**. This slice takes the lookup,
and the reason is specific rather than a preference between architectures.

**Routability changes at exact moments, and five slices exist to make those
moments exact.** C3c made a binding routable only once the revision carrying it
activates (`published_revision IS NOT NULL`); C3e made a rollout terminal only
at activation; D1 re-plans households serving a stale set; D4 and D5 retire the
chat an owner leaves and guard both readings of an identity. Every one of those
is a statement about *when* a sender starts or stops resolving.

A replicated projection puts lag inside that decision. Seconds of it are enough
to re-open the exact defects: a retired member keeps routing after their
binding is gone, and a member staged for a revision that failed starts routing
before it activated. The work those slices did would be intact in the control
plane and wrong at the only place it is consulted.

A synchronous lookup has no lag. Its cost is a hot-path dependency on the
control plane, and that cost is bounded by machinery C5b already built — see
L2.

**And it makes the separation total.** The gateway stops opening a
control-plane database at all. It holds its own ingress WAL and nothing else:
no bindings table, no households table, no field cipher, no lookup key. C5c's
K1 said the gateway holds roots and never household data; after this it holds
no control-plane data of any kind.

## The invariants

Stated before the code, and read back against the diff before pushing.

**L1 — The answer is asked for, never cached.** Freshness is the whole reason
this is a lookup rather than a projection, so a cache with any lifetime is the
projection wearing a different name. If a cache is ever wanted, it belongs in a
slice that states what staleness is acceptable and reconciles it with the
activation boundary.

**L2 — A lookup that could not be made is RETRYABLE, never `unknown_sender`.**
"The control plane did not answer" and "nobody holds this sender" are different
facts and C5b already draws that line: terminal outcomes drop the row,
retryable ones keep it for the redeliver worker. Collapsing them would drop a
family's message *terminally* because a deployment was restarting.

**L3 — The endpoint returns identifiers, never content.** The `household_id`
and the `runtime_ref`, which are what routing and delivery need. Not the
external id, not the chat, not a manifest, not a role. The gateway asks who,
and gets who.

**L4 — One rule, in one place.** The gateway's local SQL and the endpoint's SQL
would be two implementations of "which household holds this sender", and two
implementations of one comparison is the C5a defect — a gateway signing
`body|timestamp` while the runtime verified the bare body, each with passing
tests of its own. The resolution rule becomes one function both sides call.

**L5 — The gateway authenticates as itself, on private transport.** The shape
the internal bootstrap endpoints already use: a bearer credential, a
`.flycast` host requirement, and rate limiting. The credential is the
gateway's own; it is never a household's, and it never grants anything but
this question.

## Acceptance

* A bound sender resolves through the endpoint to the same household the local
  rule resolves, asserted by running one sender through both.
* A staged binding does not resolve — C3c's rule, applied at the endpoint.
* An unreachable control plane is retryable: the WAL keeps the row and the
  redeliver worker delivers it once the control plane returns.
* An unknown sender is terminal and keeps nothing.
* The endpoint refuses a missing or wrong credential, and refuses public
  transport.
* The endpoint's response carries no identifier the gateway did not ask about.
* The gateway opens no control-plane database.
* `deploy/gateway/` mounts only the ingress WAL and names no household secret.
* Full suite green, ruff and sanitizer clean.

## Deliberately not here

* **The public provider edge**, still. C5d's boundary is unchanged.
* **Caching, and the availability work it implies.** L1 says why: a cache is
  the projection this slice rejected, and choosing a staleness budget is a
  decision about the activation boundary, not an optimisation.

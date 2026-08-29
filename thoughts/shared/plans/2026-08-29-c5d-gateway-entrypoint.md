---
title: "C5d — something calls the gateway, and something deploys it"
status: active
created_at: "2026-08-29"
repository: abrolia
parent_plans:
  - thoughts/shared/plans/2026-08-23-go-live-checklist.md
scope: c5d
data_policy: synthetic-only-until-explicit-gates
---

# C5d — something calls the gateway, and something deploys it

Last of the four slices C5 was cut into. C5a reconciled the signature (#85),
C5b gave the ingress WAL its reader (#87), C5c created the keys (#90). Every
piece of the WhatsApp path now exists and none of it runs: `handle_webhook`
has no caller outside tests, `GatewayRedeliverWorker.run_once` has no
scheduler, and there is no `deploy/gateway/`.

## The boundary this slice does NOT cross

`handle_webhook` verifies the inbound signature with the HOUSEHOLD's relay key
(`verify_relay_hmac(key, payload, timestamp, signature)`). That is only
coherent if whatever calls it already holds those keys — an internal relay
adapter, not Meta or Telegram, who sign with an application secret this
repository has never had: there is no `X-Hub-Signature` handling, no
`verify_token`, no `hub.challenge` anywhere in the tree.

So this slice exposes **the entrypoint the code already implements**, and the
public provider edge — application-secret verification, the subscription
challenge, and whatever maps a provider payload to a sender — is its own
slice. Confirmed as the intended shape rather than inferred.

Step E5's prose says the gateway returns `200 OK` "to Telegram/WhatsApp",
which reads like the other shape. It is the one place the plan and the code
disagree, and the code is what four slices have been built against.

## The invariants

Written down before the code, and — the lesson C5c paid for — read back
against the diff before pushing. Stating them is not the same as satisfying
them.

**E1 — The entrypoint adds no authority.** It turns an HTTP request into a
`handle_webhook` call and a result into a response. Routing, key lookup,
signature verification, the kill switch and the WAL all stay where they are.
Anything the entrypoint decided would be a second place to get that decision
wrong, and the router is where four slices of reasoning already live.

**E2 — The status says what the CALLER should do; the body says why.** These
are different questions and conflating them is how a status code starts
leaking whether a household exists.

* Anything the gateway has taken responsibility for or terminally decided →
  `200`. That covers a delivery, and it covers `relay_key_absent` and
  `runtime_unavailable`, because those rows are IN the WAL and the redeliver
  worker owns them now. A caller that retried would duplicate what is already
  scheduled.
* A signature or timestamp the gateway refused → `403`. The caller is
  misconfigured and retrying the same bytes cannot help.
* The gateway itself unable to function → `503`, which is the only case where
  retrying is the right thing.

`unknown_sender` and `ambiguous_sender` return `200` like every other terminal
outcome, so a caller with valid credentials cannot probe membership by status
— E5's "not a 404 leak".

**And the caller must authenticate first.** This originally said the detailed
codes were safe "because the caller is the internal adapter", which was an
assumption about the deployment and not a property of the code. It left an
oracle: routing runs before signature verification — it must, since the
signature is checked with the household's key and the household comes from the
route — so with invalid bytes an unknown sender was denied at routing and
answered `200`, while a BOUND sender reached verification and answered `403`.
Anyone who could reach the service could enumerate membership holding no key
at all.

`ABROLIA_GATEWAY_ADAPTER_TOKEN` is checked before any lookup, and the
entrypoint FAILS CLOSED without it. Every other optional key in this system
degrades to previous behaviour; this one must not, because an endpoint that
accepts family message bodies has no previous behaviour to degrade to. After
it, the detailed codes really are safe rather than assumed to be.

**E3 — Durable before ACK survives the HTTP layer.** The entrypoint must never
answer `200` for something `handle_webhook` neither persisted nor terminally
decided. It gets this by construction — the router returns only after one of
those — so the rule is really a prohibition: no path may answer before calling
the router, and no failure may be swallowed into a `200`.

**E4 — The body is read once, bounded, and only when it is declared.**
`Content-Length` is parsed strictly and `MAX_WHATSAPP_WEBHOOK_BYTES` is the
ceiling, mirroring the runtime's own webhook rather than inventing a second
limit. An unbounded read on a network edge is a memory exhaustion, and a
short read is a payload whose signature will never verify.

**E5 — The redeliver worker never overlaps itself.** One thread, one timer,
and a run that takes longer than the interval delays the next tick rather than
starting beside it. Two concurrent passes would both claim the same due rows
and deliver them twice.

**E6 — The gateway never fabricates the data it routes on.**
`open_control_plane_database` migrates, so a missing file becomes a fresh
empty database and the gateway comes up healthy, answers `/healthz`, and
routes nobody — every real sender an `unknown_sender` against a table nothing
ever populated. That is indistinguishable from a working deployment until a
family's message goes missing, so a missing database is a refusal to start.

HOW the gateway is given a populated one is not settled and is not settled
here — see "Deliberately not here".

## Acceptance

* A signed request for a bound sender reaches the runtime and answers `200`.
* A wrong signature answers `403` and leaves no WAL row.
* An unknown sender answers `200` with its code, and the status is
  indistinguishable from a delivered one.
* A delivery failure answers `200`, leaves a WAL row, and the scheduled worker
  delivers it on a later tick.
* A body over the ceiling answers `413` and is never read.
* The kill switch closes the entrypoint.
* The scheduler runs `run_once` repeatedly and never concurrently.
* A missing control-plane database is a refusal to start, not an empty one.
* An unauthenticated caller learns nothing, including whether a sender is
  bound.
* Full suite green, ruff and sanitizer clean.

## Deliberately not here

* **The deploy unit, and the data path it needs.** A first draft of this slice
  carried `deploy/gateway/`, and the review found it could not work: the
  gateway mounted its own volume while the authoritative database lives on the
  control plane's, with no replication between them. The deployed gateway
  would have started, passed its health check and routed nobody.

  That is not a packaging bug. The gateway holds no field cipher by design
  (C5c's K1), so it cannot simply be handed the control plane's database
  without also being handed the keys that separation exists to withhold — and
  the two candidate answers, an authenticated lookup endpoint or a replicated
  read projection, are different systems with different failure modes. It is a
  missing store lifecycle and it belongs to its own slice, C5e, with the
  deploy unit that depends on it.

  Shipping the deploy unit meanwhile would be worse than shipping none: a
  configuration that looks finished and routes nobody is the kind of thing
  that gets deployed and believed.

* **The public provider edge.** Application-secret verification, the
  subscription challenge, and provider-payload parsing. See the boundary above.
* **Telegram.** `handle_webhook` takes a channel and the router resolves one,
  but nothing has ever driven Telegram through this path, and adding a second
  channel to an entrypoint with no first user would be built against a guess.

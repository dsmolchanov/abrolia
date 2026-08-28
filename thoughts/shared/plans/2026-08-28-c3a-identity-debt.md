---
title: "Channel identity debt after C3a"
status: active
created_at: "2026-08-28"
repository: abrolia
parent_plans:
  - thoughts/shared/plans/2026-08-27-c3a-sender-identity-and-chat.md
  - thoughts/shared/plans/2026-08-23-go-live-checklist.md
scope: identity-debt
data_policy: synthetic-only-until-explicit-gates
---

# Channel identity debt after C3a

C3a (#76) split the sender's identity from the chat it speaks in, and was
merged by admin over an open Codex review after five rounds. The findings were
valid in every round and the count rose on the last pass rather than falling,
which is the signal that the remaining work is design rather than patching.
This plan is that work, plus one defect found while investigating it that is
more severe than anything the review raised.

Everything here is about ONE invariant, the one recorded in
`AGENTS.repo-invariants.md` as "A stored identity is the string the channel's
ingest actually produces". C3a made that true for rows the control plane writes
NOW. It is not yet true for the values onboarding supplies, for runtimes that
are already deployed, or for the keyed digest beside the column.

## Order, and why it is this order

| # | Item | Why here |
|---|---|---|
| D0 | Onboarding hands every household the same identity | Blocks the second household entirely. Reproduced. |
| D1 | Migration revocations never reach deployed manifests | A retired member stays authorized on a live runtime. |
| D2 | `external_id_hmac` cannot be maintained without C5 | Two review findings ask for opposite things; neither is reachable. |
| D3 | Canonicalization is `strip()`, not each channel's rule | Silent unusable bindings, same shape as the ones already fixed. |
| D4 | `_reject_foreign_holder` guards the sender, not the actor | The migration compensates once; the write path does not. |
| D5 | An abandoned channel keeps its adults routable | Predates C3a; revocation is incomplete. |

D0 first because it is the only one that stops a family being onboarded at all.
D1 second because it is the only one where the system is actively wrong rather
than merely limited: it authorizes somebody it has been told not to.

---

## D0. Onboarding gives every household the same identity, so only one can exist

**Reproduced 2026-08-28.** `control_plane/api/web.py:219-220` returns the
primary-channel selection as a pair of constants — `actor_id:
"synthetic-owner"`, `chat_id: "synthetic-chat"` — for every household, and
`control_plane/web/static/onboarding.js:240` does the same on the client.
`DesiredSpecPlanner.issue` seeds the owner binding from those values, and
`_reject_foreign_holder` (`repositories/bindings.py:607`) correctly refuses an
identity another household already holds:

```
household 1 provisioned OK
household 2 REFUSED: this channel is already bound to another household
```

So the second family to complete onboarding cannot provision. The refusal is
right — the gateway resolves senders across the whole table and would answer
`ambiguous_sender` for both — and the constants are what is wrong.

This predates C3a. Before the split the collision happened on `chat_id`,
because that constant was equally shared; C3a moved which column collides and
fixed neither. It is listed first because a pilot with one household cannot
see it, and the second household is exactly what a pilot is for.

**Not a rename.** The honest fix supplies a REAL transport identity, which
means the primary-channel step has to capture one: a Telegram user ID and chat
ID, or a WhatsApp number and its JID. `PrimaryChannelSelection`
(`control_plane/models.py:228-237`) validates them through
`_require_synthetic_actor_or_chat`, so B-07's synthetic gate has to be
satisfied by a per-household synthetic value rather than a constant — unique
per household, still unmistakably synthetic. That is the smallest change that
makes the system multi-tenant; capturing genuine IDs is a separate gate.

**Acceptance.** Two households complete onboarding, both provision, both plan
a revision, and `WhatsAppGatewayRouter.route` resolves each sender to its own
household with no `ambiguous_sender`. The test creates the second household —
today's suite never provisions two, which is how this survived.

---

## D1. A migration revoked bindings that deployed runtimes still honour

Raised by Codex on #76 as "Propagate migration revocations to deployed
manifests", and not fixed there.

`0010` retires adult bindings and any owner binding it cannot repair. The
control plane's table is then correct. The RUNTIME's is not: a household whose
revision was already rolled out holds a manifest listing that member, and
`Household.knows_binding` answers from the manifest the runtime booted with.
Nothing re-plans on its own — `ControlPlaneDatabase.migrate` does not call
`DesiredSpecPlanner.issue`, and neither does startup.

So between the migration and the household's next revision, a member the
control plane has revoked is still authorized by the runtime. That is the one
item here where the system does something wrong rather than failing to do
something.

**The design question**, which is why this is a plan and not a patch: what
re-plans a live household? Onboarding cannot — it is finished. C3b's rollout
path requires `household_status = 'provisioning'` (`worker.py:2667, 2715,
2823`), and the C3a plan already recorded that driving a live household back
through that transition is deliberate work rather than something to copy from
onboarding. Options, to be decided before implementing:

1. **A revocation sweep** that re-plans every household whose bindings changed,
   as an explicit operator action with a dry-run report. Fits the existing
   `provision_dry_run` machinery and the invariant that a report describes the
   branch the worker will actually take.
2. **Make the runtime re-read** rather than trust its boot-time manifest. Larger,
   and it moves an authorization decision onto a hot path.

(1) is the recommendation. (2) is a change to how authorization is refreshed
and should not be smuggled in as a bug fix.

**Acceptance.** A household with a deployed revision and a retired adult stops
authorizing that adult without a human editing anything, and the mechanism says
what it will do before it does it.

---

## D2. The keyed digest cannot be maintained by anything that exists

Two Codex findings on #76 ask for opposite things and both are right:

* *Recompute HMAC when rewriting the sender* — a digest describing a value the
  migration changed keeps matching the retired identity, and the strict branch
  of `WhatsAppGatewayRouter.route` (`gateway/whatsapp_router.py:112-117`)
  searches by digest ALONE, so stale is worse than missing.
* *Preserve strict routing while repairing sender IDs* — clearing it to NULL
  means strict mode finds nothing for that row.

Both follow from the same fact: `external_id_hmac` is keyed with the relay key,
which the control plane does not hold and has no path to. `_insert` writes NULL
and says so; `test_hmac_column_stays_null_until_c5_provisions_the_key` pins it.
0010 clears the digest because NULL fails closed and a stale digest does not.

There is no third option until **C5 provisions the key**. Until then strict-mode
routing does not work for anything this control plane writes, which is a
pre-existing gap C3a neither caused nor widened.

**What belongs here** is the seam, so C5 does not have to rediscover it: when
the relay key arrives, every write path that sets `external_id` sets the digest
in the same statement, and a backfill recomputes it for existing rows. The
column is never written from a value other than the `external_id` beside it.

**Acceptance.** Deferred to C5 by design. What this plan owes is a failing
guard rather than a silent gap: a test asserting that no code path writes
`external_id` without either writing the digest or nulling it, so the seam
cannot be half-implemented.

---

## D3. Canonical means each channel's rule, not `strip()`

Raised as "Apply each channel's canonical identity rules". `_canonical`
(`repositories/bindings.py`) strips surrounding whitespace, which fixes the
reported class, because every ingest path strips before it reports. It is not
the whole rule.

A WhatsApp actor is `+`-prefixed by `as_eml` and bare in `remote_jid`'s local
part; a JID is `@s.whatsapp.net` for a thread and `@g.us` for a group; Telegram
IDs are digit strings that arrive from JSON. Two spellings of one identity are
two rows, and the second one silently authorizes nobody.

**The shape of the fix** was going to be a per-channel canonicalizer owned next
to the channel, so the control plane would not be the second place that knows
WhatsApp's JID format — the mistake C3a rejected when it refused to derive a
chat from a sender.

**Corrected during implementation: that inverts the dependency.** `hermes_cloud`
imports `control_plane` (`runtime/service.py`, `gateway/whatsapp_router.py`)
and never the reverse, so a canonicalizer living beside the ingest code would
mean the control plane importing the runtime in order to write a row. The rules
therefore live in `control_plane/channels.py`, and what keeps them honest is a
test that drives a real webhook and a real Telegram update through their own
parsers and asserts the canonical form equals what came out. Agreement proven
by running something beats agreement promised by placement.

A second correction, found the same way: the rules cannot be applied to the
synthetic namespace. `synthetic-owner.<household>` is not a phone number
waiting to be reshaped, and WhatsApp's rule would refuse the only identities
B-07 permits. The namespace is canonical as written.

**Acceptance.** A parameterized test over each channel's plausible spellings,
asserting they either normalize to one stored value or are refused, and that
the stored value is what that channel's real ingest produces.

---

## D4. The cross-household guard covers the sender but not the actor

`_reject_foreign_holder` (`repositories/bindings.py:607`) refuses an
`external_id` another household holds, and has never checked `actor_id`.
Since C3a enforces `actor_id == external_id` at `_insert`, the two are the same
value on every new row and the gap is closed for anything written now.

It is not closed for the invariant itself. 0010 has to delete both halves of a
shared-actor pair precisely because legacy rows could hold one actor across two
households, and nothing prevents a future write path — a sender-to-actor
mapping, say — from reintroducing it.

**Fix.** Fold the actor into the same guard, so the rule is stated once rather
than enforced by the equality holding.

**Acceptance.** Two households cannot hold one actor on one channel, asserted
directly rather than as a consequence of the equality.

---

## D5. An abandoned CHAT keeps its members routable

**Corrected during implementation. The item below is stated too broadly, and
the rule it describes would revoke bindings nobody superseded.**

Scoping retirement to the CHANNEL is wrong in both directions. C3 deleted every
adult on the channel about to become primary, which was right only while
`_reject_unrepresentable_member` refused an adult there at all; C3a removed
that limitation, so the deletion became a way to unbind a member because the
owner arrived on their channel. And this item then asked for the mirror image —
that nothing remain on the channel the household left — which revokes an adult
verified in a thread of their own, on a non-primary channel, which is a
supported arrangement.

What is actually stale is a binding into the CONVERSATION the owner has just
left: nobody speaks there for the household any more, so an actor authorized in
it is authorized in an abandoned room. `_retire_superseded` therefore takes
every previous owner chat's non-owner bindings with it, and nothing else moves.

One case fell out of the change and is worth naming, because the old rule hid
it: an owner re-onboarding onto an identity a MEMBER of their household holds
used to be resolved by deleting that member. It is now refused. Unreachable
through the endpoints — `issue_challenge` refuses an actor equal to the issuer
— but reachable enough to have raised a bare `IntegrityError` from the unique
index once the deletion stopped clearing the way.

The original statement follows, for the record.

### As originally written

Noted in #76's PR body and deliberately not fixed there.
`_retire_superseded` (`repositories/bindings.py:337`) deletes adult rows on the
channel BECOMING primary, and every owner row. An owner who moves the household
from Telegram to WhatsApp therefore leaves the Telegram adults bound, on a
channel the household has left — and the gateway has no notion of a superseded
binding, which is the reason the method retires owner rows in the first place.

The method's own docstring already states the principle: "an owner who moves
the household off a channel has revoked it, and the table is what the gateway
believes." The adults are simply not covered by it.

**Acceptance (revised).** After a household moves its chat, no binding of any
role remains in the conversation it left, and bindings in every other
conversation — including on the channel it moved off — are untouched.

---

## Deliberately not here

* **A sender-to-internal-actor mapping.** Several #76 findings pointed at it,
  and C3a refused to build it. One human reachable on two channels is two
  members of `actors.family` today, because the system has no cross-channel
  notion of a person. That is a product decision about identity, not debt.
* **Real (non-synthetic) transport identities.** D0 makes the values unique per
  household, still synthetic. Removing the synthetic gate is B-07's.
* **Strict-mode routing.** D2 explains why it belongs to C5.

## How these shipped, and why one inventory covers four items

D0, D1, D3 and D4/D5 were written as four branches so they would review as four
small PRs, and that is how they were opened (#77, #78, #79, #80). Each was then
squash-merged into D0's branch rather than into `main`, so what reaches `main`
is one branch carrying all four.

The inventory below follows that, because the scope gate checks a real thing:
exactly one plan step may claim a branch, and it must declare every path that
branch changes. Splitting the claim across four steps would either leave paths
undeclared — which is what CI caught — or make four steps applicable at once,
which the gate refuses for a better reason than tidiness: the scope of a change
is not something it can infer.

The per-item file lists are kept below the combined one. They are the record of
which item moved what, and three of the four moved during implementation for
reasons written down beside them. Their `**Branches:**` name branches that were
deleted on merge, so no step but the first is ever applicable.

## Inventory — C3a identity debt, as merged

**Files:** `control_plane/api/web.py`, `control_plane/channels.py`,
`control_plane/cli.py`, `control_plane/models.py`,
`control_plane/onboarding/service.py`, `control_plane/provisioning/fakes.py`,
`control_plane/provisioning/rollout.py`,
`control_plane/repositories/bindings.py`,
`control_plane/web/static/onboarding.js`,
`tests/control_plane/test_api.py`, `tests/control_plane/test_binding_api.py`,
`tests/control_plane/test_channel_bindings.py`,
`tests/control_plane/test_manifest.py`,
`tests/control_plane/test_art9_household_consent.py`.

**Branches:** `codex/d0-per-household-channel-identity`.

## Inventory — D0 primary-channel identity

**Files:** `control_plane/api/web.py`, `control_plane/models.py`,
`control_plane/onboarding/service.py`,
`control_plane/web/static/onboarding.js`, `control_plane/provisioning/fakes.py`,
`tests/control_plane/test_api.py`, `tests/control_plane/test_manifest.py`,
`tests/control_plane/test_art9_household_consent.py`,
`tests/control_plane/test_channel_bindings.py`.

`control_plane/onboarding/service.py` was added to this inventory during
implementation. `_parse_selection` is the one place BOTH select routes pass
through — the web form and `/api/v1/onboarding/steps/{kind}/select` — so it is
the only seam where the identity can be owned by the server rather than
supplied by the caller. Deriving it in `web.py` alone would have left the JSON
route minting its own, which is the same defect with one fewer client.
`tests/test_gateway_routing.py` and `tests/control_plane/test_onboarding_state.py`
turned out not to need changes; the routing assertion lives with the
two-household case in `test_channel_bindings.py`.

**Branches:** `codex/d0-per-household-channel-identity-item`.

## Inventory — D1 revocation propagation

**Files:** `control_plane/provisioning/rollout.py`, `control_plane/cli.py`,
`tests/control_plane/test_channel_bindings.py`.

`control_plane/cli.py` was added to this inventory during implementation: D1
resolved to option (1), an operator-invoked sweep, and `cli.py` is where every
other sweep lives — `retention`, `runtime-health`, `resume-deletions`. Putting
it anywhere else would have made it the one operator action with no operator
entry point.

`worker.py` and `repositories/bindings.py` turned out not to need changes.
Staleness is DERIVED by comparing the binding table against the manifest of
`households.current_config_revision`, so nothing has to record a revocation and
no write path has to learn a new duty — which also means the sweep finds drift
from any cause rather than only from `0010`. `test_provision_dry_run.py` is
untouched for the same reason: this report is not a job report and makes no
claim about which job the worker reaches first, so it is not governed by the
"a report describes the branch the worker will actually take" invariant.

**Branches:** `codex/d1-revocation-propagation`.

## Inventory — D3 canonical identity per channel

**Files:** `control_plane/channels.py`,
`control_plane/repositories/bindings.py`,
`tests/control_plane/test_channel_bindings.py`,
`tests/control_plane/test_binding_api.py`.

`control_plane/channels.py` replaces the two `hermes_cloud/` entries this
inventory first named: see the dependency correction above. The runtime is
still what defines canonical — it is just read by a test rather than imported
by the repository. `tests/control_plane/test_binding_api.py` is here because
its fixtures passed a phone as a WhatsApp chat, which the new rules refuse.

**Branches:** `codex/d3-canonical-identity`.

## Inventory — D4 and D5 binding revocation guards

**Files:** `control_plane/repositories/bindings.py`,
`tests/control_plane/test_channel_bindings.py`.

**Branches:** `codex/d4-d5-revocation-guards`.

The inventory held. D4 turned out to be one predicate and D5 one scope change,
both inside the file this named — which is what a correctly scoped item looks
like, and worth noting beside D0, D1 and D3, where implementation moved the
file list every time.

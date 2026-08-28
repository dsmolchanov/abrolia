---
title: "C3a — Separate a sender's identity from the chat it speaks in"
status: active
created_at: "2026-08-27"
repository: abrolia
parent_plans:
  - thoughts/shared/plans/2026-08-23-go-live-checklist.md
scope: c3a
data_policy: synthetic-only-until-explicit-gates
---

# C3a — Separate a sender's identity from the chat it speaks in

C3 gave `channel_bindings` its first production writer and immediately hit a
wall: **a household cannot have a second member on its primary channel.** Not
because the feature is unbuilt, but because the store cannot represent it.
`control_plane/repositories/bindings.py` refuses one today, with the reason in
the error text, and `test_an_adult_on_the_primary_channel_is_refused_because_the_store_cannot_hold_one`
pins it.

This plan removes that wall. It is a schema change with three consumers that
have to move together, so it is written before it is typed.

## The defect in one paragraph

`channel_bindings.external_id` is asked two incompatible questions.
`gateway/whatsapp_router.py:128` matches it against an incoming **sender**;
`control_plane/provisioning/planner.py:174` projects it as the manifest's
`chat_id`, the **conversation** the assistant speaks in. For the owner the two
coincide, because onboarding wrote one value that happens to answer both. For a
second member they cannot, and there is no third option:

```
member gets the household's chat  -> UNIQUE (household_id, channel, external_id)
                                     collides with the owner's row
member gets an identity of their own -> the projection emits two verified chats
                                     for the primary channel, and
                                     parse_runtime_manifest refuses the revision
                                     with `channels.primary: multiple chats`
```

Both reproduced 2026-08-26. The conflation arrived with `0007`; the projection
is simply the first code to ask the column for two answers at once.

## What is already right, and must not be disturbed

The rest of the system **already separates these two things**. This is the
central fact of the plan: only the control-plane table is confused.

- **Telegram supplies both at ingest.** `hermes_cloud/channels/telegram.py:264-266`
  reads `message.from.id` as the actor and `message.chat.id` as the chat, and
  hands both to `build_run_context`.
- **The manifest carries both.** `ChannelBinding` in
  `hermes_cloud/core/runtime_manifest.py:64-69` has `actor_id` AND `chat_id`.
- **Authorization is already a pair.** `RunContext` derives from
  `verified_actor_chat_pairs` (`runtime_manifest.py:125`) and
  `Household.knows_binding` (`runcontext.py:114-117`) denies cross-pairs — an
  actor authorized in one chat is not authorized in another.

So the target shape is not new. The table simply has to say what the manifest
already says.

## The constraint that looks blocking and is not

`parse_runtime_manifest` requires exactly one verified chat on the primary
channel (`runtime_manifest.py:342-347`), feeding `primary_chat_id`
(`:128-133`), which becomes `Config.chat` (`core/config.py:178`).

It is tempting to read that as "a household has exactly one conversation",
which would make per-member chats fundamentally incompatible. It is not what
the field means. Following its consumers:

- `Pipeline.handle_update` (Telegram dialogue) replies to
  **`parsed.context.chat_id`** — the conversation the message arrived in
  (`runner/pipeline.py:615`).
- `Pipeline._handle_whatsapp_dialogue` stages its reply into **`self.chat`**
  (`:458`), and so do cards, approvals and digests (`:331, :341, :385, :429, :482, :496`).

`self.chat` is `config.require_chat()` — one place. So `primary_chat_id` is not
"the household's only conversation". It is **the household's review surface**:
where cards, approvals and unsolicited output appear for the family to see.
Dialogue already answers wherever it was spoken to.

That distinction is what makes this tractable. The review surface is genuinely
singular and should stay singular; the set of conversations the household is
reachable in is not, and never was.

## Design

### D1. The table gains `chat_id`, and the two columns get one meaning each

```sql
ALTER TABLE channel_bindings ADD COLUMN chat_id TEXT;
```

| column | meaning | who asks |
|---|---|---|
| `external_id` | the **sender's** identity on this channel — Telegram user ID, WhatsApp phone, web account ref | `gateway/whatsapp_router.py` matching an inbound sender |
| `chat_id` | the **conversation** this binding speaks in | the manifest projection, and `verified_actor_chat_pairs` |

For Telegram they differ: the sender is a user ID, the chat is a group.

**Correction, 2026-08-28 (Codex `[BLOCKER]` on #76).** This section originally
said the two *coincide* for WhatsApp, "a 1:1 thread is that number", and drew
the conclusion that a caller may omit `chat_id` and have it default to
`external_id`. That is false in this system and the conclusion was a live
defect. `hermes_cloud/ingest/whatsapp_webhook.py` normalizes the SENDER to
`+999…` and reports the CHAT as the provider's `remote_jid` —
`999…@s.whatsapp.invalid` in fixtures, `@s.whatsapp.net` in production, `@g.us`
for a group — and `trusted_run_context` authorizes that exact pair BY STRING.
A binding written as `(+999…, +999…)` therefore succeeded, published a
revision, and matched no inbound turn: a success the owner could not tell from
a working one.

`chat_id` is consequently REQUIRED at both the repository and the endpoint, and
never derived. Deriving the JID was the other option and is worse: the control
plane does not own that format, and a rule right for `@s.whatsapp.net` is wrong
for the `@g.us` group that is the very arrangement this slice exists to allow —
one value answering two questions, which is the defect being removed.

The MIGRATION still writes them equal, and that is unchanged and correct: it
preserves exactly what the projection emitted before, which acceptance 4
requires. Backfilling existing rows and guessing for new ones are different
acts. Today's rows hold the CHAT in `external_id` for the owner, because
that is what onboarding's `primary_channel` step captured — so the migration
must backfill `chat_id = external_id` and leave `external_id` alone. See M1 for
why that is correct-but-incomplete, and what it costs.

### D2. Uniqueness moves to the sender

```sql
-- replaces UNIQUE (household_id, channel, external_id)
CREATE UNIQUE INDEX channel_bindings_sender
    ON channel_bindings (household_id, channel, external_id);
```

The constraint stays on `external_id` — one sender identity is one binding —
and `chat_id` becomes free to repeat. That is exactly the shape that was
impossible before: two members, two sender identities, one shared chat.

The cross-household rule is unchanged and still load-bearing: an `external_id`
held by one household is refused to every other
(`_reject_foreign_holder`), because the gateway resolves senders across the
whole table and answers `ambiguous_sender` when two match — binding a taken ID
breaks delivery for both households, including the innocent one.

### D3. The manifest designates its review surface instead of deducing it

**Decided 2026-08-27: derive it from the owner.**

`primary_chat_id` = the `chat_id` of the primary-channel verified binding whose
`actor_id == actors.owner`.

Note the identification: the runtime's `ChannelBinding` has **no `role`
field** — only the control-plane table does — so "the owner's binding" has to
be found through `actors.owner`, which the manifest does carry
(`runtime_manifest.py:54`). That is not a workaround. The write side already
enforces exactly this pairing: `DesiredHouseholdSpecV1.validate_contract`
refuses a manifest unless some verified primary-channel binding belongs to
`actors.owner` (`control_plane/provisioning/manifest.py:106-109`). The rule is
already half-written, on the side that produces the document; D3 makes the
reading side agree instead of counting chats.

What this costs: the review surface **must** be the owner's conversation. It
cannot be anything else — a dedicated "assistant" channel that nobody talks in,
for instance, is not expressible. Nothing asks for that today.

#### The alternative, and why it was not taken

**Carry it explicitly** — add `channels.primary_chat` to the manifest and have
the planner write it. It buys exactly the flexibility named above. It was
rejected for three costs, the third of which is easy to miss:

1. **A second place for the value to live.** Derived, it cannot disagree with
   the bindings. As a field, it can point at a chat that is not among the
   verified bindings at all — so it would need a validator tying it back to
   them, which is the relationship the derived form gets for free.
2. **Schema surface.** `ChannelsV1` is `extra="forbid"` and frozen; a new field
   moves `config_sha256` for every new revision. Expected, but not free.
3. **Asymmetric rollback cost.** The runtime's parser is *lenient* about
   unknown keys — it reads `_text(raw_channels, "primary")` and ignores the
   rest — so a new manifest survives an old runtime. The control plane is
   *strict*: `extra="forbid"`, and stored revisions are re-read through
   `DesiredHouseholdSpecV1.model_validate(stored)`. So rolling the control
   plane BACK would break reading revisions already written with the field.
   The derived form has no such cost, because there is nothing new to read.

If a household ever does need a review surface that is not the owner's
conversation, the field can be added on top of this decision without rework:
"default to the owner's chat when the field is absent" is a rule that survives
both states.

`parse_runtime_manifest` changes from:

```python
primary_chats = {b.chat_id for b in bindings if b.verified and b.channel == primary}
if len(primary_chats) != 1: raise ManifestError(...)
```

to requiring exactly one primary-channel verified binding for
`actors.owner`, with `primary_chat_id` reading that binding's `chat_id`.
"Exactly one" rather than "at least one" is the tightening: a household with two
owner bindings on its primary channel would make the review surface ambiguous
again, which is the failure this whole section exists to remove — and
`_retire_superseded` already guarantees a single owner binding on the write
side. `allowed_chats` and
`verified_actor_chat_pairs` need no change at all — they already aggregate over
every verified binding, and they simply start returning more than one chat,
which `knows_binding` was always written to handle.

### D4. The lifecycle stops refusing what the store can now hold

`_reject_unrepresentable_member` (`repositories/bindings.py:456`) is deleted.
The challenge flow gains a `chat_id` alongside `external_id`, and the
primary-channel refusal in `issue_challenge` goes with it.

`ChannelBindingV1.chat_id` in the projection stops reading `binding.external_id`
and reads `binding.chat_id`.

## Migration

### M1. Backfill, and the honest gap in it

```sql
UPDATE channel_bindings SET chat_id = external_id WHERE chat_id IS NULL;
```

For every row that exists today this is **correct for the manifest and wrong
for the gateway**, and it is worth being precise about which:

- the value in `external_id` is what onboarding's `primary_channel` step
  captured, which is the CHAT (`planner.py` reads it as `chat_id`), so copying
  it into `chat_id` preserves exactly today's behaviour;
- it leaves `external_id` holding a chat ID rather than a sender ID. For
  Telegram those differ, so a strict-mode gateway lookup against that row would
  match a chat where it means to match a sender.

That gap **is already live** — it is not introduced here — because the gateway
and the planner have been reading one column two ways since 0007. What this
plan changes is that the gap becomes *nameable*: after the migration a row with
`external_id == chat_id` on Telegram is visibly un-migrated data, not an
ambiguity in the schema.

Correcting those values needs the owner's Telegram user ID, which onboarding
never captured. Two ways to get it, both out of scope here and recorded so the
choice is deliberate:

1. **Re-onboard.** `reset_from(PRIMARY_CHANNEL)` already retires and rewrites
   the owner binding (`_retire_superseded`), so an owner who re-runs the step
   after C3a's onboarding change gets both values correctly.
2. **Learn it at ingest.** The first authorized inbound message carries the
   sender ID; a binding whose `external_id` still equals its `chat_id` could
   adopt it. This is a self-healing write on a hot path and needs its own
   design — it is the kind of thing that must never widen authorization.

Given B-07 keeps every household synthetic today, (1) is sufficient and (2)
should not be built speculatively.

### M2. `external_id_hmac` stays NULL, still

Out of scope, unchanged, and already pinned by
`test_hmac_column_stays_null_until_c5_provisions_the_key`. The relay key is
C5's. Noted only so the column's emptiness is not mistaken for a regression
introduced here.

## Consumers, and the order they move in

One transaction's worth of thinking, three files that must agree:

| # | Consumer | Change |
|---|---|---|
| 1 | `control_plane/migrations/0010_*.sql` | add `chat_id`, backfill, re-index |
| 2 | `control_plane/repositories/bindings.py` | write and read `chat_id`; drop `_reject_unrepresentable_member`; challenges carry a chat |
| 3 | `control_plane/provisioning/planner.py` | project `chat_id=binding.chat_id` |
| 4 | `hermes_cloud/core/runtime_manifest.py` | owner-designated `primary_chat_id` |
| 5 | `control_plane/api/bindings.py` | accept `chat_id` on the challenge endpoint |
| 6 | `control_plane/privacy/export.py` | export the new column |
| 7 | `gateway/whatsapp_router.py` | **no change** — it always meant `external_id` as the sender, and after this it is the only thing that column means |

That last row is the point of the whole exercise: the gateway is the one
consumer that was never wrong.

## Acceptance

A slice is not done because it compiles. These are the specific things that
must be true, each naming the test that proves it:

1. **Two members, one chat, one revision that starts.** An owner and an adult
   with distinct sender IDs sharing the household's Telegram chat produce a
   manifest that `parse_runtime_manifest` accepts, with two entries in
   `verified_actor_chat_pairs` and one `primary_chat_id`. Round-tripped through
   the real parser, not asserted against `DesiredHouseholdSpecV1` alone — that
   omission is exactly how an unstartable revision looked green in C3.
2. **Cross-pair denial survives.** The adult authorized in the household chat
   is denied in a chat they were not bound to (`knows_binding`), and the owner
   is denied in the adult's. This is the property the pair model exists for and
   the one most likely to be quietly lost while relaxing a uniqueness rule.
3. **The gateway still routes by sender, and still refuses ambiguity.** Two
   members in one household resolve to that household; the same sender in two
   households is still refused at bind time.
4. **Migration is behaviour-preserving.** A database at 0009 with existing
   rows, migrated, produces a byte-identical manifest projection for every
   household that has not added a member.
5. **The old refusal is gone and its test with it.**
   `test_an_adult_on_the_primary_channel_is_refused_because_the_store_cannot_hold_one`
   is deleted, not skipped, and replaced by (1). A test that documents a
   limitation must die with the limitation.

## Risks

- **Relaxing a uniqueness constraint is one-way.** Rows that violate the old
  rule can exist the moment the index changes, so the manifest-side rule (D3)
  must land in the same change, not after it. If they are split, a revision
  becomes unstartable between the two deploys.
- **`allowed_chats` widens.** It becomes the set of every verified chat rather
  than effectively one. `knows_chat` is the legacy path used only when a
  manifest carries no verified pairs (`runcontext.py:115-116`); confirm no
  household reaches it before relying on the pair check.
- **The scope gate.** This touches `hermes_cloud/` and `control_plane/`
  together, so the implementing branch needs its `**Files:**` inventory to
  cover both trees, declared before the first push.

## Deliberately not here

- **C3b** — a verified binding plans a revision that nothing rolls out. Its own
  slice; the worker requires `household_status = 'provisioning'`
  (`worker.py:2667, 2715, 2823`), so re-provisioning a live household has to
  drive that transition deliberately rather than by copying onboarding's block.
- **A review surface that is not the owner's conversation.** The rejected
  alternative in D3, addable on top without rework if it is ever asked for.
- **Learning sender IDs at ingest.** M1 option 2.

#### Inventory — C3a implementation

**Files:** `control_plane/migrations/0010_channel_binding_chat_id.sql`,
`control_plane/repositories/bindings.py`,
`control_plane/provisioning/planner.py`, `control_plane/api/bindings.py`,
`control_plane/privacy/export.py`, `hermes_cloud/core/runtime_manifest.py`,
`tests/control_plane/test_channel_bindings.py`,
`tests/control_plane/test_binding_api.py`, `tests/control_plane/test_db.py`,
`tests/control_plane/test_export_delete.py`, `tests/test_runcontext.py`,
`tests/test_gateway_routing.py`, `tests/test_feature_flags.py`.

**Branches:** `codex/c3a-sender-identity-and-chat`.

The two `hermes_cloud/` entries are not scope creep: D3 changes a rule the
runtime enforces, and it must land in the same change as the index that lets
rows violate the old one.

`tests/test_feature_flags.py` was added to this inventory during
implementation, and the reason is worth stating rather than hiding in a diff.
M1 leaves `chat_id` nullable in the schema — SQLite cannot add a `NOT NULL`
column to a populated table without inventing a default, and `DEFAULT ''` is a
lie that reads as data — so the column is required by a trigger instead, the
treatment `email_identities.domain_lookup_hmac` gets in `0003`. That trigger
fires on every `INSERT`, including the raw ones tests write directly against
the table, and this file has one. It is a one-line change and it is a
CONSEQUENCE of D1 rather than a second piece of work; the alternative — leaving
the column unenforced so no test had to move — would let a binding that speaks
nowhere reach the manifest, which is the failure the trigger exists to prevent.

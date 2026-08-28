"""The challenge → verification → row write that `channel_bindings` was missing.

`control_plane/migrations/0007_channel_bindings.sql` created the table with
`verified_at` and `verified_by_actor_id` already in it, and no production code
ever wrote a row: `gateway/whatsapp_router.py` reads the table to route a
sender to a household, and only tests ever put anything there for it to find.
The columns described a verification nothing performed.

This module is that verification. An owner issues a challenge naming the
channel, the external ID and the actor the second adult will become; whoever
holds the code answers it once; the answer is what writes the binding row.

Three properties are load-bearing, and each has a test named after it:

* **The code is a bearer credential**, so it is stored only as a keyed digest
  (`LookupHasher`, the treatment `auth_tokens.token_hash` gives a magic link)
  and is returned to the caller exactly once, at issue, to be delivered
  out of band. Nothing here logs it, and it never reaches argv or a manifest.
* **An external ID belongs to at most one household.** The gateway resolves a
  sender by looking the ID up across every household and denying
  `ambiguous_sender` when more than one matches, so binding an ID that another
  household already holds does not create a shared channel — it silently
  breaks delivery for BOTH, including the household that was there first.
  Verification refuses instead.
* **A challenge can mint an adult, never an owner.** Ownership follows the
  account that holds the household; a code delivered over a channel cannot
  confer it. The schema's CHECK constraint says the same thing in SQL.

Two columns, one question each. `external_id` is the SENDER's identity on the
channel — what `gateway/whatsapp_router.py` matches an inbound message against
— and `chat_id` is the CONVERSATION the binding speaks in, which is what the
manifest projects. C3a separated them (migration 0010); before that one column
answered both questions, and a household therefore could not hold a second
member on its primary channel at all. Uniqueness stays on the sender, so two
members may now share one chat.

A binding names THREE things and only two of them are free. `actor_id` is the
identity the RUNTIME authorizes — `build_run_context` is handed
`message.from.id` on Telegram and the `+999…` actor on WhatsApp — and
`external_id` is the identity the GATEWAY routes by. Both are the transport
sender, so they must be the same value, and `_reject_actor_that_is_not_the_sender`
refuses a binding where they differ rather than publishing one the runtime will
never honour. Only `chat_id` is genuinely independent.

Neither `external_id` nor `chat_id` is derivable from the other, and the caller
must supply both. It is
tempting to treat a WhatsApp 1:1 thread as "the same as the number", and it is
not: `hermes_cloud/ingest/whatsapp_webhook.py` normalizes the SENDER to
`+999…` and reports the CHAT as the provider's `remote_jid`,
`999…@s.whatsapp.net` — and a group is `@g.us`, which is precisely the shared
conversation this slice exists to allow. `trusted_run_context` authorizes the
exact pair, by string, so a binding built by copying one field into the other
is a binding no inbound turn can ever match. Deriving a chat from a sender is
the conflation this module just removed, wearing a different hat.

What this does NOT yet establish, stated plainly because the column is called
`verified_at` and would otherwise be read as more than it is: the control plane
has no sender for telegram or WhatsApp — outbound goes through the gateway and
the runtime — so it cannot put the code into the channel it is binding. The
code is returned to the owner to deliver, which proves that whoever answers
holds the code, and leaves the claim "this external ID is that person's" as the
OWNER'S ATTESTATION. Channel possession becomes provable when C5 gives this
side a sender; `issue_challenge` returns the code for exactly that seam to take
over. Until then B-07 keeps every external ID synthetic, so no real person's
number can be attested for.
"""

from __future__ import annotations

import secrets
import sqlite3
import time
from dataclasses import dataclass

from control_plane.channels import (
    ChannelIdentityError,
    canonical_chat,
    canonical_sender,
)
from control_plane.crypto import LookupHasher
from control_plane.db import new_id
from control_plane.repositories.base import Repository

#: How long an unanswered challenge stays answerable. Short by intent: the
#: code travels over a channel that is not yet trusted — that is the whole
#: reason the challenge exists — so its useful life is the span of one
#: conversation, not one session.
CHALLENGE_TTL_SECONDS = 15 * 60

#: Roles a challenge may confer. `owner` is deliberately absent; see the module
#: docstring and the schema CHECK.
CHALLENGE_ROLES = ("adult",)

CHANNELS = ("telegram", "whatsapp", "web")

#: How many challenges a household may have outstanding at once, and how many
#: members it may bind. Both are bounds on a collection an authenticated owner
#: can grow one request at a time: every issued challenge is a stored row, and
#: every verified binding becomes a manifest entry AND a config revision. A
#: household with thousands of either is not a household that outgrew the
#: product, it is a loop — and the cost lands on a shared SQLite volume and on
#: a manifest the runtime has to parse.
#:
#: The numbers are deliberately far above any real family and far below
#: anything that hurts, so the limit never has to be reasoned about in the
#: ordinary case.
MAX_OPEN_CHALLENGES = 10
MAX_BINDINGS = 25


class BindingError(PermissionError):
    """A binding was refused. The message is safe to show; it names no code."""


@dataclass(frozen=True)
class ChallengeRecord:
    id: str
    household_id: str
    channel: str
    actor_id: str
    role: str
    expires_at: float


@dataclass(frozen=True)
class IssuedChallenge:
    """The record, plus the one and only copy of the raw code."""

    record: ChallengeRecord
    code: str


@dataclass(frozen=True)
class BindingRecord:
    id: str
    household_id: str
    channel: str
    #: The SENDER's identity on this channel. What the gateway matches.
    external_id: str
    #: The CONVERSATION this binding speaks in. What the manifest projects.
    chat_id: str
    actor_id: str
    role: str
    verified_at: float
    verified_by_actor_id: str


class ChannelBindingsRepository(Repository):
    def __init__(self, database, cipher, lookup, token_hasher: LookupHasher) -> None:
        super().__init__(database, cipher, lookup)
        self.token_hasher = token_hasher

    # --- reading ---------------------------------------------------------

    def verified(
        self, connection: sqlite3.Connection, *, household_id: str
    ) -> tuple[BindingRecord, ...]:
        """Every verified binding of a household, oldest first.

        Ordering is by `verified_at` then `id` so the manifest a planner builds
        from these rows is stable: an unordered projection would change
        `config_sha256` between two runs that bound nothing new.
        """
        rows = connection.execute(
            "SELECT * FROM channel_bindings WHERE household_id = ?"
            " ORDER BY verified_at, id",
            (household_id,),
        ).fetchall()
        return tuple(self._record(row) for row in rows)

    def owner_actor(
        self, connection: sqlite3.Connection, *, household_id: str
    ) -> str | None:
        """The actor a challenge is issued and attributed under.

        Read from the binding table rather than the manifest so that the
        authority a new binding rests on is the same record the new binding
        joins. `None` means the household has no owner binding yet, and a
        household without a first member cannot acquire a second.
        """
        row = connection.execute(
            "SELECT actor_id FROM channel_bindings WHERE household_id = ?"
            " AND role = 'owner' ORDER BY verified_at, id LIMIT 1",
            (household_id,),
        ).fetchone()
        return None if row is None else str(row["actor_id"])

    @staticmethod
    def _record(row: sqlite3.Row) -> BindingRecord:
        chat_id = row["chat_id"]
        if chat_id is None:
            # 0010 backfills every row and a trigger refuses a NULL insert, so
            # this is unreachable from a migrated database. It is raised rather
            # than defaulted to `external_id`, because that default IS the
            # conflation C3a removed — it would silently answer the chat
            # question with the sender's identity all over again.
            raise BindingError("binding row predates the chat/sender split")
        return BindingRecord(
            id=row["id"],
            household_id=row["household_id"],
            channel=row["channel"],
            external_id=row["external_id"],
            chat_id=str(chat_id),
            actor_id=row["actor_id"],
            role=row["role"],
            verified_at=float(row["verified_at"]),
            verified_by_actor_id=row["verified_by_actor_id"],
        )

    # --- writing ---------------------------------------------------------

    def ensure_owner_binding(
        self,
        connection: sqlite3.Connection,
        *,
        household_id: str,
        channel: str,
        external_id: str,
        chat_id: str,
        actor_id: str,
        now: float | None = None,
    ) -> BindingRecord:
        """Make `channel_bindings` match the authoritative owner state.

        The owner never answers a challenge: their binding is established by
        the onboarding step that already proved the channel, and re-proving it
        over that same channel would verify nothing. Running twice for the same
        owner state is therefore a no-op — the planner runs on every revision,
        and a second revision must not depend on whether a first one ran.

        "The same owner state" is the part that was wrong. This returned early
        whenever `(channel, external_id)` already existed, whatever that row
        WAS, so two reset shapes slipped past reconciliation entirely:

        * the tuple belonged to an ADULT. No owner row was written at all, the
          previous owner row survived on the channel the household had just
          left, and the manifest either lost its owner binding or attributed
          the primary channel to the adult;
        * the tuple was the owner's but the ACTOR had changed during reset. The
          old actor stayed authoritative and the new one never appeared.

        Both reproduced. Matching now means role, actor and tuple together;
        anything else is a reset, and a reset retires what it replaces.

        C3a adds `chat_id` to that list for the same reason it added the actor.
        An owner who re-runs `primary_channel` onto a different conversation
        keeps their sender identity — on Telegram the user ID does not change
        when the family moves to a new group — so a match on the tuple alone
        would leave the household's REVIEW SURFACE pointing at the chat it just
        left, which is where cards, approvals and digests appear.
        """
        now = time.time() if now is None else now
        # Before the lookup, for the same reason as in `issue_challenge`: the
        # query below matches by string, so a padded value would miss the row
        # it means to reconcile and insert a second one beside it.
        external_id, chat_id, actor_id = self._canonical_pair(
            channel, external_id=external_id, chat_id=chat_id, actor_id=actor_id
        )
        existing = connection.execute(
            "SELECT * FROM channel_bindings WHERE household_id = ? AND channel = ?"
            " AND external_id = ?",
            (household_id, channel, external_id),
        ).fetchone()
        if (
            existing is not None
            and existing["role"] == "owner"
            and existing["actor_id"] == actor_id
            and existing["chat_id"] == chat_id
        ):
            # Genuinely the same owner state: nothing to reconcile, and
            # nothing to invalidate. An earlier revision of this method also
            # dropped outstanding challenges here, reasoning that a reset onto
            # an identical tuple crosses an onboarding generation invisibly.
            # That was over-applied, and it broke concurrency: the planner runs
            # on EVERY revision, including the one issued immediately after a
            # verification, so redeeming one invitation deleted every other
            # outstanding one.
            #
            # It was also unnecessary. If the owner state did not change, an
            # outstanding invitation still creates exactly the binding it
            # always would have — no boundary is crossed. When the owner state
            # DOES change, `_retire_superseded` below invalidates the
            # challenges, which is the only case where the generation actually
            # moved.
            return self._record(existing)
        self._reject_foreign_holder(
            connection, channel=channel, external_id=external_id, household_id=household_id
        )
        self._retire_superseded(
            connection, household_id=household_id, channel=channel
        )
        return self._insert(
            connection,
            household_id=household_id,
            channel=channel,
            external_id=external_id,
            chat_id=chat_id,
            actor_id=actor_id,
            role="owner",
            verified_at=now,
            verified_by_actor_id=actor_id,
        )

    @staticmethod
    def _retire_superseded(
        connection: sqlite3.Connection, *, household_id: str, channel: str
    ) -> None:
        """Establishing an owner binding RETIRES what it replaces.

        `reset_from(PRIMARY_CHANNEL)` lets a household re-run the step onto a
        different chat, or a different channel entirely. This method only ever
        inserted, so the previous owner row survived — and two of them are not
        merely untidy:

        * two owner rows on one channel make the projection emit two verified
          chats for the primary channel, which `parse_runtime_manifest`
          refuses, so re-onboarding produced a revision that cannot start;
        * a row on the channel the household LEFT keeps its sender routable —
          `gateway/whatsapp_router.py` resolves senders across the whole table
          and has no notion of a binding having been superseded, so a revoked
          channel stayed authorized.

        The second is the one that matters: an owner who moves the household
        off a channel has revoked it, and the table is what the gateway
        believes.

        Adult bindings on the channel now becoming primary go with it. C3a
        made them REPRESENTABLE — two members may share a chat now — but not
        CURRENT: they were verified against the arrangement the owner has just
        replaced, and their `chat_id` is a conversation the household may have
        left. Carrying them across would authorize an actor in a chat nobody
        re-attested. Outstanding challenges are dropped for the same reason: a
        code issued against the old arrangement must not redeem into the new
        one.
        """
        connection.execute(
            "DELETE FROM channel_bindings WHERE household_id = ? AND role = 'owner'",
            (household_id,),
        )
        connection.execute(
            "DELETE FROM channel_bindings WHERE household_id = ? AND channel = ?"
            " AND role != 'owner'",
            (household_id, channel),
        )
        connection.execute(
            "DELETE FROM channel_binding_challenges WHERE household_id = ?"
            " AND consumed_at IS NULL",
            (household_id,),
        )

    def issue_challenge(
        self,
        connection: sqlite3.Connection,
        *,
        household_id: str,
        channel: str,
        external_id: str,
        actor_id: str,
        role: str,
        issued_by_actor_id: str,
        chat_id: str,
        now: float | None = None,
        ttl_seconds: float = CHALLENGE_TTL_SECONDS,
    ) -> IssuedChallenge:
        """Stand a question up that a binding will be the answer to.

        `external_id` is the SENDER — the identity the gateway will match an
        inbound message against. `chat_id` is the CONVERSATION that member
        speaks in, and it may be one another member already holds: that is the
        arrangement C3a exists to allow, an owner and an adult in the family's
        one group chat.

        BOTH are required, and `chat_id` is never defaulted from the sender.
        An earlier version of this method defaulted it, on the reasoning that a
        WhatsApp 1:1 thread is the number — see the module docstring for why
        that is false in this system. The value must be the identifier the
        channel's own ingest produces, because that is the string
        authorization compares.
        """
        now = time.time() if now is None else now
        if channel not in CHANNELS:
            raise BindingError("unknown channel")
        if role not in CHALLENGE_ROLES:
            raise BindingError("a challenge cannot confer this role")
        # Canonicalized BEFORE the uniqueness and ownership checks below, not
        # after: a padded duplicate of a bound sender would otherwise slip past
        # `_reject_foreign_holder` and the already-bound lookup, both of which
        # compare by string, and land as a second row for one identity.
        external_id, chat_id, actor_id = self._canonical_pair(
            channel, external_id=external_id, chat_id=chat_id, actor_id=actor_id
        )
        self._reject_actor_that_is_not_the_sender(
            external_id=external_id, actor_id=actor_id
        )
        if actor_id == issued_by_actor_id:
            raise BindingError("a challenge cannot rebind its issuer")
        # Refusing here as well as at verification is not redundant: an owner
        # who is told at issue time that a number is taken does not send a code
        # to somebody who could never redeem it.
        self._reject_foreign_holder(
            connection, channel=channel, external_id=external_id, household_id=household_id
        )
        # Nothing worth inviting someone to. Refusing HERE is what bounds the
        # durable collections: the caps below count challenge rows and binding
        # rows, and neither grows on a repeat verification — `verify_challenge`
        # returns the existing binding — but the endpoint replans afterwards
        # and `create_revision` inserts another encrypted manifest every time.
        # An owner looping issue-then-verify on an already-bound tuple
        # therefore grew `config_revisions` without limit while every cap
        # reported room to spare. With this refusal a verification can only
        # follow a binding that did not exist, so revisions are bounded by
        # MAX_BINDINGS.
        held = connection.execute(
            "SELECT actor_id FROM channel_bindings WHERE household_id = ?"
            " AND channel = ? AND external_id = ?",
            (household_id, channel, external_id),
        ).fetchone()
        if held is not None:
            raise BindingError(
                "this channel is already bound to that member"
                if held["actor_id"] == actor_id
                else "this channel is already bound to another member"
            )
        open_challenges = connection.execute(
            "SELECT COUNT(*) AS total FROM channel_binding_challenges"
            " WHERE household_id = ? AND consumed_at IS NULL AND expires_at > ?",
            (household_id, now),
        ).fetchone()["total"]
        if open_challenges >= MAX_OPEN_CHALLENGES:
            raise BindingError("too many outstanding challenges for this household")
        code = secrets.token_urlsafe(32)
        row_id = new_id()
        connection.execute(
            "INSERT INTO channel_binding_challenges (id, household_id, channel,"
            " external_id, chat_id, actor_id, role, code_hash,"
            " issued_by_actor_id, expires_at, attempts, consumed_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?)",
            (
                row_id,
                household_id,
                channel,
                external_id,
                chat_id,
                actor_id,
                role,
                self.token_hasher.digest(code),
                issued_by_actor_id,
                now + ttl_seconds,
                now,
            ),
        )
        record = ChallengeRecord(
            id=row_id,
            household_id=household_id,
            channel=channel,
            actor_id=actor_id,
            role=role,
            expires_at=now + ttl_seconds,
        )
        return IssuedChallenge(record=record, code=code)

    def verify_challenge(
        self,
        connection: sqlite3.Connection,
        *,
        code: str,
        household_id: str,
        owner_actor_id: str,
        now: float | None = None,
    ) -> BindingRecord:
        """Answer a challenge and write the binding it was standing in for.

        `household_id` is the CALLER'S household, and it is part of the lookup
        rather than a check afterwards. An actor ID is unique within a
        household and nowhere else — `channel_bindings.actor_id` is plain TEXT
        and two households may both call their owner `synthetic-owner`. With
        only `issued_by_actor_id` compared, an owner whose actor ID happened to
        match another household's could redeem that household's challenge: the
        binding would be written into the ISSUING household (the row carries
        its ID) while the caller's own household got the revision. Scoping the
        lookup makes the collision unreachable instead of merely unlikely.

        `owner_actor_id` is what the row records as `verified_by_actor_id`: the
        member whose authority the binding rests on.
        """
        now = time.time() if now is None else now
        row = connection.execute(
            "SELECT * FROM channel_binding_challenges WHERE code_hash = ?"
            " AND household_id = ?",
            (self.token_hasher.digest(code), household_id),
        ).fetchone()
        # One message for every rejection a holder of a bad code can reach:
        # distinguishing "no such code" from "expired" tells an attacker which
        # guesses were once real.
        if row is None or row["consumed_at"] is not None or row["expires_at"] <= now:
            raise BindingError("invalid or expired challenge")
        if row["issued_by_actor_id"] != owner_actor_id:
            raise BindingError("invalid or expired challenge")
        connection.execute(
            "UPDATE channel_binding_challenges SET consumed_at = ?,"
            " attempts = attempts + 1 WHERE id = ?",
            (now, row["id"]),
        )
        self._reject_foreign_holder(
            connection,
            channel=row["channel"],
            external_id=row["external_id"],
            household_id=row["household_id"],
        )
        existing = connection.execute(
            "SELECT * FROM channel_bindings WHERE household_id = ? AND channel = ?"
            " AND external_id = ?",
            (row["household_id"], row["channel"], row["external_id"]),
        ).fetchone()
        if existing is not None:
            # The ID is already bound in THIS household. Re-verifying is not an
            # error — a family re-runs the flow after losing track of it — but
            # it must not silently move the binding to a different actor, which
            # would hand one person's channel to another without either being
            # told.
            if existing["actor_id"] != row["actor_id"]:
                raise BindingError("this channel is already bound to another member")
            # Nor may it silently move the CONVERSATION. Two challenges naming
            # one sender and two chats are two different bindings; answering
            # the second by returning the first would report a change that did
            # not happen, and the household's review surface is derived from a
            # chat_id.
            if existing["chat_id"] != row["chat_id"]:
                raise BindingError(
                    "this sender is already bound in a different conversation"
                )
            return self._record(existing)
        return self._insert(
            connection,
            household_id=row["household_id"],
            channel=row["channel"],
            external_id=row["external_id"],
            chat_id=row["chat_id"],
            actor_id=row["actor_id"],
            role=row["role"],
            verified_at=now,
            verified_by_actor_id=owner_actor_id,
        )

    # --- internals -------------------------------------------------------

    @staticmethod
    def _canonical_pair(
        channel: str, *, external_id: str, chat_id: str, actor_id: str
    ) -> tuple[str, str, str]:
        """Put all three identities into the form that channel's ingest emits.

        `control_plane/channels.py` owns what canonical means; this translates
        its refusal into a `BindingError`, because everything else in this
        module answers a caller that way and the message is safe to show.

        The sender rule is applied to `actor_id` as well as `external_id`. They
        are two readings of one transport identity — enforced equal just below
        — so canonicalizing only one of them would make the equality check fail
        on a difference of spelling rather than of identity, which is a refusal
        the caller could not act on.
        """
        try:
            return (
                canonical_sender(channel, external_id),
                canonical_chat(channel, chat_id),
                canonical_sender(channel, actor_id),
            )
        except ChannelIdentityError as error:
            raise BindingError(str(error)) from error

    @staticmethod
    def _reject_actor_that_is_not_the_sender(
        *, external_id: str, actor_id: str
    ) -> None:
        """A published binding must authorize the pair channel ingest produces.

        `actor_id` is not a name this system is free to choose. Every channel's
        ingest derives the actor from the TRANSPORT SENDER — Telegram reads
        `message.from.id` (`hermes_cloud/channels/telegram.py:264`), WhatsApp
        reports the `+999…` normalized sender — and `Household.knows_binding`
        compares the resulting `(actor, chat)` pair by string. The gateway
        meanwhile resolves a household from `external_id`, which is the same
        sender seen at the other end of the wire. Two columns, one identity.

        Letting them differ produced a binding that succeeded at every layer
        that could see it and failed at the only one that mattered: the row was
        written, the revision was published, the rollout was scheduled, and then
        every real inbound turn from that member was classified `unknown` and
        got no family capabilities. Nothing reported it, because nothing in the
        control plane can see an authorization that never happens.

        Refusing here is the same remedy C3 chose for the chat conflation:
        refuse where somebody asks, rather than write a row whose failure
        surfaces somewhere nobody is watching. Translating a sender into an
        internal actor would be the alternative, and it is a real design — a
        verified sender-to-actor mapping carried through the manifest — that
        this code does not have and must not pretend to.
        """
        if actor_id != external_id:
            raise BindingError(
                "a member's actor must be the identity their channel sends"
                " from — the runtime authorizes the sender, so a binding under"
                " any other name would never match a message"
            )

    @staticmethod
    def _reject_foreign_holder(
        connection: sqlite3.Connection,
        *,
        channel: str,
        external_id: str,
        household_id: str,
    ) -> None:
        held = connection.execute(
            "SELECT 1 FROM channel_bindings WHERE channel = ? AND external_id = ?"
            " AND household_id != ? LIMIT 1",
            (channel, external_id, household_id),
        ).fetchone()
        if held is not None:
            raise BindingError("this channel is already bound to another household")

    @staticmethod
    def _reject_when_full(connection: sqlite3.Connection, *, household_id: str) -> None:
        total = connection.execute(
            "SELECT COUNT(*) AS total FROM channel_bindings WHERE household_id = ?",
            (household_id,),
        ).fetchone()["total"]
        if total >= MAX_BINDINGS:
            raise BindingError("this household has as many bindings as it may hold")

    def _insert(
        self,
        connection: sqlite3.Connection,
        *,
        household_id: str,
        channel: str,
        external_id: str,
        chat_id: str,
        actor_id: str,
        role: str,
        verified_at: float,
        verified_by_actor_id: str,
    ) -> BindingRecord:
        # Every write passes here, so the identity invariant is checked here as
        # well as at issue time. `issue_challenge` refuses early so that nobody
        # is handed a code that could not have redeemed; this is the one place
        # that no path can go around, including `ensure_owner_binding`.
        external_id, chat_id, actor_id = self._canonical_pair(
            channel, external_id=external_id, chat_id=chat_id, actor_id=actor_id
        )
        self._reject_actor_that_is_not_the_sender(
            external_id=external_id, actor_id=actor_id
        )
        self._reject_when_full(connection, household_id=household_id)
        row_id = new_id()
        # `external_id_hmac` stays NULL, and that is a KNOWN GAP rather than an
        # oversight: the digest the gateway compares against is keyed with the
        # relay key, which the control plane does not hold and has no path to —
        # `provisioning/secrets.py` is C5's work and does not exist yet. A
        # gateway running in strict HMAC mode therefore cannot see any binding
        # written here. `test_hmac_column_stays_null_until_c5_provisions_the_key`
        # pins it so the gap fails a test rather than a delivery.
        connection.execute(
            "INSERT INTO channel_bindings (id, household_id, channel, external_id,"
            " chat_id, external_id_hmac, actor_id, role, verified_at,"
            " verified_by_actor_id) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
            (
                row_id,
                household_id,
                channel,
                external_id,
                chat_id,
                actor_id,
                role,
                verified_at,
                verified_by_actor_id,
            ),
        )
        return BindingRecord(
            id=row_id,
            household_id=household_id,
            channel=channel,
            external_id=external_id,
            chat_id=chat_id,
            actor_id=actor_id,
            role=role,
            verified_at=verified_at,
            verified_by_actor_id=verified_by_actor_id,
        )

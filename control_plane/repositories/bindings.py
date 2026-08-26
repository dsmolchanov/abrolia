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
    external_id: str
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
        return BindingRecord(
            id=row["id"],
            household_id=row["household_id"],
            channel=row["channel"],
            external_id=row["external_id"],
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
        """
        now = time.time() if now is None else now
        existing = connection.execute(
            "SELECT * FROM channel_bindings WHERE household_id = ? AND channel = ?"
            " AND external_id = ?",
            (household_id, channel, external_id),
        ).fetchone()
        if (
            existing is not None
            and existing["role"] == "owner"
            and existing["actor_id"] == actor_id
        ):
            # Genuinely the same owner state. Outstanding challenges still go:
            # a code issued under the previous onboarding generation must not
            # redeem into this one just because the chat happens to match.
            connection.execute(
                "DELETE FROM channel_binding_challenges WHERE household_id = ?"
                " AND consumed_at IS NULL AND created_at < ?",
                (household_id, now),
            )
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

        Adult bindings on the channel now becoming primary go with it. They
        cannot be represented there — see `_reject_unrepresentable_member` —
        so leaving them would rebuild the unstartable manifest by another
        route. Outstanding challenges for the household are dropped too: a code
        issued against the arrangement that just changed must not redeem into
        the one that replaced it.
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
        now: float | None = None,
        ttl_seconds: float = CHALLENGE_TTL_SECONDS,
    ) -> IssuedChallenge:
        now = time.time() if now is None else now
        if channel not in CHANNELS:
            raise BindingError("unknown channel")
        if role not in CHALLENGE_ROLES:
            raise BindingError("a challenge cannot confer this role")
        if not external_id.strip():
            raise BindingError("external ID is required")
        if actor_id == issued_by_actor_id:
            raise BindingError("a challenge cannot rebind its issuer")
        # Refusing here as well as at verification is not redundant: an owner
        # who is told at issue time that a number is taken does not send a code
        # to somebody who could never redeem it.
        self._reject_foreign_holder(
            connection, channel=channel, external_id=external_id, household_id=household_id
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
            " external_id, actor_id, role, code_hash, issued_by_actor_id,"
            " expires_at, attempts, consumed_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?)",
            (
                row_id,
                household_id,
                channel,
                external_id,
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
        self._reject_unrepresentable_member(
            connection,
            household_id=row["household_id"],
            channel=row["channel"],
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
            return self._record(existing)
        return self._insert(
            connection,
            household_id=row["household_id"],
            channel=row["channel"],
            external_id=row["external_id"],
            actor_id=row["actor_id"],
            role=row["role"],
            verified_at=now,
            verified_by_actor_id=owner_actor_id,
        )

    # --- internals -------------------------------------------------------

    @staticmethod
    def _reject_unrepresentable_member(
        connection: sqlite3.Connection,
        *,
        household_id: str,
        channel: str,
    ) -> None:
        """Refuse an adult on the PRIMARY channel: the store cannot hold one.

        `channel_bindings.external_id` is asked two incompatible questions.
        `gateway/whatsapp_router.py:128` matches it against an incoming SENDER;
        `provisioning/planner.py` projects it as the manifest's `chat_id`, the
        place the assistant SPEAKS. For the owner the two coincide, because
        onboarding wrote one value that happens to answer both. For a second
        adult on that same channel they cannot:

        * give the adult the household's chat and the row collides with the
          owner's under `UNIQUE (household_id, channel, external_id)`;
        * give the adult an identity of their own and the projection emits two
          verified chats for the primary channel, which
          `parse_runtime_manifest` refuses with `channels.primary: multiple
          chats` — a revision that cannot start.

        Both were reproduced. The conflation arrived with `0007` and this
        projection is simply the first thing to ask it for two answers at once.
        Separating the sender's identity from the chat is a schema change with
        three consumers, so it is its own slice; until then the honest thing is
        to refuse where someone asks, rather than write a row whose deployment
        fails later.

        An adult on a NON-primary channel is unaffected and supported: a
        different channel takes no part in the primary-chat constraint and
        cannot collide with the owner's row.
        """
        owner = connection.execute(
            "SELECT channel FROM channel_bindings WHERE household_id = ?"
            " AND role = 'owner' ORDER BY verified_at, id LIMIT 1",
            (household_id,),
        ).fetchone()
        if owner is None or owner["channel"] != channel:
            return
        raise BindingError(
            "a second member cannot yet be bound on the household's primary"
            " channel — the binding store does not separate a sender's"
            " identity from the chat it speaks in"
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
        actor_id: str,
        role: str,
        verified_at: float,
        verified_by_actor_id: str,
    ) -> BindingRecord:
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
            " external_id_hmac, actor_id, role, verified_at, verified_by_actor_id)"
            " VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)",
            (
                row_id,
                household_id,
                channel,
                external_id,
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
            actor_id=actor_id,
            role=role,
            verified_at=verified_at,
            verified_by_actor_id=verified_by_actor_id,
        )

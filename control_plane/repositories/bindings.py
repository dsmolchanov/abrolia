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
        """Seed the owner's own binding from verified onboarding, idempotently.

        The owner never answers a challenge: their binding is established by
        the onboarding step that already proved the channel, and re-proving it
        over that same channel would verify nothing. Calling this twice returns
        the first row rather than raising on the UNIQUE constraint — the
        planner runs on every revision, and a second revision must not depend
        on whether a first one happened to run.
        """
        now = time.time() if now is None else now
        existing = connection.execute(
            "SELECT * FROM channel_bindings WHERE household_id = ? AND channel = ?"
            " AND external_id = ?",
            (household_id, channel, external_id),
        ).fetchone()
        if existing is not None:
            return self._record(existing)
        self._reject_foreign_holder(
            connection, channel=channel, external_id=external_id, household_id=household_id
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
        owner_actor_id: str,
        now: float | None = None,
    ) -> BindingRecord:
        """Answer a challenge and write the binding it was standing in for.

        `owner_actor_id` is what the row records as `verified_by_actor_id`: the
        household member whose authority the binding rests on. It is checked
        against the actor that issued the challenge, so a code issued under one
        owner cannot be redeemed into a binding attributed to another.
        """
        now = time.time() if now is None else now
        row = connection.execute(
            "SELECT * FROM channel_binding_challenges WHERE code_hash = ?",
            (self.token_hasher.digest(code),),
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

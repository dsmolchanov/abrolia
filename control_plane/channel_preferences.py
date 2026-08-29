"""Where the assistant reaches a household, and where it falls back to.

Canon Phase 5 §3 gave this table a schema in `0006` and no writer, which is
how it survived two audits looking like working code: rows can be read, the
constraints are right, and nothing has ever put one there.

Two decisions define what it now holds.

**`primary_channel` is a PROJECTION, not a second choice.** The channel a
household speaks on is decided by the `primary_channel` onboarding step, and
`DesiredSpecPlanner.issue` already reads that step to build `channels.primary`
in the manifest. Letting an endpoint set this column independently would make
two records of one fact, disagreeing the moment either moved — the conflation
C3a spent five review rounds removing from `channel_bindings`. So the planner
seeds this row from the same result it builds the manifest from, and the way to
change the primary channel is to re-run the step that proves it.

**The fallback is a REFERENCE to an account, not a copy of an address.**
`fallback_account_id` names whose verified contact address to use;
`accounts.recovery_email_ciphertext` stays the only place that address lives.
The self-ingestion rule — a fallback must never be the household's own agent
inbox, or the assistant answers itself forever — is then something this module
can check ON ITS OWN, by comparing `accounts.recovery_email_lookup_hmac` with
the live `email_identities.address_lookup_hmac`. Both are keyed with the same
`LookupHasher`, so equality of address is equality of digest, with nothing
decrypted and nothing supplied by the caller.

That last part is the whole point of the rewrite. The previous version took the
fallback address AND the agent inbox as arguments and compared them to each
other, so the invariant held exactly as far as the caller's own knowledge did;
beside it sat `_validate_no_self_ingestion`, which read an identity row,
described in comments what a real check would need, and returned None.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Literal

from control_plane.db import ControlPlaneDatabase

PrimaryChannel = Literal["telegram", "whatsapp", "web"]
FallbackChannel = Literal["email"]

ALLOWED_PRIMARY: set[str] = {"telegram", "whatsapp", "web"}
ALLOWED_FALLBACK: set[str] = {"email"}

#: Statuses in which an email identity is still the household's inbox. The same
#: set `email_identities_live_address` indexes on, so "live" means here what it
#: means to the uniqueness rule.
LIVE_IDENTITY_STATUSES = (
    "selected",
    "provisioning",
    "waiting_user",
    "verified",
    "activating",
    "active",
    "needs_attention",
    "outcome_unknown",
)


class ChannelPreferenceError(ValueError):
    """A preference was refused. The message names no address."""


@dataclass(frozen=True)
class ChannelPreference:
    subject_type: str
    subject_id: str
    primary_channel: str
    fallback_channel: str
    #: The account whose verified contact address the fallback sends to.
    fallback_account_id: str | None
    verified_at: float | None
    updated_at: float


class ChannelPreferencesRepository:
    def __init__(self, db: ControlPlaneDatabase) -> None:
        self.db = db

    # --- reading ---------------------------------------------------------

    def get(self, subject_type: str, subject_id: str) -> ChannelPreference | None:
        row = self.db.query_one(
            "SELECT * FROM channel_preferences WHERE subject_type = ? AND subject_id = ?",
            (subject_type, subject_id),
        )
        return None if row is None else self._record(row)

    def get_household(self, household_id: str) -> ChannelPreference | None:
        return self.get("household", household_id)

    # --- writing ---------------------------------------------------------

    def set_household(
        self,
        connection: sqlite3.Connection,
        *,
        household_id: str,
        primary_channel: str,
        fallback_account_id: str,
        fallback_channel: str = "email",
        verified_at: float | None = None,
        now: float | None = None,
    ) -> ChannelPreference:
        """Record where this household is reached, inside the caller's transaction.

        A CONNECTION rather than `self.db.write()`, because the only writer is
        the planner and it is already inside one: opening a second transaction
        raises `nested control-plane write transaction`, so the previous
        signature could not have been called from the one place that has the
        facts. Sharing the caller's transaction also means a preference cannot
        outlive the revision it was seeded beside.
        """
        if primary_channel not in ALLOWED_PRIMARY:
            raise ChannelPreferenceError(f"unknown primary_channel {primary_channel!r}")
        if fallback_channel not in ALLOWED_FALLBACK:
            raise ChannelPreferenceError(
                f"unknown fallback_channel {fallback_channel!r}"
            )
        now = time.time() if now is None else now
        household = connection.execute(
            "SELECT id, status FROM households WHERE id = ?", (household_id,)
        ).fetchone()
        if household is None or household["status"] in ("deleting", "deleted"):
            raise ChannelPreferenceError("household not found or deleted")
        self._reject_unusable_fallback(
            connection, household_id=household_id, account_id=fallback_account_id
        )
        connection.execute(
            "INSERT INTO channel_preferences (subject_type, subject_id,"
            " primary_channel, fallback_channel, fallback_account_id,"
            " verified_at, updated_at) VALUES ('household', ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(subject_type, subject_id) DO UPDATE SET"
            " primary_channel=excluded.primary_channel,"
            " fallback_channel=excluded.fallback_channel,"
            " fallback_account_id=excluded.fallback_account_id,"
            " verified_at=excluded.verified_at, updated_at=excluded.updated_at",
            (
                household_id,
                primary_channel,
                fallback_channel,
                fallback_account_id,
                verified_at,
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM channel_preferences WHERE subject_type = 'household'"
            " AND subject_id = ?",
            (household_id,),
        ).fetchone()
        return self._record(row)

    @staticmethod
    def _reject_unusable_fallback(
        connection: sqlite3.Connection, *, household_id: str, account_id: str
    ) -> None:
        """The fallback must be a verified contact, and not this household's inbox.

        Both halves are asked of the DATABASE rather than of the caller. The
        account must be an active owner of this household — a fallback that
        reaches somebody who is not the household is a disclosure, not a
        delivery — and its verified contact must not be the address the
        household's own agent answers on, which would make every failed
        delivery arrive back as a new inbound message.

        The second check compares lookup digests. `accounts` and
        `email_identities` hash their addresses with the same `LookupHasher`,
        so two rows naming one address carry one digest, and this module needs
        neither the plaintext nor a key to know that.
        """
        owner = connection.execute(
            "SELECT a.id, a.status, a.recovery_email_lookup_hmac"
            " FROM accounts AS a"
            " JOIN household_memberships AS m ON m.account_id = a.id"
            " WHERE a.id = ? AND m.household_id = ? AND m.role = 'owner'"
            " AND m.status = 'active'",
            (account_id, household_id),
        ).fetchone()
        if owner is None:
            raise ChannelPreferenceError(
                "a fallback must be an active owner of this household"
            )
        if owner["status"] != "active":
            raise ChannelPreferenceError("that account cannot receive a fallback")
        placeholders = ",".join("?" for _ in LIVE_IDENTITY_STATUSES)
        collision = connection.execute(
            "SELECT 1 FROM email_identities WHERE household_id = ?"
            f" AND address_lookup_hmac = ? AND status IN ({placeholders}) LIMIT 1",
            (household_id, owner["recovery_email_lookup_hmac"], *LIVE_IDENTITY_STATUSES),
        ).fetchone()
        if collision is not None:
            raise ChannelPreferenceError(
                "fallback must not be the household's agent inbox"
                " (self-ingestion loop)"
            )

    @staticmethod
    def _record(row: sqlite3.Row) -> ChannelPreference:
        return ChannelPreference(
            subject_type=row["subject_type"],
            subject_id=row["subject_id"],
            primary_channel=row["primary_channel"],
            fallback_channel=row["fallback_channel"],
            fallback_account_id=row["fallback_account_id"],
            verified_at=row["verified_at"],
            updated_at=row["updated_at"],
        )

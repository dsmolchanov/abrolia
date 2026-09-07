from __future__ import annotations

import sqlite3
import time

from control_plane.email.local_part import (
    collision_candidate,
    normalize_local_part,
    suggest_local_part,
)
from control_plane.email.models import EmailIdentityRecord, EmailOption
from control_plane.email.repository import EmailIdentityRepository
from control_plane.owners import owner_contact_query


class MailboxRefused(ValueError):
    """A mailbox this household may not have. The message names no address.

    A `ValueError` subclass so the browser route, which redirects the family
    back to the form on any of them, keeps treating it as a correctable
    selection. A TYPE of its own so the JSON route can answer 409 instead of
    letting a refusal the caller can act on arrive as a 500: `select_step`
    catches Pydantic's `ValidationError` and nothing else, and a plain
    `ValueError` there is an unhandled exception.
    """


class EmailIdentityService:
    def __init__(self, repository: EmailIdentityRepository) -> None:
        self.repository = repository

    def select(
        self,
        connection: sqlite3.Connection,
        *,
        household_id: str,
        selection: dict,
        now: float | None = None,
    ) -> EmailIdentityRecord:
        now = time.time() if now is None else now
        option = EmailOption.from_selection(selection["kind"])
        address = None
        if option is EmailOption.MANAGED_ABROLIA:
            address = f"{normalize_local_part(selection['local_part'])}@abrolia.com"
        elif option is EmailOption.OWN_DOMAIN:
            address = (
                f"{normalize_local_part(selection['local_part'])}@{selection['domain']}"
            )
        if address is not None:
            self._reject_owner_contact(connection, household_id, address)
        try:
            identity = self.repository.create_selected(
                connection,
                household_id=household_id,
                option=option,
                address=address,
                now=now,
            )
        except sqlite3.IntegrityError as error:
            # A taken address is the family's to fix, not a server fault. The
            # reservation is UNIQUE on (domain, local part), and the onboarding
            # page offered every household the same `family.assistant`, so the
            # SECOND household to choose the managed option raised this — as a
            # 500, because nothing above translates a database error. Same
            # class as the other refusals here: correctable, and it names no
            # address, because whose mailbox that is is not this family's
            # business.
            raise MailboxRefused(
                "that assistant address is already taken; choose another"
            ) from error
        self.repository.mark_provisioning(connection, identity.id, now=now)
        current = connection.execute(
            "SELECT * FROM email_identities WHERE id = ?", (identity.id,)
        ).fetchone()
        return self.repository._record(current)

    def _reject_owner_contact(
        self, connection: sqlite3.Connection, household_id: str, address: str
    ) -> None:
        """A household's assistant must not answer on an owner's own address.

        `channel_preferences` records the owner's verified contact as the
        fallback the assistant writes to when the primary channel fails, so a
        mailbox equal to that address makes every failed delivery arrive back
        as a new inbound message. `ChannelPreferencesRepository` refuses that
        pairing, and refusing it THERE alone would be too late: the planner
        runs after the provider has created and verified the inbox, where a
        refusal is no longer something the family can correct — it propagates
        out of the provisioning job's transaction instead of out of the
        selection they can change.

        So the collision is refused at the door, in the one place both options
        compose an address: managed and own-domain differ in where the domain
        comes from and not in this. The comparison is between lookup digests
        under one `LookupHasher`, so it needs no decryption, and it asks about
        every ACTIVE OWNER rather than the one the planner happens to pick,
        because the rule is about the household's inbox and not about which
        owner row is first.
        """
        sql, params = owner_contact_query(
            self.repository.lookup, household_id=household_id, address=address
        )
        if connection.execute(sql, params).fetchone() is not None:
            raise MailboxRefused(
                "that mailbox is an owner's own contact address"
                " (self-ingestion loop)"
            )

    #: How many `name`, `name2`, `name3` … candidates `suggest` tries before it
    #: gives up and offers the plain suggestion. Small on purpose: a family
    #: hitting the end of it is a family that should type its own address, and
    #: the form now lets them.
    SUGGESTION_ATTEMPTS = 20

    def suggest(self, household_id: str, *, now: float | None = None) -> str:
        """A local part this household can actually take.

        It used to answer from the family's name alone, and the onboarding page
        did not use it at all — every household was offered the same hardcoded
        `family.assistant`, which the first one reserved and every later one
        collided with. An offer that is not available is not an offer, so
        availability is part of the answer.
        """
        base = self._suggest_from_profile(household_id)
        for sequence in range(1, self.SUGGESTION_ATTEMPTS + 1):
            candidate = collision_candidate(base, sequence)
            if self.available(candidate, now=now):
                return candidate
        return base

    def _suggest_from_profile(self, household_id: str) -> str:
        profile = self.repository.db.query_one(
            "SELECT * FROM household_profiles WHERE household_id = ?", (household_id,)
        )
        if profile is None:
            raise ValueError("household profile is incomplete")
        first = self.repository.decrypt_json(
            "household_profiles",
            household_id,
            "first_name",
            profile["first_name_ciphertext"],
            profile["encryption_key_version"],
        )
        last = self.repository.decrypt_json(
            "household_profiles",
            household_id,
            "last_name",
            profile["last_name_ciphertext"],
            profile["encryption_key_version"],
        )
        return suggest_local_part(first, last)

    def available(self, local_part: str, *, now: float | None = None) -> bool:
        return self.repository.address_available("abrolia.com", local_part, now=now)

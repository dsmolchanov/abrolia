from __future__ import annotations

import sqlite3
import time

from control_plane.crypto import normalize_email
from control_plane.email.local_part import normalize_local_part, suggest_local_part
from control_plane.email.models import EmailIdentityRecord, EmailOption
from control_plane.email.repository import EmailIdentityRepository


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
        identity = self.repository.create_selected(
            connection,
            household_id=household_id,
            option=option,
            address=address,
            now=now,
        )
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
        held = connection.execute(
            "SELECT 1 FROM accounts AS a JOIN household_memberships AS m"
            " ON m.account_id = a.id WHERE m.household_id = ? AND m.role = 'owner'"
            " AND m.status = 'active' AND a.recovery_email_lookup_hmac = ? LIMIT 1",
            (household_id, self.repository.lookup.email(normalize_email(address))),
        ).fetchone()
        if held is not None:
            raise ValueError(
                "that mailbox is an owner's own contact address"
                " (self-ingestion loop)"
            )

    def suggest(self, household_id: str) -> str:
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

from __future__ import annotations

import sqlite3
import time

from control_plane.crypto import LookupHasher, normalize_email
from control_plane.email.local_part import normalize_local_part, suggest_local_part
from control_plane.email.models import EmailIdentityRecord, EmailOption
from control_plane.email.repository import EmailIdentityRepository

#: The one question every mailbox path must ask before it becomes durable.
#:
#: C4a makes an active owner's verified contact the household's email fallback,
#: so a mailbox equal to that address is a loop: every failed delivery arrives
#: back as a new inbound message. `ChannelPreferencesRepository` refuses to
#: record such a pairing, and by then the mailbox exists — so the question has
#: to be asked by each path that can create one, and asked the same way.
#:
#: Stated once here because it has now been missed twice: the repository
#: refused a pairing nothing had asked it about, and the fix for the managed
#: and own-domain options left `gmail_agent` out, since its address is not
#: known until Google grants it. Two spellings of one rule is how the third
#: consumer gets forgotten.
#:
#: It asks about EVERY active owner rather than one, because the fallback is
#: chosen from them and not fixed in advance.
_OWNER_CONTACT_SQL = (
    "SELECT 1 FROM accounts AS a JOIN household_memberships AS m"
    " ON m.account_id = a.id WHERE m.household_id = ? AND m.role = 'owner'"
    " AND m.status = 'active' AND a.recovery_email_lookup_hmac = ? LIMIT 1"
)


def owner_contact_query(
    lookup: LookupHasher, *, household_id: str, address: str
) -> tuple[str, tuple[str, str]]:
    """The SQL and parameters that answer "is this an owner's own address?".

    Returned rather than executed, so each caller runs it the way it already
    talks to the database — inside the selection's transaction, or as a plain
    read from a service that holds no connection — while the rule itself is
    written down once.

    The comparison is between lookup digests: `accounts` and `email_identities`
    hash addresses with the same `LookupHasher`, so equality of address is
    equality of digest, and no caller needs a key or a plaintext.
    """
    return _OWNER_CONTACT_SQL, (household_id, lookup.email(normalize_email(address)))


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
        sql, params = owner_contact_query(
            self.repository.lookup, household_id=household_id, address=address
        )
        if connection.execute(sql, params).fetchone() is not None:
            raise MailboxRefused(
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

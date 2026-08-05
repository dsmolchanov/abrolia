from __future__ import annotations

import sqlite3
import time

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

from __future__ import annotations

import base64
import sqlite3
import time
import uuid
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from control_plane.db import new_id
from control_plane.models import ProfileInput
from control_plane.repositories.base import Repository


class HouseholdNotFound(KeyError):
    """Used for both absent and unauthorized households to avoid enumeration."""


@dataclass(frozen=True)
class HouseholdRecord:
    id: str
    slug: str
    status: str
    family_language: str | None
    timezone: str | None
    country_code: str | None
    residency_mode: str
    current_config_revision: int
    runtime_ref: str | None
    runtime_deleted_at: float | None


def household_slug(household_id: str) -> str:
    raw = uuid.UUID(household_id).bytes
    return "hh-" + base64.b32encode(raw).decode("ascii").lower().rstrip("=")


class HouseholdsRepository(Repository):
    def create_for_owner(
        self,
        account_id: str,
        *,
        now: float | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> HouseholdRecord:
        now = time.time() if now is None else now
        household_id = new_id()
        workflow_id = new_id()

        def insert(active: sqlite3.Connection) -> None:
            existing = active.execute(
                "SELECT h.id FROM households h JOIN household_memberships m"
                " ON m.household_id = h.id WHERE m.account_id = ?"
                " AND m.status = 'active' AND h.status NOT IN ('deleting','deleted') LIMIT 1",
                (account_id,),
            ).fetchone()
            if existing:
                raise ValueError("pilot account already owns an active household")
            active.execute(
                "INSERT INTO households (id, slug, status, residency_mode,"
                " current_config_revision, created_at, updated_at)"
                " VALUES (?, ?, 'draft', 'eu-app', 0, ?, ?)",
                (household_id, household_slug(household_id), now, now),
            )
            active.execute(
                "INSERT INTO household_memberships (account_id, household_id, role, status,"
                " created_at, accepted_at) VALUES (?, ?, 'owner', 'active', ?, ?)",
                (account_id, household_id, now, now),
            )
            active.execute(
                "INSERT INTO onboarding_workflows (id, household_id, state, current_step,"
                " version, created_at, updated_at)"
                " VALUES (?, ?, 'profile_required', 'profile', 0, ?, ?)",
                (workflow_id, household_id, now, now),
            )
            for ordinal, kind in enumerate((
                "profile", "email_identity", "whatsapp_identity", "primary_channel"
            )):
                active.execute(
                    "INSERT INTO onboarding_steps (workflow_id, kind, ordinal, status,"
                    " public_status_json, attempt, updated_at) VALUES (?, ?, ?, ?, '{}', 0, ?)",
                    (workflow_id, kind, ordinal, "available" if ordinal == 0 else "locked", now),
                )

        if connection is None:
            with self.db.write() as active:
                insert(active)
        else:
            insert(connection)
        record = self.get(household_id)
        assert record is not None
        return record

    def get(self, household_id: str) -> HouseholdRecord | None:
        row = self.db.query_one("SELECT * FROM households WHERE id = ?", (household_id,))
        if row is None:
            return None
        return HouseholdRecord(
            id=row["id"],
            slug=row["slug"],
            status=row["status"],
            family_language=row["family_language"],
            timezone=row["timezone"],
            country_code=row["country_code"],
            residency_mode=row["residency_mode"],
            current_config_revision=row["current_config_revision"],
            runtime_ref=row["runtime_ref"],
            runtime_deleted_at=row["runtime_deleted_at"],
        )

    def for_account(self, account_id: str) -> list[HouseholdRecord]:
        rows = self.db.query(
            "SELECT h.id FROM households h JOIN household_memberships m"
            " ON m.household_id = h.id WHERE m.account_id = ? AND m.status = 'active'"
            " AND h.status NOT IN ('deleting','deleted') ORDER BY h.created_at",
            (account_id,),
        )
        return [record for row in rows if (record := self.get(row["id"])) is not None]

    def authorized(self, account_id: str, household_id: str) -> HouseholdRecord:
        row = self.db.query_one(
            "SELECT h.id FROM households h JOIN household_memberships m"
            " ON m.household_id = h.id WHERE h.id = ? AND m.account_id = ?"
            " AND m.status = 'active' AND h.status NOT IN ('deleting','deleted')",
            (household_id, account_id),
        )
        if row is None:
            raise HouseholdNotFound(household_id)
        record = self.get(row["id"])
        assert record is not None
        return record

    def current_for_account(self, account_id: str) -> HouseholdRecord:
        records = self.for_account(account_id)
        if not records:
            raise HouseholdNotFound(account_id)
        return records[0]

    def save_profile(
        self,
        household_id: str,
        profile: ProfileInput,
        *,
        now: float | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        now = time.time() if now is None else now
        try:
            ZoneInfo(profile.timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError("timezone must be a valid IANA name") from error
        if profile.residency_mode.value == "eu-strict":
            raise ValueError("eu-strict is unavailable without an approved EU provider")
        first = self.encrypt_json(
            "household_profiles", household_id, "first_name", profile.first_name
        )
        last = self.encrypt_json(
            "household_profiles", household_id, "last_name", profile.last_name
        )

        def update(active: sqlite3.Connection) -> None:
            active.execute(
                "INSERT INTO household_profiles (household_id, first_name_ciphertext,"
                " last_name_ciphertext, encryption_key_version, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (household_id) DO UPDATE SET"
                " first_name_ciphertext = excluded.first_name_ciphertext,"
                " last_name_ciphertext = excluded.last_name_ciphertext,"
                " encryption_key_version = excluded.encryption_key_version,"
                " updated_at = excluded.updated_at",
                (
                    household_id,
                    first.ciphertext,
                    last.ciphertext,
                    first.key_version,
                    now,
                    now,
                ),
            )
            active.execute(
                "UPDATE households SET status = 'onboarding', family_language = ?,"
                " timezone = ?, country_code = ?, residency_mode = ?, updated_at = ?"
                " WHERE id = ? AND status NOT IN ('deleting','deleted')",
                (
                    profile.family_language,
                    profile.timezone,
                    profile.country_code,
                    profile.residency_mode.value,
                    now,
                    household_id,
                ),
            )

        if connection is None:
            with self.db.write() as active:
                update(active)
        else:
            update(connection)

    def profile(self, household_id: str) -> dict[str, str] | None:
        row = self.db.query_one(
            "SELECT p.*, h.family_language, h.timezone, h.country_code, h.residency_mode"
            " FROM household_profiles p JOIN households h ON h.id = p.household_id"
            " WHERE p.household_id = ?",
            (household_id,),
        )
        if row is None:
            return None
        return {
            "first_name": self.decrypt_json(
                "household_profiles", household_id, "first_name",
                row["first_name_ciphertext"], row["encryption_key_version"],
            ),
            "last_name": self.decrypt_json(
                "household_profiles", household_id, "last_name",
                row["last_name_ciphertext"], row["encryption_key_version"],
            ),
            "family_language": row["family_language"],
            "timezone": row["timezone"],
            "country_code": row["country_code"],
            "residency_mode": row["residency_mode"],
        }

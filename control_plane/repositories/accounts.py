from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from control_plane.crypto import normalize_email
from control_plane.db import new_id
from control_plane.repositories.base import Repository


@dataclass(frozen=True)
class AccountRecord:
    id: str
    recovery_email: str
    status: str
    email_verified_at: float
    created_at: float

    @property
    def masked_email(self) -> str:
        local, domain = self.recovery_email.rsplit("@", 1)
        return f"{local[:1]}***@{domain}"


class AccountsRepository(Repository):
    def create_verified(
        self,
        recovery_email: str,
        *,
        now: float | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> AccountRecord:
        now = time.time() if now is None else now
        account_id = new_id()
        normalized = normalize_email(recovery_email)
        encrypted = self.encrypt_json(
            "accounts",
            account_id,
            "recovery_email",
            {"display": recovery_email.strip(), "normalized": normalized},
        )
        params = (
            account_id,
            self.lookup.email(normalized),
            encrypted.ciphertext,
            encrypted.key_version,
            now,
            "active",
            now,
            now,
        )
        sql = (
            "INSERT INTO accounts (id, recovery_email_lookup_hmac,"
            " recovery_email_ciphertext, encryption_key_version, email_verified_at,"
            " status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        )
        if connection is None:
            with self.db.write() as active:
                active.execute(sql, params)
        else:
            connection.execute(sql, params)
        record = self.get(account_id)
        assert record is not None
        return record

    def get(self, account_id: str) -> AccountRecord | None:
        row = self.db.query_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
        if row is None:
            return None
        email = self.decrypt_json(
            "accounts",
            row["id"],
            "recovery_email",
            row["recovery_email_ciphertext"],
            row["encryption_key_version"],
        )["display"]
        return AccountRecord(
            id=row["id"],
            recovery_email=email,
            status=row["status"],
            email_verified_at=row["email_verified_at"],
            created_at=row["created_at"],
        )

    def by_email(self, email: str) -> AccountRecord | None:
        row = self.db.query_one(
            "SELECT id FROM accounts WHERE recovery_email_lookup_hmac = ?",
            (self.lookup.email(normalize_email(email)),),
        )
        return self.get(row["id"]) if row else None

    def set_status(self, account_id: str, status: str, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self.db.write() as connection:
            updated = connection.execute(
                "UPDATE accounts SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, account_id),
            ).rowcount
            if not updated:
                raise KeyError(account_id)

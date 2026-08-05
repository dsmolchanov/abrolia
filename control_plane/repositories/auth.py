from __future__ import annotations

import hmac
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from control_plane.crypto import LookupHasher, normalize_email
from control_plane.db import new_id
from control_plane.repositories.base import Repository


class InvalidCredential(PermissionError):
    pass


@dataclass(frozen=True)
class AuthTokenRecord:
    id: str
    purpose: str
    account_id: str | None
    email: str | None
    expires_at: float


@dataclass(frozen=True)
class SessionRecord:
    id: str
    account_id: str
    idle_expires_at: float
    absolute_expires_at: float
    reauthenticated_at: float
    revoked_at: float | None


class AuthRepository(Repository):
    def __init__(self, database, cipher, lookup, token_hasher: LookupHasher) -> None:
        super().__init__(database, cipher, lookup)
        self.token_hasher = token_hasher

    def issue_token(
        self,
        raw_token: str,
        *,
        purpose: str,
        expires_at: float,
        account_id: str | None = None,
        email: str | None = None,
        now: float | None = None,
    ) -> str:
        now = time.time() if now is None else now
        token_id = new_id()
        email_hmac = None
        ciphertext = None
        key_version = None
        if email is not None:
            normalized = normalize_email(email)
            email_hmac = self.lookup.email(normalized)
            encrypted = self.encrypt_json(
                "auth_tokens",
                token_id,
                "email_reference",
                {"display": email.strip(), "normalized": normalized},
            )
            ciphertext = encrypted.ciphertext
            key_version = encrypted.key_version
        with self.db.write() as connection:
            connection.execute(
                "INSERT INTO auth_tokens (id, token_hash, purpose, account_id,"
                " email_lookup_hmac, email_reference_ciphertext, encryption_key_version,"
                " expires_at, attempts, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
                (
                    token_id,
                    self.token_hasher.digest(raw_token),
                    purpose,
                    account_id,
                    email_hmac,
                    ciphertext,
                    key_version,
                    expires_at,
                    now,
                ),
            )
        return token_id

    def consume_token(
        self,
        raw_token: str,
        *,
        purpose: str | None = None,
        now: float | None = None,
    ) -> AuthTokenRecord:
        now = time.time() if now is None else now
        with self.db.write() as connection:
            row = self.consume_token_in(
                connection, raw_token, purpose=purpose, now=now
            )
        return self.token_record(row)

    def consume_token_in(
        self,
        connection: sqlite3.Connection,
        raw_token: str,
        *,
        purpose: str | None = None,
        now: float,
    ):
        digest = self.token_hasher.digest(raw_token)
        row = connection.execute(
            "SELECT * FROM auth_tokens WHERE token_hash = ?",
            (digest,),
        ).fetchone()
        if row is None or row["used_at"] is not None or row["expires_at"] <= now:
            raise InvalidCredential("invalid or expired magic link")
        if purpose is not None and row["purpose"] != purpose:
            raise InvalidCredential("magic-link purpose mismatch")
        connection.execute(
            "UPDATE auth_tokens SET used_at = ?, attempts = attempts + 1 WHERE id = ?",
            (now, row["id"]),
        )
        return row

    def token_record(self, row) -> AuthTokenRecord:
        email = None
        if row["email_reference_ciphertext"] is not None:
            email = self.decrypt_json(
                "auth_tokens",
                row["id"],
                "email_reference",
                row["email_reference_ciphertext"],
                row["encryption_key_version"],
            )["display"]
        return AuthTokenRecord(
            id=row["id"],
            purpose=row["purpose"],
            account_id=row["account_id"],
            email=email,
            expires_at=row["expires_at"],
        )

    def create_session(
        self,
        *,
        raw_token: str,
        raw_csrf: str,
        account_id: str,
        idle_expires_at: float,
        absolute_expires_at: float,
        reauthenticated_at: float,
        security_metadata: dict[str, Any] | None = None,
        now: float | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> str:
        now = time.time() if now is None else now
        session_id = new_id()
        params = (
            session_id,
            self.token_hasher.digest(raw_token),
            account_id,
            self.token_hasher.digest(raw_csrf),
            idle_expires_at,
            absolute_expires_at,
            reauthenticated_at,
            now,
            self.public_json(security_metadata or {}),
            now,
        )
        sql = (
            "INSERT INTO sessions (id, token_hash, account_id, csrf_hash, idle_expires_at,"
            " absolute_expires_at, reauthenticated_at, last_seen_at,"
            " security_metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        if connection is None:
            with self.db.write() as active:
                active.execute(sql, params)
        else:
            connection.execute(sql, params)
        return session_id

    def authenticate_session(
        self,
        raw_token: str,
        *,
        now: float | None = None,
        idle_ttl: float = 24 * 60 * 60,
    ) -> SessionRecord:
        now = time.time() if now is None else now
        digest = self.token_hasher.digest(raw_token)
        with self.db.write() as connection:
            row = connection.execute(
                "SELECT s.* FROM sessions s JOIN accounts a ON a.id = s.account_id"
                " WHERE s.token_hash = ? AND a.status = 'active'",
                (digest,),
            ).fetchone()
            if (
                row is None
                or row["revoked_at"] is not None
                or row["idle_expires_at"] <= now
                or row["absolute_expires_at"] <= now
            ):
                raise InvalidCredential("invalid or expired session")
            idle_expires_at = min(now + idle_ttl, row["absolute_expires_at"])
            connection.execute(
                "UPDATE sessions SET last_seen_at = ?, idle_expires_at = ? WHERE id = ?",
                (now, idle_expires_at, row["id"]),
            )
        return SessionRecord(
            id=row["id"],
            account_id=row["account_id"],
            idle_expires_at=idle_expires_at,
            absolute_expires_at=row["absolute_expires_at"],
            reauthenticated_at=row["reauthenticated_at"],
            revoked_at=row["revoked_at"],
        )

    def verify_csrf(self, session_id: str, raw_csrf: str) -> bool:
        row = self.db.query_one("SELECT csrf_hash FROM sessions WHERE id = ?", (session_id,))
        return bool(row) and hmac.compare_digest(
            row["csrf_hash"], self.token_hasher.digest(raw_csrf)
        )

    def revoke_session(self, session_id: str, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self.db.write() as connection:
            connection.execute(
                "UPDATE sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (now, session_id),
            )

    def revoke_account_sessions(self, account_id: str, *, now: float | None = None) -> int:
        now = time.time() if now is None else now
        with self.db.write() as connection:
            return connection.execute(
                "UPDATE sessions SET revoked_at = ? WHERE account_id = ? AND revoked_at IS NULL",
                (now, account_id),
            ).rowcount

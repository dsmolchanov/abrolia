"""Encrypted-at-rest Google OAuth grant storage for one household runtime."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from hermes_cloud.core.db import Database


class GoogleGrantError(RuntimeError):
    pass


@dataclass(frozen=True, repr=False)
class GoogleGrant:
    identity_id: str
    revision: int
    refresh_credential: str
    provider_subject: str
    scopes: tuple[str, ...]
    key_version: int


@dataclass(frozen=True, repr=False)
class RefreshedAccess:
    access_token: str
    expires_at: float
    rotated_refresh_credential: str | None = None


class GoogleTokenRefresher(Protocol):
    def refresh(self, refresh_credential: str) -> RefreshedAccess: ...


class GoogleGrantStore:
    def __init__(
        self,
        database: Database,
        keys: dict[int, bytes],
        *,
        active_version: int,
        clock=time.time,
    ) -> None:
        if active_version not in keys or any(len(key) != 32 for key in keys.values()):
            raise ValueError("Google grant keyring is invalid")
        self.db = database
        self._keys = dict(keys)
        self.active_version = active_version
        self.clock = clock

    @staticmethod
    def _aad(identity_id: str, revision: int) -> bytes:
        return f"gmail-oauth:{identity_id}:{revision}".encode()

    def _seal(self, value: str, identity_id: str, revision: int) -> bytes:
        nonce = os.urandom(12)
        return nonce + AESGCM(self._keys[self.active_version]).encrypt(
            nonce, value.encode(), self._aad(identity_id, revision)
        )

    def put(
        self,
        *,
        identity_id: str,
        revision: int,
        refresh_credential: str,
        provider_subject: str,
        scopes: tuple[str, ...],
    ) -> None:
        if not refresh_credential or not provider_subject or not scopes:
            raise ValueError("Google grant is incomplete")
        now = self.clock()
        sealed = self._seal(refresh_credential, identity_id, revision)
        with self.db.write() as connection:
            connection.execute(
                "INSERT INTO oauth_grants (binding_identity_id, binding_revision,"
                " encrypted_refresh_credential, key_version, provider_subject, scopes_json,"
                " created_at, updated_at, revoked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)"
                " ON CONFLICT (binding_identity_id, binding_revision) DO UPDATE SET"
                " encrypted_refresh_credential = excluded.encrypted_refresh_credential,"
                " key_version = excluded.key_version, provider_subject = excluded.provider_subject,"
                " scopes_json = excluded.scopes_json, updated_at = excluded.updated_at,"
                " revoked_at = NULL",
                (
                    identity_id,
                    revision,
                    sealed,
                    self.active_version,
                    provider_subject,
                    json.dumps(sorted(set(scopes)), separators=(",", ":")),
                    now,
                    now,
                ),
            )

    def load(self, identity_id: str, revision: int) -> GoogleGrant:
        row = self.db.query_one(
            "SELECT * FROM oauth_grants WHERE binding_identity_id = ?"
            " AND binding_revision = ? AND revoked_at IS NULL",
            (identity_id, revision),
        )
        if row is None:
            raise GoogleGrantError("Google grant is unavailable")
        key = self._keys.get(int(row["key_version"]))
        if key is None:
            raise GoogleGrantError("Google grant key version is unavailable")
        sealed = bytes(row["encrypted_refresh_credential"])
        try:
            plaintext = AESGCM(key).decrypt(sealed[:12], sealed[12:], self._aad(identity_id, revision))
        except (InvalidTag, ValueError) as error:
            raise GoogleGrantError("Google grant authentication failed") from error
        return GoogleGrant(
            identity_id,
            revision,
            plaintext.decode(),
            str(row["provider_subject"]),
            tuple(json.loads(row["scopes_json"])),
            int(row["key_version"]),
        )

    def access_token(
        self, identity_id: str, revision: int, refresher: GoogleTokenRefresher
    ) -> RefreshedAccess:
        grant = self.load(identity_id, revision)
        refreshed = refresher.refresh(grant.refresh_credential)
        if refreshed.rotated_refresh_credential:
            self.put(
                identity_id=identity_id,
                revision=revision,
                refresh_credential=refreshed.rotated_refresh_credential,
                provider_subject=grant.provider_subject,
                scopes=grant.scopes,
            )
        return refreshed

    def revoke(self, identity_id: str, revision: int) -> None:
        with self.db.write() as connection:
            connection.execute(
                "UPDATE oauth_grants SET encrypted_refresh_credential = X'', revoked_at = ?,"
                " updated_at = ? WHERE binding_identity_id = ? AND binding_revision = ?",
                (self.clock(), self.clock(), identity_id, revision),
            )

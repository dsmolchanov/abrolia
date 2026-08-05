from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from control_plane.crypto import LookupHasher
from control_plane.db import new_id
from control_plane.repositories.base import Repository


@dataclass(frozen=True)
class ConfigRevisionRecord:
    id: str
    household_id: str
    revision: int
    schema_version: int
    manifest_sha256: str
    status: str


class ConfigRepository(Repository):
    def __init__(self, database, cipher, lookup, token_hasher: LookupHasher) -> None:
        super().__init__(database, cipher, lookup)
        self.token_hasher = token_hasher

    def create_revision(
        self,
        connection: sqlite3.Connection,
        *,
        household_id: str,
        schema_version: int,
        manifest: dict[str, Any],
        manifest_sha256: str,
        now: float | None = None,
    ) -> ConfigRevisionRecord:
        now = time.time() if now is None else now
        current = connection.execute(
            "SELECT COALESCE(MAX(revision), 0) AS revision FROM config_revisions"
            " WHERE household_id = ?",
            (household_id,),
        ).fetchone()["revision"]
        revision = int(current) + 1
        if int(manifest.get("config_revision", 0)) != revision:
            raise ValueError("manifest config revision is not the next immutable revision")
        row_id = new_id()
        encrypted = self.encrypt_json("config_revisions", row_id, "manifest", manifest)
        connection.execute(
            "INSERT INTO config_revisions (id, household_id, revision, schema_version,"
            " manifest_ciphertext, encryption_key_version, manifest_sha256, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'planned', ?)",
            (
                row_id,
                household_id,
                revision,
                schema_version,
                encrypted.ciphertext,
                encrypted.key_version,
                manifest_sha256,
                now,
            ),
        )
        return ConfigRevisionRecord(
            id=row_id,
            household_id=household_id,
            revision=revision,
            schema_version=schema_version,
            manifest_sha256=manifest_sha256,
            status="planned",
        )

    def get(self, household_id: str, revision: int) -> ConfigRevisionRecord | None:
        row = self.db.query_one(
            "SELECT * FROM config_revisions WHERE household_id = ? AND revision = ?",
            (household_id, revision),
        )
        if row is None:
            return None
        return ConfigRevisionRecord(
            id=row["id"],
            household_id=row["household_id"],
            revision=row["revision"],
            schema_version=row["schema_version"],
            manifest_sha256=row["manifest_sha256"],
            status=row["status"],
        )

    def manifest(self, household_id: str, revision: int) -> dict[str, Any]:
        row = self.db.query_one(
            "SELECT * FROM config_revisions WHERE household_id = ? AND revision = ?",
            (household_id, revision),
        )
        if row is None:
            raise KeyError((household_id, revision))
        return self.decrypt_json(
            "config_revisions",
            row["id"],
            "manifest",
            row["manifest_ciphertext"],
            row["encryption_key_version"],
        )

    def issue_bootstrap(
        self,
        connection: sqlite3.Connection,
        *,
        raw_token: str,
        household_id: str,
        runtime_ref: str,
        revision: int,
        manifest_sha256: str,
        expires_at: float,
        now: float | None = None,
    ) -> str:
        now = time.time() if now is None else now
        token_id = new_id()
        connection.execute(
            "UPDATE bootstrap_tokens SET revoked_at = ? WHERE household_id = ?"
            " AND config_revision = ? AND used_at IS NULL AND revoked_at IS NULL",
            (now, household_id, revision),
        )
        connection.execute(
            "UPDATE config_revisions SET status = 'issued', issued_at = ?"
            " WHERE household_id = ? AND revision = ?"
            " AND status IN ('planned','issued','claimed')",
            (now, household_id, revision),
        )
        connection.execute(
            "INSERT INTO bootstrap_tokens (id, token_hash, household_id, runtime_ref,"
            " config_revision, manifest_sha256, expires_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                token_id,
                self.token_hasher.digest(raw_token),
                household_id,
                runtime_ref,
                revision,
                manifest_sha256,
                expires_at,
                now,
            ),
        )
        return token_id

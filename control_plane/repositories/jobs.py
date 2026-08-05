from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from control_plane.crypto import canonical_json
from control_plane.db import new_id
from control_plane.repositories.base import Repository


@dataclass(frozen=True)
class JobRecord:
    id: str
    household_id: str
    workflow_id: str
    kind: str
    operation: str
    intent_key: str
    desired_revision: int | None
    status: str
    provider: str
    attempts: int
    lease_until: float | None
    error_code: str | None
    reclaimed: bool = False


class JobsRepository(Repository):
    def create(
        self,
        connection: sqlite3.Connection,
        *,
        household_id: str,
        workflow_id: str,
        kind: str,
        operation: str,
        intent_key: str,
        request: dict[str, Any],
        provider: str,
        desired_revision: int | None = None,
        now: float | None = None,
    ) -> tuple[str, bool]:
        now = time.time() if now is None else now
        existing = connection.execute(
            "SELECT id FROM provisioning_jobs WHERE intent_key = ?", (intent_key,)
        ).fetchone()
        if existing:
            return existing["id"], False
        job_id = new_id()
        encrypted = self.encrypt_json("provisioning_jobs", job_id, "request", request)
        request_sha = hashlib.sha256(canonical_json(request)).hexdigest()
        connection.execute(
            "INSERT INTO provisioning_jobs (id, household_id, workflow_id, kind, operation,"
            " intent_key, desired_revision, request_sha, request_ciphertext,"
            " encryption_key_version, status, provider, attempts, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, 0, ?, ?)",
            (
                job_id,
                household_id,
                workflow_id,
                kind,
                operation,
                intent_key,
                desired_revision,
                request_sha,
                encrypted.ciphertext,
                encrypted.key_version,
                provider,
                now,
                now,
            ),
        )
        return job_id, True

    def _record(self, row, *, reclaimed: bool = False) -> JobRecord:
        return JobRecord(
            id=row["id"],
            household_id=row["household_id"],
            workflow_id=row["workflow_id"],
            kind=row["kind"],
            operation=row["operation"],
            intent_key=row["intent_key"],
            desired_revision=row["desired_revision"],
            status=row["status"],
            provider=row["provider"],
            attempts=row["attempts"],
            lease_until=row["lease_until"],
            error_code=row["error_code"],
            reclaimed=reclaimed,
        )

    def get(self, job_id: str) -> JobRecord | None:
        row = self.db.query_one("SELECT * FROM provisioning_jobs WHERE id = ?", (job_id,))
        return self._record(row) if row else None

    def request(self, job_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM provisioning_jobs WHERE id = ?", (job_id,))
        if row is None:
            raise KeyError(job_id)
        return self.decrypt_json(
            "provisioning_jobs",
            job_id,
            "request",
            row["request_ciphertext"],
            row["encryption_key_version"],
        )

    def result(self, job_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM provisioning_jobs WHERE id = ?", (job_id,))
        if row is None:
            raise KeyError(job_id)
        if row["result_ciphertext"] is None:
            return None
        return self.decrypt_json(
            "provisioning_jobs",
            job_id,
            "result",
            row["result_ciphertext"],
            row["encryption_key_version"],
        )

    def external_ref(self, job_id: str) -> str | None:
        row = self.db.query_one("SELECT * FROM provisioning_jobs WHERE id = ?", (job_id,))
        if row is None:
            raise KeyError(job_id)
        if row["external_ref_ciphertext"] is None:
            return None
        value = self.decrypt_json(
            "provisioning_jobs",
            job_id,
            "external_ref",
            row["external_ref_ciphertext"],
            row["encryption_key_version"],
        )
        return value if isinstance(value, str) else None

    def lease(
        self,
        worker_id: str,
        *,
        lease_seconds: float = 120.0,
        now: float | None = None,
    ) -> JobRecord | None:
        if self.db.workers_paused:
            return None
        now = time.time() if now is None else now
        with self.db.write() as connection:
            row = connection.execute(
                "SELECT * FROM provisioning_jobs WHERE"
                " ((status = 'pending' AND (not_before IS NULL OR not_before <= ?))"
                "  OR (status = 'waiting_user' AND not_before IS NOT NULL"
                "      AND not_before <= ?)"
                "  OR (status = 'running' AND lease_until <= ?))"
                " ORDER BY created_at, id LIMIT 1",
                (now, now, now),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE provisioning_jobs SET status = 'running', leased_by = ?,"
                " lease_until = ?, attempts = attempts + 1, updated_at = ? WHERE id = ?",
                (worker_id, now + lease_seconds, now, row["id"]),
            )
        current = self.db.query_one(
            "SELECT * FROM provisioning_jobs WHERE id = ?", (row["id"],)
        )
        assert current is not None
        return self._record(current, reclaimed=row["status"] == "running")

    def settle(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        external_ref: str | None = None,
        error_code: str | None = None,
        now: float | None = None,
    ) -> None:
        now = time.time() if now is None else now
        row = connection.execute(
            "SELECT encryption_key_version FROM provisioning_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise KeyError(job_id)
        key_version = row["encryption_key_version"]
        result_ciphertext = None
        external_ciphertext = None
        if result is not None:
            result_ciphertext = self.encrypt_json(
                "provisioning_jobs", job_id, "result", result, key_version=key_version
            ).ciphertext
        if external_ref is not None:
            external_ciphertext = self.encrypt_json(
                "provisioning_jobs",
                job_id,
                "external_ref",
                external_ref,
                key_version=key_version,
            ).ciphertext
        connection.execute(
            "UPDATE provisioning_jobs SET status = ?, result_ciphertext = ?,"
            " external_ref_ciphertext = ?, error_code = ?, lease_until = NULL,"
            " leased_by = NULL, not_before = NULL, updated_at = ?, settled_at = ?"
            " WHERE id = ?",
            (
                status,
                result_ciphertext,
                external_ciphertext,
                error_code,
                now,
                now if status in {"succeeded", "failed", "outcome_unknown", "cancelled"} else None,
                job_id,
            ),
        )

    def retry_later(
        self,
        job_id: str,
        *,
        not_before: float,
        error_code: str,
        now: float | None = None,
    ) -> None:
        now = time.time() if now is None else now
        with self.db.write() as connection:
            connection.execute(
                "UPDATE provisioning_jobs SET status = 'pending', not_before = ?,"
                " error_code = ?, lease_until = NULL, leased_by = NULL, settled_at = NULL,"
                " updated_at = ?"
                " WHERE id = ? AND status IN ('running','outcome_unknown')"
                " AND NOT (status = 'outcome_unknown' AND COALESCE(error_code, '') IN"
                " ('cancel_requires_reconciliation','reset_requires_reconciliation'))",
                (not_before, error_code, now, job_id),
            )

    def schedule_waiting_poll(
        self,
        job_id: str,
        *,
        not_before: float,
        error_code: str,
        now: float | None = None,
    ) -> bool:
        """Schedule a bounded provider inspection without leaving waiting-user UX."""
        now = time.time() if now is None else now
        with self.db.write() as connection:
            updated = connection.execute(
                "UPDATE provisioning_jobs SET not_before = ?, error_code = ?,"
                " lease_until = NULL, leased_by = NULL, settled_at = NULL, updated_at = ?"
                " WHERE id = ? AND status = 'waiting_user'",
                (not_before, error_code, now, job_id),
            )
        return updated.rowcount == 1

    def cancel_household(self, household_id: str, *, now: float | None = None) -> int:
        now = time.time() if now is None else now
        with self.db.write() as connection:
            return connection.execute(
                "UPDATE provisioning_jobs SET status = 'cancelled', settled_at = ?,"
                " updated_at = ?, lease_until = NULL, leased_by = NULL"
                " WHERE household_id = ? AND status IN ('pending','waiting_user')",
                (now, now, household_id),
            ).rowcount

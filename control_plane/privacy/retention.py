"""Daily retention sweep for short-lived credentials and workflow metadata."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from control_plane.crypto import canonical_json
from control_plane.repositories.base import Repository

DAY = 24 * 60 * 60


@dataclass(frozen=True)
class RetentionResult:
    deleted: dict[str, int]
    scrubbed: dict[str, int]


class RetentionService:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def run(self, *, now: float | None = None) -> RetentionResult:
        now = time.time() if now is None else now
        deleted: dict[str, int] = {}
        scrubbed: dict[str, int] = {}
        with self.repository.db.write() as connection:
            deleted["auth_tokens"] = connection.execute(
                "DELETE FROM auth_tokens WHERE"
                " (used_at IS NOT NULL AND used_at < ?) OR expires_at < ?",
                (now - DAY, now - DAY),
            ).rowcount
            deleted["sessions"] = connection.execute(
                "DELETE FROM sessions WHERE"
                " (revoked_at IS NOT NULL AND revoked_at < ?) OR absolute_expires_at < ?",
                (now - 30 * DAY, now - 30 * DAY),
            ).rowcount
            deleted["rate_limit_buckets"] = connection.execute(
                "DELETE FROM rate_limit_buckets WHERE updated_at < ?", (now - DAY,)
            ).rowcount
            deleted["idempotency_requests"] = connection.execute(
                "DELETE FROM idempotency_requests WHERE expires_at < ?", (now,)
            ).rowcount
            jobs = connection.execute(
                "SELECT id, encryption_key_version FROM provisioning_jobs"
                " WHERE status != 'outcome_unknown'"
                " AND settled_at IS NOT NULL AND settled_at < ?",
                (now - 30 * DAY,),
            ).fetchall()
            empty_sha = hashlib.sha256(canonical_json({})).hexdigest()
            scrubbed_jobs = 0
            for row in jobs:
                encrypted = self.repository.encrypt_json(
                    "provisioning_jobs",
                    row["id"],
                    "request",
                    {},
                    key_version=row["encryption_key_version"],
                )
                scrubbed_jobs += connection.execute(
                    "UPDATE provisioning_jobs SET request_ciphertext = ?, request_sha = ?,"
                    " result_ciphertext = NULL, external_ref_ciphertext = NULL"
                    " WHERE id = ?",
                    (encrypted.ciphertext, empty_sha, row["id"]),
                ).rowcount
            scrubbed["provisioning_job_payloads"] = scrubbed_jobs
            scrubbed["bootstrap_token_hashes"] = connection.execute(
                "UPDATE bootstrap_tokens SET token_hash = 'retained:' || id"
                " WHERE COALESCE(used_at, revoked_at, expires_at) < ?"
                " AND token_hash NOT LIKE 'retained:%'",
                (now - 30 * DAY,),
            ).rowcount
            deleted["provisioning_jobs"] = connection.execute(
                "DELETE FROM provisioning_jobs WHERE status != 'outcome_unknown'"
                " AND settled_at IS NOT NULL AND settled_at < ?",
                (now - 90 * DAY,),
            ).rowcount
            deleted["bootstrap_tokens"] = connection.execute(
                "DELETE FROM bootstrap_tokens WHERE COALESCE(used_at, revoked_at, expires_at) < ?",
                (now - 90 * DAY,),
            ).rowcount
            deleted["consent_receipts"] = connection.execute(
                "DELETE FROM consent_receipts WHERE revoked_at IS NOT NULL AND revoked_at < ?"
                " AND household_id IS NULL AND account_id IS NULL",
                (now - 3 * 365 * DAY,),
            ).rowcount
            deleted["deletion_tombstones"] = connection.execute(
                "DELETE FROM deletion_tombstones WHERE expires_at < ?", (now,)
            ).rowcount
        return RetentionResult(deleted, scrubbed)

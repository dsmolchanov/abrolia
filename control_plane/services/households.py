from __future__ import annotations

import hashlib
import json
import time

from control_plane.crypto import canonical_json
from control_plane.onboarding.contracts import IdempotencyConflict, WorkflowConflict
from control_plane.repositories.households import HouseholdRecord, HouseholdsRepository

HOUSEHOLD_ROUTE = "/api/v1/households"
IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60


class HouseholdService:
    def __init__(self, repository: HouseholdsRepository) -> None:
        self.repository = repository

    def list_for_account(self, account_id: str) -> list[HouseholdRecord]:
        return self.repository.for_account(account_id)

    def create(self, account_id: str) -> HouseholdRecord:
        return self.repository.create_for_owner(account_id)

    def create_idempotent(
        self,
        account_id: str,
        *,
        idempotency_key: str,
        expected_version: int,
        now: float | None = None,
    ) -> tuple[dict[str, object], bool]:
        """Create/converge the one pilot household under command preconditions."""
        now = time.time() if now is None else now
        key_hmac = self.repository.lookup.digest(idempotency_key)
        request_sha = hashlib.sha256(
            canonical_json({"expected_version": expected_version})
        ).hexdigest()
        with self.repository.db.write() as connection:
            existing_request = connection.execute(
                "SELECT * FROM idempotency_requests WHERE account_id = ? AND route = ?"
                " AND idempotency_key_hmac = ?",
                (account_id, HOUSEHOLD_ROUTE, key_hmac),
            ).fetchone()
            if existing_request is not None and existing_request["expires_at"] > now:
                if existing_request["request_sha"] != request_sha:
                    raise IdempotencyConflict(
                        "idempotency key was already used with another request"
                    )
                return json.loads(existing_request["response_body_json"]), True
            if existing_request is not None:
                connection.execute(
                    "DELETE FROM idempotency_requests WHERE account_id = ? AND route = ?"
                    " AND idempotency_key_hmac = ?",
                    (account_id, HOUSEHOLD_ROUTE, key_hmac),
                )
            current = connection.execute(
                "SELECT h.*, w.version FROM households h JOIN household_memberships m"
                " ON m.household_id = h.id JOIN onboarding_workflows w"
                " ON w.household_id = h.id WHERE m.account_id = ?"
                " AND m.status = 'active' AND h.status NOT IN ('deleting','deleted')"
                " ORDER BY h.created_at LIMIT 1",
                (account_id,),
            ).fetchone()
            if current is None:
                if expected_version != 0:
                    raise WorkflowConflict(
                        f"stale workflow version {expected_version}; current version is 0"
                    )
                household = self.repository.create_for_owner(
                    account_id, now=now, connection=connection
                )
                version = 0
                created = True
            else:
                version = int(current["version"])
                if version != expected_version:
                    raise WorkflowConflict(
                        f"stale workflow version {expected_version}; current version is {version}"
                    )
                household = self.repository.get(current["id"])
                assert household is not None
                created = False
            payload: dict[str, object] = {
                "id": household.id,
                "slug": household.slug,
                "status": household.status,
                "version": version,
                "created": created,
            }
            connection.execute(
                "INSERT INTO idempotency_requests (account_id, route, idempotency_key_hmac,"
                " request_sha, response_status, response_body_json, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, 200, ?, ?, ?)",
                (
                    account_id,
                    HOUSEHOLD_ROUTE,
                    key_hmac,
                    request_sha,
                    json.dumps(payload, sort_keys=True),
                    now,
                    now + IDEMPOTENCY_TTL_SECONDS,
                ),
            )
        return payload, False

    def require(self, account_id: str, household_id: str) -> HouseholdRecord:
        # The repository deliberately returns the same not-found condition for
        # absent and unauthorized UUIDs, closing IDOR enumeration.
        return self.repository.authorized(account_id, household_id)

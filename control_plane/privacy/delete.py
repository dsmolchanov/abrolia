"""Dual-boundary deletion with honest partial/unknown outcomes."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from typing import Protocol

from control_plane.crypto import canonical_json
from control_plane.provisioning.contracts import InspectState, ProviderRegistry
from control_plane.repositories import (
    AccountsRepository,
    AuthRepository,
    HouseholdsRepository,
    JobsRepository,
)
from control_plane.repositories.households import HouseholdNotFound

TOMBSTONE_TTL_SECONDS = 3 * 365 * 24 * 60 * 60
IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60
DELETE_ROUTE = "/api/v1/onboarding/delete"


class RuntimeDeleter(Protocol):
    def delete(self, runtime_ref: str) -> InspectState: ...


class SyntheticRuntimeDeleter:
    def delete(self, runtime_ref: str) -> InspectState:
        return (
            InspectState.ABSENT
            if runtime_ref.startswith("synthetic-runtime:")
            else InspectState.UNKNOWN
        )


class UnavailableRuntimeDeleter:
    """Fail closed when the dedicated runtime wipe transport is not configured."""

    def delete(self, runtime_ref: str) -> InspectState:
        del runtime_ref
        return InspectState.UNKNOWN


@dataclass(frozen=True)
class DeletionResult:
    household_id: str
    completion_status: str
    runtime_status: str
    provider_statuses: dict[str, str]
    retained_consent_receipts: int
    tombstone_expires_at: float
    replayed: bool = False

    def public_dict(self) -> dict[str, object]:
        return {
            "household_id": self.household_id,
            "completion_status": self.completion_status,
            "runtime_status": self.runtime_status,
            "provider_statuses": self.provider_statuses,
            "retained_consent_receipts": self.retained_consent_receipts,
            "tombstone_expires_at": self.tombstone_expires_at,
        }

    @classmethod
    def from_public_dict(
        cls, payload: dict[str, object], *, replayed: bool = False
    ) -> DeletionResult:
        provider_statuses = payload.get("provider_statuses", {})
        if not isinstance(provider_statuses, dict):
            raise ValueError("invalid deletion result")
        return cls(
            household_id=str(payload["household_id"]),
            completion_status=str(payload["completion_status"]),
            runtime_status=str(payload["runtime_status"]),
            provider_statuses={str(key): str(value) for key, value in provider_statuses.items()},
            retained_consent_receipts=int(payload["retained_consent_receipts"]),
            tombstone_expires_at=float(payload["tombstone_expires_at"]),
            replayed=replayed,
        )


class DeletionService:
    def __init__(
        self,
        accounts: AccountsRepository,
        auth: AuthRepository,
        households: HouseholdsRepository,
        jobs: JobsRepository,
        providers: ProviderRegistry,
        *,
        runtime: RuntimeDeleter,
    ) -> None:
        self.accounts = accounts
        self.auth = auth
        self.households = households
        self.jobs = jobs
        self.providers = providers
        self.runtime = runtime
        # Phase 1 deploys one writer. Serializing the external orchestration in
        # process closes the gap between the durable intent and provider calls.
        self._lock = threading.RLock()

    def delete(
        self,
        account_id: str,
        household_id: str,
        *,
        idempotency_key: str,
        expected_version: int | None = None,
        now: float | None = None,
    ) -> DeletionResult:
        now = time.time() if now is None else now
        if not idempotency_key:
            raise ValueError("idempotency key is required")
        idempotency_key_hmac = self.accounts.lookup.digest(idempotency_key)
        request_sha = hashlib.sha256(
            canonical_json(
                {"household_id": household_id, "expected_version": expected_version}
            )
        ).hexdigest()
        with self._lock:
            with self.accounts.db.write() as connection:
                existing = connection.execute(
                    "SELECT * FROM idempotency_requests WHERE account_id = ?"
                    " AND route = ? AND idempotency_key_hmac = ?",
                    (account_id, DELETE_ROUTE, idempotency_key_hmac),
                ).fetchone()
                if existing is not None and existing["expires_at"] > now:
                    if existing["request_sha"] != request_sha:
                        from control_plane.onboarding.contracts import IdempotencyConflict

                        raise IdempotencyConflict(
                            "idempotency key was already used with another deletion request"
                        )
                    payload = json.loads(existing["response_body_json"])
                    if payload.get("completion_status") != "pending":
                        return DeletionResult.from_public_dict(payload, replayed=True)
                else:
                    if existing is not None:
                        connection.execute(
                            "DELETE FROM idempotency_requests WHERE account_id = ?"
                            " AND route = ? AND idempotency_key_hmac = ?",
                            (account_id, DELETE_ROUTE, idempotency_key_hmac),
                        )
                    authorized = connection.execute(
                        "SELECT h.id, w.version, a.recovery_email_lookup_hmac"
                        " FROM households h JOIN household_memberships m"
                        " ON m.household_id = h.id JOIN accounts a ON a.id = m.account_id"
                        " JOIN onboarding_workflows w ON w.household_id = h.id"
                        " WHERE h.id = ? AND m.account_id = ? AND m.role = 'owner'"
                        " AND m.status = 'active' AND h.status != 'deleted'"
                        " AND a.status = 'active'",
                        (household_id, account_id),
                    ).fetchone()
                    if authorized is None:
                        raise HouseholdNotFound(household_id)
                    if expected_version is not None and authorized["version"] != expected_version:
                        from control_plane.onboarding.contracts import WorkflowConflict

                        raise WorkflowConflict(
                            f"stale workflow version {expected_version}; current version is "
                            f"{authorized['version']}"
                        )
                    connection.execute(
                        "UPDATE households SET status = 'deleting', updated_at = ? WHERE id = ?",
                        (now, household_id),
                    )
                    connection.execute(
                        "UPDATE accounts SET status = 'deleting', updated_at = ? WHERE id = ?",
                        (now, account_id),
                    )
                    connection.execute(
                        "UPDATE sessions SET revoked_at = ? WHERE account_id = ?"
                        " AND revoked_at IS NULL",
                        (now, account_id),
                    )
                    connection.execute(
                        "UPDATE auth_tokens SET used_at = COALESCE(used_at, ?)"
                        " WHERE account_id = ? OR email_lookup_hmac = ?",
                        (now, account_id, authorized["recovery_email_lookup_hmac"]),
                    )
                    # A running call has crossed the provider boundary. Leave it
                    # running so the worker/reconciler can prove its outcome.
                    connection.execute(
                        "UPDATE provisioning_jobs SET status = 'cancelled', settled_at = ?,"
                        " updated_at = ?, lease_until = NULL, leased_by = NULL"
                        " WHERE household_id = ? AND status IN ('pending','waiting_user')",
                        (now, now, household_id),
                    )
                    connection.execute(
                        "UPDATE bootstrap_tokens SET revoked_at = ? WHERE household_id = ?"
                        " AND used_at IS NULL AND revoked_at IS NULL",
                        (now, household_id),
                    )
                    pending = {
                        "household_id": household_id,
                        "completion_status": "pending",
                        "runtime_status": "unknown",
                        "provider_statuses": {},
                        "retained_consent_receipts": 0,
                        "tombstone_expires_at": now + TOMBSTONE_TTL_SECONDS,
                    }
                    connection.execute(
                        "INSERT INTO idempotency_requests (account_id, route,"
                        " idempotency_key_hmac, request_sha, response_status, response_body_json,"
                        " created_at, expires_at) VALUES (?, ?, ?, ?, 202, ?, ?, ?)",
                        (
                            account_id,
                            DELETE_ROUTE,
                            idempotency_key_hmac,
                            request_sha,
                            json.dumps(pending, sort_keys=True),
                            now,
                            now + IDEMPOTENCY_TTL_SECONDS,
                        ),
                    )
            result = self.resume(household_id, now=now)
            with self.accounts.db.write() as connection:
                if result.completion_status == "complete":
                    # Do not retain the raw deleted household ID in a replay body
                    # when the account still belongs to another household.
                    connection.execute(
                        "DELETE FROM idempotency_requests WHERE account_id = ?"
                        " AND route = ? AND idempotency_key_hmac = ?",
                        (account_id, DELETE_ROUTE, idempotency_key_hmac),
                    )
                else:
                    connection.execute(
                        "UPDATE idempotency_requests SET response_status = 202,"
                        " response_body_json = ? WHERE account_id = ? AND route = ?"
                        " AND idempotency_key_hmac = ?",
                        (
                            json.dumps(result.public_dict(), sort_keys=True),
                            account_id,
                            DELETE_ROUTE,
                            idempotency_key_hmac,
                        ),
                    )
            return result

    def resume(self, household_id: str, *, now: float | None = None) -> DeletionResult:
        """Continue an already-authorized deletion without a user session."""

        now = time.time() if now is None else now
        with self._lock:
            return self._resume_locked(household_id, now=now)

    def _resume_locked(self, household_id: str, *, now: float) -> DeletionResult:
        household = self.households.get(household_id)
        if household is None or household.status != "deleting":
            raise HouseholdNotFound(household_id)
        owner = self.accounts.db.query_one(
            "SELECT account_id FROM household_memberships WHERE household_id = ?"
            " AND role = 'owner' AND status = 'active' LIMIT 1",
            (household_id,),
        )
        owner_account_id = owner["account_id"] if owner is not None else None
        resource_rows = self.accounts.db.query(
            "SELECT * FROM external_resources WHERE household_id = ?"
            " ORDER BY created_at DESC",
            (household_id,),
        )
        resources = [
            (
                row,
                self.jobs.decrypt_json(
                    "external_resources",
                    row["id"],
                    "external_id",
                    row["external_id_ciphertext"],
                    row["encryption_key_version"],
                ),
            )
            for row in resource_rows
        ]
        unresolved_jobs = self.accounts.db.query(
            "SELECT id, status FROM provisioning_jobs WHERE household_id = ?"
            " AND status IN ('running','outcome_unknown')",
            (household_id,),
        )
        if household.runtime_deleted_at is not None:
            runtime_state = InspectState.ABSENT
        else:
            try:
                runtime_state = (
                    self.runtime.delete(household.runtime_ref)
                    if household.runtime_ref
                    else InspectState.ABSENT
                )
                if not isinstance(runtime_state, InspectState):
                    runtime_state = InspectState.UNKNOWN
            except Exception:
                runtime_state = InspectState.UNKNOWN
            if household.runtime_ref and runtime_state is InspectState.ABSENT:
                # Persist proof before attempting Fly deprovision. If deleting the
                # app later has an unknown outcome, resume must not require the
                # now-unreachable runtime to prove its wipe a second time.
                with self.accounts.db.write() as connection:
                    connection.execute(
                        "UPDATE households SET runtime_deleted_at = COALESCE("
                        "runtime_deleted_at, ?), updated_at = ? WHERE id = ?",
                        (now, now, household_id),
                    )
        provider_statuses: dict[str, str] = {}
        for row, external_ref in resources:
            key = f"{row['provider']}:{row['resource_type']}:{row['stable_name']}"
            if row["status"] == "deleted":
                provider_statuses[key] = InspectState.ABSENT.value
                continue
            try:
                result = self.providers.get(row["provider"]).deprovision(external_ref)
                provider_statuses[key] = result.state.value
            except Exception:
                provider_statuses[key] = InspectState.UNKNOWN.value
        all_states = [runtime_state.value, *provider_statuses.values()]
        if unresolved_jobs:
            all_states.append(InspectState.UNKNOWN.value)
        complete = all(state == "absent" for state in all_states)
        any_unknown = any(state == "unknown" for state in all_states)
        completion = "complete" if complete else ("outcome_unknown" if any_unknown else "partial")
        expires_at = now + TOMBSTONE_TTL_SECONDS
        tombstone = self.accounts.lookup.digest(household_id)
        with self.accounts.db.write() as connection:
            for row, _external_ref in resources:
                key = f"{row['provider']}:{row['resource_type']}:{row['stable_name']}"
                provider_state = provider_statuses[key]
                durable_status = (
                    "deleted"
                    if provider_state == InspectState.ABSENT.value
                    else (
                        "outcome_unknown"
                        if provider_state == InspectState.UNKNOWN.value
                        else "deleting"
                    )
                )
                connection.execute(
                    "UPDATE external_resources SET status = ?, updated_at = ? WHERE id = ?",
                    (durable_status, now, row["id"]),
                )
            connection.execute(
                "INSERT INTO deletion_tombstones (household_id_hmac, deleted_at, expires_at,"
                " completion_status, created_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (household_id_hmac) DO UPDATE SET deleted_at = excluded.deleted_at,"
                " expires_at = excluded.expires_at, completion_status = excluded.completion_status",
                (tombstone, now, expires_at, completion, now),
            )
            if owner_account_id is None:
                retained = connection.execute(
                    "SELECT COUNT(*) AS count FROM consent_receipts WHERE household_id = ?",
                    (household_id,),
                ).fetchone()["count"]
            else:
                retained = connection.execute(
                    "SELECT COUNT(*) AS count FROM consent_receipts"
                    " WHERE household_id = ? OR account_id = ?",
                    (household_id, owner_account_id),
                ).fetchone()["count"]
            if complete:
                connection.execute("DELETE FROM households WHERE id = ?", (household_id,))
                if owner_account_id is not None:
                    remaining = connection.execute(
                        "SELECT 1 FROM household_memberships WHERE account_id = ? LIMIT 1",
                        (owner_account_id,),
                    ).fetchone()
                    if remaining is None:
                        connection.execute(
                            "DELETE FROM accounts WHERE id = ?", (owner_account_id,)
                        )
                    else:
                        connection.execute(
                            "UPDATE accounts SET status = 'active', updated_at = ? WHERE id = ?",
                            (now, owner_account_id),
                        )
            else:
                connection.execute(
                    "UPDATE households SET status = 'deleting', updated_at = ? WHERE id = ?",
                    (now, household_id),
                )
        return DeletionResult(
            household_id,
            completion,
            runtime_state.value,
            provider_statuses,
            retained,
            expires_at,
        )

    def resume_pending(
        self, *, limit: int = 100, now: float | None = None
    ) -> list[DeletionResult]:
        """Background/operator entrypoint for durable partial deletion requests."""

        if limit < 1:
            return []
        rows = self.accounts.db.query(
            "SELECT id FROM households WHERE status = 'deleting'"
            " ORDER BY updated_at, id LIMIT ?",
            (limit,),
        )
        results: list[DeletionResult] = []
        for row in rows:
            try:
                results.append(self.resume(row["id"], now=now))
            except HouseholdNotFound:
                # Another serialized pass completed it between listing and resume.
                continue
        return results

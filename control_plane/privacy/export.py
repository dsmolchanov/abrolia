"""Explicit, classified export that never emits credentials or hides a boundary failure."""

from __future__ import annotations

from typing import Any, Protocol

from control_plane.models import TABLE_CLASSIFICATION
from control_plane.repositories import (
    AccountsRepository,
    HouseholdsRepository,
    JobsRepository,
    OnboardingRepository,
)


class RuntimeExporter(Protocol):
    def export(self, runtime_ref: str) -> dict[str, Any]: ...


class SyntheticRuntimeExporter:
    def export(self, runtime_ref: str) -> dict[str, Any]:
        if not runtime_ref.startswith("synthetic-runtime:"):
            raise RuntimeError("synthetic exporter refused a non-synthetic runtime")
        return {"runtime_ref": runtime_ref, "mode": "synthetic", "records": []}


class UnavailableRuntimeExporter:
    """Fail-closed placeholder until a real runtime DSAR transport is wired."""

    def export(self, runtime_ref: str) -> dict[str, Any]:
        del runtime_ref
        raise RuntimeError("runtime export boundary is unavailable")


class HouseholdExporter:
    def __init__(
        self,
        accounts: AccountsRepository,
        households: HouseholdsRepository,
        onboarding: OnboardingRepository,
        jobs: JobsRepository,
        *,
        runtime: RuntimeExporter,
    ) -> None:
        self.accounts = accounts
        self.households = households
        self.onboarding = onboarding
        self.jobs = jobs
        self.runtime = runtime

    def _rows(self, table: str, sql: str, params: tuple) -> list[dict[str, Any]]:
        if not TABLE_CLASSIFICATION[table].export:
            raise ValueError(f"table {table} is not exportable")
        return [dict(row) for row in self.accounts.db.query(sql, params)]

    def export(self, account_id: str, household_id: str) -> dict[str, Any]:
        owner = self.accounts.db.query_one(
            "SELECT h.id FROM households h JOIN household_memberships m"
            " ON m.household_id = h.id WHERE h.id = ? AND m.account_id = ?"
            " AND m.role = 'owner' AND m.status = 'active'"
            " AND h.status NOT IN ('deleting','deleted')",
            (household_id, account_id),
        )
        if owner is None:
            from control_plane.repositories.households import HouseholdNotFound

            raise HouseholdNotFound(household_id)
        household = self.households.get(household_id)
        assert household is not None
        account = self.accounts.get(account_id)
        if account is None:
            raise KeyError(account_id)
        profile = self.households.profile(household_id)
        workflow = self.onboarding.workflow_for_household(household_id)
        steps = []
        for row in self.accounts.db.query(
            "SELECT * FROM onboarding_steps WHERE workflow_id = ? ORDER BY ordinal",
            (workflow.id,),
        ):
            steps.append({
                "kind": row["kind"],
                "ordinal": row["ordinal"],
                "status": row["status"],
                "selection_kind": row["selection_kind"],
                "selection": self.onboarding.selection(workflow.id, row["kind"]),
                "result": self.onboarding.result(workflow.id, row["kind"]),
                "public_status": self.onboarding.parse_public_json(row["public_status_json"]),
                "error_code": row["error_code"],
                "attempt": row["attempt"],
                "updated_at": row["updated_at"],
            })
        jobs = []
        for row in self.accounts.db.query(
            "SELECT * FROM provisioning_jobs WHERE household_id = ? ORDER BY created_at",
            (household_id,),
        ):
            jobs.append({
                "id": row["id"],
                "kind": row["kind"],
                "operation": row["operation"],
                "intent_key": row["intent_key"],
                "desired_revision": row["desired_revision"],
                "status": row["status"],
                "provider": row["provider"],
                "attempts": row["attempts"],
                "error_code": row["error_code"],
                "request": self.jobs.request(row["id"]),
                "result": self.jobs.result(row["id"]),
                "created_at": row["created_at"],
                "settled_at": row["settled_at"],
            })
        resources = []
        for row in self.accounts.db.query(
            "SELECT * FROM external_resources WHERE household_id = ? ORDER BY created_at",
            (household_id,),
        ):
            external_id = self.jobs.decrypt_json(
                "external_resources",
                row["id"],
                "external_id",
                row["external_id_ciphertext"],
                row["encryption_key_version"],
            )
            resources.append({
                "provider": row["provider"],
                "resource_type": row["resource_type"],
                "stable_name": row["stable_name"],
                "external_id": external_id,
                "status": row["status"],
                "config_revision": row["config_revision"],
            })
        revisions = []
        for row in self.accounts.db.query(
            "SELECT * FROM config_revisions WHERE household_id = ? ORDER BY revision",
            (household_id,),
        ):
            revisions.append({
                "revision": row["revision"],
                "schema_version": row["schema_version"],
                "manifest_sha256": row["manifest_sha256"],
                "status": row["status"],
                "manifest": self.onboarding.decrypt_json(
                    "config_revisions",
                    row["id"],
                    "manifest",
                    row["manifest_ciphertext"],
                    row["encryption_key_version"],
                ),
                "created_at": row["created_at"],
                "activated_at": row["activated_at"],
            })
        email_identities = []
        for row in self.accounts.db.query(
            "SELECT * FROM email_identities WHERE household_id = ? ORDER BY created_at",
            (household_id,),
        ):
            address = None
            provider_subject = None
            if row["address_ciphertext"] is not None:
                address = self.onboarding.decrypt_json(
                    "email_identities",
                    row["id"],
                    "address",
                    row["address_ciphertext"],
                    row["encryption_key_version"],
                )
            if row["provider_subject_ciphertext"] is not None:
                provider_subject = self.onboarding.decrypt_json(
                    "email_identities",
                    row["id"],
                    "provider_subject",
                    row["provider_subject_ciphertext"],
                    row["encryption_key_version"],
                )
            email_identities.append({
                "id": row["id"],
                "option": row["option"],
                "status": row["status"],
                "address": address,
                "address_masked": row["address_masked"],
                "provider_subject": provider_subject,
                "provider_resource_refs": self.onboarding.parse_public_json(
                    row["provider_resource_refs_json"]
                ),
                "secret_binding_ref": row["secret_binding_ref"],
                "granted_scopes": self.onboarding.parse_public_json(
                    row["granted_scopes_json"] or "[]"
                ),
                "version": row["version"],
                "verified_at": row["verified_at"],
                "activated_at": row["activated_at"],
                "disconnected_at": row["disconnected_at"],
            })
        email_reservations = self._rows(
            "email_address_reservations",
            "SELECT id, normalized_domain, normalized_local_part, email_identity_id,"
            " status, expires_at, created_at, consumed_at"
            " FROM email_address_reservations WHERE household_id = ? ORDER BY created_at",
            (household_id,),
        )
        email_activation_receipts = self._rows(
            "email_activation_receipts",
            "SELECT email_identity_id, desired_revision, runtime_ref, provider,"
            " inbound_check, outbound_check, checked_at, receipt_digest, status,"
            " runtime_health_status, runtime_health_checked_at,"
            " runtime_health_owns_attention, runtime_health_identity_version"
            " FROM email_activation_receipts WHERE email_identity_id IN"
            " (SELECT id FROM email_identities WHERE household_id = ?)"
            " ORDER BY desired_revision",
            (household_id,),
        )
        transitions = self._rows(
            "onboarding_transitions",
            "SELECT id, workflow_version, command, from_state, to_state, step_kind,"
            " from_step_status, to_step_status, account_id, session_id, request_id,"
            " related_job_id, redacted_metadata_json, created_at"
            " FROM onboarding_transitions WHERE workflow_id = ? ORDER BY workflow_version",
            (workflow.id,),
        )
        memberships = self._rows(
            "household_memberships",
            "SELECT account_id, role, status, created_at, accepted_at, revoked_at"
            " FROM household_memberships WHERE household_id = ?",
            (household_id,),
        )
        consents = self._rows(
            "consent_receipts",
            "SELECT id, purpose, text_version, text_sha256, locale, accepted_at, revoked_at"
            " FROM consent_receipts WHERE household_id = ? OR account_id = ?",
            (household_id, account_id),
        )
        runtime_status = "absent"
        runtime_export = None
        if household.runtime_ref:
            try:
                runtime_export = self.runtime.export(household.runtime_ref)
                runtime_status = "complete"
            except Exception:
                # Provider bodies and exception text are deliberately not returned.
                runtime_status = "outcome_unknown"
        return {
            "schema_version": 1,
            "completion_status": (
                "complete" if runtime_status in {"absent", "complete"} else "outcome_unknown"
            ),
            "account": {
                "id": account.id,
                "recovery_email": account.recovery_email,
                "status": account.status,
                "email_verified_at": account.email_verified_at,
                "created_at": account.created_at,
            },
            "household": {
                "id": household.id,
                "slug": household.slug,
                "status": household.status,
                "family_language": household.family_language,
                "timezone": household.timezone,
                "country_code": household.country_code,
                "residency_mode": household.residency_mode,
                "current_config_revision": household.current_config_revision,
                "runtime_ref": household.runtime_ref,
                "runtime_deleted_at": household.runtime_deleted_at,
            },
            "profile": profile,
            "memberships": memberships,
            "workflow": {
                "id": workflow.id,
                "state": workflow.state,
                "current_step": workflow.current_step,
                "version": workflow.version,
            },
            "steps": steps,
            "transitions": transitions,
            "jobs": jobs,
            "external_resources": resources,
            "config_revisions": revisions,
            "email_identities": email_identities,
            "email_address_reservations": email_reservations,
            "email_activation_receipts": email_activation_receipts,
            "consent_receipts": consents,
            "runtime": {
                "status": runtime_status,
                "data": runtime_export,
                "error_code": (
                    "runtime_export_unavailable"
                    if runtime_status == "outcome_unknown"
                    else None
                ),
            },
            "excluded_credentials": [
                "auth token hashes",
                "session token and CSRF hashes",
                "bootstrap token hashes",
                "rate-limit bucket hashes",
            ],
            "retention_exceptions": {
                "consent_receipts": "retained for accountability after account deletion"
            },
        }

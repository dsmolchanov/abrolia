"""Two-phase, one-time bootstrap protocol bound to an expected runtime."""

from __future__ import annotations

import hmac
import time
from dataclasses import dataclass
from typing import Any

from control_plane.provisioning.manifest import manifest_sha256
from control_plane.repositories.configs import ConfigRepository
from control_plane.repositories.jobs import JobsRepository
from control_plane.repositories.onboarding import OnboardingRepository, WorkflowRecord


class BootstrapDenied(PermissionError):
    pass


class BootstrapGone(BootstrapDenied):
    pass


class BootstrapConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class BootstrapClaim:
    household_id: str
    runtime_ref: str
    config_revision: int
    manifest_sha256: str
    manifest: dict[str, Any]


@dataclass(frozen=True)
class ActivationReceipt:
    household_id: str
    runtime_ref: str
    config_revision: int
    manifest_sha256: str
    cleanup_pending: bool = False


class BootstrapService:
    def __init__(
        self,
        configs: ConfigRepository,
        onboarding: OnboardingRepository,
        jobs: JobsRepository,
    ) -> None:
        self.configs = configs
        self.onboarding = onboarding
        self.jobs = jobs

    def _token_row(self, connection, raw_token: str):
        digest = self.configs.token_hasher.digest(raw_token)
        return connection.execute(
            "SELECT * FROM bootstrap_tokens WHERE token_hash = ?", (digest,)
        ).fetchone()

    def _assert_not_deleted(self, connection, household_id: str) -> None:
        tombstone = self.configs.lookup.digest(household_id)
        if connection.execute(
            "SELECT 1 FROM deletion_tombstones WHERE household_id_hmac = ?", (tombstone,)
        ).fetchone():
            raise BootstrapGone("bootstrap target was deleted")

    @staticmethod
    def _constant_match(actual: str, expected: str) -> bool:
        return hmac.compare_digest(str(actual).encode(), str(expected).encode())

    def _validate_binding(
        self,
        row,
        *,
        household_id: str,
        runtime_ref: str,
        config_revision: int,
    ) -> None:
        if not (
            self._constant_match(row["household_id"], household_id)
            and self._constant_match(row["runtime_ref"], runtime_ref)
            and row["config_revision"] == config_revision
        ):
            raise BootstrapDenied("bootstrap binding mismatch")

    def _enqueue_cleanup(
        self,
        connection,
        *,
        household_id: str,
        workflow_id: str,
        runtime_ref: str,
        config_revision: int,
        now: float,
    ) -> None:
        """Schedule secret removal only after the runtime fsyncs its receipt."""

        self.jobs.create(
            connection,
            household_id=household_id,
            workflow_id=workflow_id,
            kind="bootstrap_cleanup",
            operation="delete_bootstrap_secret",
            intent_key=f"{household_id}:bootstrap-cleanup:{config_revision}",
            desired_revision=config_revision,
            request={
                "runtime_ref": runtime_ref,
                "name": "HERMES_BOOTSTRAP_TOKEN",
                "cleanup_authorization": "runtime_receipt_acknowledged",
            },
            provider="internal-secret-sink",
            now=now,
        )

    def claim(
        self,
        raw_token: str,
        *,
        household_id: str,
        runtime_ref: str,
        config_revision: int,
        now: float | None = None,
    ) -> BootstrapClaim:
        now = time.time() if now is None else now
        with self.configs.db.write() as connection:
            row = self._token_row(connection, raw_token)
            if row is None:
                raise BootstrapDenied("invalid bootstrap credential")
            self._validate_binding(
                row,
                household_id=household_id,
                runtime_ref=runtime_ref,
                config_revision=config_revision,
            )
            self._assert_not_deleted(connection, household_id)
            if row["used_at"] is not None or row["revoked_at"] is not None:
                raise BootstrapGone("bootstrap credential is no longer available")
            if row["expires_at"] <= now:
                raise BootstrapGone("bootstrap credential expired")
            household = connection.execute(
                "SELECT * FROM households WHERE id = ?", (household_id,)
            ).fetchone()
            revision = connection.execute(
                "SELECT * FROM config_revisions WHERE household_id = ? AND revision = ?",
                (household_id, config_revision),
            ).fetchone()
            if (
                household is None
                or revision is None
                or household["status"] in {"deleting", "deleted"}
                or not self._constant_match(household["runtime_ref"], runtime_ref)
                or revision["status"] not in {"issued", "claimed"}
                or not self._constant_match(
                    revision["manifest_sha256"], row["manifest_sha256"]
                )
            ):
                raise BootstrapConflict("bootstrap target is not claimable")
            if row["claimed_at"] is None:
                connection.execute(
                    "UPDATE bootstrap_tokens SET claimed_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
                connection.execute(
                    "UPDATE config_revisions SET status = 'claimed', claimed_at = ?"
                    " WHERE id = ? AND status = 'issued'",
                    (now, revision["id"]),
                )
            manifest = self.configs.manifest(household_id, config_revision)
            if manifest_sha256(manifest) != revision["manifest_sha256"]:
                raise BootstrapConflict("stored manifest authentication failed")
            return BootstrapClaim(
                household_id,
                runtime_ref,
                config_revision,
                revision["manifest_sha256"],
                manifest,
            )

    def activate(
        self,
        raw_token: str,
        *,
        household_id: str,
        runtime_ref: str,
        config_revision: int,
        activated_sha256: str,
        receipt_acknowledged: bool = False,
        now: float | None = None,
    ) -> ActivationReceipt:
        now = time.time() if now is None else now
        with self.configs.db.write() as connection:
            row = self._token_row(connection, raw_token)
            if row is None:
                raise BootstrapDenied("invalid bootstrap credential")
            self._validate_binding(
                row,
                household_id=household_id,
                runtime_ref=runtime_ref,
                config_revision=config_revision,
            )
            self._assert_not_deleted(connection, household_id)
            if row["revoked_at"] is not None:
                raise BootstrapGone("bootstrap credential is no longer available")
            if row["used_at"] is not None:
                # The activation transaction may commit while its HTTP response is
                # lost. An exact replay is a read-only receipt lookup: it cannot
                # activate another revision or enqueue a second cleanup job.
                if not self._constant_match(row["manifest_sha256"], activated_sha256):
                    raise BootstrapConflict("activation revision hash mismatch")
                household = connection.execute(
                    "SELECT * FROM households WHERE id = ?", (household_id,)
                ).fetchone()
                revision = connection.execute(
                    "SELECT * FROM config_revisions WHERE household_id = ? AND revision = ?",
                    (household_id, config_revision),
                ).fetchone()
                workflow = connection.execute(
                    "SELECT id, state FROM onboarding_workflows WHERE household_id = ?",
                    (household_id,),
                ).fetchone()
                if (
                    household is not None
                    and revision is not None
                    and workflow is not None
                    and household["status"] == "active"
                    and household["current_config_revision"] == config_revision
                    and revision["status"] == "active"
                    and workflow["state"] == "complete"
                    and self._constant_match(
                        revision["manifest_sha256"], activated_sha256
                    )
                ):
                    if receipt_acknowledged:
                        self._enqueue_cleanup(
                            connection,
                            household_id=household_id,
                            workflow_id=workflow["id"],
                            runtime_ref=runtime_ref,
                            config_revision=config_revision,
                            now=now,
                        )
                    return ActivationReceipt(
                        household_id,
                        runtime_ref,
                        config_revision,
                        activated_sha256,
                        cleanup_pending=receipt_acknowledged,
                    )
                raise BootstrapGone("bootstrap credential is no longer available")
            if receipt_acknowledged:
                raise BootstrapConflict(
                    "activation receipt cannot be acknowledged before activation"
                )
            if row["expires_at"] <= now:
                raise BootstrapGone("bootstrap credential expired")
            if row["claimed_at"] is None:
                raise BootstrapConflict("bootstrap must be claimed before activation")
            if not self._constant_match(row["manifest_sha256"], activated_sha256):
                raise BootstrapConflict("activation revision hash mismatch")
            household = connection.execute(
                "SELECT * FROM households WHERE id = ?", (household_id,)
            ).fetchone()
            revision = connection.execute(
                "SELECT * FROM config_revisions WHERE household_id = ? AND revision = ?",
                (household_id, config_revision),
            ).fetchone()
            if (
                household is None
                or revision is None
                or household["status"] in {"deleting", "deleted"}
                or not self._constant_match(household["runtime_ref"], runtime_ref)
                or revision["status"] != "claimed"
            ):
                raise BootstrapConflict("activation target is not ready")
            manifest = self.configs.manifest(household_id, config_revision)
            if manifest_sha256(manifest) != activated_sha256:
                raise BootstrapConflict("activation content hash mismatch")
            workflow_row = connection.execute(
                "SELECT * FROM onboarding_workflows WHERE household_id = ?", (household_id,)
            ).fetchone()
            statuses = {
                item["kind"]: item["status"]
                for item in connection.execute(
                    "SELECT kind, status FROM onboarding_steps WHERE workflow_id = ?",
                    (workflow_row["id"],),
                )
            }
            if any(
                statuses.get(kind) != "verified"
                for kind in ("email_identity", "whatsapp_identity", "primary_channel")
            ):
                raise BootstrapConflict("activation requires all verified onboarding results")
            owner = connection.execute(
                "SELECT account_id FROM household_memberships WHERE household_id = ?"
                " AND role = 'owner' AND status = 'active' LIMIT 1",
                (household_id,),
            ).fetchone()
            if owner is None:
                raise BootstrapConflict("activation owner is unavailable")
            connection.execute(
                "UPDATE config_revisions SET status = 'superseded' WHERE household_id = ?"
                " AND status = 'active' AND revision != ?",
                (household_id, config_revision),
            )
            connection.execute(
                "UPDATE config_revisions SET status = 'active', activated_at = ? WHERE id = ?",
                (now, revision["id"]),
            )
            connection.execute(
                "UPDATE bootstrap_tokens SET used_at = ? WHERE id = ?", (now, row["id"])
            )
            connection.execute(
                "UPDATE households SET status = 'active', current_config_revision = ?,"
                " updated_at = ? WHERE id = ?",
                (config_revision, now, household_id),
            )
            workflow = WorkflowRecord(
                workflow_row["id"],
                workflow_row["household_id"],
                workflow_row["state"],
                workflow_row["current_step"],
                workflow_row["version"],
            )
            new_version = workflow.version + 1
            connection.execute(
                "UPDATE onboarding_workflows SET state = 'complete', current_step = 'runtime',"
                " version = ?, updated_at = ?, completed_at = ? WHERE id = ?",
                (new_version, now, now, workflow.id),
            )
            self.onboarding.append_transition(
                connection,
                workflow=workflow,
                new_version=new_version,
                command="activate_runtime",
                to_state="complete",
                account_id=owner["account_id"],
                session_id=None,
                request_id=f"bootstrap:{row['id']}",
                step_kind="runtime",
                related_job_id=None,
                metadata={"config_revision": config_revision},
                now=now,
            )
        return ActivationReceipt(
            household_id,
            runtime_ref,
            config_revision,
            activated_sha256,
        )

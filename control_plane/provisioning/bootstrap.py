"""Two-phase, one-time bootstrap protocol bound to an expected runtime."""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from control_plane.privacy.consent import (
    CURRENT_RECEIPT_SQL,
    current_receipt_params,
    manifest_required_purposes,
)
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
        return connection.execute("SELECT * FROM bootstrap_tokens WHERE token_hash = ?", (digest,)).fetchone()

    def _assert_not_deleted(self, connection, household_id: str) -> None:
        tombstone = self.configs.lookup.digest(household_id)
        if connection.execute(
            "SELECT 1 FROM deletion_tombstones WHERE household_id_hmac = ?", (tombstone,)
        ).fetchone():
            raise BootstrapGone("bootstrap target was deleted")

    def _manifest_or_none(
        self, household_id: str, config_revision: int
    ) -> dict[str, Any] | None:
        try:
            return self.configs.manifest(household_id, config_revision)
        except KeyError:
            return None

    @staticmethod
    def _assert_current_consent(
        connection, household_id: str, purposes: Iterable[str]
    ) -> None:
        """Every purpose named must be held right now, unrevoked and current.

        Activation is the last moment before a runtime can receive real family
        content, so checking only the S5 restriction here left the Art. 9(2)(a)
        consent unverified at the boundary that matters most: a household could
        revoke it and still activate.
        """
        for purpose in purposes:
            receipt = connection.execute(
                CURRENT_RECEIPT_SQL,
                current_receipt_params(household_id, purpose),
            ).fetchone()
            if receipt is None:
                raise BootstrapConflict(
                    f"activation consent receipt is missing for {purpose}"
                )

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
                or not self._constant_match(revision["manifest_sha256"], row["manifest_sha256"])
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
        email_inbound_check: str | None = None,
        email_outbound_check: str | None = None,
        email_receipt_digest: str | None = None,
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
                    and self._constant_match(revision["manifest_sha256"], activated_sha256)
                ):
                    email_receipt = connection.execute(
                        "SELECT inbound_check, outbound_check, receipt_digest"
                        " FROM email_activation_receipts WHERE desired_revision = ?"
                        " AND runtime_ref = ? ORDER BY checked_at DESC LIMIT 1",
                        (config_revision, runtime_ref),
                    ).fetchone()
                    supplied_health = (
                        email_inbound_check,
                        email_outbound_check,
                        email_receipt_digest,
                    )
                    if any(value is not None for value in supplied_health) and (
                        email_receipt is None
                        or supplied_health
                        != (
                            email_receipt["inbound_check"],
                            email_receipt["outbound_check"],
                            email_receipt["receipt_digest"],
                        )
                    ):
                        raise BootstrapConflict("activation email receipt mismatch")
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
            self._assert_current_consent(
                connection,
                household_id,
                manifest_required_purposes(
                    self._manifest_or_none(household_id, config_revision)
                ),
            )
            if receipt_acknowledged:
                raise BootstrapConflict("activation receipt cannot be acknowledged before activation")
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
            provider_kind = str(manifest.get("email", {}).get("provider_kind") or "")
            if provider_kind == "synthetic" and email_inbound_check is None:
                email_inbound_check = email_outbound_check = "healthy"
                email_receipt_digest = hashlib.sha256(
                    f"synthetic-email-health:{activated_sha256}".encode()
                ).hexdigest()
            if (
                email_inbound_check != "healthy"
                or email_outbound_check != "healthy"
                or email_receipt_digest is None
            ):
                raise BootstrapConflict("activation requires healthy email receipt")
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
            identity = connection.execute(
                "SELECT id, status FROM email_identities WHERE household_id = ?"
                " AND status NOT IN ('disconnecting','deleted')"
                " ORDER BY created_at DESC LIMIT 1",
                (household_id,),
            ).fetchone()
            if identity is None or identity["status"] not in {
                "verified",
                "activating",
                "active",
            }:
                raise BootstrapConflict("activation email identity is unavailable")
            connection.execute(
                "UPDATE email_identities SET status = 'activating', version = version + 1,"
                " updated_at = ? WHERE id = ? AND status = 'verified'",
                (now, identity["id"]),
            )
            connection.execute(
                "INSERT INTO email_activation_receipts (email_identity_id, desired_revision,"
                " runtime_ref, provider, inbound_check, outbound_check, checked_at,"
                " receipt_digest, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active')"
                " ON CONFLICT (email_identity_id, desired_revision) DO UPDATE SET"
                " runtime_ref = excluded.runtime_ref, provider = excluded.provider,"
                " inbound_check = excluded.inbound_check, outbound_check = excluded.outbound_check,"
                " checked_at = excluded.checked_at, receipt_digest = excluded.receipt_digest,"
                " status = 'active'",
                (
                    identity["id"],
                    config_revision,
                    runtime_ref,
                    provider_kind,
                    email_inbound_check,
                    email_outbound_check,
                    now,
                    email_receipt_digest,
                ),
            )
            connection.execute(
                "UPDATE email_identities SET status = 'active', activated_at = ?,"
                " version = version + 1, updated_at = ? WHERE id = ?",
                (now, now, identity["id"]),
            )
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
            connection.execute("UPDATE bootstrap_tokens SET used_at = ? WHERE id = ?", (now, row["id"]))
            connection.execute(
                "UPDATE households SET status = 'active', current_config_revision = ?,"
                " updated_at = ? WHERE id = ?",
                (config_revision, now, household_id),
            )
            # Publishing the bindings this revision carries, in the same
            # transaction that makes the revision live. A binding is written
            # STAGED by `verify_challenge` and is not routable until here: the
            # gateway matches a sender with no revision predicate of its own,
            # so before this a new member's traffic reached a runtime still
            # serving the previous revision, whose manifest has no pair for
            # them.
            #
            # Scoped to this household, deliberately spelled out rather than
            # left to the reader: `published_revision IS NULL` alone would
            # publish every staged binding in the deployment, handing another
            # household's pending member a routable identity on the strength
            # of this one activating.
            connection.execute(
                "UPDATE channel_bindings SET published_revision = ?"
                " WHERE household_id = ? AND published_revision IS NULL",
                (config_revision, household_id),
            )
            # And the job that produced this revision is finished — here,
            # because here is where it actually finished.
            #
            # Activation previously had no link to that job in either
            # direction: the worker settled it `succeeded` at launch, long
            # before the runtime called back, so a runtime that never arrived
            # left the household at `provisioning` with a settled row nothing
            # would revisit. The job now waits, and this is what ends the wait.
            #
            # `settled_at IS NULL` makes it idempotent, which the replay path
            # needs: an activation whose HTTP response was lost re-enters here
            # and must not settle a second time.
            pending_runtime = connection.execute(
                "SELECT id FROM provisioning_jobs WHERE household_id = ?"
                " AND kind = 'runtime' AND desired_revision = ?"
                " AND settled_at IS NULL",
                (household_id, config_revision),
            ).fetchall()
            for pending in pending_runtime:
                self.jobs.settle(
                    connection, pending["id"], status="succeeded", now=now
                )
            workflow = WorkflowRecord(
                workflow_row["id"],
                workflow_row["household_id"],
                workflow_row["state"],
                workflow_row["current_step"],
                workflow_row["version"],
            )
            new_version = workflow.version + 1
            if workflow.state == "complete":
                # A rollout to a household that already finished setup. The
                # revision above is activated exactly as for onboarding — that
                # is the point — but the ONBOARDING record is left alone.
                #
                # Rewriting it here would undo the decision made one step
                # earlier in `_workflow_states_for`: re-stamping `completed_at`
                # moves the date a family finished setting up to the day
                # somebody added an adult, and appending an `activate_runtime`
                # transition records a `complete -> complete` event that never
                # happened. Guarding the worker's transition and not this one
                # protected half the path.
                #
                # Where the rollout IS recorded: `provisioning_jobs` carries
                # the job, and `config_revisions` carries `activated_at` for
                # the revision, which is the history that actually describes it.
                return ActivationReceipt(
                    household_id,
                    runtime_ref,
                    config_revision,
                    activated_sha256,
                )
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

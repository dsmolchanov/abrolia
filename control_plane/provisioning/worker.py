from __future__ import annotations

import secrets
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from control_plane.crypto import SecretMaterial
from control_plane.db import new_id
from control_plane.models import StepKind, StepStatus
from control_plane.observability import StructuredLogger
from control_plane.onboarding.state import VERIFY_RESULT, next_status
from control_plane.provisioning.contracts import (
    InspectState,
    OutcomeUnknown,
    ProviderRateLimited,
    ProviderRegistry,
    ProviderRejected,
    ProviderWaiting,
    ProvisionResult,
    SecretSink,
)
from control_plane.provisioning.planner import DesiredSpecPlanner
from control_plane.repositories.configs import ConfigRepository
from control_plane.repositories.households import HouseholdsRepository
from control_plane.repositories.jobs import JobRecord, JobsRepository
from control_plane.repositories.onboarding import OnboardingRepository, WorkflowRecord


@dataclass(frozen=True)
class WorkResult:
    job_id: str
    status: str
    error_code: str | None = None


class _ProjectionCancelled(RuntimeError):
    pass


class ProvisioningWorker:
    def __init__(
        self,
        *,
        jobs: JobsRepository,
        onboarding: OnboardingRepository,
        households: HouseholdsRepository,
        configs: ConfigRepository,
        planner: DesiredSpecPlanner,
        providers: ProviderRegistry,
        secret_sink: SecretSink,
        worker_id: str = "worker",
        runtime_provider: str = "dry-run-runtime",
        bootstrap_ttl_seconds: int = 3600,
        max_safe_attempts: int = 5,
        logger: StructuredLogger | None = None,
        clock=time.time,
    ) -> None:
        self.jobs = jobs
        self.onboarding = onboarding
        self.households = households
        self.configs = configs
        self.planner = planner
        self.providers = providers
        self.secret_sink = secret_sink
        self.worker_id = worker_id
        self.runtime_provider = runtime_provider
        self.bootstrap_ttl_seconds = bootstrap_ttl_seconds
        self.max_safe_attempts = max_safe_attempts
        self.logger = logger
        self.clock = clock

    def run_once(self) -> WorkResult | None:
        started = time.monotonic()
        result = self._run_once()
        if result is not None:
            self._emit_result(result, started=started)
        return result

    def _emit_result(self, result: WorkResult, *, started: float) -> None:
        if self.logger is None:
            return
        job = self.jobs.get(result.job_id)
        fields = {
            "workflow_id": job.workflow_id if job else "unavailable",
            "step_kind": job.kind if job else "unavailable",
            "job_id": result.job_id,
            "status": result.status,
            "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
            "attempts": job.attempts if job else 0,
            "error_code": result.error_code,
            "provider": job.provider if job else "unavailable",
        }
        try:
            self.logger.emit("provisioning_job_finished", **fields)
        except Exception:
            # Telemetry is deliberately lossy and must never change a durable
            # command result. Fall back to fixed safe values, then give up.
            with suppress(Exception):
                self.logger.emit(
                    "provisioning_job_finished",
                    workflow_id=fields["workflow_id"],
                    step_kind=fields["step_kind"],
                    job_id=result.job_id,
                    status=result.status,
                    duration_ms=fields["duration_ms"],
                    attempts=fields["attempts"],
                    error_code="telemetry_redacted",
                )

    def _run_once(self) -> WorkResult | None:
        now = self.clock()
        job = self.jobs.lease(self.worker_id, now=now)
        if job is None:
            return None
        request = self.jobs.request(job.id)
        if job.kind == "bootstrap_cleanup":
            return self._cleanup_bootstrap(job, request)
        provider = None
        result = None
        try:
            provider = self.providers.get(job.provider)
            if job.kind == "cleanup":
                return self._cleanup(job, request, provider)
            if job.operation == "inspect":
                return self._inspect_job(job, request, provider)
            split_runtime = job.kind == "runtime" and all(
                callable(getattr(provider, name, None)) for name in ("prepare", "launch")
            )
            if job.reclaimed:
                inspected = provider.inspect(request if split_runtime else job.intent_key)
                if inspected.state is InspectState.READY and inspected.result is not None:
                    if job.kind == "runtime":
                        inspected.result.secret_material.clear()
                        return self._settle_runtime_ready(
                            job, inspected.result
                        )
                    result = inspected.result
                elif inspected.state is InspectState.FAILED:
                    return self._mark_step_problem(
                        job,
                        request,
                        "failed",
                        inspected.error_code or "provider_rejected",
                    )
                elif inspected.state is not InspectState.ABSENT:
                    return self._mark_step_problem(
                        job,
                        request,
                        "outcome_unknown",
                        "lease_reclaim_inconclusive",
                    )
            if result is None:
                result = (
                    provider.prepare(request, job.intent_key)
                    if split_runtime
                    else provider.ensure(request, job.intent_key)
                )
            current = self.jobs.get(job.id)
            if current is None or current.status == "cancelled":
                return self._cleanup_cancelled_result(job, result, provider)
            if job.kind == "runtime":
                return self._finish_runtime(job, request, result, provider)
            self._reject_identity_secret(result)
            return self._finish_step(job, request, result)
        except _ProjectionCancelled:
            if result is None or provider is None:
                return WorkResult(job.id, "cancelled", "projection_cancelled")
            return self._cleanup_cancelled_result(job, result, provider)
        except ProviderRateLimited as error:
            current = self.jobs.get(job.id)
            if current is None or current.status == "cancelled":
                return WorkResult(job.id, "cancelled")
            if job.attempts >= self.max_safe_attempts:
                return self._mark_step_problem(
                    job, request, "failed", "rate_limit_exhausted"
                )
            self.jobs.retry_later(
                job.id,
                not_before=self.clock() + error.retry_after,
                error_code=error.code,
                now=self.clock(),
            )
            current = self.jobs.get(job.id)
            return WorkResult(
                job.id,
                current.status if current is not None else "cancelled",
                error.code,
            )
        except ProviderWaiting as error:
            return self._mark_step_problem(job, request, "waiting_user", error.code)
        except ProviderRejected as error:
            return self._mark_step_problem(job, request, "failed", error.code)
        except (OutcomeUnknown, TimeoutError, ConnectionError):
            return self._mark_step_problem(
                job, request, "outcome_unknown", "outcome_unknown"
            )

    @staticmethod
    def _reject_identity_secret(result: ProvisionResult) -> None:
        if not result.secret_material.is_empty:
            result.secret_material.clear()
            raise ProviderRejected(
                "identity adapter returned a secret before runtime secret handoff existed"
            )

    def _inspect_job(self, job: JobRecord, request: dict, provider) -> WorkResult:
        inspected = provider.inspect(request["stable_ref"])
        if inspected.state is InspectState.READY and inspected.result is not None:
            self._reject_identity_secret(inspected.result)
            return self._finish_step(job, request, inspected.result)
        if inspected.state is InspectState.PENDING:
            return self._mark_step_problem(
                job, request, "waiting_user", "waiting_user"
            )
        if inspected.state in {InspectState.UNKNOWN, InspectState.READY}:
            return self._mark_step_problem(
                job, request, "outcome_unknown", "inspect_inconclusive"
            )
        return self._mark_step_problem(
            job,
            request,
            "failed",
            inspected.error_code or "provider_absent",
        )

    def _cleanup_cancelled_result(
        self, job: JobRecord, result: ProvisionResult, provider
    ) -> WorkResult:
        result.secret_material.clear()
        current = self.jobs.get(job.id)
        resource_id = None
        exact_ref: Any = (
            result.public_result if job.kind == "runtime" else result.external_ref
        )
        secret_cleanup_unknown = False
        if current is not None:
            with self.jobs.db.write() as connection:
                resource_id = self._external_resource(
                    connection,
                    job,
                    exact_ref,
                    status="deleting",
                    revision=job.desired_revision,
                )
        if job.kind == "runtime":
            try:
                self.secret_sink.delete(result.external_ref, "HERMES_BOOTSTRAP_TOKEN")
            except Exception:
                secret_cleanup_unknown = True
                if current is not None:
                    with self.jobs.db.write() as connection:
                        self.jobs.create(
                            connection,
                            household_id=job.household_id,
                            workflow_id=job.workflow_id,
                            kind="bootstrap_cleanup",
                            operation="delete_bootstrap_secret",
                            intent_key=f"{job.household_id}:late-bootstrap-cleanup:{job.id}",
                            desired_revision=job.desired_revision,
                            request={
                                "runtime_ref": result.external_ref,
                                "name": "HERMES_BOOTSTRAP_TOKEN",
                                "cleanup_authorization": "runtime_cancelled",
                            },
                            provider="internal-secret-sink",
                            now=self.clock(),
                        )
        try:
            inspected = provider.deprovision(exact_ref)
        except Exception:
            inspected = None
        if current is not None and resource_id is not None:
            with self.jobs.db.write() as connection:
                resource_status = (
                    "deleted"
                    if inspected is not None and inspected.state is InspectState.ABSENT
                    else "outcome_unknown"
                )
                connection.execute(
                    "UPDATE external_resources SET status = ?, updated_at = ? WHERE id = ?",
                    (resource_status, self.clock(), resource_id),
                )
                if resource_status == "deleted" and not secret_cleanup_unknown:
                    current_job = connection.execute(
                        "SELECT status FROM provisioning_jobs WHERE id = ?",
                        (job.id,),
                    ).fetchone()
                    if current_job is not None and current_job["status"] in {
                        "running",
                        "outcome_unknown",
                        "cancelled",
                    }:
                        self.jobs.settle(
                            connection,
                            job.id,
                            status="cancelled",
                            error_code="cancelled_and_compensated",
                            now=self.clock(),
                        )
                if resource_status != "deleted":
                    self.jobs.create(
                        connection,
                        household_id=job.household_id,
                        workflow_id=job.workflow_id,
                        kind="cleanup",
                        operation="deprovision",
                        intent_key=f"{job.household_id}:late-cleanup:{job.id}",
                        request={
                            "resource_id": resource_id,
                            "resource_type": job.kind,
                            "external_ref": exact_ref,
                        },
                        provider=job.provider,
                        now=self.clock(),
                    )
        if (
            not secret_cleanup_unknown
            and inspected is not None
            and inspected.state is InspectState.ABSENT
        ):
            return WorkResult(job.id, "cancelled")
        return WorkResult(job.id, "outcome_unknown", "cancelled_cleanup_unknown")

    def drain(self, *, limit: int = 100) -> list[WorkResult]:
        results: list[WorkResult] = []
        for _ in range(limit):
            result = self.run_once()
            if result is None:
                break
            results.append(result)
        return results

    def reconcile(self, job_id: str) -> WorkResult:
        job = self.jobs.get(job_id)
        if job is None or job.status != "outcome_unknown":
            raise ValueError("only an outcome_unknown job can be reconciled")
        request = self.jobs.request(job.id)
        if job.kind == "bootstrap_cleanup":
            return self._cleanup_bootstrap(job, request)
        provider = self.providers.get(job.provider)
        if job.kind == "cleanup":
            external_ref = request.get("external_ref")
            if not external_ref:
                return self._mark_step_problem(
                    job, request, "failed", "missing_external_ref"
                )
            inspected = provider.inspect(external_ref)
            if inspected.state is InspectState.ABSENT:
                with self.jobs.db.write() as connection:
                    self.jobs.settle(
                        connection, job.id, status="succeeded", now=self.clock()
                    )
                    if request.get("resource_id"):
                        connection.execute(
                            "UPDATE external_resources SET status = 'deleted', updated_at = ?"
                            " WHERE id = ?",
                            (self.clock(), request["resource_id"]),
                        )
                return WorkResult(job.id, "succeeded")
            if inspected.state is InspectState.READY:
                return self._cleanup(job, request, provider)
            if inspected.state is InspectState.FAILED:
                return self._mark_step_problem(
                    job,
                    request,
                    "failed",
                    inspected.error_code or "cleanup_rejected",
                )
            return WorkResult(job.id, "outcome_unknown", "reconcile_inconclusive")
        if job.kind == "runtime":
            split_runtime = all(
                callable(getattr(provider, name, None)) for name in ("prepare", "launch")
            )
            inspected = None
            prepared = None
            try:
                inspected = provider.inspect(request if split_runtime else job.intent_key)
                with self.jobs.db.write() as connection:
                    runtime_state = self._runtime_state(connection, job)
                projection_current = self._runtime_projection_is_current(
                    runtime_state, job
                )
                if not projection_current:
                    if inspected.state is InspectState.READY and inspected.result:
                        inspected.result.secret_material.clear()
                        return self._cleanup_cancelled_result(
                            job, inspected.result, provider
                        )
                    if inspected.state is InspectState.ABSENT:
                        return self._settle_superseded_runtime_absent(job)
                    return WorkResult(
                        job.id,
                        "outcome_unknown",
                        "cancelled_cleanup_unknown",
                    )
                if inspected.state is InspectState.READY and inspected.result:
                    inspected.result.secret_material.clear()
                    return self._settle_runtime_ready(job, inspected.result)
                if inspected.state is InspectState.FAILED:
                    return self._mark_step_problem(
                        job,
                        request,
                        "failed",
                        inspected.error_code or "provider_rejected",
                    )
                if inspected.state in {InspectState.ABSENT, InspectState.PENDING}:
                    prepared = (
                        provider.prepare(request, job.intent_key)
                        if split_runtime
                        else provider.ensure(request, job.intent_key)
                    )
                    return self._finish_runtime(job, request, prepared, provider)
            except _ProjectionCancelled:
                result = prepared or (inspected.result if inspected is not None else None)
                if result is None:
                    return WorkResult(job.id, "cancelled", "projection_cancelled")
                return self._cleanup_cancelled_result(job, result, provider)
            except ProviderRejected as error:
                return self._mark_step_problem(job, request, "failed", error.code)
            except (OutcomeUnknown, TimeoutError, ConnectionError):
                return self._mark_step_problem(
                    job, request, "outcome_unknown", "reconcile_inconclusive"
                )
            return WorkResult(job.id, "outcome_unknown", "reconcile_inconclusive")
        inspected = provider.inspect(job.intent_key)
        if inspected.state is InspectState.READY and inspected.result:
            try:
                self._reject_identity_secret(inspected.result)
                return self._finish_step(job, request, inspected.result)
            except _ProjectionCancelled:
                return self._cleanup_cancelled_result(
                    job, inspected.result, self.providers.get(job.provider)
                )
        if inspected.state is InspectState.ABSENT:
            return self._mark_step_problem(
                job, request, "failed", "provider_absent"
            )
        return WorkResult(job.id, "outcome_unknown", "reconcile_inconclusive")

    def _mark_step_problem(
        self,
        job: JobRecord,
        request: dict,
        job_status: str,
        error_code: str,
    ) -> WorkResult:
        now = self.clock()
        with self.jobs.db.write() as connection:
            current = connection.execute(
                "SELECT status, error_code FROM provisioning_jobs WHERE id = ?", (job.id,)
            ).fetchone()
            if current is None:
                return WorkResult(job.id, "cancelled", "job_missing")
            if current["status"] == "cancelled":
                if job_status != "outcome_unknown":
                    return WorkResult(job.id, "cancelled")
                # Cancellation cannot turn an ambiguous provider call into a
                # definite absence. Keep it reconcilable, but never project it
                # back into the now-terminal onboarding workflow.
                error_code = "cancelled_provider_outcome_unknown"
                self.jobs.settle(
                    connection,
                    job.id,
                    status="outcome_unknown",
                    error_code=error_code,
                    now=now,
                )
                return WorkResult(job.id, "outcome_unknown", error_code)
            if (
                current["status"] == "outcome_unknown"
                and current["error_code"]
                in {
                    "cancel_requires_reconciliation",
                    "reset_requires_reconciliation",
                }
                and job_status == "waiting_user"
            ):
                # cancel/reset deliberately quarantined this in-flight intent.
                # A late "waiting for user" response is not proof of absence,
                # and the superseded UI projection can no longer drive CHECK.
                # Keep the intent explicitly reconcilable instead of silently
                # downgrading it to a stranded waiting_user job.
                return WorkResult(job.id, "outcome_unknown", current["error_code"])
            if current["status"] not in {"running", "outcome_unknown"}:
                return WorkResult(job.id, current["status"], current["status"])
            self.jobs.settle(
                connection,
                job.id,
                status=job_status,
                error_code=error_code,
                now=now,
            )
            step_kind = request.get("step_kind")
            if step_kind in {item.value for item in StepKind if item is not StepKind.RUNTIME}:
                step_status = {
                    "waiting_user": "waiting_user",
                    "outcome_unknown": "verifying",
                }.get(job_status, "failed")
                connection.execute(
                    "UPDATE onboarding_steps SET status = ?, error_code = ?,"
                    " public_status_json = ?, updated_at = ? WHERE workflow_id = ? AND kind = ?"
                    " AND status IN ('provisioning','waiting_user','verifying')"
                    " AND EXISTS (SELECT 1 FROM onboarding_workflows w"
                    " JOIN households h ON h.id = w.household_id"
                    " WHERE w.id = onboarding_steps.workflow_id"
                    " AND w.state != 'cancelled'"
                    " AND h.status NOT IN ('draft','deleting','deleted'))",
                    (
                        step_status,
                        error_code,
                        self.onboarding.public_json({
                            "state": {
                                "waiting_user": "waiting_for_you",
                                "verifying": "needs_reconciliation",
                            }.get(step_status, "needs_attention")
                        }),
                        now,
                        job.workflow_id,
                        step_kind,
                    ),
                )
        return WorkResult(job.id, job_status, error_code)

    def _external_resource(
        self,
        connection,
        job: JobRecord,
        external_ref: Any,
        *,
        status: str,
        revision: int | None = None,
    ) -> str:
        existing = connection.execute(
            "SELECT * FROM external_resources WHERE provider = ? AND resource_type = ?"
            " AND stable_name = ?",
            (job.provider, job.kind, job.intent_key),
        ).fetchone()
        resource_id = existing["id"] if existing else new_id()
        encrypted = self.jobs.encrypt_json(
            "external_resources", resource_id, "external_id", external_ref
        )
        now = self.clock()
        if existing:
            connection.execute(
                "UPDATE external_resources SET external_id_ciphertext = ?,"
                " encryption_key_version = ?, status = ?, config_revision = ?, updated_at = ?"
                " WHERE id = ?",
                (
                    encrypted.ciphertext,
                    encrypted.key_version,
                    status,
                    revision,
                    now,
                    resource_id,
                ),
            )
        else:
            connection.execute(
                "INSERT INTO external_resources (id, household_id, provider, resource_type,"
                " stable_name, external_id_ciphertext, encryption_key_version, status,"
                " config_revision, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    resource_id,
                    job.household_id,
                    job.provider,
                    job.kind,
                    job.intent_key,
                    encrypted.ciphertext,
                    encrypted.key_version,
                    status,
                    revision,
                    now,
                    now,
                ),
            )
        return resource_id

    def _finish_step(
        self, job: JobRecord, request: dict, result: ProvisionResult
    ) -> WorkResult:
        now = self.clock()
        step_kind = StepKind(request["step_kind"])
        durable_result = {
            "external_ref": result.external_ref,
            "public_result": result.public_result,
            "verified": True,
        }
        with self.jobs.db.write() as connection:
            current_job = connection.execute(
                "SELECT status FROM provisioning_jobs WHERE id = ?", (job.id,)
            ).fetchone()
            if current_job is None or current_job["status"] not in {
                "running",
                "outcome_unknown",
            }:
                raise _ProjectionCancelled
            step = connection.execute(
                "SELECT * FROM onboarding_steps WHERE workflow_id = ? AND kind = ?",
                (job.workflow_id, step_kind.value),
            ).fetchone()
            workflow_state = connection.execute(
                "SELECT w.state, h.status AS household_status"
                " FROM onboarding_workflows w JOIN households h ON h.id = w.household_id"
                " WHERE w.id = ?",
                (job.workflow_id,),
            ).fetchone()
            if (
                step is None
                or step["status"] not in {"provisioning", "waiting_user", "verifying"}
                or workflow_state is None
                or workflow_state["state"] == "cancelled"
                or workflow_state["household_status"] in {"draft", "deleting", "deleted"}
            ):
                raise _ProjectionCancelled
            desired = next_status(step_kind, StepStatus(step["status"]), VERIFY_RESULT)
            encrypted = self.onboarding.encrypt_json(
                "onboarding_steps",
                f"{job.workflow_id}:{step_kind.value}",
                "result",
                durable_result,
                key_version=step["encryption_key_version"],
            )
            connection.execute(
                "UPDATE onboarding_steps SET status = ?, result_ciphertext = ?,"
                " public_status_json = ?, error_code = NULL, updated_at = ?"
                " WHERE workflow_id = ? AND kind = ?",
                (
                    desired.value,
                    encrypted.ciphertext,
                    self.onboarding.public_json({
                        "state": "verified",
                        **result.public_result,
                    }),
                    now,
                    job.workflow_id,
                    step_kind.value,
                ),
            )
            self.jobs.settle(
                connection,
                job.id,
                status="succeeded",
                result=durable_result,
                external_ref=result.external_ref,
                now=now,
            )
            self._external_resource(connection, job, result.external_ref, status="ready")
            workflow_row = connection.execute(
                "SELECT * FROM onboarding_workflows WHERE id = ?", (job.workflow_id,)
            ).fetchone()
            workflow = WorkflowRecord(
                workflow_row["id"],
                workflow_row["household_id"],
                workflow_row["state"],
                workflow_row["current_step"],
                workflow_row["version"],
            )
            new_version = workflow.version + 1
            if step_kind is StepKind.PRIMARY_CHANNEL:
                planned = self.planner.issue(connection, household_id=job.household_id)
                runtime_job_id, _ = self.jobs.create(
                    connection,
                    household_id=job.household_id,
                    workflow_id=job.workflow_id,
                    kind="runtime",
                    operation="ensure_runtime",
                    intent_key=f"{job.household_id}:runtime:{planned.revision.revision}",
                    desired_revision=planned.revision.revision,
                    request={
                        "step_kind": "runtime",
                        "manifest": planned.spec.model_dump(mode="json"),
                    },
                    provider=self.runtime_provider,
                    now=now,
                )
                connection.execute(
                    "UPDATE onboarding_workflows SET state = 'runtime_provisioning',"
                    " current_step = 'runtime', version = ?, updated_at = ? WHERE id = ?",
                    (new_version, now, job.workflow_id),
                )
                connection.execute(
                    "UPDATE households SET status = 'provisioning', current_config_revision = ?,"
                    " updated_at = ? WHERE id = ?",
                    (planned.revision.revision, now, job.household_id),
                )
                related_job = runtime_job_id
                to_state = "runtime_provisioning"
            else:
                next_kind = (
                    StepKind.WHATSAPP
                    if step_kind is StepKind.EMAIL
                    else StepKind.PRIMARY_CHANNEL
                )
                connection.execute(
                    "UPDATE onboarding_steps SET status = 'available', updated_at = ?"
                    " WHERE workflow_id = ? AND kind = ? AND status = 'locked'",
                    (now, job.workflow_id, next_kind.value),
                )
                connection.execute(
                    "UPDATE onboarding_workflows SET current_step = ?, version = ?,"
                    " updated_at = ? WHERE id = ?",
                    (next_kind.value, new_version, now, job.workflow_id),
                )
                related_job = job.id
                to_state = workflow.state
            owner = connection.execute(
                "SELECT account_id FROM household_memberships WHERE household_id = ?"
                " AND role = 'owner' AND status = 'active' LIMIT 1",
                (job.household_id,),
            ).fetchone()
            self.onboarding.append_transition(
                connection,
                workflow=workflow,
                new_version=new_version,
                command="provider_result",
                to_state=to_state,
                account_id=owner["account_id"],
                session_id=None,
                request_id=f"worker:{job.id}",
                step_kind=step_kind.value,
                from_step_status=step["status"],
                to_step_status="verified",
                related_job_id=related_job,
                metadata={"provider": job.provider},
                now=now,
            )
        return WorkResult(job.id, "succeeded")

    @staticmethod
    def _runtime_state(connection, job: JobRecord):
        return connection.execute(
            "SELECT j.status AS job_status, w.state AS workflow_state,"
            " h.status AS household_status, h.current_config_revision, h.runtime_ref"
            " FROM provisioning_jobs j"
            " JOIN onboarding_workflows w ON w.id = j.workflow_id"
            " JOIN households h ON h.id = j.household_id"
            " WHERE j.id = ?",
            (job.id,),
        ).fetchone()

    @staticmethod
    def _runtime_projection_is_current(state, job: JobRecord) -> bool:
        return bool(
            state is not None
            and state["job_status"] in {"running", "outcome_unknown"}
            and state["workflow_state"] in {"runtime_provisioning", "activating"}
            and state["household_status"] == "provisioning"
            and state["current_config_revision"] == job.desired_revision
        )

    def _settle_superseded_runtime_absent(self, job: JobRecord) -> WorkResult:
        with self.jobs.db.write() as connection:
            state = self._runtime_state(connection, job)
            if self._runtime_projection_is_current(state, job):
                return WorkResult(
                    job.id,
                    "outcome_unknown",
                    "runtime_projection_changed_during_reconcile",
                )
            if state is not None and state["job_status"] in {
                "running",
                "outcome_unknown",
                "cancelled",
            }:
                self.jobs.settle(
                    connection,
                    job.id,
                    status="cancelled",
                    error_code="cancelled_provider_absent",
                    now=self.clock(),
                )
                connection.execute(
                    "UPDATE external_resources SET status = 'deleted', updated_at = ?"
                    " WHERE household_id = ? AND provider = ? AND resource_type = 'runtime'"
                    " AND status != 'deleted'",
                    (self.clock(), job.household_id, job.provider),
                )
        return WorkResult(job.id, "cancelled")

    def _finish_runtime(
        self, job: JobRecord, request: dict, prepared: ProvisionResult, provider
    ) -> WorkResult:
        now = self.clock()
        manifest = request["manifest"]
        raw_bootstrap = secrets.token_urlsafe(32)
        with self.jobs.db.write() as connection:
            state = self._runtime_state(connection, job)
            if state is not None and state["job_status"] == "succeeded":
                prepared.secret_material.clear()
                return WorkResult(job.id, "succeeded")
            if (
                state is None
                or state["job_status"] not in {"running", "outcome_unknown"}
                or state["workflow_state"] not in {"runtime_provisioning", "activating"}
                or state["household_status"] != "provisioning"
                or state["current_config_revision"] != job.desired_revision
            ):
                raise _ProjectionCancelled
            self.configs.issue_bootstrap(
                connection,
                raw_token=raw_bootstrap,
                household_id=job.household_id,
                runtime_ref=prepared.external_ref,
                revision=job.desired_revision,
                manifest_sha256=manifest["config_sha256"],
                expires_at=now + self.bootstrap_ttl_seconds,
                now=now,
            )
            self._external_resource(
                connection,
                job,
                prepared.public_result,
                status="creating",
                revision=job.desired_revision,
            )
            connection.execute(
                "UPDATE households SET runtime_ref = ?, updated_at = ? WHERE id = ?",
                (prepared.external_ref, now, job.household_id),
            )
            if state["workflow_state"] == "runtime_provisioning":
                workflow_row = connection.execute(
                    "SELECT * FROM onboarding_workflows WHERE id = ?", (job.workflow_id,)
                ).fetchone()
                workflow = WorkflowRecord(
                    workflow_row["id"],
                    workflow_row["household_id"],
                    workflow_row["state"],
                    workflow_row["current_step"],
                    workflow_row["version"],
                )
                new_version = workflow.version + 1
                connection.execute(
                    "UPDATE onboarding_workflows SET state = 'activating', version = ?,"
                    " updated_at = ? WHERE id = ?",
                    (new_version, now, job.workflow_id),
                )
                owner = connection.execute(
                    "SELECT account_id FROM household_memberships WHERE household_id = ?"
                    " AND role = 'owner' AND status = 'active' LIMIT 1",
                    (job.household_id,),
                ).fetchone()
                if owner is None:
                    raise _ProjectionCancelled
                self.onboarding.append_transition(
                    connection,
                    workflow=workflow,
                    new_version=new_version,
                    command="runtime_prepared",
                    to_state="activating",
                    account_id=owner["account_id"],
                    session_id=None,
                    request_id=f"worker:{job.id}",
                    step_kind="runtime",
                    related_job_id=job.id,
                    metadata={"config_revision": job.desired_revision},
                    now=now,
                )
        merged = {
            name: bytearray(value) for name, value in prepared.secret_material.items()
        }
        prepared.secret_material.clear()
        merged["HERMES_BOOTSTRAP_TOKEN"] = bytearray(raw_bootstrap, "ascii")
        merged["HERMES_RUNTIME_DSAR_TOKEN"] = bytearray(
            self.configs.token_hasher.digest(
                f"runtime-dsar:{prepared.external_ref}"
            ),
            "ascii",
        )
        install_failed = False
        try:
            self.secret_sink.install(
                prepared.external_ref, SecretMaterial.from_mapping(merged)
            )
        except Exception:
            install_failed = True
        finally:
            for value in merged.values():
                value[:] = b"\x00" * len(value)
            merged.clear()
        if install_failed:
            return self._mark_step_problem(
                job, request, "outcome_unknown", "secret_install_unknown"
            )
        with self.jobs.db.write() as connection:
            state = self._runtime_state(connection, job)
            if (
                state is None
                or state["job_status"] not in {"running", "outcome_unknown"}
                or state["workflow_state"] != "activating"
                or state["household_status"] != "provisioning"
                or state["current_config_revision"] != job.desired_revision
                or state["runtime_ref"] != prepared.external_ref
            ):
                raise _ProjectionCancelled
        launched = prepared
        launch = getattr(provider, "launch", None)
        if callable(launch):
            launched = launch(request, prepared, job.intent_key)
            if not launched.secret_material.is_empty:
                launched.secret_material.clear()
                raise ProviderRejected("runtime launch returned unstaged secret material")
        return self._settle_runtime_ready(job, launched)

    def _settle_runtime_ready(
        self, job: JobRecord, result: ProvisionResult
    ) -> WorkResult:
        durable_result = {
            "external_ref": result.external_ref,
            "public_result": result.public_result,
            "verified": True,
        }
        with self.jobs.db.write() as connection:
            state = self._runtime_state(connection, job)
            if state is not None and state["job_status"] == "succeeded":
                return WorkResult(job.id, "succeeded")
            if (
                state is None
                or state["job_status"] not in {"running", "outcome_unknown"}
                or state["workflow_state"] not in {"activating", "complete"}
                or state["household_status"] not in {"provisioning", "active"}
                or state["current_config_revision"] != job.desired_revision
                or state["runtime_ref"] != result.external_ref
            ):
                raise _ProjectionCancelled
            self.jobs.settle(
                connection,
                job.id,
                status="succeeded",
                result=durable_result,
                external_ref=result.external_ref,
                now=self.clock(),
            )
            self._external_resource(
                connection,
                job,
                result.public_result,
                status="ready",
                revision=job.desired_revision,
            )
        return WorkResult(job.id, "succeeded")

    def _cleanup(self, job: JobRecord, request: dict, provider) -> WorkResult:
        external_ref = request.get("external_ref") or request.get("result", {}).get(
            "external_ref"
        )
        if not external_ref:
            self._mark_step_problem(job, request, "failed", "missing_external_ref")
            return WorkResult(job.id, "failed", "missing_external_ref")
        inspected = provider.deprovision(external_ref)
        if inspected.state is InspectState.FAILED:
            self._mark_step_problem(job, request, "failed", "cleanup_rejected")
            return WorkResult(job.id, "failed", "cleanup_rejected")
        if inspected.state is not InspectState.ABSENT:
            self._mark_step_problem(job, request, "outcome_unknown", "cleanup_unknown")
            return WorkResult(job.id, "outcome_unknown", "cleanup_unknown")
        with self.jobs.db.write() as connection:
            self.jobs.settle(connection, job.id, status="succeeded", now=self.clock())
            if request.get("resource_id"):
                connection.execute(
                    "UPDATE external_resources SET status = 'deleted', updated_at = ?"
                    " WHERE id = ?",
                    (self.clock(), request["resource_id"]),
                )
        return WorkResult(job.id, "succeeded")

    def _cleanup_bootstrap(self, job: JobRecord, request: dict) -> WorkResult:
        if request.get("cleanup_authorization") not in {
            "runtime_receipt_acknowledged",
            "runtime_cancelled",
        }:
            with self.jobs.db.write() as connection:
                self.jobs.settle(
                    connection,
                    job.id,
                    status="failed",
                    error_code="bootstrap_cleanup_unauthorized",
                    now=self.clock(),
                )
            return WorkResult(job.id, "failed", "bootstrap_cleanup_unauthorized")
        try:
            self.secret_sink.delete(request["runtime_ref"], request["name"])
        except Exception:
            with self.jobs.db.write() as connection:
                self.jobs.settle(
                    connection,
                    job.id,
                    status="outcome_unknown",
                    error_code="bootstrap_cleanup_unknown",
                    now=self.clock(),
                )
            return WorkResult(job.id, "outcome_unknown", "bootstrap_cleanup_unknown")
        with self.jobs.db.write() as connection:
            self.jobs.settle(connection, job.id, status="succeeded", now=self.clock())
        return WorkResult(job.id, "succeeded")

from __future__ import annotations

import json
import secrets
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from control_plane.crypto import SecretFieldError, SecretMaterial, normalize_email
from control_plane.db import new_id
from control_plane.email.contracts import EmailFailureKind, EmailProviderError
from control_plane.email.domain_policy import canonicalize_domain
from control_plane.email.models import (
    SYNTHETIC_EMAIL_SECRET_BINDING,
    EmailDnsPublicStatus,
    EmailGoogleOAuthPublicStatus,
    EmailIdentityStatus,
    EmailNerveAttachmentPublicStatus,
    EmailOption,
    EmailPublicBinding,
)
from control_plane.email.repository import EmailIdentityRepository
from control_plane.models import StepKind, StepStatus
from control_plane.observability import StructuredLogger
from control_plane.onboarding.state import VERIFY_RESULT, next_status
from control_plane.providers.email.nerve_client import email_org_external_ref
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
        email_identities: EmailIdentityRepository | None = None,
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
        self.email_identities = email_identities
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
            job = self.jobs.get(result.job_id)
            if (
                result.status == "succeeded"
                and job is not None
                and job.operation == "ensure_secret_namespace"
            ):
                # Namespace creation is an internal prerequisite, not a visible
                # onboarding step. If the owner already submitted the email
                # choice, preserve one-tick UX while still crossing the Fly app
                # boundary before the identity provider call.
                continued_at = time.monotonic()
                continued = self._run_once()
                if continued is not None:
                    self._emit_result(continued, started=continued_at)
                    return continued
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

    def _durable_work_result(
        self,
        job_id: str,
        *,
        fallback_status: str,
        fallback_error: str,
    ) -> WorkResult:
        current = self.jobs.get(job_id)
        if current is None:
            return WorkResult(job_id, "cancelled", "job_missing")
        return WorkResult(
            job_id,
            current.status or fallback_status,
            current.error_code or fallback_error,
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
            if job.operation == "ensure_secret_namespace":
                ensure_namespace = getattr(provider, "ensure_secret_namespace", None)
                if not callable(ensure_namespace):
                    raise ProviderRejected(
                        "runtime provider does not support an early secret namespace"
                    )
                result = ensure_namespace(request["household_id"], job.intent_key)
                current = self.jobs.get(job.id)
                if current is None or current.status == "cancelled":
                    return self._cleanup_cancelled_namespace(job, result, provider)
                return self._finish_secret_namespace(job, result)
            provider_request = request
            namespace_ref = None
            if job.kind == "email_identity":
                namespace_ref = self._secret_namespace_ref(job.household_id)
                if namespace_ref is None:
                    self.jobs.retry_later(
                        job.id,
                        not_before=self.clock() + 5,
                        error_code="secret_namespace_not_ready",
                        now=self.clock(),
                    )
                    return self._durable_work_result(
                        job.id,
                        fallback_status="pending",
                        fallback_error="secret_namespace_not_ready",
                    )
                provider_request = (
                    {
                        "identity_id": request["email_identity_id"],
                        "household_id": job.household_id,
                        "option": request["option"],
                        "selection": request["selection"],
                        "secret_namespace_ref": namespace_ref,
                    }
                    if request.get("email_identity_id") and request.get("option")
                    else request
                )
            if job.operation == "inspect":
                inspect_request = (
                    {**request, "secret_namespace_ref": namespace_ref}
                    if job.kind == "email_identity"
                    else request
                )
                return self._inspect_job(
                    job, inspect_request, provider, namespace_ref=namespace_ref
                )
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
                    provider.prepare(provider_request, job.intent_key)
                    if split_runtime
                    else provider.ensure(provider_request, job.intent_key)
                )
            current = self.jobs.get(job.id)
            if current is None or current.status == "cancelled":
                return self._cleanup_cancelled_result(job, result, provider)
            if job.kind == "runtime":
                return self._finish_runtime(job, request, result, provider)
            if job.kind == "email_identity":
                assert namespace_ref is not None
                if not self._stage_email_secret(job, request, result, namespace_ref):
                    return self._mark_step_problem(
                        job, request, "outcome_unknown", "secret_handoff_unknown"
                    )
            else:
                self._reject_identity_secret(result)
            return self._finish_step(job, request, result)
        except _ProjectionCancelled:
            if result is None or provider is None:
                return WorkResult(job.id, "cancelled", "projection_cancelled")
            if job.operation == "ensure_secret_namespace":
                return self._cleanup_cancelled_namespace(job, result, provider)
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
            return self._durable_work_result(
                job.id,
                fallback_status="pending",
                fallback_error=error.code,
            )
        except ProviderWaiting as error:
            return self._handle_provider_waiting(job, request, error)
        except EmailProviderError as error:
            return self._handle_email_provider_error(job, request, error)
        except SecretFieldError:
            if job.kind == "email_identity":
                return self._mark_step_problem(
                    job, request, "outcome_unknown", "provider_result_invalid"
                )
            return self._mark_step_problem(
                job, request, "failed", "provider_rejected"
            )
        except ProviderRejected as error:
            return self._mark_step_problem(job, request, "failed", error.code)
        except (OutcomeUnknown, TimeoutError, ConnectionError):
            return self._mark_step_problem(
                job, request, "outcome_unknown", "outcome_unknown"
            )

    def _handle_email_provider_error(
        self,
        job: JobRecord,
        request: dict,
        error: EmailProviderError,
    ) -> WorkResult:
        if error.kind in {
            EmailFailureKind.SAFE_RETRY,
            EmailFailureKind.PROVIDER_DEGRADED,
        }:
            if job.attempts >= self.max_safe_attempts:
                return self._mark_step_problem(
                    job, request, "failed", "email_retry_exhausted"
                )
            self.jobs.retry_later(
                job.id,
                not_before=self.clock() + 30,
                error_code="email_retry_scheduled",
                now=self.clock(),
            )
            return self._durable_work_result(
                job.id,
                fallback_status="pending",
                fallback_error="email_retry_scheduled",
            )
        status, code = {
            EmailFailureKind.USER_ACTION: ("waiting_user", "email_user_action_required"),
            EmailFailureKind.DEFINITIVE_FAILURE: ("failed", "email_provider_rejected"),
            EmailFailureKind.AUTH_REVOKED: ("failed", "email_auth_revoked"),
            EmailFailureKind.OUTCOME_UNKNOWN: (
                "outcome_unknown",
                "email_outcome_unknown",
            ),
        }[error.kind]
        return self._mark_step_problem(job, request, status, code)

    def _handle_provider_waiting(
        self,
        job: JobRecord,
        request: dict[str, Any],
        error: ProviderWaiting,
    ) -> WorkResult:
        public_result = error.public_result
        external_ref = error.external_ref
        if job.kind == "email_identity":
            try:
                public_result, external_ref = self._validated_email_waiting_result(
                    job,
                    request,
                    public_result=public_result,
                    external_ref=external_ref,
                )
            except (SecretFieldError, TypeError, ValueError):
                return self._mark_step_problem(
                    job, request, "outcome_unknown", "provider_result_invalid"
                )
            if external_ref is not None:
                cleanup = self._schedule_cancelled_waiting_cleanup(
                    job, request, external_ref
                )
                if cleanup is not None:
                    return cleanup
        return self._mark_step_problem(
            job,
            request,
            "waiting_user",
            error.code,
            public_result=public_result,
            external_ref=external_ref,
        )

    def _schedule_cancelled_waiting_cleanup(
        self, job: JobRecord, request: dict[str, Any], external_ref: str
    ) -> WorkResult | None:
        current = self.jobs.db.query_one(
            "SELECT status, error_code FROM provisioning_jobs WHERE id = ?", (job.id,)
        )
        if (
            current is None
            or current["status"] != "outcome_unknown"
            or current["error_code"]
            not in {"cancel_requires_reconciliation", "reset_requires_reconciliation"}
        ):
            return None
        with self.jobs.db.write() as connection:
            resource_id = self._external_resource(
                connection,
                job,
                external_ref,
                status="deleting",
                revision=job.desired_revision,
            )
            self.jobs.create(
                connection,
                household_id=job.household_id,
                workflow_id=job.workflow_id,
                kind="cleanup",
                operation="deprovision",
                intent_key=f"{job.household_id}:late-waiting-cleanup:{job.id}",
                desired_revision=job.desired_revision,
                request={
                    "resource_id": resource_id,
                    "resource_type": "email_identity",
                    "external_ref": external_ref,
                    "email_identity_id": request.get("email_identity_id"),
                    "parent_job_id": job.id,
                },
                provider=job.provider,
                now=self.clock(),
            )
        return WorkResult(job.id, "outcome_unknown", current["error_code"])

    @staticmethod
    def _reject_identity_secret(result: ProvisionResult) -> None:
        if not result.secret_material.is_empty:
            result.secret_material.clear()
            raise ProviderRejected(
                "identity adapter returned a secret before runtime secret handoff existed"
            )

    def _secret_namespace_ref(
        self, household_id: str, *, include_deleting: bool = False
    ) -> str | None:
        status_clause = (
            "status IN ('ready','deleting','outcome_unknown')"
            if include_deleting
            else "status = 'ready'"
        )
        row = self.jobs.db.query_one(
            "SELECT * FROM external_resources WHERE household_id = ?"
            " AND resource_type = 'secret_namespace' AND "
            + status_clause
            + " ORDER BY updated_at DESC, id DESC LIMIT 1",
            (household_id,),
        )
        if row is None:
            return None
        value = self.jobs.decrypt_json(
            "external_resources",
            row["id"],
            "external_id",
            row["external_id_ciphertext"],
            row["encryption_key_version"],
        )
        return value if isinstance(value, str) and value else None

    def _secret_namespace_state(
        self, household_id: str, runtime_ref: str | None = None
    ) -> tuple[str, str | None] | None:
        rows = self.jobs.db.query(
            "SELECT * FROM external_resources WHERE household_id = ?"
            " AND resource_type = 'secret_namespace'"
            " ORDER BY updated_at DESC, id DESC",
            (household_id,),
        )
        if not rows or (runtime_ref is None and len(rows) != 1):
            return None
        for row in rows:
            value = self.jobs.decrypt_json(
                "external_resources",
                row["id"],
                "external_id",
                row["external_id_ciphertext"],
                row["encryption_key_version"],
            )
            value = value if isinstance(value, str) and value else None
            if runtime_ref is None or value == runtime_ref:
                return row["status"], value
        return None

    def _email_cleanup_pending(self, household_id: str) -> bool:
        disconnecting = self.jobs.db.query_one(
            "SELECT 1 FROM email_identities WHERE household_id = ?"
            " AND status = 'disconnecting' LIMIT 1",
            (household_id,),
        )
        if disconnecting is None:
            return False
        provider_cleanup = self.jobs.db.query_one(
            "SELECT 1 FROM external_resources WHERE household_id = ?"
            " AND resource_type = 'email_identity' AND status != 'deleted' LIMIT 1",
            (household_id,),
        )
        if provider_cleanup is not None:
            return True
        unresolved_provider_job = self.jobs.db.query_one(
            "SELECT 1 FROM provisioning_jobs WHERE household_id = ?"
            " AND kind = 'email_identity'"
            " AND status IN ('pending','running','waiting_user','outcome_unknown') LIMIT 1",
            (household_id,),
        )
        if unresolved_provider_job is not None:
            return True
        secret_cleanup = self.jobs.db.query_one(
            "SELECT 1 FROM provisioning_jobs WHERE household_id = ?"
            " AND kind = 'bootstrap_cleanup' AND operation = 'delete_email_secret'"
            " AND status != 'succeeded' LIMIT 1",
            (household_id,),
        )
        return secret_cleanup is not None

    def _defer_runtime_cleanup(
        self, job: JobRecord, request: dict[str, Any]
    ) -> WorkResult | None:
        if request.get("resource_type") not in {"runtime", "secret_namespace"}:
            return None
        if not self._email_cleanup_pending(job.household_id):
            return None
        self.jobs.retry_later(
            job.id,
            not_before=self.clock() + 5,
            error_code="email_cleanup_pending",
            now=self.clock(),
        )
        return self._durable_work_result(
            job.id,
            fallback_status="pending",
            fallback_error="email_cleanup_pending",
        )

    def _settle_cancelled_parent(
        self, connection, parent_job_id: Any
    ) -> None:
        if not isinstance(parent_job_id, str):
            return
        parent = connection.execute(
            "SELECT status FROM provisioning_jobs WHERE id = ?", (parent_job_id,)
        ).fetchone()
        if parent is not None and parent["status"] in {
            "running",
            "outcome_unknown",
            "cancelled",
        }:
            self.jobs.settle(
                connection,
                parent_job_id,
                status="cancelled",
                error_code="cancelled_and_compensated",
                now=self.clock(),
            )

    def _stage_email_secret(
        self,
        job: JobRecord,
        request: dict,
        result: ProvisionResult,
        namespace_ref: str,
    ) -> bool:
        try:
            public_result = self._validated_email_public_result(job, request, result)
        except ProviderRejected as error:
            result.secret_material.clear()
            raise OutcomeUnknown("email provider result is not safely attributable") from error
        binding_ref = public_result.get("secret_binding_ref")
        if result.secret_material.is_empty:
            if not isinstance(binding_ref, str) or not binding_ref:
                return True
            provider = self.providers.get(job.provider)
            verifier = getattr(provider, "pre_staged_secret_verified", None)
            return bool(
                callable(verifier)
                and verifier(request, namespace_ref, binding_ref)
            )
        material_names = [name for name, _value in result.secret_material.items()]
        if not isinstance(binding_ref, str) or material_names != [binding_ref]:
            result.secret_material.clear()
            raise OutcomeUnknown(
                "email secret material does not match its public binding"
            )
        try:
            self.secret_sink.install(namespace_ref, result.secret_material)
        except Exception:
            result.secret_material.clear()
            return False
        return True

    def _validated_email_public_result(
        self,
        job: JobRecord,
        request: dict[str, Any],
        result: ProvisionResult,
    ) -> dict[str, Any]:
        try:
            public_result = EmailPublicBinding.model_validate(
                result.public_result
            ).model_dump(mode="json", exclude_none=True)
        except (TypeError, ValueError) as error:
            raise ProviderRejected(
                "email provider returned an invalid public binding"
            ) from error
        expected_provider = self._expected_email_public_provider(job)
        if public_result["provider"] != expected_provider:
            raise ProviderRejected(
                "email provider identity does not match the configured adapter"
            )
        identity_id = request.get("email_identity_id")
        if self.email_identities is not None and isinstance(identity_id, str):
            identity = self.email_identities.get(identity_id)
            if identity is None:
                raise ProviderRejected("email provider returned an unknown identity")
            if identity.address is not None and normalize_email(
                identity.address
            ) != normalize_email(public_result["agent_inbox"]):
                raise ProviderRejected(
                    "email provider address does not match the selected mailbox"
                )
        self._validate_email_external_ref(job, request, result.external_ref, public_result)
        return public_result

    def _expected_email_public_provider(self, job: JobRecord) -> str:
        provider = self.providers.get(job.provider)
        expected = getattr(provider, "email_public_provider", None)
        if expected not in {"synthetic", "nerve", "gmail"}:
            raise ProviderRejected(
                "email adapter does not declare its public provider identity"
            )
        return expected

    @staticmethod
    def _canonical_uuid(value: Any) -> bool:
        if not isinstance(value, str):
            return False
        try:
            return str(UUID(value)) == value
        except ValueError:
            return False

    def _validate_email_external_ref(
        self,
        job: JobRecord,
        request: dict[str, Any],
        external_ref: str,
        public_result: dict[str, Any],
    ) -> None:
        provider = public_result["provider"]
        identity_id = request.get("email_identity_id")
        provider_refs = public_result.get("provider_refs", {})
        if provider == "synthetic":
            if not isinstance(identity_id, str) or external_ref != (
                f"synthetic-email:{identity_id}"
            ):
                raise ProviderRejected("synthetic email reference does not match intent")
            if provider_refs and provider_refs.get("identity_id") != identity_id:
                raise ProviderRejected("synthetic email identity reference drifted")
            return
        if provider == "gmail":
            if request.get("option") != EmailOption.GMAIL.value:
                raise ProviderRejected("Gmail provider does not match the selected option")
            if not isinstance(identity_id, str):
                raise ProviderRejected("Gmail identity is missing")
            refs = public_result.get("provider_refs", {})
            expected_ref = f"google-oauth:{identity_id}"
            if external_ref != expected_ref or refs.get("google_subject") != (
                public_result.get("provider_subject")
            ):
                raise ProviderRejected("Gmail resource reference does not match intent")
            return
        try:
            reference = json.loads(external_ref)
        except (TypeError, ValueError) as error:
            raise ProviderRejected("Nerve returned an invalid resource reference") from error
        if external_ref != json.dumps(
            reference, sort_keys=True, separators=(",", ":")
        ):
            raise ProviderRejected("Nerve returned a non-canonical resource reference")
        option = request.get("option")
        if option == EmailOption.MANAGED_ABROLIA.value:
            resource_key = "grant_id"
            exact_keys = {
                "household_id",
                "stable_ref",
                "org_id",
                resource_key,
                "inbox_id",
                "key_id",
                "webhook_id",
                "address",
                "org_external_ref",
            }
        elif option == EmailOption.OWN_DOMAIN.value:
            resource_key = "domain_id"
            exact_keys = {
                "household_id",
                "stable_ref",
                "org_id",
                resource_key,
                "domain",
                "inbox_id",
                "key_id",
                "webhook_id",
                "address",
                "org_external_ref",
            }
        else:
            raise ProviderRejected("Nerve provider does not match the selected option")
        if not isinstance(reference, dict) or set(reference) != exact_keys:
            raise ProviderRejected("Nerve returned an unexpected resource reference")
        expected = {
            "household_id": job.household_id,
            "stable_ref": request.get("stable_ref", job.intent_key),
            "org_id": provider_refs.get("org_id"),
            resource_key: provider_refs.get(resource_key),
            "inbox_id": provider_refs.get("inbox_id"),
            "key_id": provider_refs.get("key_id"),
            "webhook_id": provider_refs.get("webhook_id"),
            "address": public_result["agent_inbox"],
            "org_external_ref": email_org_external_ref(
                job.household_id, str(request.get("email_identity_id", ""))
            ),
        }
        if option == EmailOption.OWN_DOMAIN.value:
            expected["domain"] = canonicalize_domain(
                str(request.get("selection", {}).get("domain", "")),
                allow_test=True,
            )
        if reference != expected or not all(
            self._canonical_uuid(reference[key])
            for key in ("org_id", resource_key, "inbox_id", "key_id", "webhook_id")
        ):
            raise ProviderRejected("Nerve resource reference does not match intent")

    def _validated_email_waiting_result(
        self,
        job: JobRecord,
        request: dict[str, Any],
        *,
        public_result: dict[str, Any],
        external_ref: str | None,
    ) -> tuple[dict[str, Any], str | None]:
        expected_provider = self._expected_email_public_provider(job)
        if not public_result:
            if expected_provider != "synthetic" or external_ref is not None:
                raise ValueError("untyped email waiting reference")
            return {}, None
        if expected_provider == "gmail":
            typed = EmailGoogleOAuthPublicStatus.model_validate(public_result)
            expected_ref = f"google-oauth:{request.get('email_identity_id', '')}"
            if external_ref != expected_ref:
                raise ValueError("Gmail waiting state has no durable identity reference")
            return typed.model_dump(mode="json", exclude_none=True), external_ref
        if expected_provider != "nerve":
            raise ValueError("waiting state does not match the configured adapter")
        option = request.get("option")
        if option == EmailOption.MANAGED_ABROLIA.value:
            typed_readiness = EmailNerveAttachmentPublicStatus.model_validate(
                public_result
            )
            if external_ref is None:
                raise ValueError("managed Nerve waiting state has no durable reference")
            try:
                reference = json.loads(external_ref)
            except (TypeError, ValueError) as error:
                raise ValueError("invalid pending managed Nerve reference") from error
            if external_ref != json.dumps(
                reference, sort_keys=True, separators=(",", ":")
            ):
                raise ValueError("non-canonical pending managed Nerve reference")
            exact_keys = {
                "household_id",
                "stable_ref",
                "org_id",
                "grant_id",
                "inbox_id",
                "key_id",
                "webhook_id",
                "address",
                "org_external_ref",
            }
            expected_address = normalize_email(
                f"{request.get('selection', {}).get('local_part', '')}@abrolia.com"
            )
            if (
                not isinstance(reference, dict)
                or set(reference) != exact_keys
                or reference["household_id"] != job.household_id
                or reference["stable_ref"] != request.get("stable_ref", job.intent_key)
                or reference["org_id"] != typed_readiness.nerve_org_id
                or reference["org_external_ref"] != email_org_external_ref(
                    job.household_id, str(request.get("email_identity_id", ""))
                )
                or normalize_email(str(reference["address"])) != expected_address
                or not all(
                    self._canonical_uuid(reference[key])
                    for key in (
                        "org_id",
                        "grant_id",
                        "inbox_id",
                        "key_id",
                        "webhook_id",
                    )
                )
            ):
                raise ValueError("pending managed Nerve reference does not match intent")
            return (
                typed_readiness.model_dump(mode="json", exclude_none=True),
                external_ref,
            )
        if option != EmailOption.OWN_DOMAIN.value:
            raise ValueError("DNS waiting state does not match the selected option")
        typed = EmailDnsPublicStatus.model_validate(public_result)
        expected_domain = canonicalize_domain(
            str(request.get("selection", {}).get("domain", "")), allow_test=True
        )
        if canonicalize_domain(typed.domain, allow_test=True) != expected_domain:
            raise ValueError("DNS waiting state does not match the selected domain")
        normalized = typed.model_dump(mode="json", exclude_none=True)
        if external_ref is not None:
            try:
                reference = json.loads(external_ref)
            except (TypeError, ValueError) as error:
                raise ValueError("invalid pending Nerve reference") from error
            if external_ref != json.dumps(
                reference, sort_keys=True, separators=(",", ":")
            ):
                raise ValueError("non-canonical pending Nerve reference")
            exact_keys = {
                "household_id",
                "stable_ref",
                "org_id",
                "domain_id",
                "domain",
                "inbox_id",
                "key_id",
                "webhook_id",
                "address",
                "org_external_ref",
            }
            if not isinstance(reference, dict) or set(reference) != exact_keys:
                raise ValueError("unexpected pending Nerve reference")
            if not isinstance(reference["address"], str):
                raise ValueError("pending Nerve address is invalid")
            expected_address = normalize_email(
                f"{request.get('selection', {}).get('local_part', '')}@{expected_domain}"
            )
            if (
                reference["household_id"] != job.household_id
                or reference["stable_ref"] != request.get("stable_ref", job.intent_key)
                or reference["domain"] != expected_domain
                or reference["org_external_ref"] != email_org_external_ref(
                    job.household_id, str(request.get("email_identity_id", ""))
                )
                or normalize_email(reference["address"]) != expected_address
                or not self._canonical_uuid(reference["org_id"])
                or not self._canonical_uuid(reference["domain_id"])
                or any(reference[key] for key in ("inbox_id", "key_id", "webhook_id"))
            ):
                raise ValueError("pending Nerve reference does not match intent")
        return normalized, external_ref

    def _inspect_job(
        self,
        job: JobRecord,
        request: dict,
        provider,
        *,
        namespace_ref: str | None = None,
    ) -> WorkResult:
        inspect_intent = getattr(provider, "inspect_intent", None)
        inspected = (
            inspect_intent(request, request["stable_ref"])
            if callable(inspect_intent)
            else provider.inspect(request["stable_ref"])
        )
        if inspected.state is InspectState.READY and inspected.result is not None:
            if job.kind == "email_identity":
                if namespace_ref is None or not self._stage_email_secret(
                    job, request, inspected.result, namespace_ref
                ):
                    return self._mark_step_problem(
                        job, request, "outcome_unknown", "secret_handoff_unknown"
                    )
            else:
                self._reject_identity_secret(inspected.result)
            return self._finish_step(job, request, inspected.result)
        if inspected.state is InspectState.PENDING:
            public_result = inspected.public_result
            waiting_external_ref = None
            if job.kind == "email_identity":
                try:
                    waiting_external_ref = self.jobs.external_ref(job.id)
                    public_result, waiting_external_ref = (
                        self._validated_email_waiting_result(
                        job,
                        request,
                        public_result=public_result,
                        external_ref=waiting_external_ref,
                        )
                    )
                except (SecretFieldError, TypeError, ValueError) as error:
                    raise OutcomeUnknown(
                        "email provider returned an invalid waiting result"
                    ) from error
            return self._mark_step_problem(
                job,
                request,
                "waiting_user",
                "waiting_user",
                public_result=public_result,
                external_ref=waiting_external_ref,
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
        original_request = self.jobs.request(job.id)
        if job.kind == "email_identity":
            try:
                self._validated_email_public_result(job, original_request, result)
            except ProviderRejected:
                return self._mark_step_problem(
                    job,
                    original_request,
                    "outcome_unknown",
                    "cancelled_provider_result_invalid",
                )
        email_identity_id = (
            original_request.get("email_identity_id")
            if job.kind == "email_identity"
            else None
        )
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
        elif job.kind == "email_identity":
            binding_ref = result.public_result.get("secret_binding_ref")
            namespace_ref = self._secret_namespace_ref(
                job.household_id, include_deleting=True
            )
            if isinstance(binding_ref, str) and binding_ref:
                if namespace_ref is None:
                    secret_cleanup_unknown = True
                else:
                    try:
                        self.secret_sink.delete(namespace_ref, binding_ref)
                    except Exception:
                        secret_cleanup_unknown = True
                if secret_cleanup_unknown and current is not None:
                    with self.jobs.db.write() as connection:
                        self.jobs.create(
                            connection,
                            household_id=job.household_id,
                            workflow_id=job.workflow_id,
                            kind="bootstrap_cleanup",
                            operation="delete_email_secret",
                            intent_key=(
                                f"{job.household_id}:late-email-secret-cleanup:"
                                f"{job.id}"
                            ),
                            request={
                                "runtime_ref": namespace_ref,
                                "name": binding_ref,
                                "parent_job_id": job.id,
                                "resource_id": resource_id,
                                "email_identity_id": email_identity_id,
                                "cleanup_authorization": (
                                    "email_identity_cancelled"
                                ),
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
                    if (
                        job.kind == "email_identity"
                        and self.email_identities is not None
                        and isinstance(email_identity_id, str)
                    ):
                        self.email_identities.finish_disconnect(
                            connection, email_identity_id, now=self.clock()
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
                            "email_identity_id": email_identity_id,
                            "parent_job_id": job.id,
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
        try:
            return self._reconcile(job_id)
        except EmailProviderError as error:
            job = self.jobs.get(job_id)
            if job is None:
                raise KeyError(job_id) from None
            return self._handle_email_provider_error(
                job, self.jobs.request(job.id), error
            )
        except SecretFieldError:
            job = self.jobs.get(job_id)
            if job is None:
                raise KeyError(job_id) from None
            if job.kind == "email_identity":
                return self._mark_step_problem(
                    job,
                    self.jobs.request(job.id),
                    "outcome_unknown",
                    "provider_result_invalid",
                )
            return self._mark_step_problem(
                job,
                self.jobs.request(job.id),
                "failed",
                "provider_rejected",
            )
        except ProviderRejected as error:
            job = self.jobs.get(job_id)
            if job is None:
                raise KeyError(job_id) from None
            return self._mark_step_problem(
                job,
                self.jobs.request(job.id),
                "failed",
                error.code,
            )
        except (OutcomeUnknown, TimeoutError, ConnectionError):
            job = self.jobs.get(job_id)
            if job is None:
                raise KeyError(job_id) from None
            return self._mark_step_problem(
                job,
                self.jobs.request(job.id),
                "outcome_unknown",
                "reconcile_inconclusive",
            )

    def _reconcile(self, job_id: str) -> WorkResult:
        job = self.jobs.get(job_id)
        if job is None or job.status != "outcome_unknown":
            raise ValueError("only an outcome_unknown job can be reconciled")
        request = self.jobs.request(job.id)
        if job.kind == "bootstrap_cleanup":
            return self._cleanup_bootstrap(job, request)
        provider = self.providers.get(job.provider)
        if job.kind == "cleanup":
            deferred = self._defer_runtime_cleanup(job, request)
            if deferred is not None:
                return deferred
            external_ref = request.get("external_ref")
            if not external_ref:
                return self._mark_step_problem(
                    job, request, "failed", "missing_external_ref"
                )
            if request.get("resource_type") == "runtime":
                # Inspecting the shared app cannot distinguish an absent
                # workload from its intentionally retained secret namespace.
                # Re-run the exact idempotent workload cleanup instead.
                return self._cleanup(job, request, provider)
            inspected = provider.inspect(external_ref)
            if inspected.state is InspectState.ABSENT:
                email_identity_id = request.get("email_identity_id")
                if (
                    request.get("resource_type") == "email_identity"
                    and not self._delete_email_cleanup_secret(
                        job, request, email_identity_id
                    )
                ):
                    return WorkResult(
                        job.id, "outcome_unknown", "secret_cleanup_unknown"
                    )
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
                    if (
                        request.get("resource_type") == "email_identity"
                        and self.email_identities is not None
                        and isinstance(email_identity_id, str)
                    ):
                        self._settle_cancelled_parent(
                            connection, request.get("parent_job_id")
                        )
                        self.email_identities.finish_disconnect(
                            connection, email_identity_id, now=self.clock()
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
            if job.operation == "ensure_secret_namespace":
                ensure_namespace = getattr(provider, "ensure_secret_namespace", None)
                if not callable(ensure_namespace):
                    return self._mark_step_problem(
                        job, request, "failed", "provider_rejected"
                    )
                try:
                    result = ensure_namespace(request["household_id"], job.intent_key)
                    current = self.jobs.get(job.id)
                    if current is None or current.status == "cancelled":
                        return self._cleanup_cancelled_namespace(job, result, provider)
                    return self._finish_secret_namespace(job, result)
                except _ProjectionCancelled:
                    return self._cleanup_cancelled_namespace(job, result, provider)
                except ProviderRejected as error:
                    return self._mark_step_problem(job, request, "failed", error.code)
                except (OutcomeUnknown, TimeoutError, ConnectionError):
                    return self._mark_step_problem(
                        job, request, "outcome_unknown", "reconcile_inconclusive"
                    )
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
        reconcile_email = getattr(provider, "reconcile", None)
        if job.kind == "email_identity" and callable(reconcile_email):
            namespace_ref = self._secret_namespace_ref(job.household_id)
            if namespace_ref is None:
                return WorkResult(
                    job.id, "outcome_unknown", "secret_namespace_not_ready"
                )
            provider_request = {
                "identity_id": request["email_identity_id"],
                "household_id": job.household_id,
                "option": request["option"],
                "selection": request["selection"],
                "secret_namespace_ref": namespace_ref,
            }
            try:
                result = reconcile_email(
                    provider_request,
                    request.get("stable_ref", job.intent_key),
                )
                if (
                    job.error_code == "secret_handoff_unknown"
                    and result.secret_material.is_empty
                ):
                    return WorkResult(
                        job.id, "outcome_unknown", "secret_handoff_unknown"
                    )
                if not self._stage_email_secret(
                    job, request, result, namespace_ref
                ):
                    return self._mark_step_problem(
                        job, request, "outcome_unknown", "secret_handoff_unknown"
                    )
                return self._finish_step(job, request, result)
            except _ProjectionCancelled:
                return self._cleanup_cancelled_result(job, result, provider)
            except ProviderRateLimited as error:
                self.jobs.retry_later(
                    job.id,
                    not_before=self.clock() + error.retry_after,
                    error_code=error.code,
                    now=self.clock(),
                )
                return self._durable_work_result(
                    job.id,
                    fallback_status="pending",
                    fallback_error=error.code,
                )
            except ProviderWaiting as error:
                return self._handle_provider_waiting(job, request, error)
            except ProviderRejected as error:
                return self._mark_step_problem(job, request, "failed", error.code)
            except (OutcomeUnknown, TimeoutError, ConnectionError):
                return WorkResult(job.id, "outcome_unknown", "reconcile_inconclusive")
        inspected = provider.inspect(request.get("stable_ref", job.intent_key))
        if inspected.state is InspectState.READY and inspected.result:
            try:
                if job.kind == "email_identity":
                    if (
                        job.error_code == "secret_handoff_unknown"
                        and inspected.result.secret_material.is_empty
                    ):
                        return WorkResult(
                            job.id,
                            "outcome_unknown",
                            "secret_handoff_unknown",
                        )
                    namespace_ref = self._secret_namespace_ref(job.household_id)
                    if namespace_ref is None or not self._stage_email_secret(
                        job, request, inspected.result, namespace_ref
                    ):
                        return self._mark_step_problem(
                            job,
                            request,
                            "outcome_unknown",
                            "secret_handoff_unknown",
                        )
                else:
                    self._reject_identity_secret(inspected.result)
                return self._finish_step(job, request, inspected.result)
            except _ProjectionCancelled:
                return self._cleanup_cancelled_result(
                    job, inspected.result, self.providers.get(job.provider)
                )
        if inspected.state is InspectState.FAILED:
            return self._mark_step_problem(
                job,
                request,
                "failed",
                inspected.error_code or "provider_rejected",
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
        *,
        public_result: dict[str, Any] | None = None,
        external_ref: str | None = None,
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
            if external_ref and job_status == "waiting_user":
                self._external_resource(
                    connection,
                    job,
                    external_ref,
                    status="creating",
                )
            if (
                job.kind == "email_identity"
                and self.email_identities is not None
                and request.get("email_identity_id")
            ):
                problem_status = {
                    "waiting_user": EmailIdentityStatus.WAITING_USER,
                    "outcome_unknown": EmailIdentityStatus.OUTCOME_UNKNOWN,
                    "failed": EmailIdentityStatus.NEEDS_ATTENTION,
                }[job_status]
                self.email_identities.mark_problem(
                    connection,
                    request["email_identity_id"],
                    status=problem_status,
                    now=now,
                )
                self.email_identities.finish_disconnect(
                    connection, request["email_identity_id"], now=now
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
                            }.get(step_status, "needs_attention"),
                            **(public_result or {}),
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
        resource_type: str | None = None,
        stable_name: str | None = None,
    ) -> str:
        resource_type = resource_type or job.kind
        stable_name = stable_name or job.intent_key
        existing = connection.execute(
            "SELECT * FROM external_resources WHERE provider = ? AND resource_type = ?"
            " AND stable_name = ?",
            (job.provider, resource_type, stable_name),
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
                    resource_type,
                    stable_name,
                    encrypted.ciphertext,
                    encrypted.key_version,
                    status,
                    revision,
                    now,
                    now,
                ),
            )
        return resource_id

    def _finish_secret_namespace(
        self, job: JobRecord, result: ProvisionResult
    ) -> WorkResult:
        if not result.secret_material.is_empty:
            result.secret_material.clear()
            raise ProviderRejected(
                "secret namespace creation returned unexpected secret material"
            )
        durable_result = {
            "external_ref": result.external_ref,
            "public_result": result.public_result,
            "verified": True,
        }
        with self.jobs.db.write() as connection:
            current = connection.execute(
                "SELECT j.status, w.state AS workflow_state, h.status AS household_status"
                " FROM provisioning_jobs j"
                " JOIN onboarding_workflows w ON w.id = j.workflow_id"
                " JOIN households h ON h.id = j.household_id WHERE j.id = ?",
                (job.id,),
            ).fetchone()
            if current is None:
                return WorkResult(job.id, "cancelled", "job_missing")
            if current["status"] == "succeeded":
                return WorkResult(job.id, "succeeded")
            if current["status"] == "cancelled":
                raise _ProjectionCancelled
            if (
                current["workflow_state"] == "cancelled"
                or current["household_status"] in {"draft", "deleting", "deleted"}
            ):
                raise _ProjectionCancelled
            if current["status"] not in {"running", "outcome_unknown"}:
                return WorkResult(job.id, current["status"], current["status"])
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
                result.external_ref,
                status="ready",
                resource_type="secret_namespace",
            )
        return WorkResult(job.id, "succeeded")

    def _cleanup_cancelled_namespace(
        self, job: JobRecord, result: ProvisionResult, provider
    ) -> WorkResult:
        result.secret_material.clear()
        with self.jobs.db.write() as connection:
            current = connection.execute(
                "SELECT status FROM provisioning_jobs WHERE id = ?", (job.id,)
            ).fetchone()
            resource_id = self._external_resource(
                connection,
                job,
                result.external_ref,
                status="deleting",
                resource_type="secret_namespace",
            )
        try:
            inspected = provider.deprovision(result.external_ref)
        except Exception:
            inspected = None
        if inspected is not None and inspected.state is InspectState.ABSENT:
            with self.jobs.db.write() as connection:
                connection.execute(
                    "UPDATE external_resources SET status = 'deleted', updated_at = ?"
                    " WHERE id = ?",
                    (self.clock(), resource_id),
                )
                if current is not None and current["status"] in {
                    "running",
                    "outcome_unknown",
                }:
                    self.jobs.settle(
                        connection,
                        job.id,
                        status="cancelled",
                        error_code="cancelled_and_compensated",
                        now=self.clock(),
                    )
            return WorkResult(job.id, "cancelled")
        with self.jobs.db.write() as connection:
            connection.execute(
                "UPDATE external_resources SET status = 'outcome_unknown', updated_at = ?"
                " WHERE id = ?",
                (self.clock(), resource_id),
            )
            if current is not None and current["status"] in {
                "running",
                "outcome_unknown",
            }:
                self.jobs.settle(
                    connection,
                    job.id,
                    status="outcome_unknown",
                    error_code="cancelled_cleanup_unknown",
                    now=self.clock(),
                )
            self.jobs.create(
                connection,
                household_id=job.household_id,
                workflow_id=job.workflow_id,
                kind="cleanup",
                operation="deprovision",
                intent_key=f"{job.household_id}:late-namespace-cleanup:{job.id}",
                request={
                    "resource_id": resource_id,
                    "resource_type": "secret_namespace",
                    "external_ref": result.external_ref,
                },
                provider=job.provider,
                now=self.clock(),
            )
        return WorkResult(job.id, "outcome_unknown", "cancelled_cleanup_unknown")

    def _finish_step(
        self, job: JobRecord, request: dict, result: ProvisionResult
    ) -> WorkResult:
        now = self.clock()
        step_kind = StepKind(request["step_kind"])
        public_result = result.public_result
        if step_kind is StepKind.EMAIL:
            public_result = self._validated_email_public_result(job, request, result)
        durable_result = {
            "external_ref": result.external_ref,
            "public_result": public_result,
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
            if (
                step_kind is StepKind.EMAIL
                and self.email_identities is not None
                and request.get("email_identity_id")
            ):
                address = public_result.get("agent_inbox")
                if not isinstance(address, str) or not address:
                    raise ProviderRejected("email provider returned no verified address")
                raw_refs = public_result.get("provider_refs")
                provider_refs = (
                    {str(key): str(value) for key, value in raw_refs.items()}
                    if isinstance(raw_refs, dict)
                    else {"external_ref": result.external_ref}
                )
                raw_scopes = public_result.get("granted_scopes", [])
                scopes = (
                    [str(scope) for scope in raw_scopes]
                    if isinstance(raw_scopes, (list, tuple))
                    else []
                )
                try:
                    self.email_identities.mark_verified(
                        connection,
                        request["email_identity_id"],
                        address=address,
                        provider_subject=(
                            str(public_result["provider_subject"])
                            if public_result.get("provider_subject")
                            else None
                        ),
                        provider_refs=provider_refs,
                        secret_binding_ref=(
                            str(public_result["secret_binding_ref"])
                            if public_result.get("secret_binding_ref")
                            else None
                        ),
                        granted_scopes=scopes,
                        now=now,
                    )
                except ValueError as error:
                    raise ProviderRejected(
                        "email provider returned a mismatched identity"
                    ) from error
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
                        **public_result,
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
        deferred = self._defer_runtime_cleanup(job, request)
        if deferred is not None:
            return deferred
        external_ref = request.get("external_ref") or request.get("result", {}).get(
            "external_ref"
        )
        if not external_ref:
            self._mark_step_problem(job, request, "failed", "missing_external_ref")
            return WorkResult(job.id, "failed", "missing_external_ref")
        if request.get("resource_type") == "runtime":
            # A runtime reset tears down the Machine and volume but must retain
            # the dedicated app: provider credentials live in that app's
            # secret namespace and upstream verified steps may still own them.
            deprovision_runtime = getattr(provider, "deprovision_runtime", None)
            if not callable(deprovision_runtime):
                self._mark_step_problem(
                    job,
                    request,
                    "outcome_unknown",
                    "runtime_cleanup_unsupported",
                )
                return WorkResult(
                    job.id, "outcome_unknown", "runtime_cleanup_unsupported"
                )
            deprovision = deprovision_runtime
        else:
            deprovision = provider.deprovision
        inspected = deprovision(external_ref)
        if inspected.state is InspectState.FAILED:
            self._mark_step_problem(job, request, "failed", "cleanup_rejected")
            return WorkResult(job.id, "failed", "cleanup_rejected")
        if inspected.state is not InspectState.ABSENT:
            self._mark_step_problem(job, request, "outcome_unknown", "cleanup_unknown")
            return WorkResult(job.id, "outcome_unknown", "cleanup_unknown")
        email_identity_id = request.get("email_identity_id")
        if (
            request.get("resource_type") == "email_identity"
            and not self._delete_email_cleanup_secret(
                job, request, email_identity_id
            )
        ):
            self._mark_step_problem(
                job, request, "outcome_unknown", "secret_cleanup_unknown"
            )
            return WorkResult(job.id, "outcome_unknown", "secret_cleanup_unknown")
        with self.jobs.db.write() as connection:
            self.jobs.settle(connection, job.id, status="succeeded", now=self.clock())
            if request.get("resource_id"):
                connection.execute(
                    "UPDATE external_resources SET status = 'deleted', updated_at = ?"
                    " WHERE id = ?",
                    (self.clock(), request["resource_id"]),
                )
            if (
                request.get("resource_type") == "email_identity"
                and self.email_identities is not None
                and isinstance(email_identity_id, str)
            ):
                self._settle_cancelled_parent(
                    connection, request.get("parent_job_id")
                )
                self.email_identities.finish_disconnect(
                    connection, email_identity_id, now=self.clock()
                )
        return WorkResult(job.id, "succeeded")

    def _delete_email_cleanup_secret(
        self, job: JobRecord, request: dict[str, Any], identity_id: Any
    ) -> bool:
        if isinstance(identity_id, str):
            return self._delete_email_binding_secret(identity_id)
        binding_ref = request.get("legacy_secret_binding_ref")
        if (
            job.provider != "fake-email"
            or binding_ref != SYNTHETIC_EMAIL_SECRET_BINDING
        ):
            return False
        return self._delete_email_binding_secret_for_household(
            job.household_id, binding_ref
        )

    def _delete_email_binding_secret_for_household(
        self, household_id: str, binding_ref: str
    ) -> bool:
        namespace_ref = self._secret_namespace_ref(
            household_id, include_deleting=True
        )
        if namespace_ref is None:
            namespace_state = self._secret_namespace_state(household_id)
            return namespace_state is not None and namespace_state[0] == "deleted"
        try:
            self.secret_sink.delete(namespace_ref, binding_ref)
        except Exception:
            return False
        return True

    def _delete_email_binding_secret(self, identity_id: str) -> bool:
        if self.email_identities is None:
            return True
        identity_row = self.jobs.db.query_one(
            "SELECT household_id, secret_binding_ref FROM email_identities WHERE id = ?"
            " AND status = 'disconnecting'",
            (identity_id,),
        )
        if identity_row is None:
            return False
        if not identity_row["secret_binding_ref"]:
            return True
        return self._delete_email_binding_secret_for_household(
            identity_row["household_id"], identity_row["secret_binding_ref"]
        )

    def _cleanup_bootstrap(self, job: JobRecord, request: dict) -> WorkResult:
        if request.get("cleanup_authorization") not in {
            "email_identity_cancelled",
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
        runtime_ref = request.get("runtime_ref")
        namespace_state = None
        if (
            request.get("cleanup_authorization") == "email_identity_cancelled"
        ):
            namespace_state = self._secret_namespace_state(
                job.household_id,
                runtime_ref if isinstance(runtime_ref, str) else None,
            )
            if not runtime_ref and namespace_state is not None:
                runtime_ref = namespace_state[1]
        namespace_absent = (
            namespace_state is not None and namespace_state[0] == "deleted"
        )
        if (not isinstance(runtime_ref, str) or not runtime_ref) and not namespace_absent:
            with self.jobs.db.write() as connection:
                self.jobs.settle(
                    connection,
                    job.id,
                    status="outcome_unknown",
                    error_code="bootstrap_cleanup_unknown",
                    now=self.clock(),
                )
            return WorkResult(job.id, "outcome_unknown", "bootstrap_cleanup_unknown")
        if not namespace_absent:
            try:
                self.secret_sink.delete(runtime_ref, request["name"])
            except Exception:
                current_namespace = self._secret_namespace_state(
                    job.household_id,
                    runtime_ref if isinstance(runtime_ref, str) else None,
                )
                if not (
                    request.get("cleanup_authorization")
                    == "email_identity_cancelled"
                    and current_namespace is not None
                    and current_namespace[0] == "deleted"
                ):
                    with self.jobs.db.write() as connection:
                        self.jobs.settle(
                            connection,
                            job.id,
                            status="outcome_unknown",
                            error_code="bootstrap_cleanup_unknown",
                            now=self.clock(),
                        )
                    return WorkResult(
                        job.id, "outcome_unknown", "bootstrap_cleanup_unknown"
                    )
        with self.jobs.db.write() as connection:
            self.jobs.settle(connection, job.id, status="succeeded", now=self.clock())
            if request.get("cleanup_authorization") == "email_identity_cancelled":
                resource = connection.execute(
                    "SELECT status FROM external_resources WHERE id = ?",
                    (request.get("resource_id"),),
                ).fetchone()
                if (
                    resource is not None
                    and resource["status"] == "deleted"
                    and self.email_identities is not None
                ):
                    identity_id = request.get("email_identity_id")
                    if isinstance(identity_id, str):
                        self._settle_cancelled_parent(
                            connection, request.get("parent_job_id")
                        )
                        self.email_identities.finish_disconnect(
                            connection, identity_id, now=self.clock()
                        )
        return WorkResult(job.id, "succeeded")

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from control_plane.models import StepKind, StepStatus
from control_plane.provisioning.contracts import (
    InspectResult,
    InspectState,
    ProviderRegistry,
    ProviderRejected,
    ProviderWaiting,
    ProvisionResult,
)
from control_plane.provisioning.fakes import (
    DeterministicFakeProvisioner,
    DryRunRuntimeProvisioner,
)
from control_plane.provisioning.secrets import InMemorySecretSink

BASE_TIME = 1_800_000_000.0
EMAIL_SELECTION = {"kind": "abrolia_managed", "local_part": "family-agent"}
WHATSAPP_SELECTION = {
    "kind": "shared_abrolia",
    "member_phone_test_ref": "synthetic-phone:worker-owner",
    "privacy_notice_receipt_id": "synthetic-worker-consent",
}
CHANNEL_SELECTION = {
    "kind": "telegram",
    "actor_id": "synthetic-worker-owner",
    "chat_id": "synthetic-worker-chat",
}


def _create_job(cp_stack, *, intent_key: str = "lease-contract") -> str:
    workflow = cp_stack.onboarding.workflow_for_household(cp_stack.household.id)
    with cp_stack.database.write() as connection:
        job_id, created = cp_stack.jobs.create(
            connection,
            household_id=cp_stack.household.id,
            workflow_id=workflow.id,
            kind="email_identity",
            operation="ensure",
            intent_key=intent_key,
            request={"step_kind": "email_identity", "selection": EMAIL_SELECTION},
            provider="fake-email",
            now=BASE_TIME,
        )
    assert created
    return job_id


def _selected_email_with_provider(cp_stack, behavior: str):
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )
    fake = DeterministicFakeProvisioner("email", behavior=behavior)
    registry = ProviderRegistry()
    registry.register("fake-email", fake)
    return fake, cp_stack.make_worker(providers=registry, now=BASE_TIME + 3)


def _advance_to_runtime(cp_stack) -> None:
    cp_stack.complete_profile()
    worker = cp_stack.make_worker(now=BASE_TIME + 20)
    for kind, selection in (
        (StepKind.EMAIL, EMAIL_SELECTION),
        (StepKind.WHATSAPP, WHATSAPP_SELECTION),
        (StepKind.PRIMARY_CHANNEL, CHANNEL_SELECTION),
    ):
        cp_stack.service.select(
            cp_stack.household.id,
            kind,
            selection,
            context=cp_stack.context(),
        )
        assert worker.run_once().status == "succeeded"


def test_profile_queues_and_worker_ensures_app_only_secret_namespace(cp_stack) -> None:
    cp_stack.complete_profile(provision_namespace=False)
    job = cp_stack.database.query_one(
        "SELECT * FROM provisioning_jobs WHERE operation = 'ensure_secret_namespace'"
    )
    assert job is not None
    assert cp_stack.jobs.request(job["id"]) == {
        "household_id": cp_stack.household.id
    }
    assert cp_stack.households.get(cp_stack.household.id).runtime_ref is None
    assert cp_stack.database.query("SELECT id FROM config_revisions") == []

    result = cp_stack.make_worker(now=BASE_TIME + 2).run_once()

    assert result is not None and result.status == "succeeded"
    durable = cp_stack.jobs.result(job["id"])
    assert durable is not None
    assert durable["public_result"]["stage"] == "secret_namespace_ready"
    assert durable["public_result"]["planned_writes"] == ["app"]
    resource = cp_stack.database.query_one(
        "SELECT resource_type, status, config_revision FROM external_resources"
    )
    assert tuple(resource) == ("secret_namespace", "ready", None)
    assert cp_stack.households.get(cp_stack.household.id).runtime_ref is None
    assert cp_stack.database.query("SELECT id FROM config_revisions") == []


def test_cancel_during_namespace_creation_persists_recoverable_cleanup(cp_stack) -> None:
    cp_stack.complete_profile(provision_namespace=False)

    class CancelNamespace(DryRunRuntimeProvisioner):
        allow_delete = False

        def ensure_secret_namespace(self, household_id, idempotency_key):
            result = super().ensure_secret_namespace(household_id, idempotency_key)
            cp_stack.service.cancel(
                cp_stack.household.id,
                context=cp_stack.context(),
                now=BASE_TIME + 2,
            )
            return result

        def deprovision(self, external_ref):
            if not self.allow_delete:
                return InspectResult(InspectState.UNKNOWN)
            return super().deprovision(external_ref)

    provider = CancelNamespace()
    registry = ProviderRegistry()
    registry.register("dry-run-runtime", provider)
    worker = cp_stack.make_worker(providers=registry, now=BASE_TIME + 2)

    result = worker.run_once()

    assert result is not None and result.status == "outcome_unknown"
    namespace_job = cp_stack.database.query_one(
        "SELECT * FROM provisioning_jobs WHERE operation = 'ensure_secret_namespace'"
    )
    cleanup = cp_stack.database.query_one(
        "SELECT * FROM provisioning_jobs WHERE kind = 'cleanup'"
    )
    resource = cp_stack.database.query_one(
        "SELECT resource_type, status FROM external_resources"
    )
    assert namespace_job["status"] == "outcome_unknown"
    assert cleanup is not None and cleanup["status"] == "pending"
    assert tuple(resource) == ("secret_namespace", "outcome_unknown")

    provider.allow_delete = True
    assert worker.run_once().status == "succeeded"
    assert cp_stack.database.query_one(
        "SELECT status FROM external_resources"
    )["status"] == "deleted"

class SplitRuntimeProvisioner:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.launched = False
        self.prepared: ProvisionResult | None = None
        self.result: ProvisionResult | None = None

    def prepare(self, intent, idempotency_key):
        del idempotency_key
        self.calls.append("prepare")
        household_id = intent["manifest"]["household_id"]
        self.prepared = ProvisionResult(
            external_ref=f"synthetic-runtime:{household_id}",
            public_result={
                "app_ref": f"synthetic-runtime:{household_id}",
                "volume_ref": "synthetic-volume",
                "stage": "prepared",
            },
        )
        return self.prepared

    def launch(self, intent, prepared, idempotency_key):
        del intent, idempotency_key
        assert prepared == self.prepared
        self.calls.append("launch")
        self.launched = True
        self.result = ProvisionResult(
            external_ref=prepared.external_ref,
            public_result={
                **prepared.public_result,
                "machine_ref": "synthetic-machine",
                "stage": "launched",
            },
        )
        return self.result

    def ensure(self, intent, idempotency_key):
        raise AssertionError("split runtime must not use one-phase ensure")

    def inspect(self, intent_or_ref):
        del intent_or_ref
        if self.launched and self.result is not None:
            return InspectResult(InspectState.READY, self.result)
        return InspectResult(InspectState.PENDING)

    def deprovision(self, exact_ref):
        del exact_ref
        self.calls.append("deprovision")
        self.launched = False
        return InspectResult(InspectState.ABSENT)


def test_lease_is_exclusive_and_expired_running_job_is_reclaimed(cp_stack) -> None:
    job_id = _create_job(cp_stack)
    first = cp_stack.jobs.lease("worker-one", lease_seconds=10, now=BASE_TIME + 1)
    assert first.id == job_id
    assert first.status == "running"
    assert first.attempts == 1
    assert cp_stack.jobs.lease("worker-two", now=BASE_TIME + 5) is None

    reclaimed = cp_stack.jobs.lease(
        "worker-two", lease_seconds=10, now=BASE_TIME + 12
    )
    assert reclaimed.id == job_id
    assert reclaimed.attempts == 2
    row = cp_stack.database.query_one(
        "SELECT leased_by, status FROM provisioning_jobs WHERE id = ?", (job_id,)
    )
    assert row["leased_by"] == "worker-two"
    assert row["status"] == "running"


def test_job_intent_key_is_unique_even_if_command_is_replayed(cp_stack) -> None:
    workflow = cp_stack.onboarding.workflow_for_household(cp_stack.household.id)
    with cp_stack.database.write() as connection:
        first_id, first_created = cp_stack.jobs.create(
            connection,
            household_id=cp_stack.household.id,
            workflow_id=workflow.id,
            kind="email_identity",
            operation="ensure",
            intent_key="same-provider-intent",
            request={"step_kind": "email_identity", "selection": EMAIL_SELECTION},
            provider="fake-email",
        )
        second_id, second_created = cp_stack.jobs.create(
            connection,
            household_id=cp_stack.household.id,
            workflow_id=workflow.id,
            kind="email_identity",
            operation="ensure",
            intent_key="same-provider-intent",
            request={"step_kind": "email_identity", "selection": EMAIL_SELECTION},
            provider="fake-email",
        )
    assert first_created and not second_created
    assert first_id == second_id
    assert len(cp_stack.database.query("SELECT id FROM provisioning_jobs")) == 1


def test_unknown_outcome_is_never_blindly_retried(cp_stack) -> None:
    fake, worker = _selected_email_with_provider(cp_stack, "unknown")
    first = worker.run_once()
    assert first.status == "outcome_unknown"
    assert cp_stack.jobs.get(first.job_id).status == "outcome_unknown"
    assert fake.ensure_calls == 1

    assert worker.run_once() is None
    assert fake.ensure_calls == 1
    assert cp_stack.jobs.get(first.job_id).attempts == 1


def test_reconcile_recovers_accepted_unknown_without_duplicate_resource(cp_stack) -> None:
    fake, worker = _selected_email_with_provider(cp_stack, "crash_after_accept")
    unknown = worker.run_once()
    assert unknown.status == "outcome_unknown"
    assert len(fake.resources) == 1

    reconciled = worker.reconcile(unknown.job_id)
    assert reconciled.status == "succeeded"
    assert fake.ensure_calls == 1
    assert len(fake.resources) == 1
    assert cp_stack.jobs.get(unknown.job_id).status == "succeeded"
    assert len(cp_stack.database.query(
        "SELECT id FROM external_resources WHERE stable_name = ?",
        (cp_stack.jobs.get(unknown.job_id).intent_key,),
    )) == 1
    snapshot = cp_stack.onboarding.snapshot(cp_stack.household.id)
    email = next(step for step in snapshot.steps if step.kind is StepKind.EMAIL)
    assert email.status is StepStatus.VERIFIED


def test_reconcile_marks_definitively_absent_unknown_as_failed(cp_stack) -> None:
    fake, worker = _selected_email_with_provider(cp_stack, "unknown")
    unknown = worker.run_once()
    fake.resources.clear()

    reconciled = worker.reconcile(unknown.job_id)
    assert reconciled.status == "failed"
    assert reconciled.error_code == "provider_absent"
    assert cp_stack.jobs.get(unknown.job_id).status == "failed"


def test_provider_call_runs_without_an_open_control_plane_transaction(cp_stack) -> None:
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )

    class TransactionProbe(DeterministicFakeProvisioner):
        def ensure(self, intent, idempotency_key):
            with cp_stack.database.write() as connection:
                connection.execute(
                    "INSERT INTO rate_limit_buckets"
                    " (bucket_hmac, kind, window_started_at, attempts, updated_at)"
                    " VALUES ('provider-probe', 'test', 1, 1, 1)"
                )
            return super().ensure(intent, idempotency_key)

    registry = ProviderRegistry()
    registry.register("fake-email", TransactionProbe("email"))
    result = cp_stack.make_worker(providers=registry, now=BASE_TIME + 3).run_once()
    assert result.status == "succeeded"
    assert cp_stack.database.query_one(
        "SELECT bucket_hmac FROM rate_limit_buckets WHERE bucket_hmac = 'provider-probe'"
    ) is not None


def test_expired_lease_inspects_an_accepted_intent_before_any_retry(cp_stack) -> None:
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )
    leased = cp_stack.jobs.lease(
        "crashed-worker", lease_seconds=1, now=BASE_TIME + 3
    )
    request = cp_stack.jobs.request(leased.id)
    fake = DeterministicFakeProvisioner("email")
    fake.ensure(request, leased.intent_key)
    assert fake.ensure_calls == 1
    registry = ProviderRegistry()
    registry.register("fake-email", fake)

    recovered = cp_stack.make_worker(
        providers=registry, now=BASE_TIME + 5
    ).run_once()
    assert recovered.status == "succeeded"
    assert fake.ensure_calls == 1
    assert cp_stack.jobs.get(leased.id).attempts == 2


@pytest.mark.parametrize("accepted_before_kill", [False, True])
def test_sigkill_after_lease_recovers_by_inspect_before_ensure(
    cp_stack, tmp_path: Path, accepted_before_kill: bool
) -> None:
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )
    job = cp_stack.database.query_one(
        "SELECT * FROM provisioning_jobs WHERE kind = 'email_identity'"
    )
    marker = tmp_path / "provider-accepted.marker"
    helper = Path(__file__).with_name("chaos_child.py")
    project_root = Path(__file__).parents[2]
    environment = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(project_root),
    }
    mode = "lease-and-accept" if accepted_before_kill else "lease-only"
    child = subprocess.Popen(
        [
            sys.executable,
            str(helper),
            str(cp_stack.database.path),
            mode,
            job["id"],
            str(marker),
        ],
        cwd=project_root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "crash-window-open"
        child.kill()
        assert child.wait(timeout=5) < 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)

    class MarkerProvisioner(DeterministicFakeProvisioner):
        def inspect(self, stable_ref):
            if marker.exists():
                return InspectResult(
                    InspectState.READY,
                    self._result({"selection": EMAIL_SELECTION}, stable_ref),
                )
            return InspectResult(InspectState.ABSENT)

    fake = MarkerProvisioner("email")
    registry = ProviderRegistry()
    registry.register("fake-email", fake)
    recovered = cp_stack.make_worker(
        providers=registry, now=BASE_TIME + 5
    ).run_once()
    assert recovered.status == "succeeded"
    assert fake.ensure_calls == (0 if accepted_before_kill else 1)
    assert cp_stack.jobs.get(job["id"]).attempts == 2


def test_provider_rejection_after_cancel_cannot_reopen_terminal_projection(cp_stack) -> None:
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )

    class CancelThenReject(DeterministicFakeProvisioner):
        def ensure(self, intent, idempotency_key):
            cp_stack.service.cancel(
                cp_stack.household.id,
                context=cp_stack.context(),
                now=BASE_TIME + 3,
            )
            raise ProviderRejected("synthetic rejection after cancellation")

    registry = ProviderRegistry()
    registry.register("fake-email", CancelThenReject("email"))
    result = cp_stack.make_worker(providers=registry, now=BASE_TIME + 3).run_once()
    assert result.status == "failed"
    job = cp_stack.database.query_one(
        "SELECT status FROM provisioning_jobs WHERE kind = 'email_identity'"
    )
    step = cp_stack.database.query_one(
        "SELECT status FROM onboarding_steps WHERE kind = 'email_identity'"
    )
    workflow = cp_stack.onboarding.workflow_for_household(cp_stack.household.id)
    assert job["status"] == "failed"
    assert step["status"] == "cancelled"
    assert workflow.state == "cancelled"


def test_late_waiting_response_after_cancel_stays_reconcilable(cp_stack) -> None:
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )

    class CancelThenWait(DeterministicFakeProvisioner):
        def ensure(self, intent, idempotency_key):
            del intent
            self.ensure_calls += 1
            self.pending.add(idempotency_key)
            cp_stack.service.cancel(
                cp_stack.household.id,
                context=cp_stack.context(),
                now=BASE_TIME + 3,
            )
            raise ProviderWaiting("synthetic provider accepted a pending intent")

    fake = CancelThenWait("email")
    registry = ProviderRegistry()
    registry.register("fake-email", fake)
    registry.register("dry-run-runtime", DryRunRuntimeProvisioner())
    worker = cp_stack.make_worker(providers=registry, now=BASE_TIME + 3)

    late = worker.run_once()
    assert late.status == "outcome_unknown"
    assert late.error_code == "cancel_requires_reconciliation"
    assert cp_stack.jobs.get(late.job_id).status == "outcome_unknown"
    assert worker.reconcile(late.job_id).status == "outcome_unknown"

    fake.pending.clear()
    resolved = worker.reconcile(late.job_id)
    assert resolved.status == "failed"
    assert resolved.error_code == "provider_absent"
    assert cp_stack.jobs.get(late.job_id).status == "failed"
    assert cp_stack.onboarding.workflow_for_household(
        cp_stack.household.id
    ).state == "cancelled"


def test_late_success_with_unknown_compensation_creates_recoverable_cleanup(
    cp_stack,
) -> None:
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )

    class CancelThenSucceed(DeterministicFakeProvisioner):
        allow_delete = False

        def ensure(self, intent, idempotency_key):
            cp_stack.service.cancel(
                cp_stack.household.id,
                context=cp_stack.context(),
                now=BASE_TIME + 3,
            )
            return super().ensure(intent, idempotency_key)

        def deprovision(self, external_ref):
            if not self.allow_delete:
                return InspectResult(InspectState.UNKNOWN)
            return super().deprovision(external_ref)

    fake = CancelThenSucceed("email")
    registry = ProviderRegistry()
    registry.register("fake-email", fake)
    registry.register("dry-run-runtime", DryRunRuntimeProvisioner())
    worker = cp_stack.make_worker(providers=registry, now=BASE_TIME + 3)
    late = worker.run_once()
    assert late.status == "outcome_unknown"
    cleanup = cp_stack.database.query_one(
        "SELECT * FROM provisioning_jobs WHERE kind = 'cleanup'"
        " AND provider = 'fake-email'"
    )
    assert cleanup is not None and cleanup["status"] == "pending"
    cleanup_statuses = {worker.run_once().status, worker.run_once().status}
    assert cleanup_statuses == {"succeeded", "outcome_unknown"}

    fake.allow_delete = True
    recovered = worker.reconcile(cleanup["id"])
    assert recovered.status == "succeeded"
    assert cp_stack.database.query_one(
        "SELECT status FROM external_resources"
    )["status"] == "deleted"
    assert not fake.resources


def test_runtime_worker_stages_bootstrap_between_prepare_and_launch(cp_stack) -> None:
    _advance_to_runtime(cp_stack)
    calls: list[str] = []

    class OrderedSink(InMemorySecretSink):
        def install(self, runtime_ref, material):
            calls.append("stage-secret")
            super().install(runtime_ref, material)

    provider = SplitRuntimeProvisioner(calls)
    registry = ProviderRegistry()
    registry.register("dry-run-runtime", provider)
    sink = OrderedSink()
    result = cp_stack.make_worker(
        providers=registry, secret_sink=sink, now=BASE_TIME + 30
    ).run_once()
    assert result.status == "succeeded"
    assert calls == ["prepare", "stage-secret", "launch"]
    runtime_ref = f"synthetic-runtime:{cp_stack.household.id}"
    assert sink.get(runtime_ref, "HERMES_BOOTSTRAP_TOKEN") is not None
    assert sink.get(runtime_ref, "HERMES_RUNTIME_DSAR_TOKEN") == (
        cp_stack.configs.token_hasher.digest(f"runtime-dsar:{runtime_ref}").encode(
            "ascii"
        )
    )
    stored = cp_stack.database.query_one(
        "SELECT status FROM external_resources WHERE resource_type = 'runtime'"
    )
    assert stored["status"] == "ready"


def test_bootstrap_cleanup_without_runtime_receipt_or_cancellation_is_rejected(
    cp_stack,
) -> None:
    _advance_to_runtime(cp_stack)
    sink = InMemorySecretSink()
    worker = cp_stack.make_worker(secret_sink=sink, now=BASE_TIME + 30)
    assert worker.run_once().status == "succeeded"
    runtime_ref = f"synthetic-runtime:{cp_stack.household.id}"
    assert sink.get(runtime_ref, "HERMES_BOOTSTRAP_TOKEN") is not None
    workflow = cp_stack.onboarding.workflow_for_household(cp_stack.household.id)
    with cp_stack.database.write() as connection:
        cleanup_id, created = cp_stack.jobs.create(
            connection,
            household_id=cp_stack.household.id,
            workflow_id=workflow.id,
            kind="bootstrap_cleanup",
            operation="delete_bootstrap_secret",
            intent_key=f"{cp_stack.household.id}:unauthorized-bootstrap-cleanup",
            desired_revision=1,
            request={
                "runtime_ref": runtime_ref,
                "name": "HERMES_BOOTSTRAP_TOKEN",
            },
            provider="internal-secret-sink",
            now=BASE_TIME + 31,
        )
    assert created

    cleanup = worker.run_once()

    assert cleanup.job_id == cleanup_id
    assert cleanup.status == "failed"
    assert cleanup.error_code == "bootstrap_cleanup_unauthorized"
    assert sink.get(runtime_ref, "HERMES_BOOTSTRAP_TOKEN") is not None


def test_runtime_secret_install_unknown_reconciles_without_blind_machine_launch(
    cp_stack,
) -> None:
    _advance_to_runtime(cp_stack)
    calls: list[str] = []

    class FailOnceSink(InMemorySecretSink):
        failed = False

        def install(self, runtime_ref, material):
            calls.append("stage-secret")
            if not self.failed:
                self.failed = True
                material.clear()
                raise RuntimeError("synthetic secret sink response lost")
            super().install(runtime_ref, material)

    provider = SplitRuntimeProvisioner(calls)
    registry = ProviderRegistry()
    registry.register("dry-run-runtime", provider)
    sink = FailOnceSink()
    worker = cp_stack.make_worker(
        providers=registry, secret_sink=sink, now=BASE_TIME + 30
    )
    unknown = worker.run_once()
    assert unknown.status == "outcome_unknown"
    assert unknown.error_code == "secret_install_unknown"
    assert calls == ["prepare", "stage-secret"]
    assert not provider.launched

    recovered = worker.reconcile(unknown.job_id)
    assert recovered.status == "succeeded"
    assert calls == ["prepare", "stage-secret", "prepare", "stage-secret", "launch"]
    tokens = cp_stack.database.query(
        "SELECT revoked_at, used_at FROM bootstrap_tokens ORDER BY created_at, id"
    )
    assert len(tokens) == 2
    assert sum(row["revoked_at"] is not None for row in tokens) == 1
    assert sum(row["revoked_at"] is None for row in tokens) == 1


def test_cancel_during_runtime_secret_stage_compensates_before_machine_launch(
    cp_stack,
) -> None:
    _advance_to_runtime(cp_stack)
    calls: list[str] = []

    class CancellingSink(InMemorySecretSink):
        def install(self, runtime_ref, material):
            calls.append("stage-secret")
            cp_stack.service.cancel(
                cp_stack.household.id,
                context=cp_stack.context(),
                now=BASE_TIME + 31,
            )
            super().install(runtime_ref, material)

    provider = SplitRuntimeProvisioner(calls)
    registry = ProviderRegistry()
    registry.register("dry-run-runtime", provider)
    sink = CancellingSink()
    result = cp_stack.make_worker(
        providers=registry, secret_sink=sink, now=BASE_TIME + 30
    ).run_once()
    assert result.status == "cancelled"
    assert calls == ["prepare", "stage-secret", "deprovision"]
    assert not provider.launched
    runtime_ref = f"synthetic-runtime:{cp_stack.household.id}"
    assert sink.get(runtime_ref, "HERMES_BOOTSTRAP_TOKEN") is None
    assert cp_stack.onboarding.workflow_for_household(
        cp_stack.household.id
    ).state == "cancelled"
    assert cp_stack.database.query_one(
        "SELECT status FROM external_resources WHERE resource_type = 'runtime'"
    )["status"] == "deleted"
    assert cp_stack.jobs.get(result.job_id).status == "cancelled"
    with pytest.raises(ValueError, match="only an outcome_unknown"):
        cp_stack.make_worker(providers=registry, secret_sink=sink).reconcile(
            result.job_id
        )


def test_superseded_runtime_reconcile_absent_resolves_without_recreate(cp_stack) -> None:
    _advance_to_runtime(cp_stack)
    calls: list[str] = []

    class AbsentRuntime(SplitRuntimeProvisioner):
        def inspect(self, intent_or_ref):
            del intent_or_ref
            return InspectResult(InspectState.ABSENT)

    class UnknownInstallSink(InMemorySecretSink):
        def install(self, runtime_ref, material):
            del runtime_ref
            calls.append("stage-secret")
            material.clear()
            raise RuntimeError("synthetic secret install response loss")

    provider = AbsentRuntime(calls)
    registry = ProviderRegistry()
    registry.register("dry-run-runtime", provider)
    sink = UnknownInstallSink()
    worker = cp_stack.make_worker(
        providers=registry,
        secret_sink=sink,
        now=BASE_TIME + 30,
    )
    unknown = worker.run_once()
    assert unknown.status == "outcome_unknown"
    assert calls == ["prepare", "stage-secret"]
    cp_stack.service.cancel(
        cp_stack.household.id,
        context=cp_stack.context(),
        now=BASE_TIME + 31,
    )

    resolved = worker.reconcile(unknown.job_id)

    assert resolved.status == "cancelled"
    assert cp_stack.jobs.get(unknown.job_id).status == "cancelled"
    assert calls == ["prepare", "stage-secret"]

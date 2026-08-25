from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from io import StringIO
from pathlib import Path

import pytest

from control_plane.api.onboarding import _response
from control_plane.crypto import SecretMaterial
from control_plane.email.contracts import EmailFailureKind, EmailProviderError
from control_plane.email.models import (
    SYNTHETIC_EMAIL_SECRET_BINDING,
    EmailIdentityStatus,
)
from control_plane.models import StepKind, StepStatus
from control_plane.observability import StructuredLogger
from control_plane.onboarding.contracts import CommandResult
from control_plane.privacy.consent import (
    CONTENT_RESTRICTION_PURPOSE,
    HOUSEHOLD_CONTENT_PURPOSE,
    consent_version_and_sha,
)
from control_plane.providers.email.nerve_client import org_teardown_ref
from control_plane.provisioning.contracts import (
    InspectResult,
    InspectState,
    OutcomeUnknown,
    ProviderRateLimited,
    ProviderRegistry,
    ProviderRejected,
    ProviderWaiting,
    ProvisionResult,
)
from control_plane.provisioning.fakes import (
    DeterministicFakeProvisioner,
    DryRunRuntimeProvisioner,
)
from control_plane.provisioning.manifest_toml import manifest_to_toml
from control_plane.provisioning.secrets import InMemorySecretSink

BASE_TIME = 1_800_000_000.0
_RESTRICTION_VERSION, _RESTRICTION_SHA = consent_version_and_sha(
    "special_category_content_restriction"
)
_HOUSEHOLD_VERSION, _HOUSEHOLD_SHA = consent_version_and_sha(
    "special_category_household_content"
)


@pytest.fixture(autouse=True)
def _enable_gated_email_options(monkeypatch: pytest.MonkeyPatch) -> None:
    """`family_domain` and `gmail_agent` are behind fail-closed kill switches.

    Several cases here move a job's `provider` column to a REAL provider name as
    a fixture device — `fake-email` is exempt from the consent gate and so would
    exercise the gate rather than the behaviour under test. That makes those
    jobs look like cut options to the worker's kill-switch check, which is
    correct of the check and beside the point of these tests. The switches
    themselves are asserted in `test_email_option_flags.py`.
    """
    monkeypatch.setenv("ABROLIA_BYO_EMAIL_ENABLED", "1")
    monkeypatch.setenv("ABROLIA_GMAIL_ENABLED", "1")
    # And the managed/BYO incident brake, for the same reason: these cases move
    # a job's provider to a real Nerve name to reach the behaviour under test,
    # which the brake also stops.
    monkeypatch.setenv("ABROLIA_REAL_EMAIL_ENABLED", "1")


# A real provider owes the Art 9(2)(a) consent as well as the S5 restriction,
# and an unrecognised provider counts as real by design (fail-closed). The two
# tests below deliberately wire `future-real-email`, so they carry both.
REAL_PROVIDER_CONSENT = {
    "special_category_household_consent": True,
    "special_category_household_receipt_id": "10000000-0000-4000-8000-000000000024",
    "special_category_household_text_version": _HOUSEHOLD_VERSION,
    "special_category_household_text_sha256": _HOUSEHOLD_SHA,
}
EMAIL_SELECTION = {
    "kind": "abrolia_managed",
    "local_part": "family-agent",
    "special_category_restriction_acknowledged": True,
    "special_category_restriction_receipt_id": "10000000-0000-4000-8000-000000000014",
    "special_category_restriction_text_version": _RESTRICTION_VERSION,
    "special_category_restriction_text_sha256": _RESTRICTION_SHA,
}
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


def test_dry_run_runtime_cleanup_preserves_namespace_until_full_deprovision() -> None:
    household_id = "00000000-0000-4000-8000-000000000042"
    provider = DryRunRuntimeProvisioner()
    namespace = provider.ensure_secret_namespace(household_id, "namespace-intent")
    runtime = provider.ensure(
        {"manifest": {"household_id": household_id}}, "runtime-intent"
    )
    assert namespace.external_ref == runtime.external_ref

    runtime_cleanup = provider.deprovision_runtime(runtime.external_ref)

    assert runtime_cleanup.state is InspectState.ABSENT
    assert set(provider.resources) == {"namespace-intent"}

    namespace_cleanup = provider.deprovision(namespace.external_ref)

    assert namespace_cleanup.state is InspectState.ABSENT
    assert provider.resources == {}


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


def test_retry_later_reopens_non_quarantined_unknown_and_clears_settlement(
    cp_stack,
) -> None:
    job_id = _create_job(cp_stack, intent_key="retry-known-safe-unknown")
    leased = cp_stack.jobs.lease("worker-one", now=BASE_TIME + 1)
    assert leased is not None and leased.id == job_id
    with cp_stack.database.write() as connection:
        cp_stack.jobs.settle(
            connection,
            job_id,
            status="outcome_unknown",
            error_code="provider_degraded",
            now=BASE_TIME + 2,
        )

    cp_stack.jobs.retry_later(
        job_id,
        not_before=BASE_TIME + 30,
        error_code="retry_scheduled",
        now=BASE_TIME + 3,
    )
    row = cp_stack.database.query_one(
        "SELECT status, error_code, not_before, settled_at"
        " FROM provisioning_jobs WHERE id = ?",
        (job_id,),
    )

    assert tuple(row) == ("pending", "retry_scheduled", BASE_TIME + 30, None)


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


def test_reconcile_of_an_absent_unknown_resumes_through_the_adapter(cp_stack) -> None:
    """Absence is the adapter's finding now, not the worker's.

    The deleted reconcile tail probed with `inspect` and settled a
    definitively absent unknown `failed/provider_absent` itself. Forward
    resume goes through the adapter's `reconcile` — which for this fake, like
    Nerve's (`return self.ensure(...)`), re-runs the idempotent provision
    under the same key. "Unknown" is still all anyone knows, so the job stays
    exactly as reconcilable as it was; what must NOT happen is the worker
    deciding absence on its own authority ever again.
    """

    fake, worker = _selected_email_with_provider(cp_stack, "unknown")
    unknown = worker.run_once()
    assert fake.ensure_calls == 1
    fake.resources.clear()

    reconciled = worker.reconcile(unknown.job_id)
    assert fake.ensure_calls == 2
    assert reconciled.status == "outcome_unknown"
    assert cp_stack.jobs.get(unknown.job_id).status == "outcome_unknown"


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


def test_sigkill_after_one_time_secret_response_never_falsely_verifies(
    cp_stack, tmp_path: Path
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
    email_identity_id = cp_stack.jobs.request(job["id"])["email_identity_id"]
    marker = tmp_path / "one-time-secret-consumed.marker"
    helper = Path(__file__).with_name("chaos_child.py")
    project_root = Path(__file__).parents[2]
    child = subprocess.Popen(
        [
            sys.executable,
            str(helper),
            str(cp_stack.database.path),
            "lease-and-accept",
            job["id"],
            str(marker),
        ],
        cwd=project_root,
        env={
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(project_root),
        },
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

    class ConsumedOneTimeSecret(DeterministicFakeProvisioner):
        def inspect(self, stable_ref):
            assert marker.exists()
            return InspectResult(
                InspectState.READY,
                ProvisionResult(
                    external_ref=f"synthetic-email:{email_identity_id}",
                    public_result={
                        "agent_inbox": "family-agent@" + "abrolia.com",
                        "provider": "synthetic",
                        "provider_refs": {"identity_id": email_identity_id},
                        "secret_binding_ref": "ABROLIA_EMAIL_PROVIDER_KEY",
                    },
                    secret_material=SecretMaterial(),
                ),
            )

    registry = ProviderRegistry()
    registry.register("fake-email", ConsumedOneTimeSecret("email"))
    recovered = cp_stack.make_worker(
        providers=registry, now=BASE_TIME + 5
    ).run_once()

    assert recovered.status == "outcome_unknown"
    assert recovered.error_code == "secret_handoff_unknown"
    assert cp_stack.jobs.get(job["id"]).status == "outcome_unknown"


def test_secret_canary_is_confined_to_sink_across_sink_crash_and_public_surfaces(
    cp_stack, tmp_path: Path
) -> None:
    """SIGKILL after sink commit; reclaim using sink proof without leaking value."""
    canary = "-".join(("phase", "b", "secret", "canary", "value"))
    binding = SYNTHETIC_EMAIL_SECRET_BINDING
    sink_path = tmp_path / "durable-secret-sink"

    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )
    job = cp_stack.database.query_one(
        "SELECT id, intent_key FROM provisioning_jobs WHERE kind = 'email_identity'"
    )
    request = cp_stack.jobs.request(job["id"])
    identity_id = request["email_identity_id"]

    helper = Path(__file__).with_name("chaos_child.py")
    project_root = Path(__file__).parents[2]
    child = subprocess.Popen(
        [
            sys.executable,
            str(helper),
            str(cp_stack.database.path),
            "sink-commit",
            str(sink_path),
        ],
        cwd=project_root,
        env={
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(project_root),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "crash-window-open"
        child.kill()
        _stdout, child_stderr = child.communicate(timeout=5)
        assert child.returncode is not None and child.returncode < 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)

    crashed = cp_stack.jobs.get(job["id"])
    assert crashed.status == "running"
    assert sink_path.read_bytes() == canary.encode()
    assert canary not in child_stderr

    class DurableFileSink(InMemorySecretSink):
        def contains(self, runtime_ref, name):
            if name == binding and sink_path.is_file():
                return True
            return super().contains(runtime_ref, name)

        def get(self, runtime_ref, name):
            if name == binding and sink_path.is_file():
                return sink_path.read_bytes()
            return super().get(runtime_ref, name)

        def delete(self, runtime_ref, name):
            if name == binding:
                sink_path.unlink(missing_ok=True)
            super().delete(runtime_ref, name)

    recovered_email = DeterministicFakeProvisioner("email")
    recovered_email.resources[job["intent_key"]] = ProvisionResult(
        external_ref=f"synthetic-email:{identity_id}",
        public_result={
            "agent_inbox": "family-agent@" + "abrolia.com",
            "provider": "synthetic",
            "provider_refs": {"identity_id": identity_id},
            "secret_binding_ref": binding,
        },
    )

    providers = ProviderRegistry()
    providers.register("fake-email", recovered_email)
    providers.register("fake-whatsapp", DeterministicFakeProvisioner("whatsapp"))
    providers.register("fake-channel", DeterministicFakeProvisioner("channel"))
    providers.register("fake-cleanup", DeterministicFakeProvisioner("cleanup"))
    providers.register("dry-run-runtime", DryRunRuntimeProvisioner())
    sink = DurableFileSink()
    log_stream = StringIO()
    worker = cp_stack.make_worker(
        providers=providers, secret_sink=sink, now=BASE_TIME + 10
    )
    worker.logger = StructuredLogger(log_stream)
    recovered = worker.run_once()
    assert recovered is not None and recovered.status == "succeeded"
    assert recovered_email.ensure_calls == 0

    for kind, selection in (
        (StepKind.WHATSAPP, WHATSAPP_SELECTION),
        (StepKind.PRIMARY_CHANNEL, CHANNEL_SELECTION),
    ):
        cp_stack.service.select(
            cp_stack.household.id,
            kind,
            selection,
            context=cp_stack.context(),
            now=BASE_TIME + 3,
        )
        assert worker.run_once().status == "succeeded"

    runtime = worker.run_once()
    assert runtime is not None and runtime.status == "succeeded"
    namespace = worker._secret_namespace_ref(cp_stack.household.id)
    assert namespace is not None
    assert sink.get(namespace, binding) == canary.encode()

    snapshot = cp_stack.onboarding.snapshot(cp_stack.household.id)
    api_body = _response(CommandResult(snapshot)).body
    manifest = manifest_to_toml(cp_stack.configs.manifest(cp_stack.household.id, 1))
    job_json = repr([
        cp_stack.jobs.request(row["id"])
        for row in cp_stack.database.query("SELECT id FROM provisioning_jobs")
    ])
    cp_stack.database.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    database_bytes = cp_stack.database.path.read_bytes()
    wal_path = cp_stack.database.path.with_name(cp_stack.database.path.name + "-wal")
    if wal_path.exists():
        database_bytes += wal_path.read_bytes()

    encoded = canary.encode()
    assert encoded not in database_bytes
    assert encoded not in api_body
    assert canary not in manifest
    assert canary not in job_json
    assert canary not in log_stream.getvalue()


def test_issued_runtime_job_rechecks_current_content_restriction_receipt(
    cp_stack,
) -> None:
    _advance_to_runtime(cp_stack)
    with cp_stack.database.write() as connection:
        connection.execute(
            "DELETE FROM consent_receipts WHERE household_id = ? AND purpose = ?",
            (cp_stack.household.id, "special_category_content_restriction"),
        )
    runtime_provider = DryRunRuntimeProvisioner()
    providers = ProviderRegistry()
    providers.register("dry-run-runtime", runtime_provider)

    blocked = cp_stack.make_worker(
        providers=providers, now=BASE_TIME + 100
    ).run_once()

    assert blocked is not None and blocked.status == "failed"
    assert blocked.error_code == "content_restriction_receipt_required"
    assert runtime_provider.ensure_calls == 0
    workflow = cp_stack.onboarding.workflow_for_household(cp_stack.household.id)
    assert workflow.state == "in_progress"
    assert workflow.current_step == StepKind.EMAIL.value
    email_step = next(
        step
        for step in cp_stack.onboarding.snapshot(cp_stack.household.id).steps
        if step.kind is StepKind.EMAIL
    )
    assert email_step.status is StepStatus.VERIFIED
    assert cp_stack.configs.get(cp_stack.household.id, 1).status == "revoked"

    reset = cp_stack.service.reset_from(
        cp_stack.household.id,
        StepKind.EMAIL,
        context=cp_stack.context(),
        now=BASE_TIME + 101,
    )
    assert reset.snapshot.current_step is StepKind.EMAIL
    reset_email = next(
        step for step in reset.snapshot.steps if step.kind is StepKind.EMAIL
    )
    assert reset_email.status is StepStatus.AVAILABLE


def test_email_job_rechecks_current_content_restriction_before_provider(
    cp_stack,
) -> None:
    cp_stack.complete_profile()
    cp_stack.service.email_provider = "future-real-email"
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        {**EMAIL_SELECTION, **REAL_PROVIDER_CONSENT},
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )
    with cp_stack.database.write() as connection:
        connection.execute(
            "DELETE FROM consent_receipts WHERE household_id = ? AND purpose = ?",
            (cp_stack.household.id, "special_category_content_restriction"),
        )
    email_provider = DeterministicFakeProvisioner("email")
    providers = ProviderRegistry()
    providers.register("future-real-email", email_provider)

    blocked = cp_stack.make_worker(
        providers=providers, now=BASE_TIME + 100
    ).run_once()

    assert blocked is not None and blocked.status == "failed"
    assert blocked.error_code == "content_restriction_receipt_required"
    assert email_provider.ensure_calls == 0


def test_email_reconcile_rechecks_current_content_restriction_before_provider(
    cp_stack,
) -> None:
    cp_stack.complete_profile()
    cp_stack.service.email_provider = "future-real-email"
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        {**EMAIL_SELECTION, **REAL_PROVIDER_CONSENT},
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )
    job = cp_stack.jobs.lease("worker-before-reconcile", now=BASE_TIME + 3)
    assert job is not None and job.kind == "email_identity"
    with cp_stack.database.write() as connection:
        cp_stack.jobs.settle(
            connection,
            job.id,
            status="outcome_unknown",
            error_code="email_outcome_unknown",
            now=BASE_TIME + 4,
        )
        connection.execute(
            "DELETE FROM consent_receipts WHERE household_id = ? AND purpose = ?",
            (cp_stack.household.id, "special_category_content_restriction"),
        )

    class ProviderMustNotRun(DeterministicFakeProvisioner):
        def inspect(self, stable_ref):
            raise AssertionError(f"provider inspect called for {stable_ref}")

    providers = ProviderRegistry()
    providers.register("future-real-email", ProviderMustNotRun("email"))

    blocked = cp_stack.make_worker(
        providers=providers, now=BASE_TIME + 100
    ).reconcile(job.id)

    assert blocked.status == "failed"
    assert blocked.error_code == "content_restriction_receipt_required"


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
    # Reconciling a quarantined job is teardown-first now: it routes to the
    # shutdown probe, never to a recovery inspect. This adapter exposes no
    # derivable teardown reference, so the probe refuses rather than guess —
    # and the refusal keeps the quarantine exactly as an operator found it.
    assert worker.reconcile(late.job_id).status == "outcome_unknown"

    fake.pending.clear()
    still = worker.reconcile(late.job_id)
    assert still.status == "outcome_unknown"
    assert still.error_code == "cancel_requires_reconciliation", (
        "the reconcile of a quarantined job must not overwrite the reason "
        "an operator is holding"
    )
    assert cp_stack.jobs.get(late.job_id).status == "outcome_unknown", (
        "a late waiting response after cancel left the job somewhere "
        "reconcile can never reach again"
    )
    assert cp_stack.onboarding.workflow_for_household(
        cp_stack.household.id
    ).state == "cancelled"
    identity = cp_stack.database.query_one(
        "SELECT id, status FROM email_identities WHERE household_id = ?",
        (cp_stack.household.id,),
    )
    assert identity["status"] not in {
        EmailIdentityStatus.VERIFIED.value,
        EmailIdentityStatus.ACTIVE.value,
    }, "an identity whose workflow was cancelled ended up activated"

    # Over the three real adapters the same cancel derives a teardown
    # reference, and the cleanup it schedules settles this parent to
    # `cancelled_and_compensated` and deletes the identity — covered by
    # test_real_email_wiring.py. The synthetic provider deliberately has no
    # teardown contract; refusing beats scheduling a cleanup its
    # deprovisioner would refuse, because the resource is live either way
    # and only one of the two says it was handled.


@pytest.mark.parametrize(
    "retry_error",
    [
        pytest.param(
            lambda: ProviderRateLimited(retry_after=15),
            id="provider-rate-limited",
        ),
        pytest.param(
            lambda: EmailProviderError(
                EmailFailureKind.SAFE_RETRY, "provider-private-safe-retry"
            ),
            id="email-safe-retry",
        ),
    ],
)
def test_late_initial_retry_signal_after_cancel_preserves_quarantine(
    cp_stack, retry_error: Callable[[], Exception]
) -> None:
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )

    class CancelThenRetry(DeterministicFakeProvisioner):
        def ensure(self, intent, idempotency_key):
            del intent, idempotency_key
            cp_stack.service.cancel(
                cp_stack.household.id,
                context=cp_stack.context(),
                now=BASE_TIME + 3,
            )
            raise retry_error()

    registry = ProviderRegistry()
    registry.register("fake-email", CancelThenRetry("email"))

    result = cp_stack.make_worker(
        providers=registry, now=BASE_TIME + 3
    ).run_once()
    stored = cp_stack.jobs.get(result.job_id)

    assert result.status == "outcome_unknown"
    assert result.error_code == "cancel_requires_reconciliation"
    assert stored is not None
    assert stored.status == "outcome_unknown"
    assert stored.error_code == "cancel_requires_reconciliation"


@pytest.mark.parametrize(
    "retry_error",
    [
        pytest.param(
            lambda: ProviderRateLimited(retry_after=15),
            id="provider-rate-limited",
        ),
        pytest.param(
            lambda: EmailProviderError(
                EmailFailureKind.SAFE_RETRY, "provider-private-safe-retry"
            ),
            id="email-safe-retry",
        ),
    ],
)
def test_reconcile_retry_signal_after_reset_preserves_quarantine(
    cp_stack, retry_error: Callable[[], Exception]
) -> None:
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )

    class WaitThenRetry(DeterministicFakeProvisioner):
        def ensure(self, intent, idempotency_key):
            del intent, idempotency_key
            raise ProviderWaiting("synthetic owner action required")

        def reconcile(self, intent, idempotency_key):
            del intent, idempotency_key
            raise retry_error()

    registry = ProviderRegistry()
    registry.register("fake-email", WaitThenRetry("email"))
    worker = cp_stack.make_worker(providers=registry, now=BASE_TIME + 3)
    waiting = worker.run_once()
    assert waiting.status == "waiting_user"
    cp_stack.service.reset_from(
        cp_stack.household.id,
        StepKind.EMAIL,
        context=cp_stack.context(),
        now=BASE_TIME + 4,
    )

    reconciled = worker.reconcile(waiting.job_id)
    stored = cp_stack.jobs.get(waiting.job_id)

    assert reconciled.status == "outcome_unknown"
    assert reconciled.error_code == "reset_requires_reconciliation"
    assert stored is not None
    assert stored.status == "outcome_unknown"
    assert stored.error_code == "reset_requires_reconciliation"


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
    assert cleanup_statuses == {"pending", "outcome_unknown"}

    fake.allow_delete = True
    recovered = worker.reconcile(cleanup["id"])
    assert recovered.status == "succeeded"
    namespace_cleanup = cp_stack.make_worker(
        providers=registry, now=BASE_TIME + 10
    ).run_once()
    assert namespace_cleanup.status == "succeeded"
    assert {
        row["status"]
        for row in cp_stack.database.query("SELECT status FROM external_resources")
    } == {"deleted"}
    assert not fake.resources


def test_email_reset_strips_runtime_manifest_digest_from_cleanup_reference(
    cp_stack,
) -> None:
    cp_stack.complete_profile(provision_namespace=False)
    registry = ProviderRegistry()
    registry.register("dry-run-runtime", DryRunRuntimeProvisioner())
    registry.register("fake-email", DeterministicFakeProvisioner("email"))
    registry.register("fake-whatsapp", DeterministicFakeProvisioner("whatsapp"))
    registry.register("fake-channel", DeterministicFakeProvisioner("channel"))
    worker = cp_stack.make_worker(providers=registry, now=BASE_TIME + 20)

    assert worker.run_once().status == "succeeded"
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
            now=BASE_TIME + 21,
        )
        assert worker.run_once().status == "succeeded"
    assert worker.run_once().status == "succeeded"

    runtime = cp_stack.database.query_one(
        "SELECT id, external_id_ciphertext, encryption_key_version"
        " FROM external_resources WHERE resource_type = 'runtime'"
    )
    original_ref = cp_stack.jobs.decrypt_json(
        "external_resources",
        runtime["id"],
        "external_id",
        runtime["external_id_ciphertext"],
        runtime["encryption_key_version"],
    )
    exact_ref = {
        "app_ref": original_ref,
        "machine_ref": "machine-phase24",
        "volume_ref": "volume-phase24",
        "config_sha256": "a" * 64,
    }
    encrypted = cp_stack.jobs.encrypt_json(
        "external_resources", runtime["id"], "external_id", exact_ref
    )
    with cp_stack.database.write() as connection:
        connection.execute(
            "UPDATE external_resources SET external_id_ciphertext = ?,"
            " encryption_key_version = ? WHERE id = ?",
            (encrypted.ciphertext, encrypted.key_version, runtime["id"]),
        )
        connection.execute(
            "DELETE FROM external_resources WHERE household_id = ?"
            " AND resource_type = 'secret_namespace'",
            (cp_stack.household.id,),
        )

    cp_stack.service.reset_from(
        cp_stack.household.id,
        StepKind.EMAIL,
        context=cp_stack.context(),
        now=BASE_TIME + 30,
    )

    cleanup = cp_stack.database.query_one(
        "SELECT id FROM provisioning_jobs WHERE kind = 'cleanup'"
        " AND provider = 'dry-run-runtime'"
    )
    request = cp_stack.jobs.request(cleanup["id"])
    assert request["external_ref"] == {
        "app_ref": original_ref,
        "machine_ref": "machine-phase24",
        "volume_ref": "volume-phase24",
    }
    namespace_job = cp_stack.database.query_one(
        "SELECT id, status FROM provisioning_jobs"
        " WHERE operation = 'ensure_secret_namespace'"
        " ORDER BY created_at DESC, id DESC LIMIT 1"
    )
    assert namespace_job["status"] == "pending"
    assert cp_stack.jobs.request(namespace_job["id"]) == {
        "household_id": cp_stack.household.id
    }


def test_email_reset_runtime_cleanup_preserves_namespace_for_reconnect(
    cp_stack,
) -> None:
    cp_stack.complete_profile(provision_namespace=False)
    runtime_provider = DryRunRuntimeProvisioner()
    email_provider = DeterministicFakeProvisioner("email")
    registry = ProviderRegistry()
    registry.register("dry-run-runtime", runtime_provider)
    registry.register("fake-email", email_provider)
    registry.register("fake-whatsapp", DeterministicFakeProvisioner("whatsapp"))
    registry.register("fake-channel", DeterministicFakeProvisioner("channel"))
    worker = cp_stack.make_worker(providers=registry, now=BASE_TIME + 20)

    namespace = worker.run_once()
    assert namespace is not None and namespace.status == "succeeded"
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
            now=BASE_TIME + 21,
        )
        assert worker.run_once().status == "succeeded"
    assert worker.run_once().status == "succeeded"

    cp_stack.service.reset_from(
        cp_stack.household.id,
        StepKind.EMAIL,
        context=cp_stack.context(),
        now=BASE_TIME + 30,
    )
    cleanup_jobs = cp_stack.database.query(
        "SELECT id FROM provisioning_jobs WHERE kind = 'cleanup'"
        " ORDER BY created_at, id"
    )
    assert [
        cp_stack.jobs.request(row["id"])["resource_type"] for row in cleanup_jobs
    ] == ["email_identity", "whatsapp_identity", "channel_binding", "runtime"]
    assert [worker.run_once().status for _ in cleanup_jobs] == [
        "succeeded",
        "succeeded",
        "succeeded",
        "succeeded",
    ]

    resources = {
        row["resource_type"]: row["status"]
        for row in cp_stack.database.query(
            "SELECT resource_type, status FROM external_resources"
        )
    }
    assert resources["runtime"] == "deleted"
    assert resources["secret_namespace"] == "ready"
    assert set(runtime_provider.resources) == {
        f"{cp_stack.household.id}:secret-namespace"
    }

    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 40,
    )
    reconnected = worker.run_once()

    assert reconnected is not None and reconnected.status == "succeeded"
    assert email_provider.ensure_calls == 2


def test_cancel_runtime_cleanup_deletes_namespace_only_after_workload(
    cp_stack,
) -> None:
    cp_stack.complete_profile(provision_namespace=False)
    runtime_provider = DryRunRuntimeProvisioner()
    registry = ProviderRegistry()
    registry.register("dry-run-runtime", runtime_provider)
    registry.register("fake-email", DeterministicFakeProvisioner("email"))
    registry.register("fake-whatsapp", DeterministicFakeProvisioner("whatsapp"))
    registry.register("fake-channel", DeterministicFakeProvisioner("channel"))
    worker = cp_stack.make_worker(providers=registry, now=BASE_TIME + 20)

    assert worker.run_once().status == "succeeded"
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
            now=BASE_TIME + 21,
        )
        assert worker.run_once().status == "succeeded"
    assert worker.run_once().status == "succeeded"

    cp_stack.service.cancel(
        cp_stack.household.id,
        context=cp_stack.context(),
        now=BASE_TIME + 30,
    )
    cleanup_jobs = cp_stack.database.query(
        "SELECT id FROM provisioning_jobs WHERE kind = 'cleanup'"
        " ORDER BY created_at, id"
    )
    assert [
        cp_stack.jobs.request(row["id"])["resource_type"] for row in cleanup_jobs
    ] == [
        "email_identity",
        "whatsapp_identity",
        "channel_binding",
        "runtime",
        "secret_namespace",
    ]

    assert [worker.run_once().status for _ in range(3)] == [
        "succeeded",
        "succeeded",
        "succeeded",
    ]
    runtime_cleanup = worker.run_once()
    assert runtime_cleanup.status == "succeeded"
    assert set(runtime_provider.resources) == {
        f"{cp_stack.household.id}:secret-namespace"
    }

    namespace_cleanup = worker.run_once()

    assert namespace_cleanup.status == "succeeded"
    assert runtime_provider.resources == {}
    assert {
        row["status"]
        for row in cp_stack.database.query("SELECT status FROM external_resources")
    } == {"deleted"}


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


@pytest.mark.parametrize(
    ("carrier", "provider"),
    [
        ("durable-reference", "nerve-managed"),
        ("derived-reference", "google-oauth"),
        ("derived-org-reference", "nerve-managed"),
    ],
)
def test_a_shutdown_tears_down_what_it_can_name_without_asking_the_provider(
    cp_stack, carrier, provider
) -> None:
    """Teardown reaches every reference the control plane can NAME.

    `_shutdown_probe` may not call a provider — no email provisioner has a
    reliably read-only inspector — so what it can act on is durable state and
    arithmetic on identifiers this side already holds. It read only the
    immutable request, which is the one place Google OAuth and Nerve BYO do NOT
    put their reference: they persist it through `settle` into
    `external_ref_ciphertext`. A known binding or domain therefore stayed live
    after a withdrawal quarantined its job.

    Where nothing was recorded, derivation helps only if it yields the
    provider's OWN teardown contract. Google's does: `deprovision` takes
    `google-oauth:<identity_id>` and the identity id is in the request. Nerve's
    does not — its `_Refs` carries provider-assigned org, webhook and key ids —
    so a reference derived for Nerve would be one its deprovisioner refuses,
    and scheduling it would manufacture a failed cleanup job in place of a
    teardown. That case must schedule NOTHING and stay quarantined.

    The three cases are parameterised over real provider names for a reason: an
    earlier version used `fake-email`, whose fake accepts any reference, so the
    test agreed with itself about a reference no real provider would take.
    """
    cp_stack.complete_profile()
    # A REAL email selection, carrying both Art 9(2)(a) receipts. A real
    # provider is gated on them, so a synthetic selection would settle at the
    # consent precondition and never reach the shutdown path this is about.
    restriction_version, restriction_sha = consent_version_and_sha(
        CONTENT_RESTRICTION_PURPOSE
    )
    household_version, household_sha = consent_version_and_sha(
        HOUSEHOLD_CONTENT_PURPOSE
    )
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        {
            "kind": "abrolia_managed",
            "local_part": "family-agent",
            "special_category_restriction_acknowledged": True,
            "special_category_restriction_receipt_id": (
                "10000000-0000-4000-8000-0000000000a1"
            ),
            "special_category_restriction_text_version": restriction_version,
            "special_category_restriction_text_sha256": restriction_sha,
            "special_category_household_consent": True,
            "special_category_household_receipt_id": (
                "10000000-0000-4000-8000-0000000000a2"
            ),
            "special_category_household_text_version": household_version,
            "special_category_household_text_sha256": household_sha,
        },
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )
    called: list[str] = []

    class RecordsEveryCall(DeterministicFakeProvisioner):
        def ensure(self, intent, idempotency_key):
            called.append("ensure")
            del intent, idempotency_key
            # Created upstream, then the call did not answer.
            raise OutcomeUnknown("the provider did not answer")

        def inspect(self, intent, idempotency_key):
            called.append("inspect")
            raise AssertionError("a shutdown must not inspect")

        def reconcile(self, intent, idempotency_key):
            called.append("reconcile")
            raise AssertionError("a shutdown must not reconcile")

    # Registered under a REAL provider name. The derived reference is gated on
    # the provider being one that creates upstream state, so a fixture using
    # `fake-email` would exercise the gate rather than the behaviour.
    registry = ProviderRegistry()
    registry.register(provider, RecordsEveryCall("email"))
    worker = cp_stack.make_worker(providers=registry, now=BASE_TIME + 3)
    # The job's provider is durable and the config is frozen, so the realistic
    # state is made by moving the row rather than by flipping a flag.
    with cp_stack.database.write() as connection:
        connection.execute(
            "UPDATE provisioning_jobs SET provider = ?"
            " WHERE household_id = ? AND kind = 'email_identity'",
            (provider, cp_stack.household.id),
        )
    settled = worker.run_once()
    assert settled.status == "outcome_unknown", settled

    identity = cp_stack.database.query_one(
        "SELECT id FROM email_identities WHERE household_id = ?",
        (cp_stack.household.id,),
    )
    assert identity is not None, "the fixture produced no email identity"

    # The withdrawal quarantines the in-flight job, exactly as
    # `_supersede_unsettled_jobs` does.
    with cp_stack.database.write() as connection:
        if carrier == "durable-reference":
            # Where `settle` puts a `ProviderWaiting.external_ref`: the column,
            # not the immutable request. How it got there is incidental; that
            # the shutdown READS it is the point.
            cp_stack.jobs.settle(
                connection,
                settled.job_id,
                status="outcome_unknown",
                external_ref="nerve:org:already-created",
                error_code="withdraw_requires_reconciliation",
                now=BASE_TIME + 4,
            )
        else:
            connection.execute(
                "UPDATE provisioning_jobs SET status = 'outcome_unknown',"
                " error_code = 'withdraw_requires_reconciliation', settled_at = ?"
                " WHERE id = ?",
                (BASE_TIME + 4, settled.job_id),
            )
    called.clear()

    worker.reconcile(settled.job_id)

    # Resolving the provider OBJECT is not a provider call; asking it anything
    # is. The first version of this asserted on registry lookups and failed for
    # a reason that had nothing to do with the invariant.
    assert called == [], f"the shutdown called the provider: {called}"
    cleanup = cp_stack.database.query(
        "SELECT id FROM provisioning_jobs WHERE household_id = ? AND kind = 'cleanup'",
        (cp_stack.household.id,),
    )
    assert cleanup, f"nothing was scheduled to tear down the {carrier}"
    # The reference on the cleanup job's own request. `external_resources`
    # stores it encrypted, so the plain-text assertion belongs here.
    refs = {
        cp_stack.jobs.request(row["id"]).get("external_ref")
        for row in cleanup
    }
    # Each is the shape that provider's OWN `deprovision` accepts. The point
    # of naming them here rather than asserting "some reference" is that an
    # earlier version scheduled an org LOOKUP key, which every real
    # deprovisioner refuses.
    if carrier == "durable-reference":
        assert "nerve:org:already-created" in refs, refs
    elif provider == "google-oauth":
        assert f"google-oauth:{identity['id']}" in refs, refs
    else:
        assert org_teardown_ref(cp_stack.household.id, identity["id"]) in refs, refs


def _install_runtime(cp_stack, *, model_api_key: str | None):
    _advance_to_runtime(cp_stack)
    sink = InMemorySecretSink()
    result = cp_stack.make_worker(
        secret_sink=sink, model_api_key=model_api_key, now=BASE_TIME + 30
    ).run_once()
    assert result is not None and result.status == "succeeded"
    return sink, f"synthetic-runtime:{cp_stack.household.id}"


def test_a_runtime_is_provisioned_with_the_credential_its_chat_needs(cp_stack) -> None:
    """C2 shipped a chat route with no credential to serve it.

    `_web_chat_loop` builds the model client lazily, provisioning installed only
    the bootstrap and DSAR tokens, and the machine environment carried no model
    key — so the first real turn failed inside the client constructor, which
    `_web_chat` reports as `chat_unavailable`, and the public endpoint answered
    503 permanently. The route was right; the secret was never installed.
    """
    sink, runtime_ref = _install_runtime(cp_stack, model_api_key="synthetic-model-key")

    assert sink.get(runtime_ref, "ANTHROPIC_API_KEY") == b"synthetic-model-key"
    # It travels with the others, into the runtime's own namespace — never
    # through argv, the manifest, or a log.
    assert sink.get(runtime_ref, "HERMES_BOOTSTRAP_TOKEN") is not None
    assert sink.get(runtime_ref, "HERMES_RUNTIME_DSAR_TOKEN") is not None


def test_a_deployment_without_a_model_key_installs_none(cp_stack) -> None:
    """Absent is not a failure: it is a runtime that cannot answer chat, which
    `_web_chat` already reports honestly rather than pretending."""
    sink, runtime_ref = _install_runtime(cp_stack, model_api_key=None)
    assert sink.get(runtime_ref, "ANTHROPIC_API_KEY") is None
    assert sink.get(runtime_ref, "HERMES_BOOTSTRAP_TOKEN") is not None

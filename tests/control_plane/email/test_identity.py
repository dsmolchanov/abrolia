from __future__ import annotations

import io
import json
import sqlite3

import pytest
from pydantic import ValidationError

from control_plane.crypto import SecretMaterial
from control_plane.db import new_id
from control_plane.email.contracts import EmailFailureKind, EmailProviderError
from control_plane.email.models import (
    EmailIdentityStatus,
    EmailOption,
    EmailProvisionIntent,
    EmailPublicBinding,
)
from control_plane.models import StepKind
from control_plane.observability import StructuredLogger
from control_plane.providers.email.fake import FakeEmailIdentityProvisioner
from control_plane.provisioning.contracts import (
    InspectResult,
    InspectState,
    OutcomeUnknown,
    ProviderRegistry,
    ProviderRejected,
    ProviderWaiting,
    ProvisionResult,
)
from control_plane.provisioning.fakes import DeterministicFakeProvisioner
from control_plane.provisioning.secrets import InMemorySecretSink, SecretInstallError

BASE_TIME = 1_800_000_000.0
EMAIL_SELECTION = {"kind": "abrolia_managed", "local_part": "family-agent"}


def test_email_intent_requires_typed_selection_matching_its_option() -> None:
    with pytest.raises(ValidationError, match="does not match"):
        EmailProvisionIntent(
            identity_id="synthetic-identity",
            household_id="synthetic-household",
            option=EmailOption.MANAGED_ABROLIA,
            selection={
                "kind": "gmail_agent",
                "separate_agent_account_acknowledged": True,
            },
            secret_namespace_ref="synthetic-runtime",
        )


@pytest.mark.parametrize(
    "provider_refs",
    [
        {"webhook_key": "x" * 32},
        {"webhook_id": "x" * 32},
    ],
)
def test_email_public_binding_rejects_untyped_provider_references(
    provider_refs: dict[str, str],
) -> None:
    with pytest.raises(ValidationError):
        EmailPublicBinding(
            agent_inbox="family-agent@" + "abrolia.com",
            provider="synthetic",
            provider_refs=provider_refs,
        )


def test_email_public_binding_enforces_exact_provider_authority() -> None:
    with pytest.raises(ValidationError, match="invalid secret binding"):
        EmailPublicBinding(
            agent_inbox="family-agent@" + "abrolia.com",
            provider="synthetic",
            provider_refs={"identity_id": "synthetic:identity:family"},
            secret_binding_ref="HERMES_RUNTIME_DSAR_TOKEN",
        )

    nerve = {
        "agent_inbox": "family-agent@" + "abrolia.com",
        "provider": "nerve",
        "provider_subject": "00000000-0000-4000-8000-000000000001",
        "provider_refs": {
            "org_id": "00000000-0000-4000-8000-000000000001",
            "grant_id": "00000000-0000-4000-8000-000000000002",
            "inbox_id": "00000000-0000-4000-8000-000000000003",
            "key_id": "00000000-0000-4000-8000-000000000004",
            "webhook_id": "00000000-0000-4000-8000-000000000005",
        },
        "secret_binding_ref": "ABROLIA_NERVE_EMAIL_CREDENTIALS",
        "granted_scopes": ["nerve:email.read", "nerve:email.send"],
    }
    EmailPublicBinding.model_validate(nerve)
    with pytest.raises(ValidationError, match="invalid scope set"):
        EmailPublicBinding.model_validate({**nerve, "granted_scopes": []})
    with pytest.raises(ValidationError, match="invalid secret binding"):
        EmailPublicBinding.model_validate({**nerve, "secret_binding_ref": None})


def test_worker_rejects_public_provider_not_declared_by_adapter(cp_stack) -> None:
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )

    class MisdeclaredNerveAdapter(FakeEmailIdentityProvisioner):
        email_public_provider = "nerve"

    registry = ProviderRegistry()
    registry.register("fake-email", MisdeclaredNerveAdapter())

    result = cp_stack.make_worker(providers=registry, now=BASE_TIME + 3).run_once()

    assert result.status == "outcome_unknown"
    assert result.error_code == "outcome_unknown"
    identity = cp_stack.email_identities.current_for_household(cp_stack.household.id)
    assert identity is not None
    cp_stack.service.cancel(
        cp_stack.household.id,
        context=cp_stack.context(),
        now=BASE_TIME + 4,
    )
    assert cp_stack.email_identities.get(
        identity.id
    ).status is EmailIdentityStatus.DISCONNECTING
    reservation = cp_stack.database.query_one(
        "SELECT status FROM email_address_reservations WHERE email_identity_id = ?",
        (identity.id,),
    )
    assert reservation["status"] == "held"


def test_untyped_email_waiting_result_is_rejected_before_persistence(cp_stack) -> None:
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )
    canary = "x" * 32

    class UntypedWaitingProvider(FakeEmailIdentityProvisioner):
        def ensure(self, intent, idempotency_key):
            del intent, idempotency_key
            raise ProviderWaiting(
                "synthetic untyped wait",
                public_result={"provider_refs": {"webhook_key": canary}},
            )

    registry = ProviderRegistry()
    registry.register("fake-email", UntypedWaitingProvider())

    result = cp_stack.make_worker(
        providers=registry, now=BASE_TIME + 3
    ).run_once()

    assert result.status == "outcome_unknown"
    assert result.error_code == "provider_result_invalid"
    snapshot = cp_stack.onboarding.snapshot(cp_stack.household.id)
    email_step = next(step for step in snapshot.steps if step.kind is StepKind.EMAIL)
    assert canary not in repr(email_step.public_status)


def test_untyped_email_external_reference_is_rejected(cp_stack) -> None:
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )

    class UntypedReferenceProvider(FakeEmailIdentityProvisioner):
        def _result(self, intent, key):
            valid = super()._result(intent, key)
            return ProvisionResult(
                external_ref=json.dumps({"id": "x" * 32}),
                public_result=valid.public_result,
                secret_material=valid.secret_material,
            )

    registry = ProviderRegistry()
    registry.register("fake-email", UntypedReferenceProvider())

    result = cp_stack.make_worker(
        providers=registry, now=BASE_TIME + 3
    ).run_once()

    assert result.status == "outcome_unknown"
    assert result.error_code == "outcome_unknown"


def test_untyped_email_inspect_waiting_result_is_rejected(cp_stack) -> None:
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )

    class UntypedPendingInspect(FakeEmailIdentityProvisioner):
        def ensure(self, intent, idempotency_key):
            del intent, idempotency_key
            raise ProviderWaiting()

        def inspect(self, stable_ref):
            del stable_ref
            return InspectResult(
                InspectState.PENDING,
                public_result={"provider_refs": {"webhook_key": "x" * 32}},
            )

    registry = ProviderRegistry()
    registry.register("fake-email", UntypedPendingInspect())
    worker = cp_stack.make_worker(providers=registry, now=BASE_TIME + 3)
    assert worker.run_once().status == "waiting_user"
    cp_stack.service.check(
        cp_stack.household.id,
        StepKind.EMAIL,
        context=cp_stack.context(),
        now=BASE_TIME + 4,
    )

    result = worker.run_once()

    assert result.status == "outcome_unknown"
    assert result.error_code == "outcome_unknown"


def test_lost_inspect_without_reconcile_method_uses_original_stable_ref(cp_stack) -> None:
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )

    class LostInspectProvider(FakeEmailIdentityProvisioner):
        original_ref = None
        ready_result = None
        lose_next_inspect = True

        def ensure(self, intent, idempotency_key):
            self.original_ref = idempotency_key
            self.ready_result = self._result(intent, idempotency_key)
            raise ProviderWaiting("synthetic owner action required")

        def inspect(self, stable_ref):
            if self.lose_next_inspect:
                self.lose_next_inspect = False
                raise OutcomeUnknown("synthetic inspect response loss")
            if stable_ref == self.original_ref:
                return InspectResult(InspectState.READY, self.ready_result)
            return InspectResult(InspectState.ABSENT)

    provider = LostInspectProvider()
    registry = ProviderRegistry()
    registry.register("fake-email", provider)
    worker = cp_stack.make_worker(providers=registry, now=BASE_TIME + 3)
    assert worker.run_once().status == "waiting_user"
    cp_stack.service.check(
        cp_stack.household.id,
        StepKind.EMAIL,
        context=cp_stack.context(),
        now=BASE_TIME + 4,
    )

    unknown = worker.run_once()
    request = cp_stack.jobs.request(unknown.job_id)

    assert unknown.status == "outcome_unknown"
    assert request["stable_ref"] == provider.original_ref
    assert cp_stack.jobs.get(unknown.job_id).intent_key != provider.original_ref
    recovered = worker.reconcile(unknown.job_id)
    assert recovered.status == "succeeded"
    assert cp_stack.email_identities.current_for_household(
        cp_stack.household.id
    ).status is EmailIdentityStatus.VERIFIED


def test_untyped_email_reconcile_waiting_result_is_rejected(cp_stack) -> None:
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )

    class UntypedReconcileWait(FakeEmailIdentityProvisioner):
        def ensure(self, intent, idempotency_key):
            del intent, idempotency_key
            raise OutcomeUnknown("synthetic response loss")

        def reconcile(self, intent, idempotency_key):
            del intent, idempotency_key
            raise ProviderWaiting(
                public_result={"provider_refs": {"webhook_key": "x" * 32}}
            )

    registry = ProviderRegistry()
    registry.register("fake-email", UntypedReconcileWait())
    worker = cp_stack.make_worker(providers=registry, now=BASE_TIME + 3)
    unknown = worker.run_once()

    result = worker.reconcile(unknown.job_id)

    assert result.status == "outcome_unknown"
    assert result.error_code == "provider_result_invalid"


@pytest.mark.parametrize(
    ("kind", "expected_status", "expected_code"),
    [
        (EmailFailureKind.USER_ACTION, "waiting_user", "email_user_action_required"),
        (EmailFailureKind.SAFE_RETRY, "pending", "email_retry_scheduled"),
        (EmailFailureKind.DEFINITIVE_FAILURE, "failed", "email_provider_rejected"),
        (EmailFailureKind.AUTH_REVOKED, "failed", "email_auth_revoked"),
        (EmailFailureKind.PROVIDER_DEGRADED, "pending", "email_retry_scheduled"),
        (EmailFailureKind.OUTCOME_UNKNOWN, "outcome_unknown", "email_outcome_unknown"),
    ],
)
def test_email_error_taxonomy_always_settles_or_reschedules_job(
    cp_stack, kind: EmailFailureKind, expected_status: str, expected_code: str
) -> None:
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )

    class TaxonomyProvider(FakeEmailIdentityProvisioner):
        def ensure(self, intent, idempotency_key):
            del intent, idempotency_key
            raise EmailProviderError(kind, "provider-private-detail")

    registry = ProviderRegistry()
    registry.register("fake-email", TaxonomyProvider())
    result = cp_stack.make_worker(
        providers=registry, now=BASE_TIME + 3
    ).run_once()

    assert result.status == expected_status
    assert result.error_code == expected_code
    row = cp_stack.database.query_one(
        "SELECT status, error_code FROM provisioning_jobs WHERE id = ?",
        (result.job_id,),
    )
    assert row["status"] == expected_status
    assert row["error_code"] == expected_code
    assert "provider-private-detail" not in cp_stack.database.path.read_text(
        encoding="utf-8", errors="ignore"
    )


def test_email_error_taxonomy_is_applied_during_reconciliation(cp_stack) -> None:
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )

    class ReconcileRetryProvider(FakeEmailIdentityProvisioner):
        def ensure(self, intent, idempotency_key):
            del intent, idempotency_key
            raise OutcomeUnknown()

        def reconcile(self, intent, idempotency_key):
            del intent, idempotency_key
            raise EmailProviderError(
                EmailFailureKind.SAFE_RETRY, "provider-private-retry-detail"
            )

    registry = ProviderRegistry()
    registry.register("fake-email", ReconcileRetryProvider())
    worker = cp_stack.make_worker(providers=registry, now=BASE_TIME + 3)
    unknown = worker.run_once()
    retried = worker.reconcile(unknown.job_id)

    assert retried.status == "pending"
    assert retried.error_code == "email_retry_scheduled"
    job = cp_stack.jobs.get(unknown.job_id)
    assert job is not None and job.status == "pending"
    assert job.error_code == "email_retry_scheduled"
    assert "provider-private-retry-detail" not in cp_stack.database.path.read_text(
        encoding="utf-8", errors="ignore"
    )


def test_selection_reserves_address_and_only_one_live_identity(cp_stack) -> None:
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )

    identity = cp_stack.email_identities.current_for_household(cp_stack.household.id)
    assert identity is not None
    assert identity.option is EmailOption.MANAGED_ABROLIA
    assert identity.status is EmailIdentityStatus.PROVISIONING
    assert identity.address == "family-agent@" + "abrolia.com"
    assert identity.address_masked == "fa**********@" + "abrolia.com"
    assert not cp_stack.email_identities.address_available(
        "abrolia.com", "family-agent", now=BASE_TIME + 3
    )

    with cp_stack.database.write() as connection, pytest.raises(sqlite3.IntegrityError):
        cp_stack.email_identities.create_selected(
            connection,
            household_id=cp_stack.household.id,
            option=EmailOption.GMAIL,
            now=BASE_TIME + 3,
        )


def test_provider_secret_goes_to_namespace_not_database_or_job(cp_stack) -> None:
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )
    provider = FakeEmailIdentityProvisioner(issue_secret=True)
    registry = ProviderRegistry()
    registry.register("fake-email", provider)
    sink = InMemorySecretSink()

    result = cp_stack.make_worker(
        providers=registry, secret_sink=sink, now=BASE_TIME + 3
    ).run_once()

    assert result is not None and result.status == "succeeded"
    namespace = cp_stack.database.query_one(
        "SELECT * FROM external_resources WHERE resource_type = 'secret_namespace'"
    )
    namespace_ref = cp_stack.jobs.decrypt_json(
        "external_resources",
        namespace["id"],
        "external_id",
        namespace["external_id_ciphertext"],
        namespace["encryption_key_version"],
    )
    assert sink.get(namespace_ref, "ABROLIA_EMAIL_PROVIDER_KEY") == b"synthetic-key"
    identity = cp_stack.email_identities.current_for_household(cp_stack.household.id)
    assert identity is not None and identity.status is EmailIdentityStatus.VERIFIED
    assert identity.secret_binding_ref == "ABROLIA_EMAIL_PROVIDER_KEY"
    assert b"synthetic-key" not in cp_stack.database.path.read_bytes()
    email_job = cp_stack.database.query_one(
        "SELECT id FROM provisioning_jobs WHERE kind = 'email_identity'"
    )
    assert "synthetic-key" not in repr(cp_stack.jobs.request(email_job["id"]))
    assert "synthetic-key" not in repr(cp_stack.jobs.result(email_job["id"]))


def test_unknown_secret_handoff_never_verifies_from_secretless_inspect(cp_stack) -> None:
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )
    provider = FakeEmailIdentityProvisioner(issue_secret=True)
    registry = ProviderRegistry()
    registry.register("fake-email", provider)

    class UnknownSink(InMemorySecretSink):
        def install(self, runtime_ref, material):
            del runtime_ref
            material.clear()
            raise SecretInstallError()

    worker = cp_stack.make_worker(
        providers=registry, secret_sink=UnknownSink(), now=BASE_TIME + 3
    )
    unknown = worker.run_once()

    assert unknown is not None and unknown.status == "outcome_unknown"
    assert unknown.error_code == "secret_handoff_unknown"
    identity = cp_stack.email_identities.current_for_household(cp_stack.household.id)
    assert identity is not None and identity.status is EmailIdentityStatus.OUTCOME_UNKNOWN
    reconciled = worker.reconcile(unknown.job_id)
    assert reconciled.status == "outcome_unknown"
    assert cp_stack.email_identities.current_for_household(
        cp_stack.household.id
    ).status is EmailIdentityStatus.OUTCOME_UNKNOWN


def test_expired_lease_cannot_verify_when_one_time_secret_was_consumed(cp_stack) -> None:
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )
    provider = FakeEmailIdentityProvisioner(issue_secret=True)
    leased = cp_stack.jobs.lease("crashed-worker", lease_seconds=1, now=BASE_TIME + 3)
    request = cp_stack.jobs.request(leased.id)
    namespace = cp_stack.database.query_one(
        "SELECT id, external_id_ciphertext, encryption_key_version"
        " FROM external_resources WHERE resource_type = 'secret_namespace'"
    )
    namespace_ref = cp_stack.jobs.decrypt_json(
        "external_resources",
        namespace["id"],
        "external_id",
        namespace["external_id_ciphertext"],
        namespace["encryption_key_version"],
    )
    result = provider.ensure(
        {
            "identity_id": request["email_identity_id"],
            "household_id": cp_stack.household.id,
            "option": request["option"],
            "selection": request["selection"],
            "secret_namespace_ref": namespace_ref,
        },
        leased.intent_key,
    )
    result.secret_material.clear()
    registry = ProviderRegistry()
    registry.register("fake-email", provider)
    sink = InMemorySecretSink()

    recovered = cp_stack.make_worker(
        providers=registry, secret_sink=sink, now=BASE_TIME + 5
    ).run_once()

    assert recovered.status == "outcome_unknown"
    assert recovered.error_code == "secret_handoff_unknown"
    assert sink.get(namespace_ref, "ABROLIA_EMAIL_PROVIDER_KEY") is None
    identity = cp_stack.email_identities.current_for_household(cp_stack.household.id)
    assert identity is not None and identity.status is EmailIdentityStatus.OUTCOME_UNKNOWN


def test_expired_reservation_is_not_reported_available_while_identity_is_live(
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

    assert not cp_stack.email_identities.address_available(
        "abrolia.com", "family-agent", now=BASE_TIME + 2 + 16 * 60
    )


def test_provider_error_value_is_normalized_before_db_api_or_log(cp_stack) -> None:
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )
    canary = "GOCSPX-" + "provider-error-secret-canary"

    class CanaryProvider(FakeEmailIdentityProvisioner):
        # The worker no longer discovers a missing `reconcile` and falls back
        # to probing with `inspect`; forward resume is the adapter's own
        # method now. This stub reproduces what the deleted tail used to do,
        # so the property under test still has a vehicle that TRIES to carry
        # a secret-shaped provider error value toward persistence.

        def reconcile(self, intent, idempotency_key):
            found = self.inspect(idempotency_key)
            if found.state is InspectState.FAILED:
                raise ProviderRejected(found.error_code or "provider_rejected")
            return self.ensure(intent, idempotency_key)

        def ensure(self, intent, idempotency_key):
            del intent, idempotency_key
            raise OutcomeUnknown()

        def inspect(self, stable_ref):
            del stable_ref
            # The canary rides as an unvetted provider-supplied error code;
            # `InspectResult` must normalize it before anything downstream
            # can relay it.
            return InspectResult(InspectState.FAILED, error_code=canary)

    registry = ProviderRegistry()
    registry.register("fake-email", CanaryProvider())
    # Forward resume re-enters the adapter only once the household's secret
    # namespace exists; this fixture never provisioned runtime.
    with cp_stack.jobs.db.write() as connection:
        namespace = cp_stack.jobs.encrypt_json(
            "external_resources",
            "ns-canary",
            "external_id",
            "synthetic-namespace:canary",
        )
        connection.execute(
            "INSERT INTO external_resources (id, household_id, provider,"
            " resource_type, stable_name, external_id_ciphertext,"
            " encryption_key_version, status, created_at, updated_at)"
            " VALUES (?, ?, 'dry-run-runtime', 'secret_namespace',"
            " 'secret_namespace', ?, ?, 'ready', ?, ?)",
            (
                "ns-canary",
                cp_stack.household.id,
                namespace.ciphertext,
                namespace.key_version,
                BASE_TIME + 2,
                BASE_TIME + 2,
            ),
        )
    stream = io.StringIO()
    worker = cp_stack.make_worker(providers=registry, now=BASE_TIME + 3)
    worker.logger = StructuredLogger(stream)
    unknown = worker.run_once()
    failed = worker.reconcile(unknown.job_id)

    assert failed.status == "failed"
    assert failed.error_code == "provider_rejected"
    snapshot = cp_stack.onboarding.snapshot(cp_stack.household.id)
    email = next(step for step in snapshot.steps if step.kind is StepKind.EMAIL)
    assert email.error_code == "provider_rejected"
    assert canary not in stream.getvalue()
    cp_stack.database.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert canary.encode() not in cp_stack.database.path.read_bytes()


def test_credential_shaped_provider_result_fails_closed_without_stranding_job(
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
    canary = "GOCSPX-" + "provider-result-secret-canary"

    class CanaryProvider(FakeEmailIdentityProvisioner):
        def ensure(self, intent, idempotency_key):
            del intent, idempotency_key
            return ProvisionResult(external_ref=canary, public_result={})

    registry = ProviderRegistry()
    registry.register("fake-email", CanaryProvider())
    result = cp_stack.make_worker(
        providers=registry, now=BASE_TIME + 3
    ).run_once()

    assert result.status == "outcome_unknown"
    assert result.error_code == "provider_result_invalid"
    job = cp_stack.jobs.get(result.job_id)
    assert job is not None and job.status == "outcome_unknown"
    cp_stack.database.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert canary.encode() not in cp_stack.database.path.read_bytes()


def test_untyped_email_public_result_never_reaches_durable_or_public_state(
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
    canary = "Z" * 64

    class UntypedProvider(FakeEmailIdentityProvisioner):
        def ensure(self, intent, idempotency_key):
            result = super().ensure(intent, idempotency_key)
            return ProvisionResult(
                external_ref=result.external_ref,
                public_result={**result.public_result, "opaque": canary},
                secret_material=result.secret_material,
            )

    registry = ProviderRegistry()
    registry.register("fake-email", UntypedProvider())
    result = cp_stack.make_worker(
        providers=registry, now=BASE_TIME + 3
    ).run_once()

    assert result.status == "outcome_unknown"
    assert result.error_code == "outcome_unknown"
    assert canary not in repr(cp_stack.onboarding.snapshot(cp_stack.household.id))
    cp_stack.database.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert canary.encode() not in cp_stack.database.path.read_bytes()


def test_email_secret_name_must_match_public_binding(cp_stack) -> None:
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )

    class WrongSecretNameProvider(FakeEmailIdentityProvisioner):
        def ensure(self, intent, idempotency_key):
            result = super().ensure(intent, idempotency_key)
            return ProvisionResult(
                external_ref=result.external_ref,
                public_result={
                    **result.public_result,
                    "secret_binding_ref": "ABROLIA_EMAIL_PROVIDER_KEY",
                },
                secret_material=SecretMaterial.from_mapping({
                    "WRONG_EMAIL_KEY": "synthetic-secret"
                }),
            )

    registry = ProviderRegistry()
    registry.register("fake-email", WrongSecretNameProvider())
    sink = InMemorySecretSink()
    result = cp_stack.make_worker(
        providers=registry, secret_sink=sink, now=BASE_TIME + 3
    ).run_once()

    assert result.status == "outcome_unknown"
    assert result.error_code == "outcome_unknown"
    namespace = cp_stack.database.query_one(
        "SELECT id, external_id_ciphertext, encryption_key_version"
        " FROM external_resources WHERE resource_type = 'secret_namespace'"
    )
    namespace_ref = cp_stack.jobs.decrypt_json(
        "external_resources",
        namespace["id"],
        "external_id",
        namespace["external_id_ciphertext"],
        namespace["encryption_key_version"],
    )
    assert sink.get(namespace_ref, "ABROLIA_EMAIL_PROVIDER_KEY") is None
    assert sink.get(namespace_ref, "WRONG_EMAIL_KEY") is None


def test_cancel_during_email_secret_stage_compensates_installed_binding(
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
    provider = FakeEmailIdentityProvisioner(issue_secret=True)
    registry = ProviderRegistry()
    registry.register("fake-email", provider)

    class CancellingSink(InMemorySecretSink):
        def install(self, runtime_ref, material):
            cp_stack.service.cancel(
                cp_stack.household.id,
                context=cp_stack.context(),
                now=BASE_TIME + 4,
            )
            super().install(runtime_ref, material)

    sink = CancellingSink()
    result = cp_stack.make_worker(
        providers=registry, secret_sink=sink, now=BASE_TIME + 3
    ).run_once()
    namespace = cp_stack.database.query_one(
        "SELECT id, external_id_ciphertext, encryption_key_version"
        " FROM external_resources WHERE resource_type = 'secret_namespace'"
    )
    namespace_ref = cp_stack.jobs.decrypt_json(
        "external_resources",
        namespace["id"],
        "external_id",
        namespace["external_id_ciphertext"],
        namespace["encryption_key_version"],
    )

    assert result.status == "cancelled"
    assert sink.get(namespace_ref, "ABROLIA_EMAIL_PROVIDER_KEY") is None
    assert cp_stack.jobs.get(result.job_id).status == "cancelled"
    identity = cp_stack.database.query_one(
        "SELECT id, status FROM email_identities WHERE household_id = ?",
        (cp_stack.household.id,),
    )
    assert identity["status"] == "deleted"
    reservation = cp_stack.database.query_one(
        "SELECT status FROM email_address_reservations WHERE email_identity_id = ?",
        (identity["id"],),
    )
    assert reservation["status"] == "released"


def test_failed_cancel_compensation_converges_through_late_secret_cleanup(
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
    provider = FakeEmailIdentityProvisioner(issue_secret=True)
    registry = ProviderRegistry()
    registry.register("fake-email", provider)

    class FailOnceCancellingSink(InMemorySecretSink):
        delete_failed = False

        def install(self, runtime_ref, material):
            cp_stack.service.cancel(
                cp_stack.household.id,
                context=cp_stack.context(),
                now=BASE_TIME + 4,
            )
            super().install(runtime_ref, material)

        def delete(self, runtime_ref, name):
            if not self.delete_failed:
                self.delete_failed = True
                raise SecretInstallError("synthetic delete response loss")
            super().delete(runtime_ref, name)

    sink = FailOnceCancellingSink()
    worker = cp_stack.make_worker(
        providers=registry, secret_sink=sink, now=BASE_TIME + 3
    )
    unknown = worker.run_once()
    late_cleanup = worker.run_once()

    assert unknown.status == "outcome_unknown"
    assert unknown.error_code == "cancelled_cleanup_unknown"
    assert late_cleanup.status == "succeeded"
    assert cp_stack.jobs.get(unknown.job_id).status == "cancelled"
    identity = cp_stack.email_identities.current_for_household(cp_stack.household.id)
    assert identity is None
    stored = cp_stack.database.query_one(
        "SELECT status FROM email_identities WHERE household_id = ?",
        (cp_stack.household.id,),
    )
    assert stored["status"] == "deleted"


def test_runtime_cleanup_waits_until_deleted_namespace_proves_email_secret_absent(
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
    provider = FakeEmailIdentityProvisioner(issue_secret=True)
    runtime_calls: list[str] = []

    class RecordingRuntimeCleanup:
        def deprovision_runtime(self, external_ref):
            runtime_calls.append(external_ref)
            return InspectResult(InspectState.ABSENT)

    registry = ProviderRegistry()
    registry.register("fake-email", provider)
    registry.register("recording-runtime", RecordingRuntimeCleanup())

    class DeleteAlwaysFailsAfterCancellation(InMemorySecretSink):
        def install(self, runtime_ref, material):
            cp_stack.service.cancel(
                cp_stack.household.id,
                context=cp_stack.context(),
                now=BASE_TIME + 4,
            )
            super().install(runtime_ref, material)

        def delete(self, runtime_ref, name):
            raise SecretInstallError("synthetic namespace already unavailable")

    sink = DeleteAlwaysFailsAfterCancellation()
    worker = cp_stack.make_worker(
        providers=registry, secret_sink=sink, now=BASE_TIME + 3
    )
    unknown = worker.run_once()
    parent = cp_stack.jobs.get(unknown.job_id)
    with cp_stack.database.write() as connection:
        runtime_cleanup_id, _ = cp_stack.jobs.create(
            connection,
            household_id=cp_stack.household.id,
            workflow_id=parent.workflow_id,
            kind="cleanup",
            operation="deprovision",
            intent_key=f"{cp_stack.household.id}:runtime-cleanup-race",
            request={
                "resource_type": "runtime",
                "external_ref": f"synthetic-runtime:{cp_stack.household.id}",
            },
            provider="recording-runtime",
            now=BASE_TIME - 1,
        )

    deferred = worker.run_once()
    assert deferred.job_id == runtime_cleanup_id
    assert deferred.status == "pending"
    assert deferred.error_code == "email_cleanup_pending"
    assert runtime_calls == []

    with cp_stack.database.write() as connection:
        connection.execute(
            "UPDATE external_resources SET status = 'deleted', updated_at = ?"
            " WHERE household_id = ? AND resource_type = 'secret_namespace'",
            (BASE_TIME + 5, cp_stack.household.id),
        )
    secret_cleanup = worker.run_once()
    assert secret_cleanup.status == "succeeded"
    assert cp_stack.jobs.get(unknown.job_id).status == "cancelled"

    resumed = cp_stack.make_worker(
        providers=registry, secret_sink=sink, now=BASE_TIME + 10
    ).run_once()
    assert resumed.job_id == runtime_cleanup_id
    assert resumed.status == "succeeded"
    assert runtime_calls == [f"synthetic-runtime:{cp_stack.household.id}"]


@pytest.mark.parametrize("secret_cleanup_first", [False, True])
def test_cancel_compensation_converges_in_either_cleanup_order(
    cp_stack, secret_cleanup_first: bool
) -> None:
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )

    class ProviderDeleteUnknownOnce(FakeEmailIdentityProvisioner):
        delete_failed = False

        def deprovision(self, external_ref):
            if not self.delete_failed:
                self.delete_failed = True
                raise OutcomeUnknown("synthetic provider delete response loss")
            return super().deprovision(external_ref)

    provider = ProviderDeleteUnknownOnce(issue_secret=True)
    registry = ProviderRegistry()
    registry.register("fake-email", provider)

    class SecretDeleteUnknownOnce(InMemorySecretSink):
        delete_failed = False

        def install(self, runtime_ref, material):
            cp_stack.service.cancel(
                cp_stack.household.id,
                context=cp_stack.context(),
                now=BASE_TIME + 4,
            )
            super().install(runtime_ref, material)

        def delete(self, runtime_ref, name):
            if not self.delete_failed:
                self.delete_failed = True
                raise SecretInstallError("synthetic secret delete response loss")
            super().delete(runtime_ref, name)

    sink = SecretDeleteUnknownOnce()
    worker = cp_stack.make_worker(
        providers=registry, secret_sink=sink, now=BASE_TIME + 3
    )

    unknown = worker.run_once()
    cleanup_jobs = cp_stack.database.query(
        "SELECT id, kind FROM provisioning_jobs WHERE status = 'pending'"
        " AND (operation = 'delete_email_secret' OR intent_key = ?)",
        (f"{cp_stack.household.id}:late-cleanup:{unknown.job_id}",),
    )
    provider_cleanup_id = next(
        row["id"] for row in cleanup_jobs if row["kind"] == "cleanup"
    )
    secret_cleanup_id = next(
        row["id"] for row in cleanup_jobs if row["kind"] == "bootstrap_cleanup"
    )
    with cp_stack.database.write() as connection:
        first_id = secret_cleanup_id if secret_cleanup_first else provider_cleanup_id
        second_id = provider_cleanup_id if secret_cleanup_first else secret_cleanup_id
        connection.execute(
            "UPDATE provisioning_jobs SET created_at = ? WHERE id = ?",
            (BASE_TIME - 1, first_id),
        )
        connection.execute(
            "UPDATE provisioning_jobs SET created_at = ? WHERE id = ?",
            (BASE_TIME, second_id),
        )

    first_cleanup = worker.run_once()
    identity = cp_stack.database.query_one(
        "SELECT id, status FROM email_identities WHERE household_id = ?",
        (cp_stack.household.id,),
    )
    reservation = cp_stack.database.query_one(
        "SELECT status FROM email_address_reservations WHERE email_identity_id = ?",
        (identity["id"],),
    )

    assert unknown.status == "outcome_unknown"
    assert first_cleanup.job_id == first_id
    assert first_cleanup.status == "succeeded"
    assert cp_stack.jobs.get(unknown.job_id).status == (
        "outcome_unknown" if secret_cleanup_first else "cancelled"
    )
    assert identity["status"] == "disconnecting"
    assert reservation["status"] == "held"
    with cp_stack.database.write() as connection, pytest.raises(
        ValueError, match="cleanup must finish"
    ):
        cp_stack.email_identities.create_selected(
            connection,
            household_id=cp_stack.household.id,
            option=EmailOption.MANAGED_ABROLIA,
            address="replacement@" + "abrolia.com",
            now=BASE_TIME + 5,
        )

    second_cleanup = worker.run_once()
    stored = cp_stack.database.query_one(
        "SELECT status FROM email_identities WHERE id = ?", (identity["id"],)
    )
    reservation = cp_stack.database.query_one(
        "SELECT status FROM email_address_reservations WHERE email_identity_id = ?",
        (identity["id"],),
    )

    assert second_cleanup.job_id == second_id
    assert second_cleanup.status == "succeeded"
    parent = cp_stack.jobs.get(unknown.job_id)
    assert parent.status == "cancelled"
    assert parent.error_code == "cancelled_and_compensated"
    assert stored["status"] == "deleted"
    assert reservation["status"] == "released"


def test_disconnect_waits_for_every_email_provider_resource(cp_stack) -> None:
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )
    provider = FakeEmailIdentityProvisioner()
    registry = ProviderRegistry()
    registry.register("fake-email", provider)
    assert cp_stack.make_worker(
        providers=registry, now=BASE_TIME + 3
    ).run_once().status == "succeeded"
    identity = cp_stack.email_identities.current_for_household(cp_stack.household.id)
    assert identity is not None
    first_resource = cp_stack.database.query_one(
        "SELECT id FROM external_resources WHERE household_id = ?"
        " AND resource_type = 'email_identity'",
        (cp_stack.household.id,),
    )
    second_resource_id = new_id()
    encrypted = cp_stack.jobs.encrypt_json(
        "external_resources",
        second_resource_id,
        "external_id",
        f"synthetic-email:{identity.id}:older",
    )

    with cp_stack.database.write() as connection:
        cp_stack.email_identities.begin_disconnect(
            connection, cp_stack.household.id, now=BASE_TIME + 4
        )
        connection.execute(
            "INSERT INTO external_resources (id, household_id, provider, resource_type,"
            " stable_name, external_id_ciphertext, encryption_key_version, status,"
            " created_at, updated_at) VALUES (?, ?, 'fake-email', 'email_identity', ?, ?, ?,"
            " 'ready', ?, ?)",
            (
                second_resource_id,
                cp_stack.household.id,
                f"{identity.id}:older",
                encrypted.ciphertext,
                encrypted.key_version,
                BASE_TIME,
                BASE_TIME,
            ),
        )
        connection.execute(
            "UPDATE external_resources SET status = 'deleted' WHERE id = ?",
            (first_resource["id"],),
        )
        assert not cp_stack.email_identities.finish_disconnect(
            connection, identity.id, now=BASE_TIME + 5
        )
        connection.execute(
            "UPDATE external_resources SET status = 'deleted' WHERE id = ?",
            (second_resource_id,),
        )
        assert cp_stack.email_identities.finish_disconnect(
            connection, identity.id, now=BASE_TIME + 6
        )

    assert cp_stack.email_identities.get(identity.id).status is EmailIdentityStatus.DELETED


@pytest.mark.parametrize("unresolved_status", ["pending", "outcome_unknown"])
def test_disconnect_waits_for_unresolved_email_provider_job(
    cp_stack, unresolved_status: str
) -> None:
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )
    provider = FakeEmailIdentityProvisioner()
    registry = ProviderRegistry()
    registry.register("fake-email", provider)
    assert cp_stack.make_worker(
        providers=registry, now=BASE_TIME + 3
    ).run_once().status == "succeeded"
    identity = cp_stack.email_identities.current_for_household(cp_stack.household.id)
    assert identity is not None
    with cp_stack.database.write() as connection:
        cp_stack.email_identities.begin_disconnect(
            connection, cp_stack.household.id, now=BASE_TIME + 4
        )
        connection.execute(
            "UPDATE external_resources SET status = 'deleted'"
            " WHERE household_id = ? AND resource_type = 'email_identity'",
            (cp_stack.household.id,),
        )
        connection.execute(
            "UPDATE provisioning_jobs SET status = ?"
            " WHERE household_id = ? AND kind = 'email_identity'",
            (unresolved_status, cp_stack.household.id),
        )
        assert not cp_stack.email_identities.finish_disconnect(
            connection, identity.id, now=BASE_TIME + 5
        )
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'cancelled'"
            " WHERE household_id = ? AND kind = 'email_identity'",
            (cp_stack.household.id,),
        )
        assert cp_stack.email_identities.finish_disconnect(
            connection, identity.id, now=BASE_TIME + 6
        )

    assert cp_stack.email_identities.get(identity.id).status is EmailIdentityStatus.DELETED


def test_cancel_after_definitive_provider_rejection_releases_identity(cp_stack) -> None:
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )
    provider = FakeEmailIdentityProvisioner(behavior="reject")
    registry = ProviderRegistry()
    registry.register("fake-email", provider)
    failed = cp_stack.make_worker(
        providers=registry, now=BASE_TIME + 3
    ).run_once()
    identity = cp_stack.email_identities.current_for_household(cp_stack.household.id)
    assert failed.status == "failed"
    assert identity is not None
    assert identity.status is EmailIdentityStatus.NEEDS_ATTENTION

    cp_stack.service.cancel(
        cp_stack.household.id,
        context=cp_stack.context(),
        now=BASE_TIME + 4,
    )

    assert cp_stack.email_identities.get(identity.id).status is EmailIdentityStatus.DELETED
    reservation = cp_stack.database.query_one(
        "SELECT status FROM email_address_reservations WHERE email_identity_id = ?",
        (identity.id,),
    )
    assert reservation["status"] == "released"


def test_verified_identity_becomes_active_only_after_both_runtime_checks(cp_stack) -> None:
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )
    provider = FakeEmailIdentityProvisioner()
    registry = ProviderRegistry()
    registry.register("fake-email", provider)
    assert cp_stack.make_worker(providers=registry, now=BASE_TIME + 3).run_once().status == (
        "succeeded"
    )
    identity = cp_stack.email_identities.current_for_household(cp_stack.household.id)
    assert identity is not None and identity.status is EmailIdentityStatus.VERIFIED

    with cp_stack.database.write() as connection:
        cp_stack.email_identities.begin_activation(
            connection, identity.id, now=BASE_TIME + 4
        )
        status = cp_stack.email_identities.record_activation_receipt(
            connection,
            identity.id,
            desired_revision=1,
            runtime_ref=f"synthetic-runtime:{cp_stack.household.id}",
            provider="synthetic",
            inbound_check="healthy",
            outbound_check="pending",
            receipt_digest="a" * 64,
            now=BASE_TIME + 5,
        )
    assert status is EmailIdentityStatus.ACTIVATING

    with cp_stack.database.write() as connection:
        status = cp_stack.email_identities.record_activation_receipt(
            connection,
            identity.id,
            desired_revision=1,
            runtime_ref=f"synthetic-runtime:{cp_stack.household.id}",
            provider="synthetic",
            inbound_check="healthy",
            outbound_check="healthy",
            receipt_digest="b" * 64,
            now=BASE_TIME + 6,
        )
    assert status is EmailIdentityStatus.ACTIVE
    assert cp_stack.email_identities.current_for_household(
        cp_stack.household.id
    ).status is EmailIdentityStatus.ACTIVE


def test_email_provider_fails_when_secret_namespace_definitively_failed(
    cp_stack,
) -> None:
    cp_stack.complete_profile(provision_namespace=False)
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )
    email_provider = FakeEmailIdentityProvisioner(issue_secret=True)
    registry = ProviderRegistry()
    registry.register("dry-run-runtime", DeterministicFakeProvisioner("runtime"))
    registry.register("fake-email", email_provider)
    worker = cp_stack.make_worker(providers=registry, now=BASE_TIME + 3)

    namespace = worker.run_once()
    email = worker.run_once()

    assert namespace is not None and namespace.status == "failed"
    assert email is not None and email.status == "failed"
    assert email.error_code == "secret_namespace_failed"
    assert email_provider.ensure_calls == 0

    cp_stack.service.retry(
        cp_stack.household.id,
        StepKind.EMAIL,
        context=cp_stack.context(),
        now=BASE_TIME + 4,
    )
    retry_jobs = cp_stack.database.query(
        "SELECT kind, operation, status FROM provisioning_jobs"
        " WHERE status = 'pending' ORDER BY created_at, id"
    )
    assert [tuple(row) for row in retry_jobs] == [
        ("runtime", "ensure_secret_namespace", "pending"),
        ("email_identity", "ensure", "pending"),
    ]


def test_reset_deprovisions_identity_and_removes_staged_secret(cp_stack) -> None:
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )
    provider = FakeEmailIdentityProvisioner(issue_secret=True)
    registry = ProviderRegistry()
    registry.register("fake-email", provider)
    sink = InMemorySecretSink()
    worker = cp_stack.make_worker(
        providers=registry, secret_sink=sink, now=BASE_TIME + 3
    )
    assert worker.run_once().status == "succeeded"
    identity = cp_stack.email_identities.current_for_household(cp_stack.household.id)
    assert identity is not None
    namespace_ref = cp_stack.database.query_one(
        "SELECT id, external_id_ciphertext, encryption_key_version"
        " FROM external_resources WHERE resource_type = 'secret_namespace'"
    )
    runtime_ref = cp_stack.jobs.decrypt_json(
        "external_resources",
        namespace_ref["id"],
        "external_id",
        namespace_ref["external_id_ciphertext"],
        namespace_ref["encryption_key_version"],
    )
    assert sink.get(runtime_ref, "ABROLIA_EMAIL_PROVIDER_KEY") == b"synthetic-key"

    cp_stack.service.reset_from(
        cp_stack.household.id,
        StepKind.EMAIL,
        context=cp_stack.context(),
        now=BASE_TIME + 4,
    )
    cleanup = worker.run_once()

    assert cleanup is not None and cleanup.status == "succeeded"
    assert sink.get(runtime_ref, "ABROLIA_EMAIL_PROVIDER_KEY") is None
    assert cp_stack.email_identities.get(identity.id).status is EmailIdentityStatus.DELETED
    reservation = cp_stack.database.query_one(
        "SELECT status FROM email_address_reservations WHERE email_identity_id = ?",
        (identity.id,),
    )
    assert reservation["status"] == "released"


def test_reset_deprovisions_pre_identity_synthetic_email_resource(cp_stack) -> None:
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        EMAIL_SELECTION,
        context=cp_stack.context(),
        now=BASE_TIME + 2,
    )
    provider = FakeEmailIdentityProvisioner(issue_secret=True)
    registry = ProviderRegistry()
    registry.register("fake-email", provider)
    sink = InMemorySecretSink()
    worker = cp_stack.make_worker(
        providers=registry, secret_sink=sink, now=BASE_TIME + 3
    )
    assert worker.run_once().status == "succeeded"
    identity = cp_stack.email_identities.current_for_household(cp_stack.household.id)
    assert identity is not None
    namespace = cp_stack.database.query_one(
        "SELECT * FROM external_resources WHERE resource_type = 'secret_namespace'"
    )
    namespace_ref = cp_stack.jobs.decrypt_json(
        "external_resources",
        namespace["id"],
        "external_id",
        namespace["external_id_ciphertext"],
        namespace["encryption_key_version"],
    )
    assert sink.get(namespace_ref, "ABROLIA_EMAIL_PROVIDER_KEY") == b"synthetic-key"

    # Reproduce the production baseline: the provider resource predates the
    # durable email identity generation introduced later.
    with cp_stack.database.write() as connection:
        connection.execute(
            "DELETE FROM email_address_reservations WHERE email_identity_id = ?",
            (identity.id,),
        )
        connection.execute("DELETE FROM email_identities WHERE id = ?", (identity.id,))

    cp_stack.service.reset_from(
        cp_stack.household.id,
        StepKind.EMAIL,
        context=cp_stack.context(),
        now=BASE_TIME + 4,
    )
    cleanup = cp_stack.database.query_one(
        "SELECT id FROM provisioning_jobs WHERE kind = 'cleanup'"
        " AND provider = 'fake-email'"
    )
    assert cp_stack.jobs.request(cleanup["id"])["legacy_secret_binding_ref"] == (
        "ABROLIA_EMAIL_PROVIDER_KEY"
    )

    result = worker.run_once()
    assert result is not None and result.status == "succeeded"
    assert sink.get(namespace_ref, "ABROLIA_EMAIL_PROVIDER_KEY") is None
    assert cp_stack.database.query_one(
        "SELECT status FROM external_resources"
        " WHERE resource_type = 'email_identity'"
    )["status"] == "deleted"

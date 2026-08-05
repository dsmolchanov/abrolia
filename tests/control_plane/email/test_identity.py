from __future__ import annotations

import sqlite3

import pytest

from control_plane.email.models import EmailIdentityStatus, EmailOption
from control_plane.models import StepKind
from control_plane.providers.email.fake import FakeEmailIdentityProvisioner
from control_plane.provisioning.contracts import ProviderRegistry
from control_plane.provisioning.fakes import DeterministicFakeProvisioner
from control_plane.provisioning.secrets import InMemorySecretSink, SecretInstallError

BASE_TIME = 1_800_000_000.0
EMAIL_SELECTION = {"kind": "abrolia_managed", "local_part": "family-agent"}


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


def test_email_provider_waits_for_ready_secret_namespace(cp_stack) -> None:
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
    assert email is not None and email.status == "pending"
    assert email.error_code == "secret_namespace_not_ready"
    assert email_provider.ensure_calls == 0


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

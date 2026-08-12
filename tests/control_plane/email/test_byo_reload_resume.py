"""Phase C2 case 1 — reload and fresh login resume the same DNS state."""

from __future__ import annotations

from control_plane.email.models import EmailIdentityStatus
from control_plane.models import StepKind
from control_plane.providers.email.nerve_byo_domain import NerveByoDomainProvisioner
from control_plane.providers.email.nerve_managed import NERVE_SECRET_BINDING
from control_plane.provisioning.contracts import ProviderRegistry
from control_plane.provisioning.secrets import InMemorySecretSink
from tests.control_plane.email.byo_support import (
    BASE_TIME,
    FakeByoNerveAdmin,
    select_byo_domain,
)


def test_dns_records_resume_until_all_checks_are_active(cp_stack) -> None:
    cp_stack.complete_profile()
    select_byo_domain(cp_stack)
    client = FakeByoNerveAdmin()
    registry = ProviderRegistry()
    registry.register("nerve-byo-domain", NerveByoDomainProvisioner(client))
    sink = InMemorySecretSink()
    worker = cp_stack.make_worker(providers=registry, secret_sink=sink)

    waiting = worker.run_once()
    snapshot = cp_stack.onboarding.snapshot(cp_stack.household.id)
    email_step = next(step for step in snapshot.steps if step.kind is StepKind.EMAIL)

    assert waiting.status == "waiting_user"
    assert email_step.public_status["domain"] == "family.example.test"
    assert email_step.public_status["dns_records"][0]["type"] == "TXT"
    assert len(email_step.public_status["dns_records"][0]["purpose"]) > 128
    assert client.inbox_calls == 0
    resource = cp_stack.database.query_one(
        "SELECT status FROM external_resources WHERE resource_type = 'email_identity'"
    )
    assert resource["status"] == "creating"

    cp_stack.service.check(
        cp_stack.household.id, StepKind.EMAIL, context=cp_stack.context()
    )
    partial = worker.run_once()
    resumed = cp_stack.onboarding.snapshot(cp_stack.household.id)
    resumed_email = next(step for step in resumed.steps if step.kind is StepKind.EMAIL)
    assert partial.status == "waiting_user"
    assert resumed_email.public_status["record_status"]["mx"] is False
    assert resumed_email.public_status["dns_records"] == email_step.public_status["dns_records"]

    client.active = True
    client.checks = {"ownership": True, "mx": True, "spf": True, "dkim": True}
    cp_stack.service.check(
        cp_stack.household.id, StepKind.EMAIL, context=cp_stack.context()
    )
    verified = worker.run_once()

    assert verified.status == "succeeded"
    assert client.inbox_calls == 1
    identity = cp_stack.email_identities.current_for_household(cp_stack.household.id)
    assert identity is not None and identity.status is EmailIdentityStatus.VERIFIED
    assert identity.address == "assistant@" + "family.example.test"
    namespace = cp_stack.database.query_one(
        "SELECT id, external_id_ciphertext, encryption_key_version"
        " FROM external_resources WHERE resource_type = 'secret_namespace'"
    )
    runtime_ref = cp_stack.jobs.decrypt_json(
        "external_resources",
        namespace["id"],
        "external_id",
        namespace["external_id_ciphertext"],
        namespace["encryption_key_version"],
    )
    assert sink.get(runtime_ref, NERVE_SECRET_BINDING) is not None


def test_byo_reload_and_fresh_session_resume_same_dns_state(cp_stack) -> None:
    cp_stack.complete_profile()
    select_byo_domain(cp_stack)
    client = FakeByoNerveAdmin()
    registry = ProviderRegistry()
    registry.register("nerve-byo-domain", NerveByoDomainProvisioner(client))
    worker = cp_stack.make_worker(providers=registry)
    assert worker.run_once().status == "waiting_user"
    before = cp_stack.onboarding.snapshot(cp_stack.household.id)
    before_email = next(step for step in before.steps if step.kind is StepKind.EMAIL)
    fresh_session = cp_stack.sessions.issue(cp_stack.account.id, now=BASE_TIME + 101)

    cp_stack.service.check(
        cp_stack.household.id,
        StepKind.EMAIL,
        context=cp_stack.context(
            key="fresh-session-domain-check",
            session_id=fresh_session.id,
            expected_version=before.version,
        ),
    )
    assert worker.run_once().status == "waiting_user"
    after = cp_stack.onboarding.snapshot(cp_stack.household.id)
    after_email = next(step for step in after.steps if step.kind is StepKind.EMAIL)

    assert after_email.public_status["dns_records"] == before_email.public_status[
        "dns_records"
    ]
    assert cp_stack.database.query_one(
        "SELECT count(*) AS count FROM email_address_reservations"
        " WHERE household_id = ?",
        (cp_stack.household.id,),
    )["count"] == 1

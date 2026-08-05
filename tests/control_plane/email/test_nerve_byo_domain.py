from __future__ import annotations

import sqlite3

import pytest

from control_plane.email.models import EmailIdentityStatus, EmailOption
from control_plane.models import StepKind
from control_plane.providers.email.nerve_byo_domain import NerveByoDomainProvisioner
from control_plane.providers.email.nerve_managed import NERVE_SECRET_BINDING
from control_plane.provisioning.contracts import OutcomeUnknown, ProviderRegistry
from control_plane.provisioning.secrets import InMemorySecretSink


class FakeByoNerveAdmin:
    def __init__(self) -> None:
        self.active = False
        self.checks = {"ownership": False, "mx": False, "spf": False, "dkim": False}
        self.deleted: list[str] = []
        self.inbox_calls = 0
        self.fail_domain_delete_once = False

    def ensure_org(self, *, household_id):
        return {"org_id": f"org-{household_id}"}

    def get_org(self, *, household_id):
        return {} if f"/v1/orgs/org-{household_id}" in self.deleted else {
            "org_id": f"org-{household_id}"
        }

    def ensure_domain(self, *, org_id, domain, external_ref):
        return {"domain": {
            "id": "domain-1",
            "domain": domain,
            "external_ref": external_ref,
            "status": "active" if self.active else "pending_dns",
        }}

    def domain_dns(self, *, org_id, domain_id):
        return {"domain_id": domain_id, "dns_records": [{
            "type": "TXT",
            "host": "_nerve.family.example.test",
            "value": "synthetic-domain-proof",
            "purpose": "ownership",
            "required": True,
        }]}

    def verify_domain(self, *, org_id, domain_id):
        return {
            "domain": {"id": domain_id, "status": "active" if self.active else "pending_dns"},
            "checks": self.checks,
        }

    def ensure_inbox(self, *, org_id, address, external_ref, domain_id=None):
        self.inbox_calls += 1
        return {"inbox": {
            "id": "inbox-1",
            "address": address,
            "external_ref": external_ref,
            "org_domain_id": domain_id,
        }}

    def issue_key(self, *, org_id, external_ref):
        return {
            "id": "key-1",
            "key": "synthetic-byo-key",
            "secret_available": True,
            "external_ref": external_ref,
        }

    def ensure_webhook(self, *, org_id, url, external_ref):
        return {
            "id": "webhook-1",
            "secret": "synthetic-byo-signing-key",
            "secret_available": True,
            "external_ref": external_ref,
        }

    def rotate_webhook(self, *, org_id, webhook_id):
        return {"id": webhook_id, "secret": "synthetic-byo-signing-key"}

    def delete(self, path, *, org_id=None):
        del org_id
        if self.fail_domain_delete_once and path == "/v1/domains/domain-1":
            self.fail_domain_delete_once = False
            raise OutcomeUnknown("synthetic provider unavailable")
        self.deleted.append(path)


class LostDomainResponseNerveAdmin(FakeByoNerveAdmin):
    def __init__(self) -> None:
        super().__init__()
        self.domain_calls = 0

    def ensure_domain(self, *, org_id, domain, external_ref):
        self.domain_calls += 1
        if self.domain_calls == 1:
            raise OutcomeUnknown("synthetic lost domain response")
        return super().ensure_domain(
            org_id=org_id, domain=domain, external_ref=external_ref
        )


def _select(cp_stack) -> None:
    cp_stack.service.byo_domain_provider = "nerve-byo-domain"
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        {
            "kind": "family_domain",
            "domain": "family.example.test",
            "local_part": "assistant",
        },
        context=cp_stack.context(),
    )


def test_dns_records_resume_until_all_checks_are_active(cp_stack) -> None:
    cp_stack.complete_profile()
    _select(cp_stack)
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


def test_reset_while_waiting_removes_domain_intent_before_org(cp_stack) -> None:
    cp_stack.complete_profile()
    _select(cp_stack)
    client = FakeByoNerveAdmin()
    registry = ProviderRegistry()
    registry.register("nerve-byo-domain", NerveByoDomainProvisioner(client))
    worker = cp_stack.make_worker(providers=registry)
    assert worker.run_once().status == "waiting_user"

    cp_stack.service.reset_from(
        cp_stack.household.id, StepKind.EMAIL, context=cp_stack.context()
    )
    cleaned = worker.run_once()

    assert cleaned.status == "succeeded"
    assert client.deleted == [
        "/v1/domains/domain-1",
        f"/v1/orgs/org-{cp_stack.household.id}",
    ]


def test_same_domain_address_cannot_be_reserved_by_another_household(cp_stack) -> None:
    cp_stack.complete_profile()
    _select(cp_stack)
    other_account = cp_stack.accounts.create_verified("other-domain-owner@family.test")
    other_household = cp_stack.households.create_for_owner(other_account.id)

    with cp_stack.database.write() as connection, pytest.raises(sqlite3.IntegrityError):
        cp_stack.email_identities.create_selected(
            connection,
            household_id=other_household.id,
            option=EmailOption.OWN_DOMAIN,
            address="ASSISTANT@family.example.test",
        )


def test_provider_unavailable_during_domain_delete_stays_reconcilable(cp_stack) -> None:
    cp_stack.complete_profile()
    _select(cp_stack)
    client = FakeByoNerveAdmin()
    registry = ProviderRegistry()
    registry.register("nerve-byo-domain", NerveByoDomainProvisioner(client))
    worker = cp_stack.make_worker(providers=registry)
    assert worker.run_once().status == "waiting_user"
    cp_stack.service.reset_from(
        cp_stack.household.id, StepKind.EMAIL, context=cp_stack.context()
    )
    client.fail_domain_delete_once = True

    unknown = worker.run_once()
    recovered = worker.reconcile(unknown.job_id)

    assert unknown.status == "outcome_unknown"
    assert recovered.status == "succeeded"
    assert client.deleted[-2:] == [
        "/v1/domains/domain-1",
        f"/v1/orgs/org-{cp_stack.household.id}",
    ]


def test_lost_domain_create_response_reconciles_to_same_dns_intent(cp_stack) -> None:
    cp_stack.complete_profile()
    _select(cp_stack)
    client = LostDomainResponseNerveAdmin()
    registry = ProviderRegistry()
    registry.register("nerve-byo-domain", NerveByoDomainProvisioner(client))
    worker = cp_stack.make_worker(providers=registry)

    unknown = worker.run_once()
    waiting = worker.reconcile(unknown.job_id)

    assert unknown.status == "outcome_unknown"
    assert waiting.status == "waiting_user"
    assert client.domain_calls == 2
    snapshot = cp_stack.onboarding.snapshot(cp_stack.household.id)
    email_step = next(step for step in snapshot.steps if step.kind is StepKind.EMAIL)
    assert email_step.public_status["dns_records"][0]["value"] == (
        "synthetic-domain-proof"
    )

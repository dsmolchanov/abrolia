from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from control_plane.db import ControlPlaneDatabase
from control_plane.email.models import EmailIdentityStatus, EmailOption
from control_plane.email.repository import EmailIdentityRepository
from control_plane.models import StepKind
from control_plane.providers.email.nerve_byo_domain import NerveByoDomainProvisioner
from control_plane.providers.email.nerve_managed import NERVE_SECRET_BINDING
from control_plane.provisioning.contracts import (
    OutcomeUnknown,
    ProviderRegistry,
    ProviderWaiting,
)
from control_plane.provisioning.fakes import DryRunRuntimeProvisioner
from control_plane.provisioning.secrets import InMemorySecretSink

ORG_ID = "10000000-0000-4000-8000-000000000001"
DOMAIN_ID = "10000000-0000-4000-8000-000000000002"
INBOX_ID = "10000000-0000-4000-8000-000000000003"
KEY_ID = "10000000-0000-4000-8000-000000000004"
WEBHOOK_ID = "10000000-0000-4000-8000-000000000005"


class FakeByoNerveAdmin:
    def __init__(self) -> None:
        self.active = False
        self.checks = {"ownership": False, "mx": False, "spf": False, "dkim": False}
        self.deleted: list[str] = []
        self.inbox_calls = 0
        self.fail_domain_delete_once = False
        self.dns_records = [{
            "type": "TXT",
            "host": "_nerve.family.example.test",
            "value": "synthetic-domain-proof",
            "purpose": (
                "DMARC policy. Required by Gmail and other major providers for reliable "
                "inbox delivery. Start with p=none, then tighten to p=quarantine or "
                "p=reject once delivery is confirmed."
            ),
            "required": True,
        }]

    def ensure_org(self, *, household_id):
        return {"org_id": ORG_ID}

    def get_org(self, *, household_id):
        return {} if f"/v1/orgs/{ORG_ID}" in self.deleted else {"org_id": ORG_ID}

    def ensure_domain(self, *, org_id, domain, external_ref):
        return {"domain": {
            "id": DOMAIN_ID,
            "domain": domain,
            "external_ref": external_ref,
            "status": "active" if self.active else "pending_dns",
        }}

    def domain_dns(self, *, org_id, domain_id):
        return {"domain_id": domain_id, "dns_records": self.dns_records}

    def verify_domain(self, *, org_id, domain_id):
        return {
            "domain": {"id": domain_id, "status": "active" if self.active else "pending_dns"},
            "checks": self.checks,
        }

    def ensure_inbox(self, *, org_id, address, external_ref, domain_id=None):
        self.inbox_calls += 1
        return {"inbox": {
            "id": INBOX_ID,
            "address": address,
            "external_ref": external_ref,
            "org_domain_id": domain_id,
        }}

    def issue_key(self, *, org_id, external_ref):
        return {
            "id": KEY_ID,
            "key": "synthetic-byo-key",
            "secret_available": True,
            "external_ref": external_ref,
        }

    def list_keys(self, *, org_id):
        del org_id
        return []

    def ensure_webhook(self, *, org_id, url, external_ref):
        return {
            "id": WEBHOOK_ID,
            "secret": "synthetic-byo-signing-key",
            "secret_available": True,
            "external_ref": external_ref,
        }

    def rotate_webhook(self, *, org_id, webhook_id):
        return {"id": webhook_id, "secret": "synthetic-byo-signing-key"}

    def delete(self, path, *, org_id=None):
        del org_id
        if self.fail_domain_delete_once and path == f"/v1/domains/{DOMAIN_ID}":
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


class WrongMailboxNerveAdmin(FakeByoNerveAdmin):
    def ensure_inbox(self, *, org_id, address, external_ref, domain_id=None):
        envelope = super().ensure_inbox(
            org_id=org_id,
            address=address,
            external_ref=external_ref,
            domain_id=domain_id,
        )
        envelope["inbox"]["address"] = "assistant@other.example.test"
        return envelope


class LostVerifyResponseNerveAdmin(FakeByoNerveAdmin):
    def __init__(self) -> None:
        super().__init__()
        self.lose_next_verify = True

    def verify_domain(self, *, org_id, domain_id):
        if self.lose_next_verify:
            self.lose_next_verify = False
            raise OutcomeUnknown("synthetic lost verification response")
        return super().verify_domain(org_id=org_id, domain_id=domain_id)


class DoubleLostKeyResponseNerveAdmin(FakeByoNerveAdmin):
    def __init__(self) -> None:
        super().__init__()
        self.active = True
        self._key_calls = 0
        self.keys: dict[str, dict[str, object]] = {}

    def issue_key(self, *, org_id, external_ref):
        del org_id
        self._key_calls += 1
        key = self.keys.get(external_ref)
        if key is None:
            key = {
                "id": f"10000000-0000-4000-8000-{10 + self._key_calls:012d}",
                "external_ref": external_ref,
            }
            self.keys[external_ref] = key
        response = {**key, "secret_available": self._key_calls > 2}
        if self._key_calls > 2:
            response["key"] = "synthetic-recovered-byo-key"
        return response

    def list_keys(self, *, org_id):
        del org_id
        return [dict(item) for item in self.keys.values()]

    def delete(self, path, *, org_id=None):
        if path.startswith("/v1/keys/"):
            key_id = path.rsplit("/", 1)[-1]
            self.keys = {
                ref: key for ref, key in self.keys.items() if key["id"] != key_id
            }
        super().delete(path, org_id=org_id)


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


def test_dns_verification_polls_with_persisted_backoff_then_finishes(cp_stack) -> None:
    cp_stack.complete_profile()
    _select(cp_stack)
    client = FakeByoNerveAdmin()
    registry = ProviderRegistry()
    registry.register("nerve-byo-domain", NerveByoDomainProvisioner(client))
    sink = InMemorySecretSink()

    first = cp_stack.make_worker(
        providers=registry, secret_sink=sink, now=1_800_000_100.0
    ).run_once()
    assert first.status == "waiting_user"
    scheduled = cp_stack.database.query_one(
        "SELECT status, error_code, not_before, attempts FROM provisioning_jobs"
        " WHERE id = ?",
        (first.job_id,),
    )
    assert dict(scheduled) == {
        "status": "waiting_user",
        "error_code": "dns_verification_scheduled",
        "not_before": 1_800_000_130.0,
        "attempts": 1,
    }
    assert cp_stack.make_worker(
        providers=registry, secret_sink=sink, now=1_800_000_129.0
    ).run_once() is None

    still_waiting = cp_stack.make_worker(
        providers=registry, secret_sink=sink, now=1_800_000_130.0
    ).run_once()
    assert still_waiting.status == "waiting_user"
    assert cp_stack.database.query_one(
        "SELECT not_before FROM provisioning_jobs WHERE id = ?", (first.job_id,)
    )["not_before"] == 1_800_000_190.0

    client.active = True
    client.checks = {"ownership": True, "mx": True, "spf": True, "dkim": True}
    finished = cp_stack.make_worker(
        providers=registry, secret_sink=sink, now=1_800_000_190.0
    ).run_once()

    assert finished.status == "succeeded"
    assert finished.job_id == first.job_id
    assert dict(cp_stack.database.query_one(
        "SELECT attempts, not_before FROM provisioning_jobs WHERE id = ?", (first.job_id,)
    )) == {"attempts": 3, "not_before": None}
    assert len(cp_stack.database.query(
        "SELECT id FROM provisioning_jobs WHERE kind = 'email_identity'"
    )) == 1


def test_dns_polling_stops_after_bound_and_keeps_manual_check_available(cp_stack) -> None:
    cp_stack.complete_profile()
    _select(cp_stack)
    client = FakeByoNerveAdmin()
    registry = ProviderRegistry()
    registry.register("nerve-byo-domain", NerveByoDomainProvisioner(client))
    worker = cp_stack.make_worker(providers=registry, now=1_800_000_100.0)
    first = worker.run_once()
    assert first.status == "waiting_user"

    for _ in range(5):
        row = cp_stack.database.query_one(
            "SELECT not_before FROM provisioning_jobs WHERE id = ?", (first.job_id,)
        )
        assert row["not_before"] is not None
        result = cp_stack.make_worker(
            providers=registry, now=row["not_before"]
        ).run_once()
        assert result.status == "waiting_user"

    bounded = cp_stack.database.query_one(
        "SELECT status, error_code, not_before, attempts FROM provisioning_jobs"
        " WHERE id = ?",
        (first.job_id,),
    )
    assert dict(bounded) == {
        "status": "waiting_user",
        "error_code": "dns_manual_check_required",
        "not_before": None,
        "attempts": 6,
    }
    assert cp_stack.make_worker(providers=registry, now=1_900_000_000.0).run_once() is None

    snapshot = cp_stack.onboarding.snapshot(cp_stack.household.id)
    cp_stack.service.check(
        cp_stack.household.id,
        StepKind.EMAIL,
        context=cp_stack.context(expected_version=snapshot.version),
        now=1_900_000_001.0,
    )
    assert cp_stack.make_worker(
        providers=registry, now=1_900_000_002.0
    ).run_once().status == "waiting_user"


def test_provider_cannot_verify_a_mailbox_outside_selected_domain(cp_stack) -> None:
    cp_stack.complete_profile()
    _select(cp_stack)
    client = WrongMailboxNerveAdmin()
    client.active = True
    registry = ProviderRegistry()
    registry.register("nerve-byo-domain", NerveByoDomainProvisioner(client))
    sink = InMemorySecretSink()

    failed = cp_stack.make_worker(
        providers=registry, secret_sink=sink
    ).run_once()

    assert failed.status == "outcome_unknown"
    assert failed.error_code == "outcome_unknown"
    identity = cp_stack.email_identities.current_for_household(cp_stack.household.id)
    assert identity.status is EmailIdentityStatus.OUTCOME_UNKNOWN
    assert identity.address == "assistant@family.example.test"


def test_double_lost_key_response_reconciles_without_orphaned_credentials(
    cp_stack,
) -> None:
    cp_stack.complete_profile()
    _select(cp_stack)
    client = DoubleLostKeyResponseNerveAdmin()
    registry = ProviderRegistry()
    registry.register("nerve-byo-domain", NerveByoDomainProvisioner(client))
    sink = InMemorySecretSink()
    worker = cp_stack.make_worker(providers=registry, secret_sink=sink)

    unknown = worker.run_once()
    recovered = worker.reconcile(unknown.job_id)

    assert unknown.status == "outcome_unknown"
    assert recovered.status == "succeeded"
    assert len(client.keys) == 1
    identity = cp_stack.email_identities.current_for_household(cp_stack.household.id)
    assert list(client.keys)[0].startswith(
        f"arbolia:email:{identity.id}:key:recovery:"
    )
    assert len([path for path in client.deleted if path.startswith("/v1/keys/")]) == 2


def test_malformed_dns_instructions_fail_without_stranding_job(cp_stack) -> None:
    cp_stack.complete_profile()
    _select(cp_stack)
    client = FakeByoNerveAdmin()
    client.dns_records = [{
        "type": "TXT",
        "host": "_nerve.family.example.test",
        "value": "",
        "purpose": "invalid empty record value",
        "required": True,
    }]
    registry = ProviderRegistry()
    registry.register("nerve-byo-domain", NerveByoDomainProvisioner(client))

    failed = cp_stack.make_worker(providers=registry).run_once()

    assert failed.status == "outcome_unknown"
    assert failed.error_code == "outcome_unknown"
    assert cp_stack.jobs.get(failed.job_id).status == "outcome_unknown"


def test_empty_dns_instructions_fail_without_stranding_job(cp_stack) -> None:
    cp_stack.complete_profile()
    _select(cp_stack)
    client = FakeByoNerveAdmin()
    client.dns_records = []
    registry = ProviderRegistry()
    registry.register("nerve-byo-domain", NerveByoDomainProvisioner(client))

    failed = cp_stack.make_worker(providers=registry).run_once()

    assert failed.status == "outcome_unknown"
    assert failed.error_code == "outcome_unknown"
    assert cp_stack.jobs.get(failed.job_id).status == "outcome_unknown"


def test_malformed_pending_address_fails_without_leaving_a_lease(cp_stack) -> None:
    cp_stack.complete_profile()
    _select(cp_stack)
    client = FakeByoNerveAdmin()

    class NullAddressProvisioner(NerveByoDomainProvisioner):
        def ensure(self, intent, idempotency_key):
            try:
                return super().ensure(intent, idempotency_key)
            except ProviderWaiting as error:
                reference = json.loads(error.external_ref)
                reference["address"] = None
                raise ProviderWaiting(
                    public_result=error.public_result,
                    external_ref=json.dumps(
                        reference, sort_keys=True, separators=(",", ":")
                    ),
                ) from error

    registry = ProviderRegistry()
    registry.register("nerve-byo-domain", NullAddressProvisioner(client))

    failed = cp_stack.make_worker(providers=registry).run_once()

    assert failed.status == "outcome_unknown"
    assert failed.error_code == "provider_result_invalid"
    stored = cp_stack.jobs.get(failed.job_id)
    assert stored.status == "outcome_unknown"
    assert cp_stack.database.query_one(
        "SELECT leased_by FROM provisioning_jobs WHERE id = ?", (failed.job_id,)
    )["leased_by"] is None


def test_reset_while_waiting_removes_domain_intent_before_org(cp_stack) -> None:
    cp_stack.complete_profile()
    _select(cp_stack)
    client = FakeByoNerveAdmin()
    registry = ProviderRegistry()
    registry.register("nerve-byo-domain", NerveByoDomainProvisioner(client))
    worker = cp_stack.make_worker(providers=registry)
    assert worker.run_once().status == "waiting_user"
    identity = cp_stack.email_identities.current_for_household(cp_stack.household.id)
    assert identity is not None

    cp_stack.service.reset_from(
        cp_stack.household.id, StepKind.EMAIL, context=cp_stack.context()
    )
    cleaned = worker.run_once()

    assert cleaned.status == "succeeded"
    assert client.deleted == [
        f"/v1/domains/{DOMAIN_ID}",
        f"/v1/orgs/{ORG_ID}",
    ]
    assert cp_stack.email_identities.get(identity.id).status is EmailIdentityStatus.DELETED
    reservation = cp_stack.database.query_one(
        "SELECT status FROM email_address_reservations WHERE email_identity_id = ?",
        (identity.id,),
    )
    assert reservation["status"] == "released"


def test_cancel_during_domain_call_persists_late_waiting_cleanup(cp_stack) -> None:
    cp_stack.complete_profile()
    _select(cp_stack)

    class CancelDuringDomainEnsure(FakeByoNerveAdmin):
        cancelled = False

        def ensure_domain(self, *, org_id, domain, external_ref):
            if not self.cancelled:
                self.cancelled = True
                cp_stack.service.cancel(
                    cp_stack.household.id,
                    context=cp_stack.context(),
                    now=1_800_000_004.0,
                )
            return super().ensure_domain(
                org_id=org_id, domain=domain, external_ref=external_ref
            )

    client = CancelDuringDomainEnsure()
    registry = ProviderRegistry()
    registry.register("nerve-byo-domain", NerveByoDomainProvisioner(client))
    registry.register("dry-run-runtime", DryRunRuntimeProvisioner())
    worker = cp_stack.make_worker(providers=registry)

    late = worker.run_once()

    assert late.status == "outcome_unknown"
    assert late.error_code == "cancel_requires_reconciliation"
    cleanup = cp_stack.database.query_one(
        "SELECT id FROM provisioning_jobs WHERE intent_key = ?",
        (f"{cp_stack.household.id}:late-waiting-cleanup:{late.job_id}",),
    )
    assert cleanup is not None
    cleanup_results = [worker.run_once(), worker.run_once()]
    assert {result.status for result in cleanup_results} == {"pending", "succeeded"}
    assert cp_stack.jobs.get(late.job_id).status == "cancelled"
    identity = cp_stack.database.query_one(
        "SELECT id, status FROM email_identities WHERE household_id = ?",
        (cp_stack.household.id,),
    )
    assert identity["status"] == EmailIdentityStatus.DELETED.value
    reservation = cp_stack.database.query_one(
        "SELECT status FROM email_address_reservations WHERE email_identity_id = ?",
        (identity["id"],),
    )
    assert reservation["status"] == "released"


def test_same_normalized_domain_cannot_be_claimed_by_another_household(cp_stack) -> None:
    cp_stack.complete_profile()
    _select(cp_stack)
    other_account = cp_stack.accounts.create_verified("other-domain-owner@family.test")
    other_household = cp_stack.households.create_for_owner(other_account.id)

    with cp_stack.database.write() as connection, pytest.raises(sqlite3.IntegrityError):
        cp_stack.email_identities.create_selected(
            connection,
            household_id=other_household.id,
            option=EmailOption.OWN_DOMAIN,
            address="other@FAMILY.EXAMPLE.TEST",
        )


def test_concurrent_writers_cannot_claim_the_same_normalized_domain(cp_stack) -> None:
    account_one = cp_stack.accounts.create_verified("domain-race-one@family.test")
    account_two = cp_stack.accounts.create_verified("domain-race-two@family.test")
    household_one = cp_stack.households.create_for_owner(account_one.id)
    household_two = cp_stack.households.create_for_owner(account_two.id)
    first_db = ControlPlaneDatabase(cp_stack.config.database_path, timeout=2)
    second_db = ControlPlaneDatabase(cp_stack.config.database_path, timeout=2)
    repositories = [
        EmailIdentityRepository(first_db, cp_stack.cipher, cp_stack.lookup),
        EmailIdentityRepository(second_db, cp_stack.cipher, cp_stack.lookup),
    ]
    start = threading.Barrier(2)
    outcomes: list[str] = []

    def claim(index: int, household_id: str) -> None:
        try:
            start.wait(2)
            with repositories[index].db.write() as connection:
                repositories[index].create_selected(
                    connection,
                    household_id=household_id,
                    option=EmailOption.OWN_DOMAIN,
                    address=f"assistant-{index}@Race.Example.Test",
                )
            outcomes.append("claimed")
        except sqlite3.IntegrityError:
            outcomes.append("rejected")

    threads = [
        threading.Thread(target=claim, args=(0, household_one.id)),
        threading.Thread(target=claim, args=(1, household_two.id)),
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        assert all(not thread.is_alive() for thread in threads)
        assert sorted(outcomes) == ["claimed", "rejected"]
    finally:
        first_db.close()
        second_db.close()


def test_legacy_domain_claim_without_new_hmac_still_blocks_another_household(
    cp_stack,
) -> None:
    cp_stack.complete_profile()
    _select(cp_stack)
    identity = cp_stack.email_identities.current_for_household(cp_stack.household.id)
    with cp_stack.database.write() as connection:
        connection.execute(
            "UPDATE email_identities SET domain_lookup_hmac = NULL WHERE id = ?",
            (identity.id,),
        )
    other_account = cp_stack.accounts.create_verified("legacy-domain-owner@family.test")
    other_household = cp_stack.households.create_for_owner(other_account.id)

    with cp_stack.database.write() as connection, pytest.raises(sqlite3.IntegrityError):
        cp_stack.email_identities.create_selected(
            connection,
            household_id=other_household.id,
            option=EmailOption.OWN_DOMAIN,
            address="legacy@family.example.test",
        )


def test_owned_domain_repository_requires_address(cp_stack) -> None:
    other_account = cp_stack.accounts.create_verified("missing-domain@family.test")
    other_household = cp_stack.households.create_for_owner(other_account.id)

    with cp_stack.database.write() as connection, pytest.raises(
        ValueError, match="require a mailbox address"
    ):
        cp_stack.email_identities.create_selected(
            connection,
            household_id=other_household.id,
            option=EmailOption.OWN_DOMAIN,
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
    client.fail_domain_delete_once = True
    repeated = worker.reconcile(unknown.job_id)
    recovered = worker.reconcile(unknown.job_id)

    assert unknown.status == "outcome_unknown"
    assert repeated.status == "outcome_unknown"
    assert repeated.error_code == "reconcile_inconclusive"
    assert recovered.status == "succeeded"
    assert client.deleted[-2:] == [
        f"/v1/domains/{DOMAIN_ID}",
        f"/v1/orgs/{ORG_ID}",
    ]


def test_reconnect_waits_for_verified_domain_cleanup_to_finish(cp_stack) -> None:
    cp_stack.complete_profile()
    _select(cp_stack)
    client = FakeByoNerveAdmin()
    client.active = True
    registry = ProviderRegistry()
    registry.register("nerve-byo-domain", NerveByoDomainProvisioner(client))
    worker = cp_stack.make_worker(providers=registry)
    assert worker.run_once().status == "succeeded"

    cp_stack.service.reset_from(
        cp_stack.household.id, StepKind.EMAIL, context=cp_stack.context()
    )
    client.fail_domain_delete_once = True
    unknown = worker.run_once()
    assert unknown.status == "outcome_unknown"

    with pytest.raises(ValueError, match="cleanup must finish"):
        _select(cp_stack)

    assert worker.reconcile(unknown.job_id).status == "succeeded"
    _select(cp_stack)


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


@pytest.mark.parametrize(
    ("verified_during_response_loss", "expected_status"),
    [(False, "waiting_user"), (True, "succeeded")],
)
def test_lost_manual_inspect_reconciles_with_original_stable_reference(
    cp_stack, verified_during_response_loss: bool, expected_status: str
) -> None:
    cp_stack.complete_profile()
    _select(cp_stack)
    client = LostVerifyResponseNerveAdmin()
    registry = ProviderRegistry()
    registry.register("nerve-byo-domain", NerveByoDomainProvisioner(client))
    worker = cp_stack.make_worker(
        providers=registry, secret_sink=InMemorySecretSink()
    )
    assert worker.run_once().status == "waiting_user"
    original = cp_stack.database.query_one(
        "SELECT intent_key FROM provisioning_jobs WHERE kind = 'email_identity'"
        " AND operation = 'ensure'"
    )
    client.active = verified_during_response_loss
    cp_stack.service.check(
        cp_stack.household.id, StepKind.EMAIL, context=cp_stack.context()
    )

    unknown = worker.run_once()
    inspect_request = cp_stack.jobs.request(unknown.job_id)
    inspect_job = cp_stack.jobs.get(unknown.job_id)

    assert unknown.status == "outcome_unknown"
    assert inspect_request["stable_ref"] == original["intent_key"]
    assert inspect_job.intent_key != inspect_request["stable_ref"]
    recovered = worker.reconcile(unknown.job_id)
    assert recovered.status == expected_status

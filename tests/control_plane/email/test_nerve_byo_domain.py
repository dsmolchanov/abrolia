from __future__ import annotations

import json
import sqlite3

import pytest

from control_plane.email.models import EmailIdentityStatus, EmailOption
from control_plane.models import StepKind
from control_plane.privacy.consent import (
    HOUSEHOLD_CONTENT_PURPOSE,
    consent_version_and_sha,
)
from control_plane.privacy.withdraw import ConsentWithdrawalService
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
BASE_TIME = 1_800_000_000.0
_RESTRICTION_VERSION, _RESTRICTION_SHA = consent_version_and_sha(
    "special_category_content_restriction"
)
_HOUSEHOLD_VERSION, _HOUSEHOLD_SHA = consent_version_and_sha(
    "special_category_household_content"
)


class FakeByoNerveAdmin:
    def __init__(self) -> None:
        self.active = False
        self.checks = {"ownership": False, "mx": False, "spf": False, "dkim": False}
        self.deleted: list[str] = []
        self.inbox_calls = 0
        self.fail_domain_delete_once = False
        self.org_external_refs: list[str] = []
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

    def ensure_org(self, *, household_id, identity_id):
        self.org_external_refs.append(
            f"arbolia:household:{household_id}:email:{identity_id}"
        )
        return {"org_id": ORG_ID}

    def get_org(self, *, external_ref):
        self.org_external_refs.append(external_ref)
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


def _select(cp_stack) -> None:
    cp_stack.service.byo_domain_provider = "nerve-byo-domain"
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        {
            "kind": "family_domain",
            "domain": "family.example.test",
            "local_part": "assistant",
            "special_category_restriction_acknowledged": True,
            "special_category_restriction_receipt_id": "10000000-0000-4000-8000-000000000022",
            "special_category_restriction_text_version": _RESTRICTION_VERSION,
            "special_category_restriction_text_sha256": _RESTRICTION_SHA,
            "special_category_household_consent": True,
            "special_category_household_receipt_id": "10000000-0000-4000-8000-000000000032",
            "special_category_household_text_version": _HOUSEHOLD_VERSION,
            "special_category_household_text_sha256": _HOUSEHOLD_SHA,
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


def test_dns_status_polls_automatically_with_bounded_backoff(cp_stack) -> None:
    cp_stack.complete_profile()
    _select(cp_stack)
    client = FakeByoNerveAdmin()
    registry = ProviderRegistry()
    registry.register("nerve-byo-domain", NerveByoDomainProvisioner(client))
    sink = InMemorySecretSink()

    waiting = cp_stack.make_worker(
        providers=registry, secret_sink=sink, now=BASE_TIME + 100
    ).run_once()
    assert waiting.status == "waiting_user"
    scheduled = cp_stack.database.query_one(
        "SELECT operation, status, attempts, not_before FROM provisioning_jobs"
        " WHERE id = ?",
        (waiting.job_id,),
    )
    assert tuple(scheduled) == ("inspect", "waiting_user", 1, BASE_TIME + 130)
    assert cp_stack.make_worker(
        providers=registry, secret_sink=sink, now=BASE_TIME + 129
    ).run_once() is None

    partial = cp_stack.make_worker(
        providers=registry, secret_sink=sink, now=BASE_TIME + 130
    ).run_once()
    assert partial.status == "waiting_user"
    scheduled = cp_stack.database.query_one(
        "SELECT attempts, not_before FROM provisioning_jobs WHERE id = ?",
        (waiting.job_id,),
    )
    assert tuple(scheduled) == (2, BASE_TIME + 190)
    snapshot = cp_stack.onboarding.snapshot(cp_stack.household.id)
    email_step = next(step for step in snapshot.steps if step.kind is StepKind.EMAIL)
    assert email_step.public_status["record_status"]["mx"] is False

    client.active = True
    client.checks = {"ownership": True, "mx": True, "spf": True, "dkim": True}
    assert cp_stack.make_worker(
        providers=registry, secret_sink=sink, now=BASE_TIME + 189
    ).run_once() is None
    verified = cp_stack.make_worker(
        providers=registry, secret_sink=sink, now=BASE_TIME + 190
    ).run_once()
    assert verified.status == "succeeded"
    assert client.inbox_calls == 1


def test_dns_automatic_polling_stops_after_bounded_attempts(cp_stack) -> None:
    cp_stack.complete_profile()
    _select(cp_stack)
    client = FakeByoNerveAdmin()
    registry = ProviderRegistry()
    registry.register("nerve-byo-domain", NerveByoDomainProvisioner(client))

    first = cp_stack.make_worker(providers=registry, now=BASE_TIME + 100).run_once()
    assert first.status == "waiting_user"
    for due_at in (BASE_TIME + 130, BASE_TIME + 190, BASE_TIME + 310, BASE_TIME + 550):
        result = cp_stack.make_worker(providers=registry, now=due_at).run_once()
        assert result.status == "waiting_user"

    stopped = cp_stack.database.query_one(
        "SELECT operation, status, attempts, not_before FROM provisioning_jobs"
        " WHERE id = ?",
        (first.job_id,),
    )
    assert tuple(stopped) == ("inspect", "waiting_user", 5, None)
    assert cp_stack.make_worker(
        providers=registry, now=BASE_TIME + 10_000
    ).run_once() is None


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
        "SELECT id, intent_key FROM provisioning_jobs WHERE kind = 'email_identity'"
        " AND operation = 'inspect'"
    )
    original_request = cp_stack.jobs.request(original["id"])
    assert original_request["stable_ref"] == original["intent_key"]
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


def test_byo_provisioner_never_calls_service_tokens() -> None:
    """B-01 regression for BYO: real NerveAdminClient via MockTransport, no service-tokens."""
    import httpx

    from control_plane.email.models import EmailOption, EmailProvisionIntent
    from control_plane.providers.email.nerve_byo_domain import NerveByoDomainProvisioner
    from control_plane.providers.email.nerve_client import NerveAdminClient, NerveAdminSettings

    captured: list[tuple[str, str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.method
        path = request.url.path
        headers = {k.lower(): v for k, v in request.headers.items()}
        captured.append((method, path, headers))
        assert headers.get("x-api-key") == "synthetic-bootstrap-admin-key"
        assert "service-tokens" not in path
        if path == "/v1/orgs" and method == "POST":
            return httpx.Response(200, json={"org_id": ORG_ID})
        if path == "/v1/domains" and method == "POST":
            return httpx.Response(
                200,
                json={"domain": {"id": DOMAIN_ID, "domain": "family.example.test", "external_ref": "ref", "status": "active"}},
            )
        if path == "/v1/domains/dns" and method == "GET":
            return httpx.Response(200, json={"domain_id": DOMAIN_ID, "dns_records": []})
        if path == "/v1/domains/verify" and method == "POST":
            return httpx.Response(200, json={"domain": {"id": DOMAIN_ID, "status": "active"}, "checks": {"ownership": True, "mx": True, "spf": True, "dkim": True}})
        if path == "/v1/inboxes" and method == "POST":
            return httpx.Response(200, json={"inbox": {"id": INBOX_ID, "address": "assistant@family.example.test", "external_ref": "ref"}})
        if path == "/v1/keys" and method == "POST":
            return httpx.Response(200, json={"id": KEY_ID, "key": "synthetic-byo-key", "secret_available": True})
        if path == "/v1/webhooks" and method == "POST":
            return httpx.Response(200, json={"id": WEBHOOK_ID, "secret": "synthetic-byo-signing-key", "secret_available": True})
        return httpx.Response(404, json={})

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    settings = NerveAdminSettings(
        base_url="https://nerve.example.test",
        admin_key="synthetic-bootstrap-admin-key",
        platform_org_id="20000000-0000-4000-8000-000000000001",
        platform_domain_id="30000000-0000-4000-8000-000000000001",
    )
    nerve_client = NerveAdminClient(settings, client=client)
    intent = EmailProvisionIntent(
        identity_id="byo-identity-1",
        household_id="household-1",
        option=EmailOption.OWN_DOMAIN,
        selection={"kind": "family_domain", "domain": "family.example.test", "local_part": "assistant"},
        secret_namespace_ref="ns",
    ).model_dump(mode="json")
    result = NerveByoDomainProvisioner(nerve_client).ensure(intent, "household-1:email_identity:byo-identity-1:own_domain:1")
    assert result is not None
    bootstrap = [(m, p) for m, p, _ in captured if p.startswith("/v1/")]
    assert ("POST", "/v1/orgs") in bootstrap
    assert ("POST", "/v1/domains") in bootstrap
    assert ("POST", "/v1/inboxes") in bootstrap
    assert ("POST", "/v1/keys") in bootstrap
    assert ("POST", "/v1/webhooks") in bootstrap
    assert not any("service-tokens" in p for _, p, _ in captured)


def test_withdrawal_during_domain_call_persists_late_waiting_cleanup(cp_stack) -> None:
    """The other half of the in-flight withdrawal race: `ProviderWaiting`.

    A BYO domain call comes back "waiting for the owner to publish DNS" while
    having already created the upstream domain, and the reference arrives only
    with that response. `_handle_provider_waiting` records it — and used to
    attach no teardown, because `_schedule_cancelled_waiting_cleanup` fired only
    for the two reasons it listed, `cancel` and `reset`. Withdrawal was a third,
    so a withdrawn household kept a domain nothing would ever delete.

    Its sibling — a READY result arriving after withdrawal — is
    `test_an_inbox_that_arrives_after_withdrawal_is_torn_down` in
    `tests/control_plane/test_consent_withdrawal.py`. Both barriers are the
    provider call itself, so the late response cannot precede the withdrawal.
    """
    cp_stack.complete_profile()
    _select(cp_stack)

    class WithdrawDuringDomainEnsure(FakeByoNerveAdmin):
        withdrawn = False

        def ensure_domain(self, *, org_id, domain, external_ref):
            if not self.withdrawn:
                self.withdrawn = True
                ConsentWithdrawalService(
                    cp_stack.database,
                    jobs=cp_stack.jobs,
                    onboarding=cp_stack.service,
                ).withdraw(
                    cp_stack.household.id,
                    HOUSEHOLD_CONTENT_PURPOSE,
                    now=BASE_TIME + 4,
                )
            return super().ensure_domain(
                org_id=org_id, domain=domain, external_ref=external_ref
            )

    client = WithdrawDuringDomainEnsure()
    registry = ProviderRegistry()
    registry.register("nerve-byo-domain", NerveByoDomainProvisioner(client))
    registry.register("dry-run-runtime", DryRunRuntimeProvisioner())
    worker = cp_stack.make_worker(providers=registry)

    late = worker.run_once()

    assert late.status == "outcome_unknown"
    assert late.error_code == "withdrawal_requires_reconciliation"
    cleanup = cp_stack.database.query_one(
        "SELECT id FROM provisioning_jobs WHERE intent_key = ?",
        (f"{cp_stack.household.id}:late-waiting-cleanup:{late.job_id}",),
    )
    assert cleanup is not None, "the late domain reference got no teardown job"

    # A scheduled teardown nobody can drain is not a disconnection.
    for _ in range(6):
        if worker.run_once() is None:
            break
    identity = cp_stack.database.query_one(
        "SELECT status FROM email_identities WHERE household_id = ?",
        (cp_stack.household.id,),
    )
    assert identity is None or identity["status"] == EmailIdentityStatus.DELETED.value
    live = cp_stack.database.query(
        "SELECT id FROM external_resources WHERE household_id = ?"
        " AND resource_type = 'email_identity'"
        " AND status NOT IN ('deleting','deleted')",
        (cp_stack.household.id,),
    )
    assert live == [], "a domain created during withdrawal is still live"

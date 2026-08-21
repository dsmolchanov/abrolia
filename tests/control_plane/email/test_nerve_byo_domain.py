from __future__ import annotations

import json

import pytest

from control_plane.email.models import EmailIdentityStatus, EmailOption
from control_plane.models import StepKind
from control_plane.privacy.consent import HOUSEHOLD_CONTENT_PURPOSE
from control_plane.privacy.withdraw import ConsentWithdrawalService
from control_plane.providers.email.nerve_byo_domain import NerveByoDomainProvisioner
from control_plane.providers.email.nerve_managed import NerveManagedEmailProvisioner
from control_plane.provisioning.contracts import ProviderRegistry, ProviderWaiting
from control_plane.provisioning.fakes import DryRunRuntimeProvisioner
from control_plane.provisioning.secrets import InMemorySecretSink
from tests.control_plane.email.byo_support import (
    BASE_TIME,
    DOMAIN_ID,
    INBOX_ID,
    KEY_ID,
    ORG_ID,
    WEBHOOK_ID,
    FakeByoNerveAdmin,
    HardDeleteGrantNerveAdmin,
    LostCommittedStepResponseNerveAdmin,
    LostDomainResponseNerveAdmin,
    LostVerifyResponseNerveAdmin,
    WrongMailboxNerveAdmin,
    select_byo_domain,
)


def test_provider_cannot_verify_a_mailbox_outside_selected_domain(cp_stack) -> None:
    cp_stack.complete_profile()
    select_byo_domain(cp_stack)
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
    select_byo_domain(cp_stack)
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
    select_byo_domain(cp_stack)
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
    select_byo_domain(cp_stack)
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
    select_byo_domain(cp_stack)
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
    select_byo_domain(cp_stack)

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
    select_byo_domain(cp_stack)
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
    select_byo_domain(cp_stack)
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
        select_byo_domain(cp_stack)

    assert worker.reconcile(unknown.job_id).status == "succeeded"
    select_byo_domain(cp_stack)


def test_lost_domain_create_response_reconciles_to_same_dns_intent(cp_stack) -> None:
    cp_stack.complete_profile()
    select_byo_domain(cp_stack)
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


@pytest.mark.parametrize("lost_step", ["org", "domain", "inbox", "key", "webhook"])
def test_byo_lost_committed_response_matrix_converges_without_duplicates(
    cp_stack, lost_step: str
) -> None:
    cp_stack.complete_profile()
    select_byo_domain(cp_stack)
    client = LostCommittedStepResponseNerveAdmin(lost_step)
    client.active = True
    registry = ProviderRegistry()
    registry.register("nerve-byo-domain", NerveByoDomainProvisioner(client))
    worker = cp_stack.make_worker(
        providers=registry, secret_sink=InMemorySecretSink()
    )

    unknown = worker.run_once()
    recovered = worker.reconcile(unknown.job_id)

    assert unknown.status == "outcome_unknown"
    assert recovered.status == "succeeded"
    assert client.step_calls[lost_step] == 2
    identity = cp_stack.email_identities.current_for_household(cp_stack.household.id)
    assert identity.provider_resource_refs == {
        "domain_id": DOMAIN_ID,
        "inbox_id": INBOX_ID,
        "key_id": KEY_ID,
        "org_id": ORG_ID,
        "webhook_id": WEBHOOK_ID,
    }


def test_byo_reconnect_contract_hard_deletes_old_domain_grant() -> None:
    client = HardDeleteGrantNerveAdmin()
    provider = NerveManagedEmailProvisioner(client)

    def intent(identity_id: str) -> dict:
        return {
            "identity_id": identity_id,
            "household_id": "synthetic-household",
            "option": EmailOption.MANAGED_ABROLIA.value,
            "selection": {
                "kind": "abrolia_managed",
                "local_part": f"agent-{identity_id}",
            },
            "secret_namespace_ref": "synthetic-runtime",
        }

    first = provider.ensure(intent("generation-a"), "stable-generation-a")
    first_grant = first.public_result["provider_refs"]["grant_id"]
    provider.deprovision(first.external_ref)
    second = provider.ensure(intent("generation-b"), "stable-generation-b")
    second_grant = second.public_result["provider_refs"]["grant_id"]

    assert f"/v1/domain-grants/{first_grant}" in client.deleted
    assert second_grant != first_grant


@pytest.mark.parametrize(
    ("verified_during_response_loss", "expected_status"),
    [(False, "waiting_user"), (True, "succeeded")],
)
def test_lost_manual_inspect_reconciles_with_original_stable_reference(
    cp_stack, verified_during_response_loss: bool, expected_status: str
) -> None:
    cp_stack.complete_profile()
    select_byo_domain(cp_stack)
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
    select_byo_domain(cp_stack)

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

"""The production path must establish a real web seat without simulated channels."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from control_plane.api.app import create_app
from control_plane.auth.mailer import MemoryMailer
from control_plane.config import ControlPlaneConfig
from control_plane.container import ControlPlaneContainer
from control_plane.models import StepKind, web_channel_identity
from control_plane.onboarding.contracts import WorkflowConflict
from control_plane.providers.email.nerve_managed import NerveManagedEmailProvisioner
from control_plane.provisioning.contracts import InspectState, ProviderRejected
from control_plane.provisioning.local_configuration import (
    LocalConfigurationProvisioner,
    production_provider_registry,
)
from tests.control_plane.conftest import BASE_TIME, APIHarness
from tests.control_plane.email.test_nerve_managed import MANAGED_SELECTION, FakeNerveAdmin


def _email_ready(stack, monkeypatch):
    # The namespace already exists; its external creation is outside this change.
    stack.complete_profile()
    stack.service.synthetic_only = False
    stack.onboarding.synthetic_only = False
    stack.service.real_email_enabled = True
    stack.service.real_email_all_households = True
    stack.service.email_provider = "nerve-managed"
    monkeypatch.setenv("ABROLIA_REAL_EMAIL_ENABLED", "1")
    registry = production_provider_registry()
    registry.register("local-configuration", LocalConfigurationProvisioner())
    registry.register("nerve-managed", NerveManagedEmailProvisioner(FakeNerveAdmin()))
    stack.service.select(stack.household.id, StepKind.EMAIL, MANAGED_SELECTION,
                         context=stack.context(), now=BASE_TIME + 3)
    worker = stack.make_worker(providers=registry)
    assert worker.run_once().status == "succeeded"
    return registry, worker


@pytest.mark.parametrize("recover", [None, StepKind.WHATSAPP, StepKind.PRIMARY_CHANNEL])
def test_real_email_to_web_survives_restart_and_binds_only_its_owner(
    cp_stack, monkeypatch, recover,
):
    stack = cp_stack
    registry, worker = _email_ready(stack, monkeypatch)
    for kind, selection in [
        (StepKind.WHATSAPP, {"kind": "disabled"}),
        (StepKind.PRIMARY_CHANNEL, {
            "kind": "web", "actor_id": "web-owner.somebody-else", "chat_id": "foreign-room",
        }),
    ]:
        stack.service.select(stack.household.id, kind, selection,
                             context=stack.context(), now=BASE_TIME + 10)
        job = stack.database.query_one(
            "SELECT id, provider FROM provisioning_jobs WHERE household_id = ? AND status = 'pending'",
            (stack.household.id,),
        )
        assert job["provider"] == "local-configuration"
        if recover == kind:
            leased = stack.jobs.lease("crashed-worker", now=BASE_TIME + 20)
            assert leased.id == job["id"]
            registry.get("local-configuration").ensure(stack.jobs.request(leased.id), leased.intent_key)
            with stack.database.write() as connection:
                stack.jobs.settle(connection, leased.id, status="outcome_unknown",
                                  error_code="provider_outcome_unknown", now=BASE_TIME + 21)
            # A new provider/worker has no process-local memory to recover from.
            restarted = production_provider_registry()
            restarted.register("local-configuration", LocalConfigurationProvisioner())
            worker = stack.make_worker(providers=restarted)
            assert worker.reconcile(leased.id).status == "succeeded"
        else:
            assert worker.run_once().status == "succeeded"
    snapshot = stack.onboarding.snapshot(stack.household.id)
    assert not snapshot.synthetic_only
    assert snapshot.state == "runtime_provisioning"
    result = stack.onboarding.result(snapshot.workflow_id, StepKind.WHATSAPP)
    assert result["public_result"] == {"mode": "disabled", "connected": False}
    result = stack.onboarding.result(snapshot.workflow_id, StepKind.PRIMARY_CHANNEL)
    actor, chat = web_channel_identity(stack.household.id)
    assert result["public_result"] == {"channel": "web", "actor_id": actor, "chat_id": chat}
    bindings = stack.database.query(
        "SELECT channel, account_id FROM channel_bindings WHERE household_id = ?",
        (stack.household.id,),
    )
    assert [(row["channel"], row["account_id"]) for row in bindings] == [("web", stack.account.id)]
    assert stack.database.query(
        "SELECT id FROM consent_receipts WHERE household_id = ? AND purpose LIKE 'whatsapp%'",
        (stack.household.id,),
    ) == []
    runtime = stack.database.query_one(
        "SELECT id FROM provisioning_jobs WHERE household_id = ? AND operation = 'ensure_runtime'",
        (stack.household.id,),
    )
    manifest = stack.jobs.request(runtime["id"])["manifest"]
    assert manifest["channels"]["primary"] == "web"
    assert manifest["email"]["provider_kind"] == "nerve"
    assert "whatsapp_channel_privacy" not in manifest["consent"]["required_purposes"]
    assert "whatsapp" not in manifest["provider_refs"]


@pytest.mark.parametrize("kind, selection", [
    (StepKind.WHATSAPP, {"kind": "shared_abrolia"}),
    (StepKind.WHATSAPP, {"kind": "dedicated_number"}),
    (StepKind.PRIMARY_CHANNEL, {"kind": "telegram"}),
    (StepKind.PRIMARY_CHANNEL, {"kind": "whatsapp"}),
])
def test_production_refuses_unimplemented_channels_before_creating_jobs(cp_stack, kind, selection):
    cp_stack.service.synthetic_only = False
    before = cp_stack.database.query("SELECT id FROM provisioning_jobs")
    with pytest.raises(WorkflowConflict):
        cp_stack.service.select(cp_stack.household.id, kind, selection, context=cp_stack.context())
    assert cp_stack.database.query("SELECT id FROM provisioning_jobs") == before


@pytest.mark.parametrize("name", ["fake-email", "fake-channel", "fake-whatsapp", "dry-run-runtime"])
def test_retired_providers_refuse_forward_work_but_allow_cleanup(name):
    provider = production_provider_registry().get(name)
    for operation in (provider.ensure, provider.reconcile):
        with pytest.raises(ProviderRejected, match="disabled in production"):
            operation({}, "old-intent")
    assert provider.deprovision("synthetic:old:resource").state == InspectState.ABSENT
    with pytest.raises(ProviderRejected):
        provider.deprovision("real-resource")


def test_cancel_during_local_configuration_does_not_activate_a_channel(cp_stack, monkeypatch):
    stack = cp_stack
    _, worker = _email_ready(stack, monkeypatch)
    stack.service.select(stack.household.id, StepKind.WHATSAPP, {"kind": "disabled"},
                         context=stack.context(), now=BASE_TIME + 10)
    leased = stack.jobs.lease("crashed-worker", now=BASE_TIME + 20)
    stack.service.cancel(stack.household.id, context=stack.context(), now=BASE_TIME + 21)
    worker.reconcile(leased.id)
    worker.drain()
    assert stack.onboarding.snapshot(stack.household.id).state == "cancelled"
    assert stack.jobs.get(leased.id).status == "cancelled"
    assert not stack.database.query(
        "SELECT id FROM channel_bindings WHERE household_id = ?", (stack.household.id,),
    )


@pytest.mark.parametrize("transport", ["form", "json"])
def test_production_ui_and_no_js_form_use_the_same_local_choice(tmp_path, transport):
    config = replace(ControlPlaneConfig.for_test(tmp_path), synthetic_only=False,
                     real_family_data_enabled=True)
    mailer = MemoryMailer()
    with (
        ControlPlaneContainer.build(config, mailer=mailer) as active,
        TestClient(create_app(active_container=active), base_url=config.public_origin) as client,
    ):
        harness = APIHarness(config, active, mailer, client)
        world = harness.create_principal()
        harness.authenticate(world)
        page = client.get("/onboarding")
        assert page.status_code == 200
        assert "Synthetic staging" not in page.text
        assert 'data-kind="disabled"' in page.text
        assert 'data-kind="web"' in page.text
        for choice in ["telegram", "whatsapp", "shared_abrolia", "dedicated_number"]:
            assert f'data-kind="{choice}"' not in page.text
        assert "synthetic" not in client.get("/start").text.lower()
        assert client.get("/healthz").json()["mode"] == "production"
        assert active.onboarding_repository.snapshot(world.household.id).synthetic_only is False
        # Move to the relevant local choice; email is covered by the test above.
        workflow = active.onboarding_repository.workflow_for_household(world.household.id)
        with active.database.write() as connection:
            connection.execute("UPDATE households SET status = 'onboarding' WHERE id = ?",
                               (world.household.id,))
            connection.execute("UPDATE onboarding_workflows SET state = 'in_progress', "
                               "current_step = 'whatsapp_identity' WHERE id = ?", (workflow.id,))
            connection.execute("UPDATE onboarding_steps SET status = 'available' "
                               "WHERE workflow_id = ? AND kind = 'whatsapp_identity'", (workflow.id,))
        if transport == "form":
            response = client.post("/onboarding/select/whatsapp_identity", headers={
                "Origin": config.public_origin,
            }, data={"kind": "disabled", "csrf_token": world.session.csrf_token,
                     "version": workflow.version, "idempotency_key": "production-form-command"},
                follow_redirects=False)
            assert response.status_code == 303
            assert response.headers["location"] == "/onboarding"
        else:
            response = client.post("/api/v1/onboarding/steps/whatsapp_identity/select",
                headers={**harness.mutation_headers, "If-Match": str(workflow.version),
                         "Idempotency-Key": "production-json-command"}, json={"kind": "disabled"})
            assert response.status_code == 200
            assert response.json()["synthetic_only"] is False
        assert active.worker.run_once().status == "succeeded"
        assert active.onboarding_repository.snapshot(world.household.id).current_step == "primary_channel"


def test_idempotent_response_reports_current_mode(cp_stack):
    context = cp_stack.context()
    profile = cp_stack.valid_profile()
    first = cp_stack.service.save_profile(cp_stack.household.id, profile,
                                          context=context, now=BASE_TIME + 1)
    assert first.snapshot.synthetic_only
    cp_stack.service.synthetic_only = False
    replay = cp_stack.service.save_profile(cp_stack.household.id, profile,
                                           context=context, now=BASE_TIME + 2)
    assert replay.replayed
    assert not replay.snapshot.synthetic_only


@pytest.mark.parametrize("listed", [False, True])
def test_the_gmail_card_is_rendered_only_for_an_account_that_can_connect(
    tmp_path, monkeypatch, listed,
):
    """The container wires the connect policy into the page, not just the service.

    With the Gmail switch on and real Gmail off, an account outside the
    test-user list must not see the card it would be refused at connect.
    """
    monkeypatch.setenv("ABROLIA_GMAIL_ENABLED", "1")
    email = "api-owner@family.test"
    config = replace(
        ControlPlaneConfig.for_test(tmp_path),
        synthetic_only=False,
        real_family_data_enabled=True,
        google_oauth_client_id="synthetic-client.apps.example.test",
        google_oauth_client_secret="synthetic-client-secret",
        google_oauth_test_users=(email,) if listed else ("someone-else@family.test",),
    )
    with (
        ControlPlaneContainer.build(config, mailer=MemoryMailer()) as active,
        TestClient(create_app(active_container=active), base_url=config.public_origin) as client,
    ):
        harness = APIHarness(config, active, MemoryMailer(), client)
        world = harness.create_principal(email)
        harness.authenticate(world)
        page = client.get("/onboarding")
        assert page.status_code == 200
        assert ('data-kind="gmail_agent"' in page.text) is listed

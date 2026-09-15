"""The Gmail option provisions a hidden Nerve relay, then waits for OAuth.

Plan Phase 3 (`thoughts/shared/plans/2026-09-13-gmail-send-only-forwarding.md`).
The relay is the managed adapter's inbox graph under a `fwd-…` address the
control plane derives; it is created before the family is asked to consent,
carried through the settled result as `{google, nerve}`, staged as the second
secret, emitted into the runtime manifest, and torn down from any reference
this side can name.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

from control_plane.email.models import (
    GMAIL_EMAIL_SECRET_BINDING,
    NERVE_EMAIL_SECRET_BINDING,
    EmailPublicBinding,
    gmail_relay_binding,
)
from control_plane.models import StepKind
from control_plane.providers.email.gmail_forwarding import GmailForwardingProvisioner
from control_plane.providers.email.google_oauth import GoogleOAuthProvisioner
from control_plane.providers.email.nerve_client import org_teardown_ref
from control_plane.providers.email.nerve_managed import NerveManagedEmailProvisioner
from control_plane.provisioning.contracts import (
    InspectState,
    OutcomeUnknown,
    ProviderRegistry,
    ProviderRejected,
    ProviderWaiting,
)
from control_plane.provisioning.manifest import (
    ActorsV1,
    ChannelBindingV1,
    ChannelsV1,
    ConsentAuthorityV1,
    ConsentReceiptV1,
    DesiredHouseholdSpecV1,
    EmailV1,
)
from control_plane.provisioning.manifest_toml import manifest_to_toml
from hermes_cloud.core.runtime_manifest import parse_runtime_manifest
from tests.control_plane.email.test_google_oauth import (
    GMAIL_SELECTION,
    FakeGoogleClient,
    _service,
)
from tests.control_plane.email.test_nerve_managed import INBOX_ID, ORG_ID, FakeNerveAdmin


class _AbsentNamespaceRevoker:
    """Disconnect before a runtime exists: the staged grant is removed from the
    secret namespace, as `test_google_oauth` models it."""

    def revoke_google_secret(self, app_ref, transaction_id):  # noqa: ANN001, ANN201
        return InspectState.ABSENT


def _provider(cp_stack, *, nerve=None, client=None, sink=None):
    service = _service(cp_stack, client or FakeGoogleClient(), sink)
    provider = GmailForwardingProvisioner(
        service,
        NerveManagedEmailProvisioner(nerve or FakeNerveAdmin()),
        namespace_revoker=_AbsentNamespaceRevoker(),
    )
    return service, provider


def _email_status(cp_stack) -> dict:
    snapshot = cp_stack.onboarding.snapshot(cp_stack.household.id)
    return next(step for step in snapshot.steps if step.kind is StepKind.EMAIL).public_status


def _select(cp_stack, provider, sink):
    cp_stack.complete_profile()
    cp_stack.service.gmail_provider = "google-oauth"
    cp_stack.service.select(
        cp_stack.household.id, StepKind.EMAIL, GMAIL_SELECTION, context=cp_stack.context()
    )
    registry = ProviderRegistry()
    registry.register("google-oauth", provider)
    return cp_stack.make_worker(providers=registry, secret_sink=sink)


def _consent(cp_stack, service):
    workflow = cp_stack.onboarding.workflow_for_household(cp_stack.household.id)
    started = service.start(
        household_id=cp_stack.household.id,
        account_id=cp_stack.account.id,
        session_id=cp_stack.session.id,
        workflow_version=workflow.version,
    )
    state = parse_qs(urlparse(started.authorization_url).query)["state"][0]
    service.callback(
        state=state,
        code="synthetic-code",
        household_id=cp_stack.household.id,
        account_id=cp_stack.account.id,
        session_id=cp_stack.session.id,
    )
    service.confirm(
        household_id=cp_stack.household.id, account_id=cp_stack.account.id, dedicated_mailbox=True
    )
    cp_stack.service.check(cp_stack.household.id, StepKind.EMAIL, context=cp_stack.context())


def _identity(cp_stack):
    identity = cp_stack.email_identities.current_for_household(cp_stack.household.id)
    assert identity is not None
    return identity


# --- the relay ----------------------------------------------------------------


def test_the_relay_is_created_before_anyone_is_asked_to_consent(cp_stack) -> None:
    nerve = FakeNerveAdmin()
    google = FakeGoogleClient()
    service, provider = _provider(cp_stack, nerve=nerve, client=google)
    worker = _select(cp_stack, provider, service.secret_sink)

    first = worker.run_once()

    assert first.status == "waiting_user", first
    assert nerve.org_external_refs, "no relay org was ensured"
    assert google.verifiers == [], "OAuth was started before the relay existed"
    relay = next(item for item in nerve.inboxes if item["address"].startswith("fwd-"))
    assert relay["address"] == provider.relay_address(_identity(cp_stack).id)
    assert _email_status(cp_stack)["state"] == "oauth_required"


def test_the_relay_address_is_derived_unguessable_and_stable(cp_stack) -> None:
    """Derived under the lookup HMAC rather than drawn and persisted: the same
    address on every attempt, computable before the first provider call, and
    nothing an identity id alone gives away."""
    _service_, provider = _provider(cp_stack)

    first = provider.relay_address("45000000-0000-4000-8000-000000000001")
    again = provider.relay_address("45000000-0000-4000-8000-000000000001")
    other = provider.relay_address("45000000-0000-4000-8000-000000000002")

    assert first == again != other
    local, domain = first.split("@")
    assert domain == "abrolia.com"
    assert local.startswith("fwd-") and len(local) == len("fwd-") + 26
    assert local[4:].isalnum() and local[4:] == local[4:].lower()
    assert "45000000" not in first


def test_a_relay_org_awaiting_the_attachments_flag_waits_in_the_gmail_shape(
    cp_stack,
) -> None:
    nerve = FakeNerveAdmin(attachments_enabled=False)
    service, provider = _provider(cp_stack, nerve=nerve)
    worker = _select(cp_stack, provider, service.secret_sink)

    waiting = worker.run_once()

    assert waiting.status == "waiting_user", waiting
    status = _email_status(cp_stack)
    assert status["state"] == "relay_pending"
    assert status["relay"]["readiness"] == "attachments_flag_pending"
    assert status["relay"]["nerve_org_id"] == ORG_ID
    assert status["relay"]["operator_action"]["arguments"][3] == ORG_ID

    nerve.attachments_enabled = True
    cp_stack.service.check(cp_stack.household.id, StepKind.EMAIL, context=cp_stack.context())
    assert worker.run_once().status == "waiting_user"
    assert _email_status(cp_stack)["state"] == "oauth_required"


class _KeyCallLost(FakeNerveAdmin):
    """The inbox is created, then the key call does not answer."""

    def __init__(self) -> None:
        super().__init__()
        self.lost = True

    def issue_key(self, *, org_id, external_ref):
        if self.lost:
            self.lost = False
            raise OutcomeUnknown("synthetic lost response")
        return super().issue_key(org_id=org_id, external_ref=external_ref)


def test_the_relay_graph_is_idempotent_across_a_crash_between_inbox_and_key(
    cp_stack,
) -> None:
    nerve = _KeyCallLost()
    service, provider = _provider(cp_stack, nerve=nerve)
    intent = {
        "identity_id": "45000000-0000-4000-8000-000000000001",
        "household_id": cp_stack.household.id,
        "option": "gmail",
        "selection": {"kind": "gmail_agent", "separate_agent_account_acknowledged": True},
        "secret_namespace_ref": "abrolia-hh-synthetic",
    }
    key = f"{cp_stack.household.id}:email_identity:45000000-0000-4000-8000-000000000001:gmail:1"

    with pytest.raises(OutcomeUnknown):
        provider.ensure(intent, key)
    with pytest.raises(ProviderWaiting):
        provider.ensure(intent, key)

    relays = [item for item in nerve.inboxes if item["address"].startswith("fwd-")]
    assert len(relays) == 1
    assert relays[0]["address"] == provider.relay_address(intent["identity_id"])


def test_a_lost_webhook_secret_is_recovered_by_rotation(cp_stack) -> None:
    nerve = FakeNerveAdmin(replay_credentials=True)
    service, provider = _provider(cp_stack, nerve=nerve)
    sink = service.secret_sink
    worker = _select(cp_stack, provider, sink)
    assert worker.run_once().status == "waiting_user"
    _consent(cp_stack, service)

    ready = worker.run_once()

    assert ready.status == "succeeded", ready
    namespace = f"synthetic-runtime:{cp_stack.household.id}"
    bundle = json.loads(bytes(sink.get(namespace, NERVE_EMAIL_SECRET_BINDING)))
    assert bundle["webhook_signing_key"] == "synthetic-rotated-signing-key"
    assert bundle["api_key"] == "synthetic-nerve-key"


# --- the settled result --------------------------------------------------------


def test_both_secrets_are_staged_and_the_result_names_both_halves(cp_stack) -> None:
    nerve = FakeNerveAdmin()
    service, provider = _provider(cp_stack, nerve=nerve)
    sink = service.secret_sink
    worker = _select(cp_stack, provider, sink)
    assert worker.run_once().status == "waiting_user"
    _consent(cp_stack, service)

    ready = worker.run_once()

    assert ready.status == "succeeded", ready
    namespace = f"synthetic-runtime:{cp_stack.household.id}"
    assert sink.get(namespace, GMAIL_EMAIL_SECRET_BINDING) is not None
    assert sink.get(namespace, NERVE_EMAIL_SECRET_BINDING) is not None
    identity = _identity(cp_stack)
    assert identity.address == "agent-mailbox@gmail.test"
    workflow = cp_stack.onboarding.workflow_for_household(cp_stack.household.id)
    result = cp_stack.onboarding.result(workflow.id, "email_identity")
    public = result["public_result"]
    assert public["provider"] == "gmail"
    assert public["secret_binding_ref"] == GMAIL_EMAIL_SECRET_BINDING
    assert public["inbound_secret_binding_ref"] == NERVE_EMAIL_SECRET_BINDING
    relay = gmail_relay_binding(public["inbound_binding_ref"])
    assert relay == {
        "org_id": ORG_ID,
        "inbox_id": INBOX_ID,
        "address": provider.relay_address(identity.id),
    }
    composite = json.loads(result["external_ref"])
    assert composite["google"] == f"google-oauth:{identity.id}"
    assert composite["nerve"]["inbox_id"] == INBOX_ID
    assert composite["nerve"]["org_external_ref"] == (
        f"arbolia:household:{cp_stack.household.id}:email:{identity.id}"
    )
    assert b"synthetic-nerve-key" not in cp_stack.database.path.read_bytes()


def test_without_nerve_a_gmail_household_settles_send_only(cp_stack) -> None:
    """A deployment with no Nerve configured — synthetic, test — still runs
    real Google OAuth for its test users and has nowhere to put a relay. Its
    Gmail household settles in Phase 1's shape: the grant, no relay, and the
    runtime reports `forwarding = none`."""
    service = _service(cp_stack, FakeGoogleClient())
    provider = GmailForwardingProvisioner(service, None)
    worker = _select(cp_stack, provider, service.secret_sink)
    assert worker.run_once().status == "waiting_user"
    _consent(cp_stack, service)

    settled = worker.run_once()

    assert settled.status == "succeeded", settled
    identity = _identity(cp_stack)
    result = cp_stack.onboarding.result(
        cp_stack.onboarding.workflow_for_household(cp_stack.household.id).id, "email_identity"
    )
    assert result["external_ref"] == f"google-oauth:{identity.id}"
    assert "inbound_binding_ref" not in result["public_result"]
    assert "inbound_secret_binding_ref" not in result["public_result"]
    # And the plain OAuth provisioner's result is the same shape, so an older
    # registration settles too.
    assert isinstance(provider, GoogleOAuthProvisioner)


def test_a_relay_is_whole_or_absent() -> None:
    with pytest.raises(ValueError, match="together"):
        EmailPublicBinding(
            agent_inbox="agent@gmail.test",
            provider="gmail",
            provider_subject="subject",
            provider_refs={"google_subject": "subject"},
            secret_binding_ref=GMAIL_EMAIL_SECRET_BINDING,
            granted_scopes=("email", "https://www.googleapis.com/auth/gmail.send", "openid"),
            inbound_secret_binding_ref=NERVE_EMAIL_SECRET_BINDING,
        )


@pytest.mark.parametrize(
    "inbound",
    [
        pytest.param("not json", id="not-json"),
        pytest.param(json.dumps({"org_id": ORG_ID, "inbox_id": INBOX_ID}), id="no-address"),
        pytest.param(
            json.dumps(
                # Spliced so the fixture sanitizer does not read a production
                # address into a committed test, as `test_nerve_managed` does.
                {"org_id": ORG_ID, "inbox_id": INBOX_ID, "address": "family@" + "abrolia.com"},
                sort_keys=True,
                separators=(",", ":"),
            ),
            id="not-a-relay-address",
        ),
        pytest.param(
            json.dumps(
                {"org_id": ORG_ID, "inbox_id": INBOX_ID, "address": "fwd-abc@example.com"},
                sort_keys=True,
                separators=(",", ":"),
            ),
            id="off-platform",
        ),
    ],
)
def test_the_public_binding_holds_the_relay_to_its_shape(inbound) -> None:
    with pytest.raises(ValueError):
        EmailPublicBinding(
            agent_inbox="agent@gmail.test",
            provider="gmail",
            provider_subject="subject",
            provider_refs={"google_subject": "subject"},
            secret_binding_ref=GMAIL_EMAIL_SECRET_BINDING,
            granted_scopes=("email", "https://www.googleapis.com/auth/gmail.send", "openid"),
            inbound_binding_ref=inbound,
            inbound_secret_binding_ref=NERVE_EMAIL_SECRET_BINDING,
        )


def test_only_a_gmail_binding_carries_a_relay() -> None:
    with pytest.raises(ValueError, match="only a Gmail provider"):
        EmailPublicBinding(
            agent_inbox="family@" + "abrolia.com",
            provider="synthetic",
            inbound_secret_binding_ref=NERVE_EMAIL_SECRET_BINDING,
        )


# --- the manifest --------------------------------------------------------------


def _spec(email: EmailV1) -> DesiredHouseholdSpecV1:
    return DesiredHouseholdSpecV1(
        household_id="33333333-3333-4333-8333-333333333333",
        config_revision=1,
        family_language="English",
        timezone="Europe/Prague",
        country_code="CZ",
        residency_mode="eu-app",
        actors=ActorsV1(owner="owner"),
        channels=ChannelsV1(primary="telegram"),
        channel_bindings=(ChannelBindingV1(channel="telegram", actor_id="owner", chat_id="c1"),),
        email=email,
        consent=ConsentAuthorityV1(
            required_purposes=("special_category_content_restriction",),
            receipts=(
                ConsentReceiptV1(
                    receipt_id="r1",
                    purpose="special_category_content_restriction",
                    text_version="1",
                    text_sha256="a" * 64,
                ),
            ),
        ),
    ).with_hash()


def test_the_manifest_carries_the_relay_and_the_runtime_reads_it(cp_stack) -> None:
    """Phase 2's parser is the consumer; the TOML the control plane emits has
    to be the TOML it accepts."""
    _service_, provider = _provider(cp_stack)
    relay = provider.relay_address("45000000-0000-4000-8000-000000000001")
    spec = _spec(
        EmailV1(
            agent_inbox="agent@gmail.test",
            fallback="owner@example.test",
            provider_kind="gmail",
            provider_binding_ref='{"google":"google-oauth:x","nerve":{}}',
            secret_binding_ref=GMAIL_EMAIL_SECRET_BINDING,
            inbound_provider_kind="nerve",
            inbound_binding_ref=json.dumps(
                {"org_id": ORG_ID, "inbox_id": INBOX_ID, "address": relay},
                sort_keys=True,
                separators=(",", ":"),
            ),
            inbound_secret_binding_ref=NERVE_EMAIL_SECRET_BINDING,
        )
    )

    manifest = parse_runtime_manifest(manifest_to_toml(spec))

    assert manifest.email.relay is True
    assert manifest.email.inbound_provider_kind == "nerve"
    assert manifest.email.inbound_secret_binding_ref == NERVE_EMAIL_SECRET_BINDING
    assert json.loads(manifest.email.inbound_binding_ref)["address"] == relay


@pytest.mark.parametrize(
    ("provider_kind", "fields", "match"),
    [
        ("nerve-managed", {"inbound_provider_kind": "nerve"}, "only a gmail household"),
        ("gmail", {"inbound_provider_kind": "nerve"}, "together"),
    ],
)
def test_the_planner_model_refuses_a_partial_or_misplaced_relay(provider_kind, fields, match) -> None:
    with pytest.raises(ValueError, match=match):
        EmailV1(
            agent_inbox="agent@example.test",
            fallback="owner@example.test",
            provider_kind=provider_kind,
            **fields,
        )


# --- teardown -------------------------------------------------------------------


def _torn_down(nerve: FakeNerveAdmin) -> set[str]:
    return {path.split("/")[2] for path, _org in nerve.deleted}


def test_every_reference_the_control_plane_can_name_tears_down_both_halves(
    cp_stack,
) -> None:
    """`AGENTS.repo-invariants.md`, "Withdrawal tears down what it can NAME":
    the settled composite, the bare Google reference a shutdown derives, and
    the computed org reference all reach the relay without asking Nerve what
    it holds beyond a read-only `get_org`."""
    nerve = FakeNerveAdmin()
    service, provider = _provider(cp_stack, nerve=nerve)
    worker = _select(cp_stack, provider, service.secret_sink)
    assert worker.run_once().status == "waiting_user"
    _consent(cp_stack, service)
    assert worker.run_once().status == "succeeded"
    identity = _identity(cp_stack)
    workflow = cp_stack.onboarding.workflow_for_household(cp_stack.household.id)
    settled_ref = cp_stack.onboarding.result(workflow.id, "email_identity")["external_ref"]

    for reference in (
        settled_ref,
        f"google-oauth:{identity.id}",
        org_teardown_ref(cp_stack.household.id, identity.id),
    ):
        nerve.deleted.clear()
        # The fake forgets a key once deleted, so re-seed it: each reference
        # is asserted against a relay that still has everything.
        nerve.keys = [{"id": "00000000-0000-4000-8000-000000000004", "external_ref": "k"}]
        result = provider.deprovision(reference)
        assert result.state is InspectState.ABSENT, reference
        assert {"webhooks", "keys", "inboxes", "domain-grants", "orgs"} <= _torn_down(nerve), reference


def test_teardown_refuses_a_reference_it_cannot_read(cp_stack) -> None:
    _service_, provider = _provider(cp_stack)
    with pytest.raises(ProviderRejected):
        provider.deprovision('{"google":"google-oauth:x"}')
    with pytest.raises(ProviderRejected):
        provider.deprovision("something-else")


# --- the switches ---------------------------------------------------------------


@pytest.mark.parametrize("env_name", ["ABROLIA_GMAIL_ENABLED", "ABROLIA_REAL_EMAIL_ENABLED"])
def test_either_switch_off_stops_the_relay_at_the_provider_call(
    cp_stack, monkeypatch: pytest.MonkeyPatch, env_name: str
) -> None:
    nerve = FakeNerveAdmin()
    _service_, provider = _provider(cp_stack, nerve=nerve)
    monkeypatch.setenv(env_name, "0")

    with pytest.raises(ProviderRejected, match="disabled"):
        provider.ensure(
            {
                "identity_id": "45000000-0000-4000-8000-000000000001",
                "household_id": cp_stack.household.id,
                "option": "gmail",
                "selection": {"kind": "gmail_agent", "separate_agent_account_acknowledged": True},
                "secret_namespace_ref": "abrolia-hh-synthetic",
            },
            f"{cp_stack.household.id}:email_identity:x:gmail:1",
        )
    assert nerve.org_external_refs == []
    # Teardown is never braked.
    assert provider.deprovision(
        org_teardown_ref(cp_stack.household.id, "45000000-0000-4000-8000-000000000001")
    ).state is InspectState.ABSENT


def test_without_the_nerve_client_there_is_no_relay_and_teardown_still_answers(
    cp_stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No Nerve, no relay, no brake owed: the OAuth wait states run as they
    did in Phase 1, even with real email off."""
    monkeypatch.setenv("ABROLIA_REAL_EMAIL_ENABLED", "0")
    service = _service(cp_stack, FakeGoogleClient())
    provider = GmailForwardingProvisioner(service, None)

    with pytest.raises(ProviderWaiting, match="OAuth user action") as waiting:
        provider.ensure(
            {
                "identity_id": "45000000-0000-4000-8000-000000000001",
                "household_id": cp_stack.household.id,
                "option": "gmail",
                "selection": {"kind": "gmail_agent", "separate_agent_account_acknowledged": True},
                "secret_namespace_ref": "abrolia-hh-synthetic",
            },
            f"{cp_stack.household.id}:email_identity:x:gmail:1",
        )
    assert waiting.value.public_result["state"] == "oauth_required"
    assert provider.deprovision("google-oauth:45000000-0000-4000-8000-000000000001").state is (
        InspectState.ABSENT
    )

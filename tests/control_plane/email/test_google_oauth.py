from __future__ import annotations

import base64
import hashlib
from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from control_plane.email.models import GMAIL_EMAIL_SCOPES, GMAIL_EMAIL_SECRET_BINDING
from control_plane.models import StepKind
from control_plane.privacy.consent import consent_version_and_sha
from control_plane.providers.email.google_oauth import (
    GOOGLE_TOKEN_ENDPOINT,
    GOOGLE_TOKEN_INFO_ENDPOINT,
    GOOGLE_USER_INFO_ENDPOINT,
    GoogleOAuthClient,
    GoogleOAuthInvalidState,
    GoogleOAuthProvisioner,
    GoogleOAuthSecretHandoffUnknown,
    GoogleOAuthService,
    GoogleTokenGrant,
)
from control_plane.provisioning.contracts import InspectState, ProviderRegistry, ProviderRejected
from control_plane.provisioning.secrets import InMemorySecretSink

GMAIL_SEND = "https://www.googleapis.com/auth/gmail.send"
GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"

_RESTRICTION_VERSION, _RESTRICTION_SHA = consent_version_and_sha(
    "special_category_content_restriction"
)
_HOUSEHOLD_VERSION, _HOUSEHOLD_SHA = consent_version_and_sha(
    "special_category_household_content"
)
GMAIL_SELECTION = {
    "kind": "gmail_agent",
    "separate_agent_account_acknowledged": True,
    "special_category_restriction_acknowledged": True,
    "special_category_restriction_receipt_id": "10000000-0000-4000-8000-000000000021",
    "special_category_restriction_text_version": _RESTRICTION_VERSION,
    "special_category_restriction_text_sha256": _RESTRICTION_SHA,
    "special_category_household_consent": True,
    "special_category_household_receipt_id": "10000000-0000-4000-8000-000000000031",
    "special_category_household_text_version": _HOUSEHOLD_VERSION,
    "special_category_household_text_sha256": _HOUSEHOLD_SHA,
}


class FakeGoogleClient:
    client_id = "synthetic-google-client"

    def __init__(self, *, scopes=GMAIL_EMAIL_SCOPES) -> None:
        self.scopes = tuple(scopes)
        self.revoked: list[str] = []
        self.verifiers: list[str] = []
        self.challenges: list[str] = []

    def authorization_url(self, *, state, code_challenge, redirect_uri):
        assert code_challenge and redirect_uri.endswith("/api/v1/email/google/callback")
        self.challenges.append(code_challenge)
        return f"https://accounts.example.test/authorize?state={state}"

    def exchange(self, *, code, code_verifier, redirect_uri):
        assert code == "synthetic-code"
        self.verifiers.append(code_verifier)
        return GoogleTokenGrant(
            subject="google-subject-1",
            email="agent-mailbox@gmail.test",
            scopes=self.scopes,
            refresh_credential="synthetic-refresh-credential",
            access_token="synthetic-access-token",
        )

    def revoke(self, credential):
        self.revoked.append(credential)


class FailingSecretSink(InMemorySecretSink):
    def install(self, runtime_ref, material):
        material.clear()
        raise RuntimeError("synthetic sink failure")


def _service(
    cp_stack, client, sink=None, *, clock=lambda: 1_800_000_100.0, test_users=None
):
    config = replace(
        cp_stack.config,
        google_oauth_client_id="synthetic-google-client",
        google_oauth_client_secret="synthetic-google-client-secret",
        google_oauth_test_users=test_users
        or (cp_stack.account.recovery_email.casefold(),),
    ).validate()
    return GoogleOAuthService(
        config=config,
        client=client,
        identities=cp_stack.email_identities,
        accounts=cp_stack.accounts,
        jobs=cp_stack.jobs,
        secret_sink=sink or InMemorySecretSink(),
        token_hasher=cp_stack.token_hasher,
        clock=clock,
    )


def _select_gmail(cp_stack, provider, sink):
    cp_stack.complete_profile()
    cp_stack.service.gmail_provider = "google-oauth"
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        GMAIL_SELECTION,
        context=cp_stack.context(),
    )
    registry = ProviderRegistry()
    registry.register("google-oauth", provider)
    worker = cp_stack.make_worker(providers=registry, secret_sink=sink)
    assert worker.run_once().status == "waiting_user"
    return worker


def test_oauth_pkce_confirmation_and_worker_projection(cp_stack) -> None:
    from control_plane.providers.email.gmail_forwarding import GmailForwardingProvisioner
    from control_plane.providers.email.nerve_managed import NerveManagedEmailProvisioner
    from tests.control_plane.email.test_nerve_managed import FakeNerveAdmin

    client = FakeGoogleClient()
    sink = InMemorySecretSink()
    service = _service(cp_stack, client, sink)
    # The registered `google-oauth` adapter is the relay-carrying one: a Gmail
    # result without its relay no longer settles (Phase 3).
    provider = GmailForwardingProvisioner(
        service, NerveManagedEmailProvisioner(FakeNerveAdmin())
    )
    worker = _select_gmail(cp_stack, provider, sink)

    workflow = cp_stack.onboarding.workflow_for_household(cp_stack.household.id)
    started = service.start(
        household_id=cp_stack.household.id,
        account_id=cp_stack.account.id,
        session_id=cp_stack.session.id,
        workflow_version=workflow.version,
    )
    state = parse_qs(urlparse(started.authorization_url).query)["state"][0]
    masked = service.callback(
        state=state,
        code="synthetic-code",
        household_id=cp_stack.household.id,
        account_id=cp_stack.account.id,
        session_id=cp_stack.session.id,
    )

    assert masked.endswith("@gmail.test")
    assert client.verifiers and "synthetic-refresh-credential" not in client.verifiers[0]
    expected_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(client.verifiers[0].encode()).digest()).rstrip(b"=").decode()
    )
    assert client.challenges == [expected_challenge]
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
    assert sink.get(namespace_ref, GMAIL_EMAIL_SECRET_BINDING) is not None
    assert b"synthetic-refresh-credential" not in cp_stack.database.path.read_bytes()
    assert b"synthetic-code" not in cp_stack.database.path.read_bytes()

    service.confirm(
        household_id=cp_stack.household.id,
        account_id=cp_stack.account.id,
        dedicated_mailbox=True,
    )
    cp_stack.service.check(cp_stack.household.id, StepKind.EMAIL, context=cp_stack.context())
    ready = worker.run_once()
    assert ready.status == "succeeded", ready
    identity = cp_stack.email_identities.current_for_household(cp_stack.household.id)
    assert identity is not None and identity.address == "agent-mailbox@gmail.test"
    assert identity.granted_scopes == GMAIL_EMAIL_SCOPES


def test_state_is_bound_to_session_and_single_use(cp_stack) -> None:
    client = FakeGoogleClient()
    service = _service(cp_stack, client)
    provider = GoogleOAuthProvisioner(service)
    _select_gmail(cp_stack, provider, service.secret_sink)
    workflow = cp_stack.onboarding.workflow_for_household(cp_stack.household.id)
    started = service.start(
        household_id=cp_stack.household.id,
        account_id=cp_stack.account.id,
        session_id=cp_stack.session.id,
        workflow_version=workflow.version,
    )
    state = parse_qs(urlparse(started.authorization_url).query)["state"][0]

    with pytest.raises(GoogleOAuthInvalidState):
        service.callback(
            state=state,
            code="synthetic-code",
            household_id=cp_stack.household.id,
            account_id=cp_stack.account.id,
            session_id="another-session",
        )
    service.callback(
        state=state,
        code="synthetic-code",
        household_id=cp_stack.household.id,
        account_id=cp_stack.account.id,
        session_id=cp_stack.session.id,
    )
    with pytest.raises(GoogleOAuthInvalidState):
        service.callback(
            state=state,
            code="synthetic-code",
            household_id=cp_stack.household.id,
            account_id=cp_stack.account.id,
            session_id=cp_stack.session.id,
        )


def test_disconnect_before_runtime_uses_secret_namespace_revoker(cp_stack) -> None:
    client = FakeGoogleClient()
    sink = InMemorySecretSink()
    service = _service(cp_stack, client, sink)

    class NamespaceRevoker:
        def __init__(self) -> None:
            self.calls = []

        def revoke_google_secret(self, app_ref, transaction_id):
            self.calls.append((app_ref, transaction_id))
            return InspectState.ABSENT

    revoker = NamespaceRevoker()
    provider = GoogleOAuthProvisioner(service, namespace_revoker=revoker)
    _select_gmail(cp_stack, provider, sink)
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
    identity = cp_stack.email_identities.current_for_household(cp_stack.household.id)
    assert identity is not None

    result = provider.deprovision(f"google-oauth:{identity.id}")

    assert result.state is InspectState.ABSENT
    assert len(revoker.calls) == 1
    app_ref, transaction_id = revoker.calls[0]
    assert app_ref.startswith("synthetic-runtime:")
    assert len(transaction_id) == 32
    assert sink.get(app_ref, GMAIL_EMAIL_SECRET_BINDING) is None
    row = cp_stack.database.query_one(
        "SELECT revoke_requested_at, revoke_completed_at, revoked_at"
        " FROM oauth_transactions WHERE id = ?",
        (transaction_id,),
    )
    assert all(value is not None for value in row)


@pytest.mark.parametrize(
    "granted",
    [
        # The family left "Send email on your behalf" unticked on Google's
        # consent screen (spike S4): sign-in only, nothing to send with.
        pytest.param(
            tuple(scope for scope in GMAIL_EMAIL_SCOPES if scope != GMAIL_SEND),
            id="narrower-no-send",
        ),
        # A wider grant is not a bonus: the restricted read scope is the one
        # that makes CASA mandatory, so it is revoked the moment it appears.
        pytest.param(tuple(sorted((*GMAIL_EMAIL_SCOPES, GMAIL_READONLY))), id="wider-readonly"),
    ],
)
def test_scope_downgrade_revokes_and_fails_closed(cp_stack, granted) -> None:
    assert granted != GMAIL_EMAIL_SCOPES
    client = FakeGoogleClient(scopes=granted)
    service = _service(cp_stack, client)
    provider = GoogleOAuthProvisioner(service)
    _select_gmail(cp_stack, provider, service.secret_sink)
    workflow = cp_stack.onboarding.workflow_for_household(cp_stack.household.id)
    started = service.start(
        household_id=cp_stack.household.id,
        account_id=cp_stack.account.id,
        session_id=cp_stack.session.id,
        workflow_version=workflow.version,
    )
    state = parse_qs(urlparse(started.authorization_url).query)["state"][0]

    with pytest.raises(Exception, match="unexpected scope"):
        service.callback(
            state=state,
            code="synthetic-code",
            household_id=cp_stack.household.id,
            account_id=cp_stack.account.id,
            session_id=cp_stack.session.id,
        )
    assert client.revoked == ["synthetic-refresh-credential"]


def _google_endpoints(scope: str):
    """Google's three answers to a code exchange, with the scope string as returned."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?", 1)[0]
        if url == GOOGLE_TOKEN_ENDPOINT:
            return httpx.Response(
                200,
                json={
                    "access_token": "synthetic-access-token",
                    "refresh_token": "synthetic-refresh-credential",
                    "id_token": "synthetic-id-token",
                    "scope": scope,
                },
            )
        if url == GOOGLE_TOKEN_INFO_ENDPOINT:
            return httpx.Response(
                200,
                json={
                    "aud": "synthetic-google-client",
                    "iss": "https://accounts.google.com",
                    "sub": "google-subject-1",
                },
            )
        assert url == GOOGLE_USER_INFO_ENDPOINT
        return httpx.Response(
            200,
            json={"sub": "google-subject-1", "email": "agent@gmail.test", "email_verified": True},
        )

    return handler


def test_exchange_reads_the_granted_set_under_the_names_google_writes_back() -> None:
    """Google was asked for `email` and answers `.../auth/userinfo.email`.

    The token of spike S4 (2026-09-14) read exactly
    `gmail.send userinfo.email openid`. Compared verbatim against the
    requested short name, that is "an unexpected scope set", and the callback
    revoked a grant that was precisely what it asked for. Nothing caught it
    because production holds no Gmail identity and the spike used its own
    script.
    """
    client = GoogleOAuthClient(
        client_id="synthetic-google-client",
        client_secret="synthetic-google-client-secret",
        client=httpx.Client(
            transport=httpx.MockTransport(
                _google_endpoints(
                    "https://www.googleapis.com/auth/gmail.send"
                    " https://www.googleapis.com/auth/userinfo.email openid"
                )
            )
        ),
    )

    grant = client.exchange(
        code="synthetic-code", code_verifier="verifier", redirect_uri="https://app.example.test/cb"
    )

    assert grant.scopes == GMAIL_EMAIL_SCOPES
    assert grant.email == "agent@gmail.test"


def test_exchange_does_not_invent_the_send_scope_from_an_alias() -> None:
    """An unticked checkbox grants sign-in only; folding the alias must not
    make that look like the full set."""
    client = GoogleOAuthClient(
        client_id="synthetic-google-client",
        client_secret="synthetic-google-client-secret",
        client=httpx.Client(
            transport=httpx.MockTransport(
                _google_endpoints("openid https://www.googleapis.com/auth/userinfo.email")
            )
        ),
    )

    grant = client.exchange(
        code="synthetic-code", code_verifier="verifier", redirect_uri="https://app.example.test/cb"
    )

    assert grant.scopes == ("email", "openid")
    assert grant.scopes != GMAIL_EMAIL_SCOPES


def test_secret_handoff_failure_revokes_and_requires_restart(cp_stack) -> None:
    client = FakeGoogleClient()
    sink = FailingSecretSink()
    service = _service(cp_stack, client, sink)
    provider = GoogleOAuthProvisioner(service)
    _select_gmail(cp_stack, provider, sink)
    workflow = cp_stack.onboarding.workflow_for_household(cp_stack.household.id)
    started = service.start(
        household_id=cp_stack.household.id,
        account_id=cp_stack.account.id,
        session_id=cp_stack.session.id,
        workflow_version=workflow.version,
    )
    state = parse_qs(urlparse(started.authorization_url).query)["state"][0]

    with pytest.raises(GoogleOAuthSecretHandoffUnknown):
        service.callback(
            state=state,
            code="synthetic-code",
            household_id=cp_stack.household.id,
            account_id=cp_stack.account.id,
            session_id=cp_stack.session.id,
        )
    assert client.revoked == ["synthetic-refresh-credential"]


def test_household_swap_expiry_and_stale_workflow_are_rejected(cp_stack) -> None:
    now = [1_800_000_100.0]
    client = FakeGoogleClient()
    service = _service(cp_stack, client, clock=lambda: now[0])
    provider = GoogleOAuthProvisioner(service)
    _select_gmail(cp_stack, provider, service.secret_sink)
    workflow = cp_stack.onboarding.workflow_for_household(cp_stack.household.id)

    started = service.start(
        household_id=cp_stack.household.id,
        account_id=cp_stack.account.id,
        session_id=cp_stack.session.id,
        workflow_version=workflow.version,
    )
    state = parse_qs(urlparse(started.authorization_url).query)["state"][0]
    with pytest.raises(GoogleOAuthInvalidState):
        service.callback(
            state=state,
            code="synthetic-code",
            household_id="another-household",
            account_id=cp_stack.account.id,
            session_id=cp_stack.session.id,
        )
    now[0] += 601
    with pytest.raises(GoogleOAuthInvalidState):
        service.callback(
            state=state,
            code="synthetic-code",
            household_id=cp_stack.household.id,
            account_id=cp_stack.account.id,
            session_id=cp_stack.session.id,
        )

    now[0] = 1_800_000_100.0
    fresh = service.start(
        household_id=cp_stack.household.id,
        account_id=cp_stack.account.id,
        session_id=cp_stack.session.id,
        workflow_version=workflow.version,
    )
    fresh_state = parse_qs(urlparse(fresh.authorization_url).query)["state"][0]
    with cp_stack.database.write() as connection:
        connection.execute(
            "UPDATE onboarding_workflows SET version = version + 1 WHERE id = ?",
            (workflow.id,),
        )
    with pytest.raises(GoogleOAuthInvalidState):
        service.callback(
            state=fresh_state,
            code="synthetic-code",
            household_id=cp_stack.household.id,
            account_id=cp_stack.account.id,
            session_id=cp_stack.session.id,
        )


def test_recovery_mailbox_match_is_revoked(cp_stack) -> None:
    client = FakeGoogleClient()
    client.exchange = lambda **_kwargs: GoogleTokenGrant(
        subject="google-subject-owner",
        email=cp_stack.account.recovery_email,
        scopes=GMAIL_EMAIL_SCOPES,
        refresh_credential="synthetic-refresh-credential",
        access_token="synthetic-access-token",
    )
    service = _service(cp_stack, client)
    provider = GoogleOAuthProvisioner(service)
    _select_gmail(cp_stack, provider, service.secret_sink)
    workflow = cp_stack.onboarding.workflow_for_household(cp_stack.household.id)
    started = service.start(
        household_id=cp_stack.household.id,
        account_id=cp_stack.account.id,
        session_id=cp_stack.session.id,
        workflow_version=workflow.version,
    )
    state = parse_qs(urlparse(started.authorization_url).query)["state"][0]
    with pytest.raises(ProviderRejected, match="dedicated mailbox"):
        service.callback(
            state=state,
            code="synthetic-code",
            household_id=cp_stack.household.id,
            account_id=cp_stack.account.id,
            session_id=cp_stack.session.id,
        )
    assert client.revoked == ["synthetic-refresh-credential"]


def test_another_owners_recovery_mailbox_is_revoked_too(cp_stack) -> None:
    """C4a's rule, on the one path that cannot ask it at selection.

    `EmailIdentityService.select` refuses a mailbox equal to an active owner's
    contact address, and `gmail_agent` reaches it with no address at all — the
    address exists only once Google has granted it. So a second owner (or an
    adult acting for the household) could connect the very mailbox the
    FALLBACK owner is reached at, and the refusal would arrive from
    `DesiredSpecPlanner.issue` long after the credential was installed.

    The neighbouring case covers the INITIATOR's own mailbox. This one is a
    different rule with the same remedy: the fallback is chosen from every
    active owner, so the question has to be asked about all of them.
    """
    second = cp_stack.accounts.create_verified("second-owner@family.test")
    with cp_stack.database.write() as connection:
        connection.execute(
            "INSERT INTO household_memberships (account_id, household_id, role,"
            " status, created_at, accepted_at) VALUES (?, ?, 'owner', 'active',"
            " ?, ?)",
            (second.id, cp_stack.household.id, 1_800_000_000.0, 1_800_000_000.0),
        )

    client = FakeGoogleClient()
    client.exchange = lambda **_kwargs: GoogleTokenGrant(
        subject="google-subject-second-owner",
        email=second.recovery_email,
        scopes=GMAIL_EMAIL_SCOPES,
        refresh_credential="synthetic-refresh-credential",
        access_token="synthetic-access-token",
    )
    service = _service(
        cp_stack,
        client,
        test_users=(
            cp_stack.account.recovery_email.casefold(),
            second.recovery_email.casefold(),
        ),
    )
    provider = GoogleOAuthProvisioner(service)
    _select_gmail(cp_stack, provider, service.secret_sink)
    workflow = cp_stack.onboarding.workflow_for_household(cp_stack.household.id)
    started = service.start(
        household_id=cp_stack.household.id,
        account_id=cp_stack.account.id,
        session_id=cp_stack.session.id,
        workflow_version=workflow.version,
    )
    state = parse_qs(urlparse(started.authorization_url).query)["state"][0]

    with pytest.raises(ProviderRejected, match="dedicated mailbox"):
        service.callback(
            state=state,
            code="synthetic-code",
            household_id=cp_stack.household.id,
            account_id=cp_stack.account.id,
            session_id=cp_stack.session.id,
        )

    # Revoked before anything durable: no credential reached the sink.
    assert client.revoked == ["synthetic-refresh-credential"]
    assert not service.secret_sink.contains(
        service._namespace_ref(cp_stack.household.id), GMAIL_EMAIL_SECRET_BINDING
    )

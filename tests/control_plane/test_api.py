from __future__ import annotations

from urllib.parse import urlsplit

import pytest

from control_plane.repositories.auth import InvalidCredential


def _token_from_latest_mail(api_harness) -> str:
    url = api_harness.mailer.sent[-1].url
    parts = urlsplit(url)
    assert parts.query == ""
    assert parts.fragment.startswith("token=")
    return parts.fragment.removeprefix("token=")


def _command_headers(api_harness, *, version: int, key: str) -> dict[str, str]:
    return {
        **api_harness.mutation_headers,
        "Idempotency-Key": key,
        "If-Match": f'W/"{version}"',
    }


def _profile(**changes: str) -> dict[str, str]:
    payload = {
        "first_name": "Synthetic",
        "last_name": "Owner",
        "family_language": "en",
        "timezone": "Europe/Prague",
        "country_code": "CZ",
        "residency_mode": "eu-app",
    }
    payload.update(changes)
    return payload


def test_request_link_response_is_generic_for_eligible_and_ineligible_addresses(
    api_harness,
) -> None:
    existing = api_harness.create_principal("invited-owner@pilot.test")
    headers = {"Origin": api_harness.config.public_origin}
    eligible = api_harness.client.post(
        "/api/v1/auth/request-link",
        headers=headers,
        json={"email": "invited-owner@pilot.test"},
    )
    ineligible = api_harness.client.post(
        "/api/v1/auth/request-link",
        headers=headers,
        json={"email": "not-invited@pilot.test"},
    )
    assert eligible.status_code == ineligible.status_code == 202
    assert eligible.json() == ineligible.json() == {"status": "accepted"}
    assert [message.recipient for message in api_harness.mailer.sent] == [
        "invited-owner@pilot.test"
    ]
    assert api_harness.mailer.sent[0].purpose == "login"
    token_rows = api_harness.container.database.query(
        "SELECT purpose, account_id, email_lookup_hmac FROM auth_tokens"
    )
    assert [dict(row) for row in token_rows] == [{
        "purpose": "login",
        "account_id": existing.account.id,
        "email_lookup_hmac": None,
    }]
    persisted = api_harness.container.database.connection.iterdump()
    dump = "\n".join(persisted)
    assert "invited-owner@pilot.test" not in dump
    assert "not-invited@pilot.test" not in dump


def test_authenticated_request_for_same_account_issues_reauth_not_invite(
    api_harness,
) -> None:
    world = api_harness.create_principal("reauth-owner@pilot.test")
    api_harness.authenticate(world)

    response = api_harness.client.post(
        "/api/v1/auth/request-link",
        headers={"Origin": api_harness.config.public_origin},
        json={"email": world.account.recovery_email},
    )

    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    assert api_harness.mailer.sent[-1].purpose == "reauth"
    row = api_harness.container.database.query_one(
        "SELECT purpose, account_id, email_lookup_hmac FROM auth_tokens"
    )
    assert dict(row) == {
        "purpose": "reauth",
        "account_id": world.account.id,
        "email_lookup_hmac": None,
    }


def test_magic_link_consume_sets_strict_cookies_and_rotates_existing_session(
    api_harness,
) -> None:
    origin = {"Origin": api_harness.config.public_origin}
    # New-account invites are issued only through the durable operator path.
    api_harness.container.magic_links.issue("cookie-owner@pilot.test")
    first = api_harness.client.post(
        "/api/v1/auth/consume",
        headers=origin,
        json={"token": _token_from_latest_mail(api_harness)},
    )
    assert first.status_code == 200
    first_session = api_harness.client.cookies.get(api_harness.config.session_cookie_name)
    first_csrf = api_harness.client.cookies.get(api_harness.config.csrf_cookie_name)
    assert first_session and first_csrf
    cookie_headers = first.headers.get_list("set-cookie")
    session_cookie = next(
        value
        for value in cookie_headers
        if value.startswith(api_harness.config.session_cookie_name + "=")
    )
    csrf_cookie = next(
        value
        for value in cookie_headers
        if value.startswith(api_harness.config.csrf_cookie_name + "=")
    )
    assert all(attribute in session_cookie.lower() for attribute in (
        "secure", "httponly", "samesite=strict", "path=/"
    ))
    assert "httponly" not in csrf_cookie.lower()
    assert all(attribute in csrf_cookie.lower() for attribute in (
        "secure", "samesite=strict", "path=/"
    ))

    account_id = first.json()["account"]["id"]
    api_harness.container.magic_links.issue(
        "cookie-owner@pilot.test", purpose="login", account_id=account_id
    )
    second = api_harness.client.post(
        "/api/v1/auth/consume",
        headers=origin,
        json={"token": _token_from_latest_mail(api_harness)},
    )
    second_session = api_harness.client.cookies.get(
        api_harness.config.session_cookie_name
    )
    assert second.status_code == 200
    assert second_session != first_session
    with pytest.raises(InvalidCredential):
        api_harness.container.sessions.authenticate(first_session)
    assert api_harness.container.sessions.authenticate(second_session).account_id == account_id
    body = second.json()
    assert first_session not in second.text
    assert second_session not in second.text
    assert body["account"]["recovery_email"] == "c***@pilot.test"


def test_synthetic_selection_boundaries_fail_closed_at_api(api_harness) -> None:
    world = api_harness.create_principal("selection-owner@family.test")
    api_harness.authenticate(world)
    profile = api_harness.client.put(
        "/api/v1/onboarding/profile",
        headers=_command_headers(api_harness, version=0, key="selection-profile"),
        json=_profile(),
    )
    assert profile.status_code == 200

    real_domain = api_harness.client.post(
        "/api/v1/onboarding/steps/email_identity/select",
        headers=_command_headers(api_harness, version=1, key="real-domain-rejected"),
        json={"kind": "family_domain", "domain": "family.example.com"},
    )
    assert real_domain.status_code == 422
    assert real_domain.json() == {"detail": "invalid synthetic selection"}
    assert "family.example.com" not in real_domain.text
    assert api_harness.container.onboarding_repository.snapshot(
        world.household.id
    ).version == 1
    assert api_harness.container.database.query("SELECT id FROM provisioning_jobs") == []

    managed = api_harness.client.post(
        "/api/v1/onboarding/steps/email_identity/select",
        headers=_command_headers(api_harness, version=1, key="managed-email-fake"),
        json={"kind": "abrolia_managed", "local_part": "family-agent"},
    )
    assert managed.status_code == 200
    assert api_harness.container.database.query_one(
        "SELECT provider FROM provisioning_jobs WHERE kind = 'email_identity'"
    )["provider"] == "fake-email"
    assert api_harness.container.worker.run_once().status == "succeeded"

    version = api_harness.container.onboarding_repository.snapshot(
        world.household.id
    ).version
    whatsapp = api_harness.client.post(
        "/api/v1/onboarding/steps/whatsapp_identity/select",
        headers=_command_headers(api_harness, version=version, key="synthetic-whatsapp"),
        json={
            "kind": "shared_abrolia",
            "member_phone_test_ref": "synthetic-phone:selection-owner",
            "privacy_notice_receipt_id": "synthetic-selection-consent",
        },
    )
    assert whatsapp.status_code == 200
    assert api_harness.container.worker.run_once().status == "succeeded"
    version = api_harness.container.onboarding_repository.snapshot(
        world.household.id
    ).version

    unsafe_identities = (
        {"kind": "telegram", "actor_id": "real-owner", "chat_id": "synthetic-chat"},
        {"kind": "telegram", "actor_id": "synthetic-owner", "chat_id": "external-chat"},
    )
    for offset, unsafe in enumerate(unsafe_identities, start=1):
        response = api_harness.client.post(
            "/api/v1/onboarding/steps/primary_channel/select",
            headers=_command_headers(
                api_harness,
                version=version,
                key=f"unsafe-channel-id-{offset}",
            ),
            json=unsafe,
        )
        assert response.status_code == 422
        assert response.json() == {"detail": "invalid synthetic selection"}
        assert unsafe["actor_id"] not in response.text
        assert unsafe["chat_id"] not in response.text
    assert api_harness.container.onboarding_repository.snapshot(
        world.household.id
    ).version == version


def test_auth_and_validation_errors_never_echo_submitted_credentials(api_harness) -> None:
    secret_canary = "synthetic-invalid-bootstrap-style-token-canary-123456789"
    missing_origin = api_harness.client.post(
        "/api/v1/auth/consume", json={"token": secret_canary}
    )
    assert missing_origin.status_code == 403
    assert secret_canary not in missing_origin.text
    invalid = api_harness.client.post(
        "/api/v1/auth/consume",
        headers={"Origin": api_harness.config.public_origin},
        json={"token": secret_canary},
    )
    assert invalid.status_code == 401
    assert secret_canary not in invalid.text
    structurally_invalid = api_harness.client.post(
        "/api/v1/auth/consume",
        headers={"Origin": api_harness.config.public_origin},
        json={"token": "short-secret-canary"},
    )
    assert structurally_invalid.status_code == 422
    assert "short-secret-canary" not in structurally_invalid.text


def test_private_commands_enforce_origin_csrf_preconditions_and_replay(api_harness) -> None:
    world = api_harness.create_principal()
    api_harness.authenticate(world)
    path = "/api/v1/onboarding/profile"
    complete = _command_headers(api_harness, version=0, key="profile-command-one")

    assert api_harness.client.put(path, json=_profile()).status_code == 403
    wrong_origin = {**complete, "Origin": "https://foreign.invalid"}
    assert api_harness.client.put(path, headers=wrong_origin, json=_profile()).status_code == 403
    no_csrf = {name: value for name, value in complete.items() if name != "X-CSRF-Token"}
    assert api_harness.client.put(path, headers=no_csrf, json=_profile()).status_code == 403
    wrong_csrf = {**complete, "X-CSRF-Token": "wrong-csrf"}
    assert api_harness.client.put(path, headers=wrong_csrf, json=_profile()).status_code == 403
    no_preconditions = dict(api_harness.mutation_headers)
    assert api_harness.client.put(
        path, headers=no_preconditions, json=_profile()
    ).status_code == 428

    first = api_harness.client.put(path, headers=complete, json=_profile())
    assert first.status_code == 200
    assert first.headers["etag"] == '"1"'
    replay = api_harness.client.put(path, headers=complete, json=_profile())
    assert replay.status_code == 200
    assert replay.headers["x-idempotent-replay"] == "true"
    assert replay.json() == first.json()

    changed = api_harness.client.put(
        path,
        headers=complete,
        json=_profile(first_name="Changed"),
    )
    assert changed.status_code == 409
    stale = api_harness.client.put(
        path,
        headers=_command_headers(api_harness, version=0, key="profile-stale-version"),
        json=_profile(),
    )
    assert stale.status_code == 409


def test_current_household_is_derived_from_session_not_request_input(api_harness) -> None:
    owner = api_harness.create_principal("derived-owner@family.test")
    foreign = api_harness.create_principal("foreign-owner@family.test")
    api_harness.authenticate(owner)
    headers = _command_headers(api_harness, version=0, key="derived-household-one")

    response = api_harness.client.put(
        f"/api/v1/onboarding/profile?household_id={foreign.household.id}",
        headers=headers,
        json=_profile(),
    )
    assert response.status_code == 200
    assert response.json()["household_id"] == owner.household.id
    assert api_harness.container.households.profile(owner.household.id) is not None
    assert api_harness.container.households.profile(foreign.household.id) is None

    extra_field = api_harness.client.put(
        "/api/v1/onboarding/profile",
        headers=_command_headers(api_harness, version=1, key="foreign-body-field"),
        json={**_profile(), "household_id": foreign.household.id},
    )
    assert extra_field.status_code == 422
    assert foreign.household.id not in extra_field.text
    route_paths = {
        path
        for route in api_harness.client.app.routes
        if isinstance(path := getattr(route, "path", None), str)
    }
    assert not any("{household_id}" in path for path in route_paths if path.startswith("/api/"))


def test_household_create_honors_version_and_hmac_idempotency(api_harness) -> None:
    account = api_harness.container.accounts.create_verified("new-pilot@family.test")
    session = api_harness.container.sessions.issue(account.id)
    api_harness.client.cookies.set(
        api_harness.config.session_cookie_name, session.token
    )
    api_harness.client.cookies.set(
        api_harness.config.csrf_cookie_name, session.csrf_token
    )
    headers = {
        "Origin": api_harness.config.public_origin,
        "X-CSRF-Token": session.csrf_token,
        "Idempotency-Key": "household-create-command",
        "If-Match": "0",
    }

    first = api_harness.client.post("/api/v1/households", headers=headers)
    replay = api_harness.client.post("/api/v1/households", headers=headers)

    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert first.json()["created"] is True
    assert first.headers["etag"] == '"0"'
    assert replay.headers["x-idempotent-replay"] == "true"
    household = api_harness.container.households.current_for_account(account.id)
    assert household.id == first.json()["id"]
    persisted = api_harness.container.database.query_one(
        "SELECT idempotency_key_hmac FROM idempotency_requests"
        " WHERE account_id = ? AND route = '/api/v1/households'",
        (account.id,),
    )
    assert persisted["idempotency_key_hmac"] != "household-create-command"

    changed_same_key = api_harness.client.post(
        "/api/v1/households", headers={**headers, "If-Match": "1"}
    )
    stale_new_key = api_harness.client.post(
        "/api/v1/households",
        headers={
            **headers,
            "Idempotency-Key": "household-stale-command",
            "If-Match": "1",
        },
    )
    assert changed_same_key.status_code == stale_new_key.status_code == 409


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/api/v1/households", None),
        ("put", "/api/v1/onboarding/profile", _profile()),
        ("post", "/api/v1/onboarding/steps/email_identity/select", {}),
        ("post", "/api/v1/onboarding/steps/email_identity/retry", None),
        ("post", "/api/v1/onboarding/steps/email_identity/check", None),
        ("post", "/api/v1/onboarding/reset/email_identity", None),
        ("post", "/api/v1/onboarding/cancel", None),
        ("post", "/api/v1/onboarding/delete", None),
        ("post", "/api/v1/auth/logout", None),
    ],
)
def test_every_private_unsafe_route_denies_anonymous_session(
    api_harness, method: str, path: str, body: dict | None
) -> None:
    headers = {
        "Origin": api_harness.config.public_origin,
        "X-CSRF-Token": "anonymous-csrf",
        "Idempotency-Key": "anonymous-command",
        "If-Match": "0",
    }
    response = getattr(api_harness.client, method)(path, headers=headers, json=body)
    assert response.status_code == 401


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/api/v1/households", None),
        ("put", "/api/v1/onboarding/profile", _profile()),
        ("post", "/api/v1/onboarding/steps/email_identity/select", {}),
        ("post", "/api/v1/onboarding/steps/email_identity/retry", None),
        ("post", "/api/v1/onboarding/steps/email_identity/check", None),
        ("post", "/api/v1/onboarding/reset/email_identity", None),
        ("post", "/api/v1/onboarding/cancel", None),
        ("post", "/api/v1/onboarding/delete", None),
        ("post", "/api/v1/auth/logout", None),
    ],
)
def test_every_private_unsafe_route_enforces_origin_and_csrf(
    api_harness, method: str, path: str, body: dict | None
) -> None:
    world = api_harness.create_principal()
    api_harness.authenticate(world)
    valid = {
        **api_harness.mutation_headers,
        "Idempotency-Key": "security-matrix-command",
        "If-Match": "0",
    }
    cases = (
        {name: value for name, value in valid.items() if name != "Origin"},
        {**valid, "Origin": "https://foreign.invalid"},
        {name: value for name, value in valid.items() if name != "X-CSRF-Token"},
        {**valid, "X-CSRF-Token": "wrong-csrf"},
    )

    for headers in cases:
        response = getattr(api_harness.client, method)(path, headers=headers, json=body)
        assert response.status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/me",
        "/api/v1/onboarding/current",
        "/api/v1/onboarding/export",
    ],
)
def test_every_private_safe_route_denies_anonymous_session(api_harness, path: str) -> None:
    assert api_harness.client.get(path).status_code == 401


def test_revoked_session_is_denied_and_responses_have_security_headers(api_harness) -> None:
    world = api_harness.create_principal("revoked-owner@family.test")
    api_harness.authenticate(world)
    healthy = api_harness.client.get("/api/v1/me")
    assert healthy.status_code == 200
    api_harness.container.auth.revoke_session(world.session.id)
    denied = api_harness.client.get("/api/v1/me")
    assert denied.status_code == 401
    for response in (healthy, denied, api_harness.client.get("/start")):
        assert response.headers["content-security-policy"].startswith("default-src 'self'")
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["x-content-type-options"] == "nosniff"
    assert denied.headers["cache-control"] == "no-store"


def test_authenticated_onboarding_html_is_never_cached(api_harness) -> None:
    world = api_harness.create_principal("no-store-owner@family.test")
    api_harness.authenticate(world)
    response = api_harness.client.get("/onboarding")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert "private" not in response.headers.get("cache-control", "")

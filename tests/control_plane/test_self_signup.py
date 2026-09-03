"""Self-service registration: a public request-link may create an account.

With `ABROLIA_SELF_SIGNUP_ENABLED=1` an address with no account gets an invite
link instead of nothing. Everything the public route already promised stays:
the same `accepted` answer either way, existing accounts get login/reauth, a
disabled account is not reopened, and the flag cannot be turned on over a
mailer that keeps the link to itself.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from control_plane.api.app import create_app
from control_plane.auth.mailer import MemoryMailer
from control_plane.config import ConfigurationError, ControlPlaneConfig
from control_plane.container import ControlPlaneContainer
from tests.control_plane.conftest import APIHarness


def _self_signup_config(root: Path) -> ControlPlaneConfig:
    # Production delivery is what the flag requires; the container still gets
    # a MemoryMailer, so the test reads the link instead of sending it.
    return replace(
        ControlPlaneConfig.for_test(root),
        self_signup_enabled=True,
        magic_link_delivery_enabled=True,
        resend_api_key="re_test",
        magic_link_from="Abrolia <login@example.test>",
    )


@pytest.fixture
def signup_harness(tmp_path: Path) -> APIHarness:
    config = _self_signup_config(tmp_path)
    mailer = MemoryMailer()
    active = ControlPlaneContainer.build(config, mailer=mailer)
    app = create_app(active_container=active)
    try:
        with TestClient(app, base_url=config.public_origin) as client:
            yield APIHarness(config, active, mailer, client)
    finally:
        active.close()


def test_self_signup_requires_production_delivery(tmp_path: Path) -> None:
    """A sign-up form over an in-memory mailer is a form whose links never arrive."""
    with pytest.raises(ConfigurationError, match="self-signup requires production magic-link delivery"):
        replace(ControlPlaneConfig.for_test(tmp_path), self_signup_enabled=True).validate()
    _self_signup_config(tmp_path).validate()


def test_self_signup_is_read_from_the_environment() -> None:
    assert ControlPlaneConfig.from_env({
        "ABROLIA_CONTROL_PLANE_DB": "/data/x.db",
        "ABROLIA_PUBLIC_ORIGIN": "https://app.example.test",
        "ABROLIA_ENCRYPTION_KEY": "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE=",
        "ABROLIA_LOOKUP_HMAC_KEY": "AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI=",
        "ABROLIA_TOKEN_HMAC_KEY": "AwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwM=",
        "ABROLIA_SELF_SIGNUP_ENABLED": "1",
        "ABROLIA_MAGIC_LINK_DELIVERY_ENABLED": "1",
        "ABROLIA_MAGIC_LINK_FROM": "Abrolia <login@example.test>",
        "ABROLIA_RESEND_API_KEY": "re_test",
    }).self_signup_enabled is True


def test_an_unknown_address_is_invited_and_the_link_creates_the_account(signup_harness) -> None:
    harness = signup_harness
    origin = {"Origin": harness.config.public_origin}

    response = harness.client.post(
        "/api/v1/auth/request-link", headers=origin, json={"email": "new-tester@pilot.test"}
    )
    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    assert [(m.recipient, m.purpose) for m in harness.mailer.sent] == [
        ("new-tester@pilot.test", "invite")
    ]
    assert harness.container.accounts.by_email("new-tester@pilot.test") is None, (
        "the account must not exist before the link is consumed"
    )

    token = harness.mailer.sent[0].url.rsplit("#token=", 1)[1]
    consumed = harness.client.post("/api/v1/auth/consume", headers=origin, json={"token": token})
    assert consumed.status_code == 200
    body = consumed.json()
    assert body["next"] == "/onboarding"
    account = harness.container.accounts.by_email("new-tester@pilot.test")
    assert account is not None and account.status == "active"
    assert body["account"]["id"] == account.id
    assert [h.id for h in harness.container.households.for_account(account.id)] == [
        body["household"]["id"]
    ]


def test_the_no_js_form_invites_too(signup_harness) -> None:
    harness = signup_harness
    response = harness.client.post(
        "/start/request-link",
        headers={"Origin": harness.config.public_origin},
        data={"email": "form-tester@pilot.test"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/start?sent=1"
    assert [(m.recipient, m.purpose) for m in harness.mailer.sent] == [
        ("form-tester@pilot.test", "invite")
    ]


def test_an_existing_account_still_gets_login_not_a_second_invite(signup_harness) -> None:
    harness = signup_harness
    existing = harness.create_principal("known@pilot.test")
    harness.client.post(
        "/api/v1/auth/request-link",
        headers={"Origin": harness.config.public_origin},
        json={"email": existing.account.recovery_email},
    )
    assert [(m.recipient, m.purpose) for m in harness.mailer.sent] == [
        (existing.account.recovery_email, "login")
    ]


def test_a_disabled_account_is_not_reopened_by_the_form(signup_harness) -> None:
    harness = signup_harness
    disabled = harness.create_principal("gone@pilot.test")
    harness.container.accounts.set_status(disabled.account.id, "deleted")
    response = harness.client.post(
        "/api/v1/auth/request-link",
        headers={"Origin": harness.config.public_origin},
        json={"email": disabled.account.recovery_email},
    )
    assert response.status_code == 202
    assert harness.mailer.sent == []


def test_the_start_page_says_what_the_flag_makes_true(signup_harness, api_harness) -> None:
    on = signup_harness.client.get("/start")
    assert on.status_code == 200
    assert "creates your account" in on.text
    assert ".test" not in on.text
    assert 'data-sent-message="If this address can be used' in on.text

    off = api_harness.client.get("/start")
    assert "reserved <code>.test</code> address" in off.text
    assert "creates your account" not in off.text

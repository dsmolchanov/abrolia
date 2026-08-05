from __future__ import annotations

import pytest


def test_verify_page_moves_fragment_to_post_and_clears_browser_history(api_harness) -> None:
    page = api_harness.client.get("/auth/verify#token=fragment-secret-canary")
    script = api_harness.client.get("/static/onboarding.js")
    assert page.status_code == script.status_code == 200
    assert "fragment-secret-canary" not in page.text
    assert "window.location.hash.slice(1)" in script.text
    assert 'history.replaceState(null, "", window.location.pathname)' in script.text
    assert 'fetch("/api/v1/auth/consume"' in script.text
    assert "window.location.replace(result.next)" in script.text


def test_onboarding_page_has_canonical_options_and_privacy_copy(api_harness) -> None:
    world = api_harness.create_principal("ui-owner@family.test")
    api_harness.authenticate(world)
    page = api_harness.client.get("/onboarding")
    assert page.status_code == 200
    html = page.text
    assert html.index('data-kind="abrolia_managed"') < html.index('data-kind="gmail_agent"')
    assert html.index('data-kind="shared_abrolia"') < html.index('data-kind="dedicated_number"')
    assert html.index('data-kind="telegram"') < html.index('data-kind="whatsapp"')
    assert "Separate agent Gmail" in html
    assert "personal Gmail or an app password" in html
    assert html.count("Beta") >= 2
    assert "QR linking requires explicit linked-device" in html
    assert "fallback contact only — never the agent inbox" in html
    assert "u***@family.test" in html
    assert world.account.recovery_email not in html


def test_ui_is_server_state_driven_and_polling_is_read_only(api_harness) -> None:
    world = api_harness.create_principal("resume-owner@family.test")
    api_harness.authenticate(world)
    first = api_harness.client.get("/onboarding")
    second = api_harness.client.get("/onboarding")
    script = api_harness.client.get("/static/onboarding.js").text
    assert first.status_code == second.status_code == 200
    assert 'data-version="0"' in first.text
    assert api_harness.container.onboarding_repository.snapshot(world.household.id).version == 0
    assert 'fetch("/api/v1/onboarding/current"' in script
    assert "state = snapshot" in script
    assert "commandHeaders(state.version)" in script
    assert '"Idempotency-Key": crypto.randomUUID()' in script
    assert '"If-Match": String(version)' in script
    assert script.index("setInteractive(false);") < script.index("\n  refresh();")
    assert "if (state === null && !(await refresh())) return;" in script
    assert 'state?.state)' in script
    assert '"runtime_provisioning", "activating"' in script
    assert "popstate" not in script, "back navigation must not silently mutate durable choices"


def test_ui_renders_durable_runtime_and_completion_states(api_harness) -> None:
    world = api_harness.create_principal("durable-ui-owner@family.test")
    api_harness.authenticate(world)
    cases = {
        "runtime_provisioning": "Preparing your private runtime",
        "activating": "Activating your runtime",
        "complete": "Setup complete",
    }
    for version, (workflow_state, copy) in enumerate(cases.items(), start=1):
        with api_harness.container.database.write() as connection:
            connection.execute(
                "UPDATE onboarding_workflows SET state = ?, current_step = 'runtime',"
                " version = ? WHERE household_id = ?",
                (workflow_state, version, world.household.id),
            )
        page = api_harness.client.get("/onboarding")
        assert page.status_code == 200
        assert f'data-workflow-state="{workflow_state}"' in page.text
        assert copy in page.text
        assert 'data-step="primary_channel" hidden' in page.text


def test_waiting_user_has_check_action_and_verifying_has_checking_copy(api_harness) -> None:
    world = api_harness.create_principal("check-ui-owner@family.test")
    api_harness.authenticate(world)
    with api_harness.container.database.write() as connection:
        workflow = connection.execute(
            "SELECT id FROM onboarding_workflows WHERE household_id = ?",
            (world.household.id,),
        ).fetchone()
        connection.execute(
            "UPDATE onboarding_workflows SET state = 'in_progress',"
            " current_step = 'email_identity', version = 1 WHERE id = ?",
            (workflow["id"],),
        )
        connection.execute(
            "UPDATE onboarding_steps SET status = 'waiting_user'"
            " WHERE workflow_id = ? AND kind = 'email_identity'",
            (workflow["id"],),
        )
    waiting = api_harness.client.get("/onboarding")
    assert "Complete the synthetic verification, then check again." in waiting.text
    assert 'action="/onboarding/check/email_identity"' in waiting.text
    assert '<button id="check-step" type="submit">Check again</button>' in waiting.text

    with api_harness.container.database.write() as connection:
        connection.execute(
            "UPDATE onboarding_steps SET status = 'verifying'"
            " WHERE workflow_id = ? AND kind = 'email_identity'",
            (workflow["id"],),
        )
    verifying = api_harness.client.get("/onboarding")
    assert "Checking verification" in verifying.text
    assert "durable worker is inspecting the existing provider result" in verifying.text
    script = api_harness.client.get("/static/onboarding.js").text
    assert "/api/v1/onboarding/steps/${event.currentTarget.dataset.kind}/check" in script
    assert '["provisioning", "verifying"]' in script


def test_verified_choice_changes_require_an_explicit_reset_control(api_harness) -> None:
    world = api_harness.create_principal("reset-ui-owner@family.test")
    api_harness.authenticate(world)
    shared = {
        "csrf_token": world.session.csrf_token,
        "version": "0",
        "first_name": "Reset",
        "last_name": "Owner",
        "family_language": "en",
        "timezone": "Europe/Prague",
        "country_code": "CZ",
        "residency_mode": "eu-app",
        "idempotency_key": "reset-ui-profile",
    }
    origin = {"Origin": api_harness.config.public_origin}
    assert api_harness.client.post(
        "/onboarding/profile", headers=origin, data=shared, follow_redirects=False
    ).status_code == 303
    assert api_harness.client.post(
        "/onboarding/select/email_identity",
        headers=origin,
        data={
            "csrf_token": world.session.csrf_token,
            "idempotency_key": "reset-ui-email",
            "version": "1",
            "kind": "abrolia_managed",
            "local_part": "reset-agent",
        },
        follow_redirects=False,
    ).status_code == 303
    assert api_harness.container.worker.run_once().status == "succeeded"
    html = api_harness.client.get("/onboarding").text
    script = api_harness.client.get("/static/onboarding.js").text
    assert 'data-reset="email_identity"' in html
    assert 'action="/onboarding/reset/email_identity"' in html
    assert "/api/v1/onboarding/reset/" in script


def test_onboarding_remains_usable_through_server_forms_without_javascript(
    api_harness,
) -> None:
    world = api_harness.create_principal("form-owner@family.test")
    api_harness.authenticate(world)
    response = api_harness.client.post(
        "/onboarding/profile",
        headers={"Origin": api_harness.config.public_origin},
        data={
            "csrf_token": world.session.csrf_token,
            "idempotency_key": "server-form-profile",
            "version": "0",
            "first_name": "Server",
            "last_name": "Form",
            "family_language": "en",
            "timezone": "Europe/Prague",
            "country_code": "CZ",
            "residency_mode": "eu-app",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/onboarding"
    snapshot = api_harness.container.onboarding_repository.snapshot(world.household.id)
    assert snapshot.version == 1
    assert snapshot.current_step.value == "email_identity"
    page = api_harness.client.get("/onboarding")
    assert 'action="/onboarding/select/email_identity"' in page.text
    assert 'name="csrf_token"' in page.text
    assert 'name="idempotency_key"' in page.text
    assert 'name="version" value="1"' in page.text


def test_start_page_is_first_party_and_has_no_analytics(api_harness) -> None:
    page = api_harness.client.get("/start")
    assert page.status_code == 200
    assert "reserved <code>.test</code> address" in page.text
    assert "Real family data" in page.text
    assert "analytics" not in page.text.casefold()
    assert "googletagmanager" not in page.text.casefold()
    assert "segment" not in page.text.casefold()
    assert "http://" not in page.text and "https://" not in page.text


def test_no_js_request_link_never_invites_an_unknown_test_address(api_harness) -> None:
    existing = api_harness.create_principal("form-login@pilot.test")
    origin = {"Origin": api_harness.config.public_origin}
    unknown = api_harness.client.post(
        "/start/request-link",
        headers=origin,
        data={"email": "unknown-form@pilot.test"},
        follow_redirects=False,
    )
    eligible = api_harness.client.post(
        "/start/request-link",
        headers=origin,
        data={"email": existing.account.recovery_email},
        follow_redirects=False,
    )

    assert unknown.status_code == eligible.status_code == 303
    assert unknown.headers["location"] == eligible.headers["location"] == "/start?sent=1"
    assert [(item.recipient, item.purpose) for item in api_harness.mailer.sent] == [
        (existing.account.recovery_email, "login")
    ]
    rows = api_harness.container.database.query(
        "SELECT purpose, account_id FROM auth_tokens"
    )
    assert [dict(row) for row in rows] == [{
        "purpose": "login",
        "account_id": existing.account.id,
    }]


def test_no_js_whatsapp_duplicate_post_replays_same_consent_receipt(api_harness) -> None:
    world = api_harness.create_principal("form-replay@family.test")
    api_harness.authenticate(world)
    origin = {"Origin": api_harness.config.public_origin}
    profile = api_harness.client.post(
        "/onboarding/profile",
        headers=origin,
        data={
            "csrf_token": world.session.csrf_token,
            "idempotency_key": "form-replay-profile",
            "version": "0",
            "first_name": "Replay",
            "last_name": "Owner",
            "family_language": "en",
            "timezone": "Europe/Prague",
            "country_code": "CZ",
            "residency_mode": "eu-app",
        },
        follow_redirects=False,
    )
    assert profile.headers["location"] == "/onboarding"
    email = api_harness.client.post(
        "/onboarding/select/email_identity",
        headers=origin,
        data={
            "csrf_token": world.session.csrf_token,
            "idempotency_key": "form-replay-email",
            "version": "1",
            "kind": "abrolia_managed",
            "local_part": "replay-agent",
        },
        follow_redirects=False,
    )
    assert email.headers["location"] == "/onboarding"
    assert api_harness.container.worker.run_once().status == "succeeded"
    before = api_harness.container.onboarding_repository.snapshot(world.household.id)
    assert before.current_step.value == "whatsapp_identity"
    form = {
        "csrf_token": world.session.csrf_token,
        "idempotency_key": "form-replay-whatsapp",
        "version": str(before.version),
        "kind": "shared_abrolia",
        "privacy_notice_accepted": "yes",
    }

    first = api_harness.client.post(
        "/onboarding/select/whatsapp_identity",
        headers=origin,
        data=form,
        follow_redirects=False,
    )
    after_first = api_harness.container.onboarding_repository.snapshot(world.household.id)
    second = api_harness.client.post(
        "/onboarding/select/whatsapp_identity",
        headers=origin,
        data=form,
        follow_redirects=False,
    )
    after_second = api_harness.container.onboarding_repository.snapshot(world.household.id)

    assert first.headers["location"] == second.headers["location"] == "/onboarding"
    assert after_second == after_first
    workflow = api_harness.container.onboarding_repository.workflow_for_household(
        world.household.id
    )
    selection = api_harness.container.onboarding_repository.selection(
        workflow.id, "whatsapp_identity"
    )
    assert selection["privacy_notice_receipt_id"]
    assert len(api_harness.container.database.query(
        "SELECT id FROM consent_receipts WHERE household_id = ?",
        (world.household.id,),
    )) == 1
    assert len(api_harness.container.database.query(
        "SELECT id FROM provisioning_jobs WHERE household_id = ?"
        " AND kind = 'whatsapp_identity'",
        (world.household.id,),
    )) == 1


@pytest.mark.parametrize(
    "path",
    [
        "/onboarding/profile",
        "/onboarding/select/email_identity",
        "/onboarding/retry/email_identity",
        "/onboarding/check/email_identity",
        "/onboarding/reset/email_identity",
    ],
)
def test_every_progressive_onboarding_command_enforces_origin_and_csrf(
    api_harness, path: str
) -> None:
    world = api_harness.create_principal("form-security-owner@family.test")
    api_harness.authenticate(world)
    form = {
        "csrf_token": world.session.csrf_token,
        "idempotency_key": "form-security-command",
        "version": "0",
    }

    assert api_harness.client.post(path, data=form).status_code == 403
    assert api_harness.client.post(
        path, headers={"Origin": "https://foreign.invalid"}, data=form
    ).status_code == 403
    assert api_harness.client.post(
        path,
        headers={"Origin": api_harness.config.public_origin},
        data={key: value for key, value in form.items() if key != "csrf_token"},
    ).status_code == 403
    assert api_harness.client.post(
        path,
        headers={"Origin": api_harness.config.public_origin},
        data={**form, "csrf_token": "wrong-csrf"},
    ).status_code == 403

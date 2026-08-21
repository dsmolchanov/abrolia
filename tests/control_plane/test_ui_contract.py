from __future__ import annotations

import html as html_module

import pytest

from control_plane.privacy.consent import consent_version_and_sha, consent_version_and_text


def _restriction_form_binding() -> dict[str, str]:
    version, sha256 = consent_version_and_sha(
        "special_category_content_restriction"
    )
    return {
        "special_category_restriction_text_version": version,
        "special_category_restriction_text_sha256": sha256,
    }


def _household_form_binding() -> dict[str, str]:
    """The Art. 9(2)(a) consent the real-provider options also require.

    `gmail_agent` routes to `google-oauth`, a real provider, so the gate demands
    this consent as well as the restriction. Without it a refused post says only
    that something was missing — which is not what a kill-switch test is for.
    """
    version, sha256 = consent_version_and_sha("special_category_household_content")
    return {
        "special_category_household_consent": "yes",
        "special_category_household_text_version": version,
        "special_category_household_text_sha256": sha256,
    }


def test_verify_page_moves_fragment_to_post_and_clears_browser_history(api_harness) -> None:
    page = api_harness.client.get("/auth/verify#token=fragment-secret-canary")
    script = api_harness.client.get("/static/onboarding.js")
    assert page.status_code == script.status_code == 200
    assert "fragment-secret-canary" not in page.text
    assert "window.location.hash.slice(1)" in script.text
    assert 'history.replaceState(null, "", window.location.pathname)' in script.text
    assert 'fetch("/api/v1/auth/consume"' in script.text
    assert "window.location.replace(result.next)" in script.text


def test_onboarding_page_has_canonical_options_and_privacy_copy(
    api_harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # This is the FULL card set: ordering, copy and per-card consent bindings.
    # `family_domain` and `gmail_agent` are cut from MVP and hidden unless their
    # kill switches are on, so the contract they belong to has to turn them on.
    # What the page does when they are off is asserted below, on its own terms.
    monkeypatch.setenv("ABROLIA_BYO_EMAIL_ENABLED", "1")
    monkeypatch.setenv("ABROLIA_GMAIL_ENABLED", "1")
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
    assert html.count('name="special_category_restriction_acknowledged"') == 3
    version, text = consent_version_and_text("special_category_content_restriction")
    assert version in html
    assert text in html_module.unescape(html)
    _, sha256 = consent_version_and_sha("special_category_content_restriction")
    assert html.count(f'value="{sha256}"') == 3
    assert html.count('aria-describedby="special-category-content-restriction"') == 3
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
    assert (
        "selection.special_category_restriction_text_version = "
        "form.elements.special_category_restriction_text_version.value" in script
    )
    assert (
        "selection.special_category_restriction_text_sha256 = "
        "form.elements.special_category_restriction_text_sha256.value" in script
    )
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
            "UPDATE onboarding_steps SET status = 'waiting_user',"
            " public_status_json = ?"
            " WHERE workflow_id = ? AND kind = 'email_identity'",
            (
                '{"dns_records":[{"host":"_nerve.family.example.test",'
                '"type":"MX","priority":10,"purpose":"mail-routing",'
                '"value":"synthetic-domain-proof"}],'
                '"record_status":{"mx":false,"ownership":true}}',
                workflow["id"],
            ),
        )
    waiting = api_harness.client.get("/onboarding")
    assert "Complete the synthetic verification, then check again." in waiting.text
    assert 'action="/onboarding/check/email_identity"' in waiting.text
    assert '<button id="check-step" type="submit">Check again</button>' in waiting.text
    assert "DNS records to add" in waiting.text
    assert "_nerve.family.example.test" in waiting.text
    assert "MX _nerve.family.example.test 10 synthetic-domain-proof" in waiting.text
    assert "mail-routing" in waiting.text
    assert "synthetic-domain-proof" in waiting.text
    assert "DNS verification status" in waiting.text
    assert "mx: pending" in waiting.text
    assert "ownership: verified" in waiting.text

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
    assert "current?.public_status?.dns_records" in script
    assert "current?.public_status?.record_status" in script
    assert 'ready ? "verified" : "pending"' in script
    assert "/api/v1/email/domain/guidance?domain=" in script
    assert "Recommended mail subdomain:" in script


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
        "country_code": "DE",
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
                "special_category_restriction_acknowledged": "yes",
                **_restriction_form_binding(),
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
            "country_code": "DE",
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
            "country_code": "DE",
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
            "special_category_restriction_acknowledged": "yes",
            **_restriction_form_binding(),
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
    )) == 2
    assert len(api_harness.container.database.query(
        "SELECT id FROM provisioning_jobs WHERE household_id = ?"
        " AND kind = 'whatsapp_identity'",
        (world.household.id,),
    )) == 1


def test_no_js_email_requires_and_records_english_content_restriction(api_harness) -> None:
    world = api_harness.create_principal("restriction-owner@family.test")
    api_harness.authenticate(world)
    origin = {"Origin": api_harness.config.public_origin}
    profile = api_harness.client.post(
        "/onboarding/profile",
        headers=origin,
        data={
            "csrf_token": world.session.csrf_token,
            "idempotency_key": "restriction-profile",
            "version": "0",
            "first_name": "Restriction",
            "last_name": "Owner",
            "family_language": "ru",
            "timezone": "Europe/Prague",
            "country_code": "DE",
            "residency_mode": "eu-app",
        },
        follow_redirects=False,
    )
    assert profile.headers["location"] == "/onboarding"
    shared = {
        "csrf_token": world.session.csrf_token,
        "version": "1",
        "kind": "abrolia_managed",
        "local_part": "restriction-agent",
    }
    rejected = api_harness.client.post(
        "/onboarding/select/email_identity",
        headers=origin,
        data={**shared, "idempotency_key": "restriction-missing"},
        follow_redirects=False,
    )
    assert rejected.headers["location"] == "/onboarding?error=selection"
    assert api_harness.container.onboarding_repository.snapshot(
        world.household.id
    ).version == 1

    accepted = api_harness.client.post(
        "/onboarding/select/email_identity",
        headers=origin,
        data={
            **shared,
            "idempotency_key": "restriction-accepted",
            "special_category_restriction_acknowledged": "yes",
            **_restriction_form_binding(),
        },
        follow_redirects=False,
    )
    assert accepted.headers["location"] == "/onboarding"
    receipt = api_harness.container.database.query_one(
        "SELECT purpose, text_version, locale, revoked_at FROM consent_receipts"
        " WHERE household_id = ?",
        (world.household.id,),
    )
    assert dict(receipt) == {
        "purpose": "special_category_content_restriction",
        "text_version": consent_version_and_sha(
            "special_category_content_restriction"
        )[0],
        "locale": "en",
        "revoked_at": None,
    }


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


def test_the_onboarding_page_authenticates_through_a_dependency(api_harness) -> None:
    """An auth dependency is checkable; an inline `try` is one edit from bypass.

    The inline version behaved correctly — it redirected — and that is the point
    of the rule it broke: a dependency is visible in the route's signature and
    survives an early return added above it later, where an inline check would
    simply be stepped over.

    Behaviour is unchanged: an unauthenticated browser still gets a 303 to
    `/start`, not a 401 it would render as an error page.
    """
    from control_plane.api.app import create_app
    from control_plane.api.dependencies import browser_session

    unauthenticated = api_harness.client.get("/onboarding", follow_redirects=False)
    assert unauthenticated.status_code == 303
    assert unauthenticated.headers["location"] == "/start"

    app = create_app(active_container=api_harness.container)
    route = next(
        candidate
        for candidate in app.routes
        if getattr(candidate, "path", None) == "/onboarding"
    )
    assert any(
        dependant.call is browser_session
        for dependant in route.dependant.dependencies
    ), "the page's authentication is not a declared dependency"


def test_an_authenticated_onboarding_page_still_renders(api_harness) -> None:
    api_harness.authenticate(api_harness.create_principal())

    page = api_harness.client.get("/onboarding")

    assert page.status_code == 200
    assert "onboarding" in page.text.lower()


# ---------------------------------------------------------------------------
# The MVP cut, on the page.
#
# Hiding a card is a courtesy — `OnboardingService.select` refuses a cut option
# either way, and `ProvisioningWorker` refuses it again at the provider call for
# work already queued. What the page must never do is DISAGREE with the gate.
# That has gone wrong here before, in the other direction: the server started
# keying the Art. 9(2)(a) consent on the provider while the page still keyed it
# on the rollout flag, so the form rendered no consent and the server then
# rejected the submission for lacking it — browser onboarding became impossible.
# ---------------------------------------------------------------------------

CUT_CARDS = {
    "gmail_agent": "ABROLIA_GMAIL_ENABLED",
    "family_domain": "ABROLIA_BYO_EMAIL_ENABLED",
}


def _email_cards(html: str) -> set[str]:
    return {
        kind
        for kind in ("abrolia_managed", "gmail_agent", "family_domain")
        if f'data-kind="{kind}"' in html
    }


@pytest.mark.parametrize(
    "enabled",
    [
        frozenset(),
        frozenset({"gmail_agent"}),
        frozenset({"family_domain"}),
        frozenset({"gmail_agent", "family_domain"}),
    ],
    ids=["neither", "gmail-only", "byo-only", "both"],
)
def test_the_page_offers_exactly_the_options_the_server_would_accept(
    api_harness, monkeypatch: pytest.MonkeyPatch, enabled: frozenset[str]
) -> None:
    """Every combination, not just all-off.

    Asserting only the both-off case would pass just as well if the template
    hard-coded the managed card and dropped the other two outright, which is a
    different product.
    """

    for option, env_name in CUT_CARDS.items():
        monkeypatch.setenv(env_name, "1" if option in enabled else "0")
    world = api_harness.create_principal(f"cut-{'-'.join(sorted(enabled)) or 'none'}@family.test")
    api_harness.authenticate(world)

    html = api_harness.client.get("/onboarding").text

    # Managed is deliberately ungated: something must always be offerable, or
    # the page is a dead end.
    assert _email_cards(html) == {"abrolia_managed"} | set(enabled)


@pytest.mark.parametrize("option", sorted(CUT_CARDS))
def test_a_hidden_card_is_also_refused_when_submitted_anyway(
    api_harness, monkeypatch: pytest.MonkeyPatch, option: str
) -> None:
    """The page is not the enforcement, and this is what says so.

    A form can be replayed, scripted, or posted from a page rendered before the
    operator flipped the switch. Hiding the card must not be the only thing
    stopping the option.
    """

    monkeypatch.setenv(CUT_CARDS[option], "0")
    world = api_harness.create_principal(f"replay-{option}@family.test")
    api_harness.authenticate(world)
    origin = api_harness.mutation_headers

    html = api_harness.client.get("/onboarding").text
    assert f'data-kind="{option}"' not in html, "the card was rendered after all"

    shared = {
        "csrf_token": world.session.csrf_token,
        "idempotency_key": f"replay-{option}",
        "version": "0",
        "first_name": "Test",
        "last_name": "Family",
        "family_language": "en",
        "timezone": "Europe/Prague",
        "country_code": "DE",
        "residency_mode": "eu-app",
    }
    assert api_harness.client.post(
        "/onboarding/profile", headers=origin, data=shared, follow_redirects=False
    ).status_code == 303
    assert api_harness.container.worker.run_once().status == "succeeded"

    posted = api_harness.client.post(
        "/onboarding/select/email_identity",
        headers=origin,
        data={
            "csrf_token": world.session.csrf_token,
            "idempotency_key": f"replay-select-{option}",
            "version": "1",
            "kind": option,
            "domain": "family.example.test",
            "local_part": "assistant",
            "special_category_restriction_acknowledged": "yes",
            **_restriction_form_binding(),
            **_household_form_binding(),
        },
        follow_redirects=False,
    )
    # The route redirects on rejection too, so the status code says nothing.
    # `?error=selection` is what a refused command looks like here.
    assert posted.status_code == 303
    assert posted.headers["location"] == "/onboarding?error=selection"

    # And the durable state is the real assertion: no email identity was
    # selected, whatever the page did.
    snapshot = api_harness.container.onboarding_repository.snapshot(
        world.household.id
    )
    assert snapshot.current_step.value == "email_identity", (
        "the workflow moved past the step a cut option was supposed to fail"
    )
    assert api_harness.container.database.query_one(
        "SELECT 1 FROM email_identities WHERE household_id = ?",
        (world.household.id,),
    ) is None, "a cut option created an email identity"

    # The control. Without it, "refused" proves only that SOMETHING refused —
    # a malformed field, a missing acknowledgement, the wrong version — and the
    # test would pass with the kill switch removed entirely. The identical post
    # must succeed once the operator turns the option back on.
    monkeypatch.setenv(CUT_CARDS[option], "1")
    allowed = api_harness.client.post(
        "/onboarding/select/email_identity",
        headers=origin,
        data={
            "csrf_token": world.session.csrf_token,
            "idempotency_key": f"allowed-select-{option}",
            "version": "1",
            "kind": option,
            "domain": "family.example.test",
            "local_part": "assistant",
            "special_category_restriction_acknowledged": "yes",
            **_restriction_form_binding(),
            **_household_form_binding(),
        },
        follow_redirects=False,
    )
    assert allowed.headers["location"] == "/onboarding", (
        "the switch was not the reason the first post was refused"
    )


@pytest.mark.parametrize(
    "enabled",
    [
        frozenset(),
        frozenset({"gmail_agent"}),
        frozenset({"family_domain"}),
        frozenset({"gmail_agent", "family_domain"}),
    ],
    ids=["neither", "gmail-only", "byo-only", "both"],
)
def test_the_art9_statement_appears_iff_a_rendered_form_asks_for_it(
    api_harness, monkeypatch: pytest.MonkeyPatch, enabled: frozenset[str]
) -> None:
    """Consent copy for a path nobody can choose is worse than none.

    The statement used to be keyed on whether ANY option processed real content.
    With both switches off the only offered option is the synthetic managed one,
    but `gmail_agent` still counts as real because it maps to `google-oauth` —
    so the page carried explicit Art. 9(2)(a) consent language while no rendered
    form asked for that consent and nothing on offer could produce it.

    Asserted as an equivalence in both directions, because each failure mode is
    its own harm: copy without a checkbox asks for consent to processing that
    cannot happen, and a checkbox without copy takes consent to a statement the
    family was never shown — which is not consent at all.
    """

    for option, env_name in CUT_CARDS.items():
        monkeypatch.setenv(env_name, "1" if option in enabled else "0")
    world = api_harness.create_principal(
        f"art9-{'-'.join(sorted(enabled)) or 'none'}@family.test"
    )
    api_harness.authenticate(world)

    html = api_harness.client.get("/onboarding").text
    statement_shown = 'id="special-category-household-content"' in html
    consent_asked = 'name="special_category_household_consent"' in html

    assert statement_shown == consent_asked, (
        f"statement={statement_shown} but checkbox={consent_asked}"
    )

    # An equivalence is also satisfied by neither side ever appearing, so pin
    # which combinations must produce the copy. The asymmetry is real and is the
    # whole reason the aggregate was wrong: `gmail_agent` maps to `google-oauth`
    # and processes real content in ANY configuration, while `family_domain` in
    # the synthetic circuit routes to `fake-email` and processes none. So
    # offering Gmail must raise the statement, and offering BYO alone must not.
    assert statement_shown == ("gmail_agent" in enabled)

    _, sha256 = consent_version_and_sha("special_category_household_content")
    if statement_shown:
        version, text = consent_version_and_text("special_category_household_content")
        assert version in html
        assert text in html_module.unescape(html)
        assert f'value="{sha256}"' in html
    else:
        # The binding a form would submit must be gone too, not merely the
        # visible copy — a hidden field naming a statement the page did not
        # show is the same defect wearing a different hat.
        assert f'value="{sha256}"' not in html

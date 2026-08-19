"""Art. 9(2)(a) consent and the Art. 9(4) country determination, end to end.

Three defects are pinned here, each of which let real family content be
processed without a valid Art. 9 condition:

1. the browser could not supply the consent at all, so a real-email rollout
   rejected every web submission — the feature was unshippable;
2. the requirement was keyed on "a receipt row exists", which made it
   self-satisfying, and the row was never checked for revocation or currency;
3. no country gate existed, though `lawful-bases.md` section 3 refuses real data
   for any country whose Art. 9(4) result has not been recorded.
"""

from __future__ import annotations

import html as html_module
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from control_plane.api.app import create_app
from control_plane.api.web import _selection
from control_plane.auth.mailer import MemoryMailer
from control_plane.config import ControlPlaneConfig
from control_plane.container import ControlPlaneContainer
from control_plane.models import StepKind
from control_plane.onboarding.contracts import InvalidTransition
from control_plane.privacy.art9 import permitted_countries, real_content_refusal
from control_plane.privacy.consent import (
    CONTENT_RESTRICTION_PURPOSE,
    HOUSEHOLD_CONTENT_PURPOSE,
    consent_version_and_sha,
    manifest_required_purposes,
    processes_real_household_content,
    required_consent_purposes,
)

from .conftest import APIHarness

RESTRICTION_VERSION, RESTRICTION_SHA = consent_version_and_sha(
    CONTENT_RESTRICTION_PURPOSE
)
HOUSEHOLD_VERSION, HOUSEHOLD_SHA = consent_version_and_sha(HOUSEHOLD_CONTENT_PURPOSE)


def real_email_selection(**overrides: object) -> dict[str, object]:
    selection: dict[str, object] = {
        "kind": "abrolia_managed",
        "local_part": "family-agent",
        "special_category_restriction_acknowledged": True,
        "special_category_restriction_receipt_id": "10000000-0000-4000-8000-000000000041",
        "special_category_restriction_text_version": RESTRICTION_VERSION,
        "special_category_restriction_text_sha256": RESTRICTION_SHA,
        "special_category_household_consent": True,
        "special_category_household_receipt_id": "10000000-0000-4000-8000-000000000042",
        "special_category_household_text_version": HOUSEHOLD_VERSION,
        "special_category_household_text_sha256": HOUSEHOLD_SHA,
    }
    selection.update(overrides)
    return selection


def set_country(cp_stack, country_code: str) -> None:
    """Move the household to a country, after the profile step has run.

    `complete_profile` fixes the country at DE; the gate reads the stored
    profile, so the column is what has to move.
    """
    with cp_stack.database.write() as connection:
        connection.execute(
            "UPDATE households SET country_code = ? WHERE id = ?",
            (country_code, cp_stack.household.id),
        )


def enable_real_email(cp_stack) -> None:
    """Mirror what the container does, flag AND providers together.

    `ControlPlaneContainer.build` derives the email providers from
    `real_email_enabled`; flipping only the flag produces a configuration that
    cannot exist in production, and the content gate follows the provider.
    """
    cp_stack.service.real_email_enabled = True
    cp_stack.service.real_email_household_allowlist = frozenset({cp_stack.household.id})
    cp_stack.service.email_provider = "nerve-managed"
    cp_stack.service.byo_domain_provider = "nerve-byo-domain"


# ---------------------------------------------------------------------------
# 3. The Art. 9(4) country determination.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("country", sorted(permitted_countries()))
def test_countries_with_a_clear_determination_are_permitted(country: str) -> None:
    assert real_content_refusal(country) is None


def test_only_de_nl_es_are_permitted_today() -> None:
    """Italy is in the pilot but is NOT permitted, and that is deliberate.

    `lawful-bases.md` section 3 requires the Garante's misure di garanzia to be
    reconciled against our TOMs and the result recorded before the first Italian
    household connects. No such record exists, so Italy fails closed.
    """
    assert permitted_countries() == frozenset({"DE", "NL", "ES"})


def test_italy_is_refused_and_says_why() -> None:
    refusal = real_content_refusal("IT")
    assert refusal is not None
    assert "Garante" in refusal
    assert "2-septies" in refusal


@pytest.mark.parametrize("country", ["CZ", "FR", "US", "", None])
def test_countries_without_a_recorded_determination_are_refused(country) -> None:
    """An allowlist, not a denylist: absence means nobody checked."""
    assert real_content_refusal(country) is not None


def test_country_is_case_insensitive() -> None:
    assert real_content_refusal("de") is None


def test_real_email_is_refused_for_an_undetermined_country(cp_stack) -> None:
    """The gate that was missing entirely: consent alone was enough."""
    cp_stack.complete_profile()
    enable_real_email(cp_stack)
    set_country(cp_stack, "CZ")

    with pytest.raises(InvalidTransition, match="no Art. 9\\(4\\) determination"):
        cp_stack.service.select(
            cp_stack.household.id,
            StepKind.EMAIL,
            real_email_selection(),
            context=cp_stack.context(),
        )


def test_real_email_is_refused_for_italy_until_the_garante_record_exists(
    cp_stack,
) -> None:
    cp_stack.complete_profile()
    enable_real_email(cp_stack)
    set_country(cp_stack, "IT")

    with pytest.raises(InvalidTransition, match="Garante"):
        cp_stack.service.select(
            cp_stack.household.id,
            StepKind.EMAIL,
            real_email_selection(),
            context=cp_stack.context(),
        )


def test_synthetic_rollout_is_unaffected_by_the_country_gate(cp_stack) -> None:
    """No personal data, no Art. 9 question — the gate must not reach here."""
    cp_stack.complete_profile()
    set_country(cp_stack, "CZ")

    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        {"kind": "abrolia_managed", "local_part": "family-agent"},
        context=cp_stack.context(),
    )


# ---------------------------------------------------------------------------
# 2. What a household owes is derived, never inferred from what it holds.
# ---------------------------------------------------------------------------


def test_synthetic_providers_do_not_owe_the_article_9_consent() -> None:
    purposes = required_consent_purposes(
        provider_kind="fake-email", whatsapp_dedicated_number=False
    )
    assert HOUSEHOLD_CONTENT_PURPOSE not in purposes
    assert CONTENT_RESTRICTION_PURPOSE in purposes


@pytest.mark.parametrize(
    "provider_kind", ["nerve-managed", "nerve-byo-domain", "google-oauth"]
)
def test_real_providers_owe_the_article_9_consent(provider_kind: str) -> None:
    assert HOUSEHOLD_CONTENT_PURPOSE in required_consent_purposes(
        provider_kind=provider_kind, whatsapp_dedicated_number=False
    )


@pytest.mark.parametrize("provider_kind", ["a-provider-added-tomorrow", "", None])
def test_an_unrecognised_provider_owes_the_consent(provider_kind) -> None:
    """Fail closed: forgetting to register a provider blocks, never opens."""
    assert processes_real_household_content(provider_kind)


def test_manifest_purposes_drop_the_unknown_and_keep_the_floor() -> None:
    resolved = manifest_required_purposes(
        {"consent": {"required_purposes": ["invented_purpose"]}}
    )
    assert resolved == [CONTENT_RESTRICTION_PURPOSE]
    assert manifest_required_purposes(None) == [CONTENT_RESTRICTION_PURPOSE]
    assert HOUSEHOLD_CONTENT_PURPOSE in manifest_required_purposes(
        {"consent": {"required_purposes": [HOUSEHOLD_CONTENT_PURPOSE]}}
    )


WHATSAPP_SELECTION = {
    "kind": "shared_abrolia",
    "member_phone_test_ref": "synthetic-phone:owner-one",
    "privacy_notice_receipt_id": "synthetic-receipt-wa",
}
CHANNEL_SELECTION = {
    "kind": "telegram",
    "actor_id": "synthetic-owner",
    "chat_id": "synthetic-chat",
}


def complete_onboarding(cp_stack, *, base_time: float = 1_800_000_000.0) -> None:
    """Drive a household to the point where the planner will issue a revision."""
    cp_stack.complete_profile()
    worker = cp_stack.make_worker(now=base_time + 50)
    for kind, selection in (
        (StepKind.EMAIL, real_email_selection()),
        (StepKind.WHATSAPP, WHATSAPP_SELECTION),
    ):
        cp_stack.service.select(
            cp_stack.household.id, kind, selection, context=cp_stack.context()
        )
        assert worker.run_once().status == "succeeded"
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.PRIMARY_CHANNEL,
        CHANNEL_SELECTION,
        context=cp_stack.context(),
    )


@pytest.mark.parametrize(
    ("damage", "statement"),
    [
        pytest.param(
            "revoked",
            "UPDATE consent_receipts SET revoked_at = 1.0"
            " WHERE household_id = ? AND purpose = ?",
            id="revoked",
        ),
        pytest.param(
            "superseded",
            "UPDATE consent_receipts SET text_version = 'stale-v0'"
            " WHERE household_id = ? AND purpose = ?",
            id="superseded-copy",
        ),
    ],
)
def test_planner_refuses_a_receipt_that_is_not_current(
    cp_stack, damage: str, statement: str
) -> None:
    """Presence used to be enough.

    The planner's query filtered on `household_id` alone — no `revoked_at`, no
    version or digest — so a withdrawn consent and one bound to superseded copy
    both still satisfied it, and the stale receipt was carried into the manifest
    for the runtime to enforce.
    """
    complete_onboarding(cp_stack)
    with cp_stack.database.write() as connection:
        connection.execute(
            statement, (cp_stack.household.id, CONTENT_RESTRICTION_PURPOSE)
        )

    with pytest.raises(ValueError, match="authoritative onboarding consent"):
        cp_stack.make_worker(now=1_800_000_050.0).run_once()
    assert cp_stack.database.query("SELECT id FROM config_revisions") == []


def test_planner_issues_when_every_receipt_is_current(cp_stack) -> None:
    """The same path must still succeed, or the test above proves nothing."""
    complete_onboarding(cp_stack)
    assert cp_stack.make_worker(now=1_800_000_050.0).run_once().status == "succeeded"
    assert cp_stack.database.query("SELECT id FROM config_revisions") != []


# ---------------------------------------------------------------------------
# 1. The browser must be able to give the consent at all.
# ---------------------------------------------------------------------------


class _Context:
    account_id = "20000000-0000-4000-8000-000000000001"
    idempotency_key = "30000000-0000-4000-8000-000000000001"


@pytest.mark.parametrize("option", ["abrolia_managed", "gmail_agent", "family_domain"])
def test_the_non_javascript_form_supplies_the_article_9_consent(option: str) -> None:
    """Every email option, not just the recommended one.

    `_selection` built the restriction binding for all three and the Art. 9
    consent for none, so a real-email rollout rejected every browser submission
    the product could produce.
    """
    selection = _selection(
        StepKind.EMAIL,
        {
            "kind": option,
            "special_category_restriction_acknowledged": "yes",
            "special_category_restriction_text_version": RESTRICTION_VERSION,
            "special_category_restriction_text_sha256": RESTRICTION_SHA,
            "special_category_household_consent": "yes",
            "special_category_household_text_version": HOUSEHOLD_VERSION,
            "special_category_household_text_sha256": HOUSEHOLD_SHA,
        },
        context=_Context(),
        household_id="40000000-0000-4000-8000-000000000001",
    )
    assert selection["special_category_household_consent"] is True
    assert selection["special_category_household_receipt_id"]
    assert selection["special_category_household_text_version"] == HOUSEHOLD_VERSION
    assert selection["special_category_household_text_sha256"] == HOUSEHOLD_SHA


def test_a_synthetic_rollout_form_carries_no_article_9_consent() -> None:
    selection = _selection(
        StepKind.EMAIL,
        {
            "kind": "abrolia_managed",
            "special_category_restriction_acknowledged": "yes",
            "special_category_restriction_text_version": RESTRICTION_VERSION,
            "special_category_restriction_text_sha256": RESTRICTION_SHA,
        },
        context=_Context(),
        household_id="40000000-0000-4000-8000-000000000001",
    )
    assert "special_category_household_consent" not in selection


@pytest.fixture
def real_email_harness(tmp_path: Path):
    config = replace(
        ControlPlaneConfig.for_test(tmp_path),
        real_email_enabled=True,
        # The page render does not depend on the allowlist, but the config
        # refuses to construct a real-email deployment without one.
        real_email_household_allowlist=frozenset(
            {"50000000-0000-4000-8000-000000000001"}
        ),
        nerve_base_url="https://nerve.example.test",
        nerve_admin_key="synthetic-admin-key",
        nerve_platform_org_id="20000000-0000-4000-8000-000000000001",
        nerve_platform_domain_id="30000000-0000-4000-8000-000000000001",
    )
    mailer = MemoryMailer()
    active = ControlPlaneContainer.build(config, mailer=mailer)
    app = create_app(active_container=active)
    try:
        with TestClient(app, base_url=config.public_origin) as client:
            yield APIHarness(config, active, mailer, client)
    finally:
        active.close()


def test_the_page_presents_the_consent_on_every_email_option(
    real_email_harness,
) -> None:
    world = real_email_harness.create_principal("art9-owner@family.test")
    real_email_harness.authenticate(world)
    html = real_email_harness.client.get("/onboarding").text

    assert html.count('name="special_category_household_consent"') == 3
    assert html.count(f'value="{HOUSEHOLD_SHA}"') == 3
    assert HOUSEHOLD_VERSION in html
    # The copy itself must be on the page: consent to text the family never saw
    # is not explicit consent.
    assert "Art. 9(2)(a)" in html_module.unescape(html)


def test_the_synthetic_page_asks_only_where_a_real_provider_is_routed_to(
    api_harness,
) -> None:
    """Per option, not per deployment flag.

    With the managed rollout off, `abrolia_managed` and `family_domain` route to
    `fake-email` and owe nothing. `gmail_agent` routes to `google-oauth`
    regardless, so its form must carry the consent — otherwise the server gate,
    which asks the same question, rejects a submission the page could not have
    made valid.
    """
    world = api_harness.create_principal("synthetic-owner@family.test")
    api_harness.authenticate(world)
    html = api_harness.client.get("/onboarding").text

    assert html.count('name="special_category_household_consent"') == 1
    assert html.count('name="special_category_restriction_acknowledged"') == 3
    # And it is on the Gmail form, not one of the synthetic ones.
    gmail = html.index('value="gmail_agent"')
    following = html.index('name="special_category_household_consent"', gmail)
    assert following < html.index('value="family_domain"', gmail)


def test_the_page_and_the_gate_never_disagree(api_harness) -> None:
    """They are the same predicate, and that is the property worth pinning.

    Keying the page on the rollout flag while the gate keyed on the provider
    made Gmail-only browser onboarding impossible: no consent rendered, and a
    server that demanded one.
    """
    service = api_harness.container.onboarding
    world = api_harness.create_principal("agreement-owner@family.test")
    api_harness.authenticate(world)
    html = api_harness.client.get("/onboarding").text

    for option in ("abrolia_managed", "gmail_agent", "family_domain"):
        required = service.email_option_processes_real_content(option)
        start = html.index(f'value="{option}"')
        end = html.index("</form>", start)
        rendered = 'name="special_category_household_consent"' in html[start:end]
        assert rendered == required, option


def test_the_javascript_path_submits_the_consent(api_harness) -> None:
    """The scripted path is a separate implementation and drifted before."""
    script = api_harness.client.get("/static/onboarding.js").text
    assert "selection.special_category_household_consent = true" in script
    assert "special_category_household_receipt_id = crypto.randomUUID()" in script
    assert "special_category_household_text_sha256" in script


# ---------------------------------------------------------------------------
# The two copies must be mutually satisfiable.
# ---------------------------------------------------------------------------


def test_the_restriction_does_not_forbid_what_the_consent_permits() -> None:
    """v1 forbade special-category data "about any person".

    That included the owner and their own minor children — exactly the subjects
    the Art. 9(2)(a) consent authorises — so a family could not obey the
    restriction and use the consented feature at the same time. It also
    misstated the S5 boundary, which puts only THIRD-PARTY special categories
    out of scope.
    """
    from control_plane.privacy.consent import consent_version_and_text

    restriction_version, restriction = consent_version_and_text(
        CONTENT_RESTRICTION_PURPOSE
    )
    _, consent = consent_version_and_text(HOUSEHOLD_CONTENT_PURPOSE)

    assert "about any person" not in restriction
    assert "anyone other than yourself and your own minor children" in restriction
    # Both texts must agree on who is in scope and who is not.
    assert "your own minor children" in consent
    assert "outside the scope" in restriction
    # A copy change that reintroduces the contradiction must also bump the
    # version, because every enforcement boundary compares it exactly.
    assert restriction_version.endswith("-v2")


# ---------------------------------------------------------------------------
# Gmail is a real provider whatever the managed-rollout flag says.
# ---------------------------------------------------------------------------


def gmail_selection(**overrides: object) -> dict[str, object]:
    selection: dict[str, object] = {
        "kind": "gmail_agent",
        "separate_agent_account_acknowledged": True,
        "special_category_restriction_acknowledged": True,
        "special_category_restriction_receipt_id": "10000000-0000-4000-8000-000000000051",
        "special_category_restriction_text_version": RESTRICTION_VERSION,
        "special_category_restriction_text_sha256": RESTRICTION_SHA,
        "special_category_household_consent": True,
        "special_category_household_receipt_id": "10000000-0000-4000-8000-000000000052",
        "special_category_household_text_version": HOUSEHOLD_VERSION,
        "special_category_household_text_sha256": HOUSEHOLD_SHA,
    }
    selection.update(overrides)
    return selection


def enable_gmail_only(cp_stack) -> None:
    """What the container actually does: Gmail routes to a real provider always.

    `ControlPlaneContainer.build` passes `gmail_provider="google-oauth"`
    unconditionally — it is not derived from `ABROLIA_REAL_EMAIL_ENABLED` the
    way the managed and domain providers are.
    """
    cp_stack.service.gmail_provider = "google-oauth"
    assert cp_stack.service.real_email_enabled is False


def test_gmail_requires_the_article_9_consent_with_the_rollout_flag_off(
    cp_stack,
) -> None:
    """The hole: the gate returned early on the flag, before reaching Gmail.

    The web form hides the consent in this configuration and the worker rejects
    the job for a missing receipt, so it looked contained — but an API client
    that supplies the receipts reached a real provider with no Art. 9(4)
    country validation at all.
    """
    cp_stack.complete_profile()
    enable_gmail_only(cp_stack)
    without_consent = gmail_selection()
    for field in (
        "special_category_household_consent",
        "special_category_household_receipt_id",
        "special_category_household_text_version",
        "special_category_household_text_sha256",
    ):
        without_consent.pop(field)

    with pytest.raises(InvalidTransition, match="Art 9\\(2\\)\\(a\\)"):
        cp_stack.service.select(
            cp_stack.household.id,
            StepKind.EMAIL,
            without_consent,
            context=cp_stack.context(),
        )


def test_gmail_is_country_gated_with_the_rollout_flag_off(cp_stack) -> None:
    cp_stack.complete_profile()
    enable_gmail_only(cp_stack)
    set_country(cp_stack, "CZ")

    with pytest.raises(InvalidTransition, match="no Art. 9\\(4\\) determination"):
        cp_stack.service.select(
            cp_stack.household.id,
            StepKind.EMAIL,
            gmail_selection(),
            context=cp_stack.context(),
        )


def test_gmail_is_allowed_in_a_determined_country_with_full_consent(cp_stack) -> None:
    """The counterpart, so the gate is not merely refusing everything."""
    cp_stack.complete_profile()
    enable_gmail_only(cp_stack)

    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        gmail_selection(),
        context=cp_stack.context(),
    )

    job = cp_stack.database.query_one(
        "SELECT provider FROM provisioning_jobs WHERE kind = 'email_identity'"
    )
    assert job["provider"] == "google-oauth"


def test_the_synthetic_managed_path_is_still_ungated(cp_stack) -> None:
    """Only the provider decides. `fake-email` owes nothing."""
    cp_stack.complete_profile()
    set_country(cp_stack, "CZ")

    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        {"kind": "abrolia_managed", "local_part": "family-agent"},
        context=cp_stack.context(),
    )

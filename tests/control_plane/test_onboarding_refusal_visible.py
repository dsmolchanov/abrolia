"""A refused command is visible on every step, and names what to do.

The first tester past the profile hit 409 on the email step and saw nothing:
the error slot lived inside the profile form, hidden from the second step
on. The 409 was R1 working as designed — managed email routes to Nerve and
the household is not allowlisted — but its message named nothing a tester
could act on, and the page did not show the id the operator allowlists by.
"""

from __future__ import annotations

import re

import pytest

from control_plane.models import StepKind
from control_plane.onboarding.contracts import InvalidTransition


def test_the_error_slot_is_outside_every_step(api_harness) -> None:
    world = api_harness.create_principal("slot@pilot.test")
    api_harness.authenticate(world)
    html = api_harness.client.get("/onboarding").text
    slot = '<p id="command-error" class="form-error" role="alert" hidden></p>'
    assert html.count(slot) == 1
    # Before the first step section, not inside any of them.
    assert html.index(slot) < html.index('<section class="profile-preflight"')
    assert '<form id="profile-form"' in html
    profile_form = html.split('<form id="profile-form"')[1].split("</form>")[0]
    assert "command-error" not in profile_form


def test_the_page_shows_the_household_id_the_operator_allowlists_by(api_harness) -> None:
    world = api_harness.create_principal("id@pilot.test")
    api_harness.authenticate(world)
    html = api_harness.client.get("/onboarding").text
    assert f"Household ID: <code>{world.household.id}</code>" in html


def test_every_option_card_says_continue(api_harness) -> None:
    world = api_harness.create_principal("cards@pilot.test")
    api_harness.authenticate(world)
    html = api_harness.client.get("/onboarding").text
    cards = re.findall(r'<button class="card[^"]*" data-select="[^"]+" data-kind="[^"]+">.*?</button>', html, re.S)
    assert len(cards) >= 6, "managed email, two WhatsApp options, three channels"
    for card in cards:
        assert 'class="card-cta">Continue with' in card, card[:80]


def test_the_allowlist_refusal_names_the_household_and_the_next_step(cp_stack) -> None:
    cp_stack.complete_profile()
    cp_stack.service.real_email_enabled = True
    cp_stack.service.email_provider = "nerve-managed"
    cp_stack.service.real_email_household_allowlist = frozenset()

    with pytest.raises(InvalidTransition) as refused:
        cp_stack.service.select(
            cp_stack.household.id,
            StepKind.EMAIL,
            {"kind": "abrolia_managed", "local_part": "family-agent"},
            context=cp_stack.context(),
        )
    message = str(refused.value)
    assert "not enabled for this household yet" in message
    assert cp_stack.household.id in message
    assert "Ask the operator to enable it" in message

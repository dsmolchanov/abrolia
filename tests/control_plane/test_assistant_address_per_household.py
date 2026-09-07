"""The assistant address belongs to one household, and a taken one says so.

The onboarding page hardcoded `family.assistant` for every household. The
reservation is UNIQUE on (domain, local part), so the FIRST household to
choose the managed option took that address for good and every later one hit
`sqlite3.IntegrityError` — which nothing above translates, so it reached the
family as a 500. With registration open to anyone, that is the second tester,
every time.

The address is now the family's to choose, the offered default is one the
server has checked is free, and a collision is a named refusal.
"""

from __future__ import annotations

import pytest

from control_plane.email.service import MailboxRefused

NOW = 1_760_000_000.0
MANAGED = {"kind": "abrolia_managed", "local_part": "family.assistant"}


def _second_household(cp_stack, *, first_name: str = "Test", last_name: str = "Family"):
    """A second family — what open registration makes, one household each.

    Its own account, because a pilot account owns exactly one household; the
    collision this file is about is between FAMILIES, not between two
    households of one person.
    """
    account = cp_stack.accounts.create_verified("second@pilot.test", now=NOW)
    household = cp_stack.households.create_for_owner(account.id, now=NOW)
    cp_stack.households.save_profile(
        household.id,
        cp_stack.valid_profile(first_name=first_name, last_name=last_name),
        now=NOW,
    )
    return household


def test_a_taken_address_is_a_named_refusal_not_a_crash(cp_stack) -> None:
    service = cp_stack.service.email_identities
    other = _second_household(cp_stack)

    with cp_stack.database.write() as connection:
        service.select(
            connection,
            household_id=cp_stack.household.id,
            selection=MANAGED,
            now=NOW,
        )

    # The second household asks for the same address. `MailboxRefused` is the
    # correctable class: the JSON route answers 409 and the form redirects
    # back to the choice, where the page now prints the reason.
    with pytest.raises(MailboxRefused, match="already taken"), cp_stack.database.write() as connection:
        service.select(
            connection,
            household_id=other.id,
            selection=MANAGED,
            now=NOW + 1,
        )


def test_the_refusal_names_no_address(cp_stack) -> None:
    """Whose mailbox that is is not the asking family's business."""
    service = cp_stack.service.email_identities
    other = _second_household(cp_stack)
    with cp_stack.database.write() as connection:
        service.select(
            connection, household_id=cp_stack.household.id, selection=MANAGED, now=NOW
        )

    with pytest.raises(MailboxRefused) as refused, cp_stack.database.write() as connection:
        service.select(
            connection, household_id=other.id, selection=MANAGED, now=NOW + 1
        )
    assert "family.assistant" not in str(refused.value)
    assert "abrolia.com" not in str(refused.value)


def test_the_suggestion_is_an_address_the_household_can_take(cp_stack) -> None:
    """An offer that is not available is not an offer."""
    cp_stack.complete_profile()
    service = cp_stack.service.email_identities

    first = service.suggest(cp_stack.household.id)
    assert service.available(first)

    with cp_stack.database.write() as connection:
        service.select(
            connection,
            household_id=cp_stack.household.id,
            selection={"kind": "abrolia_managed", "local_part": first},
            now=NOW,
        )

    # Same family name, second household: the suggester must move on rather
    # than hand out the address it just watched being reserved.
    other = _second_household(cp_stack)
    second = service.suggest(other.id)
    assert second != first, "two households were offered one address"
    assert service.available(second)


def test_the_page_offers_a_field_not_a_shared_constant(api_harness) -> None:
    world = api_harness.create_principal("address@pilot.test")
    api_harness.authenticate(world)
    html = api_harness.client.get("/onboarding").text

    assert '<input type="hidden" name="local_part" value="family.assistant">' not in html
    assert "<label>Assistant address<input name=\"local_part\"" in html
    assert "@abrolia.com</code>" in html

    script = api_harness.client.get("/static/onboarding.js").text
    assert '"family.assistant"' not in script
    assert "form.elements.local_part.value" in script

"""The household profile is chosen from closed vocabularies, and a refusal is visible.

A tester typed "Czechia" into a field that wanted `CZ`, or described the
family's languages in a sentence longer than 35 characters, got a 422, and saw
nothing: the page re-rendered the unchanged state. The form now offers exactly
the values the contract accepts, the contract forgives case and whitespace but
names the field it refuses, and the enhanced page prints the refusal.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from control_plane.models import ProfileInput
from control_plane.profile_choices import COUNTRIES, LANGUAGES, TIMEZONES

BASE = {
    "first_name": "Test",
    "last_name": "Family",
    "family_language": "en",
    "timezone": "Europe/Prague",
    "country_code": "CZ",
}


def test_the_vocabularies_are_well_formed() -> None:
    assert len(COUNTRIES) >= 240 and all(len(code) == 2 and code.isupper() for code, _ in COUNTRIES)
    assert len(LANGUAGES) >= 40 and all(len(code) == 2 and code.islower() for code, _ in LANGUAGES)
    assert "Europe/Prague" in TIMEZONES and "Europe/Moscow" in TIMEZONES
    assert not any(zone.startswith("Etc/") for zone in TIMEZONES), "offsets are not places"
    assert len(TIMEZONES) > 300, "the tz database must be present, not an empty fallback"


@pytest.mark.parametrize(
    ("field", "given", "stored"),
    [
        ("country_code", "cz", "CZ"),
        ("country_code", " de ", "DE"),
        ("family_language", "RU", "ru"),
        ("family_language", " uk ", "uk"),
        ("timezone", " Europe/Moscow ", "Europe/Moscow"),
    ],
)
def test_case_and_whitespace_are_forgiven(field: str, given: str, stored: str) -> None:
    profile = ProfileInput.model_validate({**BASE, field: given})
    assert getattr(profile, field) == stored


@pytest.mark.parametrize(
    ("field", "given", "message"),
    [
        ("country_code", "Czechia", "ISO 3166-1"),
        ("country_code", "XX", "ISO 3166-1"),
        ("family_language", "Russian and English at home", "offered language"),
        ("family_language", "r", "offered language"),
        ("timezone", "Prague", "IANA zone"),
        ("timezone", "Etc/GMT+1", "IANA zone"),
    ],
)
def test_an_unknown_value_is_refused_by_name(field: str, given: str, message: str) -> None:
    with pytest.raises(ValidationError) as refused:
        ProfileInput.model_validate({**BASE, field: given})
    errors = refused.value.errors()
    assert [error["loc"] for error in errors] == [(field,)]
    assert message in errors[0]["msg"]


def test_the_form_offers_exactly_the_accepted_values(api_harness) -> None:
    world = api_harness.create_principal("choices@pilot.test")
    api_harness.authenticate(world)
    page = api_harness.client.get("/onboarding")
    assert page.status_code == 200
    html = page.text
    for name in ("family_language", "timezone", "country_code"):
        assert f'<select name="{name}" required>' in html, name
        assert f'<input name="{name}"' not in html, name
    assert '<option value="en" selected>English</option>' in html
    assert '<option value="CZ" selected>Czechia</option>' in html
    assert '<option value="Europe/Prague" selected>Europe/Prague</option>' in html
    assert '<option value="ru">Russian</option>' in html
    assert '<option value="RU">Russia</option>' in html
    assert '<p id="command-error" class="form-error" role="alert" hidden></p>' in html


def test_the_enhanced_page_prints_a_refusal(api_harness) -> None:
    script = api_harness.client.get("/static/onboarding.js").text
    assert "showCommandError(response)" in script
    assert 'document.querySelector("#command-error")' in script
    # The refusal is rendered from loc + msg only; the submitted value is
    # never in the server's answer, so it cannot be echoed here either.
    assert "item.msg" in script and "input" not in script.split("showCommandError")[1].split("async function command")[0]


def test_the_api_refuses_the_old_free_text_with_a_named_field(api_harness) -> None:
    world = api_harness.create_principal("free-text@pilot.test")
    api_harness.authenticate(world)
    current = api_harness.client.get("/api/v1/onboarding/current").json()
    response = api_harness.client.put(
        "/api/v1/onboarding/profile",
        json={**BASE, "country_code": "Czechia", "residency_mode": "eu-app"},
        headers={
            **api_harness.mutation_headers,
            "Idempotency-Key": "11111111-1111-4111-8111-111111111111",
            "If-Match": str(current["version"]),
        },
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert [tuple(item["loc"]) for item in detail] == [("body", "country_code")]
    assert "ISO 3166-1" in detail[0]["msg"]
    assert "Czechia" not in response.text, "the submitted value must not be echoed"

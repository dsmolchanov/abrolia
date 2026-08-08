from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from fastapi.testclient import TestClient

from control_plane.models import ProfileInput
from control_plane.onboarding.contracts import CommandContext
from control_plane.privacy.consent import consent_version_and_sha


def _headers(api_harness, *, version: int, key: str) -> dict[str, str]:
    return {
        **api_harness.mutation_headers,
        "If-Match": f'"{version}"',
        "Idempotency-Key": key,
    }


def test_local_part_api_returns_only_suggestion_or_availability(api_harness) -> None:
    world = api_harness.create_principal("email-policy-owner@family.test")
    api_harness.authenticate(world)
    profile = api_harness.client.put(
        "/api/v1/onboarding/profile",
        headers=_headers(api_harness, version=0, key="email-policy-profile"),
        json={
            "first_name": "Éva",
            "last_name": "Novák",
            "family_language": "en",
            "timezone": "Europe/Prague",
            "country_code": "CZ",
            "residency_mode": "eu-app",
        },
    )
    assert profile.status_code == 200

    suggestion = api_harness.client.get("/api/v1/email/local-part/suggestion")
    available = api_harness.client.get(
        "/api/v1/email/local-part/availability",
        params={"local_part": "eva_novak"},
    )
    invalid = api_harness.client.get(
        "/api/v1/email/local-part/availability",
        params={"local_part": "admin"},
    )

    assert suggestion.json() == {"local_part": "eva_novak"}
    assert available.json() == {"available": True}
    assert invalid.status_code == 422
    assert invalid.json() == {"detail": "invalid local part"}

    selected = api_harness.client.post(
        "/api/v1/onboarding/steps/email_identity/select",
        headers=_headers(api_harness, version=1, key="email-policy-select"),
        json={
            "kind": "abrolia_managed",
            "local_part": "eva_novak",
            "special_category_restriction_acknowledged": True,
            "special_category_restriction_receipt_id": "10000000-0000-4000-8000-000000000001",
            "special_category_restriction_text_version": consent_version_and_sha(
                "special_category_content_restriction"
            )[0],
            "special_category_restriction_text_sha256": consent_version_and_sha(
                "special_category_content_restriction"
            )[1],
        },
    )
    assert selected.status_code == 200
    reserved = api_harness.client.get(
        "/api/v1/email/local-part/availability",
        params={"local_part": "eva_novak"},
    )
    assert reserved.json() == {"available": False}
    assert set(reserved.json()) == {"available"}


def test_local_part_suggestion_requires_completed_profile(api_harness) -> None:
    world = api_harness.create_principal("incomplete-profile@family.test")
    api_harness.authenticate(world)

    response = api_harness.client.get("/api/v1/email/local-part/suggestion")

    assert response.status_code == 409
    assert response.json() == {"detail": "household profile is incomplete"}


def test_domain_guidance_is_owner_scoped_and_returns_no_inventory(api_harness) -> None:
    unauthenticated = api_harness.client.get(
        "/api/v1/email/domain/guidance", params={"domain": "example.com"}
    )
    assert unauthenticated.status_code == 401

    world = api_harness.create_principal("domain-guidance-owner@family.test")
    api_harness.authenticate(world)
    apex = api_harness.client.get(
        "/api/v1/email/domain/guidance", params={"domain": "Example.COM."}
    )
    subdomain = api_harness.client.get(
        "/api/v1/email/domain/guidance", params={"domain": "mail.example.com"}
    )
    blocked = api_harness.client.get(
        "/api/v1/email/domain/guidance", params={"domain": "mail.abrolia.com"}
    )

    assert apex.json() == {
        "domain": "example.com",
        "registrable_domain": "example.com",
        "recommended_domain": "assistant.example.com",
        "apex_mx_risk": True,
    }
    assert subdomain.json()["apex_mx_risk"] is False
    assert blocked.status_code == 422
    assert blocked.json() == {"detail": "invalid domain"}


def test_byo_two_connection_owned_domain_race_returns_one_conflict(api_harness) -> None:
    worlds = [
        api_harness.create_principal("domain-race-a@family.test"),
        api_harness.create_principal("domain-race-b@family.test"),
    ]
    for index, world in enumerate(worlds):
        api_harness.container.onboarding.save_profile(
            world.household.id,
            ProfileInput.model_validate({
                "first_name": "Race",
                "last_name": str(index),
                "family_language": "en",
                "timezone": "Europe/Prague",
                "country_code": "CZ",
                "residency_mode": "eu-app",
            }),
            context=CommandContext(
                account_id=world.account.id,
                session_id=world.session.id,
                request_id=f"domain-race-profile-{index}",
                idempotency_key=f"domain-race-profile-{index}",
                expected_version=0,
            ),
        )

    barrier = Barrier(2)

    def select(index: int):
        world = worlds[index]
        with TestClient(
            api_harness.client.app, base_url=api_harness.config.public_origin
        ) as client:
            client.cookies.set(
                api_harness.config.session_cookie_name, world.session.token
            )
            client.cookies.set(
                api_harness.config.csrf_cookie_name, world.session.csrf_token
            )
            barrier.wait()
            return client.post(
                "/api/v1/onboarding/steps/email_identity/select",
                headers={
                    "Origin": api_harness.config.public_origin,
                    "X-CSRF-Token": world.session.csrf_token,
                    "If-Match": '"1"',
                    "Idempotency-Key": f"domain-race-select-{index}",
                },
                json={
                    "kind": "family_domain",
                    "domain": "family.example.test",
                    "local_part": f"assistant-{index}",
                    "special_category_restriction_acknowledged": True,
                    "special_category_restriction_receipt_id": (
                        f"10000000-0000-4000-8000-00000000010{index}"
                    ),
                    "special_category_restriction_text_version": (
                        consent_version_and_sha(
                            "special_category_content_restriction"
                        )[0]
                    ),
                    "special_category_restriction_text_sha256": (
                        consent_version_and_sha(
                            "special_category_content_restriction"
                        )[1]
                    ),
                },
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(select, range(2)))

    assert sorted(response.status_code for response in responses) == [200, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json() == {"detail": "domain_already_claimed"}
    assert api_harness.container.database.query_one(
        "SELECT count(*) AS count FROM email_identities"
        " WHERE option = 'own_domain' AND status != 'deleted'"
    )["count"] == 1

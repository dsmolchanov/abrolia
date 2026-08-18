from __future__ import annotations

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
            "country_code": "DE",
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

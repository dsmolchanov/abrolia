"""Phase C2 case 4 — one canonical domain has exactly one owner."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from control_plane.email.models import EmailOption
from control_plane.email.repository import EmailDomainAlreadyClaimed
from control_plane.models import ProfileInput
from control_plane.onboarding.contracts import CommandContext
from control_plane.privacy.consent import consent_version_and_sha
from tests.control_plane.email.byo_support import select_byo_domain


def test_same_normalized_domain_cannot_be_claimed_by_another_household(cp_stack) -> None:
    cp_stack.complete_profile()
    select_byo_domain(cp_stack)
    other_account = cp_stack.accounts.create_verified("other-domain-owner@family.test")
    other_household = cp_stack.households.create_for_owner(other_account.id)

    with cp_stack.database.write() as connection, pytest.raises(
        EmailDomainAlreadyClaimed
    ):
        cp_stack.email_identities.create_selected(
            connection,
            household_id=other_household.id,
            option=EmailOption.OWN_DOMAIN,
            address="other@FAMILY.EXAMPLE.TEST",
        )


def test_legacy_domain_claim_without_new_hmac_still_blocks_another_household(
    cp_stack,
) -> None:
    cp_stack.complete_profile()
    select_byo_domain(cp_stack)
    identity = cp_stack.email_identities.current_for_household(cp_stack.household.id)
    with cp_stack.database.write() as connection:
        connection.execute(
            "UPDATE email_identities SET domain_lookup_hmac = NULL WHERE id = ?",
            (identity.id,),
        )
    other_account = cp_stack.accounts.create_verified("legacy-domain-owner@family.test")
    other_household = cp_stack.households.create_for_owner(other_account.id)

    with cp_stack.database.write() as connection, pytest.raises(
        EmailDomainAlreadyClaimed
    ):
        cp_stack.email_identities.create_selected(
            connection,
            household_id=other_household.id,
            option=EmailOption.OWN_DOMAIN,
            address="legacy@family.example.test",
        )


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

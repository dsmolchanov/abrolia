"""Art 9(2)(a) household-content consent (docs/privacy/lawful-bases.md, § 3)."""

from __future__ import annotations

import pytest

from control_plane.models import StepKind
from control_plane.onboarding.contracts import InvalidTransition
from control_plane.privacy.consent import consent_version_and_sha

RESTRICTION_VERSION, RESTRICTION_SHA = consent_version_and_sha(
    "special_category_content_restriction"
)
CONSENT_VERSION, CONSENT_SHA = consent_version_and_sha(
    "special_category_household_content"
)
RESTRICTION_RECEIPT = "10000000-0000-4000-8000-000000000041"
CONSENT_RECEIPT = "10000000-0000-4000-8000-000000000042"


def _selection(**changes: object) -> dict[str, object]:
    selection: dict[str, object] = {
        "kind": "abrolia_managed",
        "local_part": "family-agent",
        "special_category_restriction_acknowledged": True,
        "special_category_restriction_receipt_id": RESTRICTION_RECEIPT,
        "special_category_restriction_text_version": RESTRICTION_VERSION,
        "special_category_restriction_text_sha256": RESTRICTION_SHA,
        "special_category_household_consent": True,
        "special_category_household_receipt_id": CONSENT_RECEIPT,
        "special_category_household_text_version": CONSENT_VERSION,
        "special_category_household_text_sha256": CONSENT_SHA,
    }
    selection.update(changes)
    return selection


def _real_email(cp_stack) -> None:
    cp_stack.complete_profile()
    cp_stack.service.real_email_enabled = True
    # The container derives the providers from this flag, and the content gate
    # follows the provider — moving only the flag builds a configuration
    # production cannot have.
    cp_stack.service.email_provider = "nerve-managed"
    cp_stack.service.real_email_household_allowlist = frozenset({
        cp_stack.household.id
    })


def test_real_email_requires_the_article_9_2_a_consent(cp_stack) -> None:
    _real_email(cp_stack)
    without_consent = _selection()
    for field in (
        "special_category_household_consent",
        "special_category_household_receipt_id",
    ):
        without_consent.pop(field)

    with pytest.raises(InvalidTransition, match="Art 9\\(2\\)\\(a\\)"):
        cp_stack.service.select(
            cp_stack.household.id,
            StepKind.EMAIL,
            without_consent,
            context=cp_stack.context(),
        )
    assert cp_stack.database.query("SELECT id FROM consent_receipts") == []


def test_stale_consent_copy_is_rejected(cp_stack) -> None:
    _real_email(cp_stack)

    with pytest.raises(InvalidTransition, match="text version does not match"):
        cp_stack.service.select(
            cp_stack.household.id,
            StepKind.EMAIL,
            _selection(special_category_household_text_version="stale-v0"),
            context=cp_stack.context(),
        )


def test_accepted_consent_is_recorded_and_never_sent_to_the_provider(
    cp_stack,
) -> None:
    _real_email(cp_stack)

    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        _selection(),
        context=cp_stack.context(),
    )

    receipt = cp_stack.database.query_one(
        "SELECT id, purpose, text_version, text_sha256 FROM consent_receipts"
        " WHERE purpose = 'special_category_household_content'"
    )
    assert receipt["id"] == CONSENT_RECEIPT
    assert receipt["text_version"] == CONSENT_VERSION
    assert receipt["text_sha256"] == CONSENT_SHA
    job = cp_stack.database.query_one(
        "SELECT id FROM provisioning_jobs WHERE kind = 'email_identity'"
    )
    # Consent metadata is control-plane accountability data; the provider gets
    # only what it needs to create the mailbox.
    assert cp_stack.jobs.request(job["id"])["selection"] == {
        "kind": "abrolia_managed",
        "local_part": "family-agent",
    }


def test_synthetic_rollout_does_not_demand_the_consent(cp_stack) -> None:
    cp_stack.complete_profile()
    minimal = _selection()
    for field in list(minimal):
        if field.startswith("special_category_household"):
            minimal.pop(field)

    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        minimal,
        context=cp_stack.context(),
    )

    assert cp_stack.database.query_one(
        "SELECT id FROM consent_receipts"
        " WHERE purpose = 'special_category_household_content'"
    ) is None


def test_consent_contract_endpoint_serves_the_exact_hashed_copy(api_harness) -> None:
    response = api_harness.client.get(
        "/api/v1/onboarding/consent/special-category-household-content"
    )

    body = response.json()
    assert body["purpose"] == "special_category_household_content"
    assert body["text_version"] == CONSENT_VERSION
    assert body["text_sha256"] == CONSENT_SHA
    assert "Art. 9(2)(a)" in body["text"]
    assert "parental responsibility" in body["text"]

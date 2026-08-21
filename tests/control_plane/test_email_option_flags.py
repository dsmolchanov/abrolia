"""The per-provider kill switches, asserted where they are supposed to bite.

`control_plane/feature_flags.py` is documented in two operator tables and in
`canon-execution-plan.md` as fail-closed, default-off gating for each provider.
It had no production caller: `ABROLIA_GMAIL_ENABLED=0` gated nothing, and the
runbook said otherwise. A flag that does not gate is worse than no flag,
because it is believed.

This module deliberately does NOT use `tests/control_plane/email/conftest.py`,
which turns both options on for the lifecycle suites.
"""

from __future__ import annotations

import pytest

from control_plane.models import StepKind
from control_plane.onboarding.contracts import InvalidTransition
from control_plane.privacy.consent import (
    CONTENT_RESTRICTION_PURPOSE,
    HOUSEHOLD_CONTENT_PURPOSE,
    consent_version_and_sha,
)

GATED = {
    "family_domain": (
        "ABROLIA_BYO_EMAIL_ENABLED",
        {"domain": "family.example.test", "local_part": "assistant"},
    ),
    "gmail_agent": (
        "ABROLIA_GMAIL_ENABLED",
        {"separate_agent_account_acknowledged": True},
    ),
}


def _selection(option: str) -> dict[str, object]:
    restriction_version, restriction_sha = consent_version_and_sha(
        CONTENT_RESTRICTION_PURPOSE
    )
    household_version, household_sha = consent_version_and_sha(
        HOUSEHOLD_CONTENT_PURPOSE
    )
    return {
        "kind": option,
        "special_category_restriction_acknowledged": True,
        "special_category_restriction_receipt_id": (
            "10000000-0000-4000-8000-0000000000b1"
        ),
        "special_category_restriction_text_version": restriction_version,
        "special_category_restriction_text_sha256": restriction_sha,
        "special_category_household_consent": True,
        "special_category_household_receipt_id": (
            "10000000-0000-4000-8000-0000000000b2"
        ),
        "special_category_household_text_version": household_version,
        "special_category_household_text_sha256": household_sha,
        **GATED[option][1],
    }


@pytest.mark.parametrize("option", sorted(GATED))
def test_a_disabled_email_option_is_refused(cp_stack, monkeypatch, option) -> None:
    """Default off means refused, even where the option routes to a fake.

    The switch is about which option the product OFFERS, not about whether real
    content can arrive — so it has to bite ahead of the synthetic early return
    in `_assert_email_rollout`. With `real_email_enabled` off, `family_domain`
    routes to `fake-email`; a gate that only guarded real providers would let it
    through and the kill switch would be decorative in exactly the configuration
    the pilot runs in.
    """
    monkeypatch.delenv(GATED[option][0], raising=False)
    cp_stack.complete_profile()

    with pytest.raises(InvalidTransition, match="disabled by flag"):
        cp_stack.service.select(
            cp_stack.household.id,
            StepKind.EMAIL,
            _selection(option),
            context=cp_stack.context(),
        )

    queued = cp_stack.database.query(
        "SELECT id FROM provisioning_jobs WHERE household_id = ?"
        " AND kind = 'email_identity'",
        (cp_stack.household.id,),
    )
    assert queued == [], "a refused option still queued provisioning work"


@pytest.mark.parametrize("option", sorted(GATED))
def test_an_enabled_email_option_is_admitted(cp_stack, monkeypatch, option) -> None:
    """And the switch is a switch: on, the option behaves as before."""
    monkeypatch.setenv(GATED[option][0], "1")
    cp_stack.complete_profile()

    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        _selection(option),
        context=cp_stack.context(),
    )

    queued = cp_stack.database.query(
        "SELECT id FROM provisioning_jobs WHERE household_id = ?"
        " AND kind = 'email_identity'",
        (cp_stack.household.id,),
    )
    assert queued, "an enabled option queued nothing"


def test_the_managed_option_is_not_gated(cp_stack, monkeypatch) -> None:
    """Deliberate, and worth pinning so the omission is not read as a miss.

    Canon has all six flags fail-closed. Wiring the managed one today would take
    email away from every deployment that has not set
    `ABROLIA_MANAGED_EMAIL_ENABLED` — including the synthetic app, which sets
    none of them. That is a separate step with a `fly.toml` change behind it.
    """
    monkeypatch.delenv("ABROLIA_MANAGED_EMAIL_ENABLED", raising=False)
    cp_stack.complete_profile()
    restriction_version, restriction_sha = consent_version_and_sha(
        CONTENT_RESTRICTION_PURPOSE
    )
    household_version, household_sha = consent_version_and_sha(
        HOUSEHOLD_CONTENT_PURPOSE
    )

    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        {
            "kind": "abrolia_managed",
            "local_part": "family-agent",
            "special_category_restriction_acknowledged": True,
            "special_category_restriction_receipt_id": (
                "10000000-0000-4000-8000-0000000000b3"
            ),
            "special_category_restriction_text_version": restriction_version,
            "special_category_restriction_text_sha256": restriction_sha,
            "special_category_household_consent": True,
            "special_category_household_receipt_id": (
                "10000000-0000-4000-8000-0000000000b4"
            ),
            "special_category_household_text_version": household_version,
            "special_category_household_text_sha256": household_sha,
        },
        context=cp_stack.context(),
    )

    queued = cp_stack.database.query(
        "SELECT id FROM provisioning_jobs WHERE household_id = ?"
        " AND kind = 'email_identity'",
        (cp_stack.household.id,),
    )
    assert queued, "the managed option is gated after all"

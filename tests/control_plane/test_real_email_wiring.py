from __future__ import annotations

from dataclasses import replace

import pytest

from control_plane.config import ControlPlaneConfig
from control_plane.container import ControlPlaneContainer
from control_plane.models import StepKind
from control_plane.onboarding.contracts import IdempotencyConflict, InvalidTransition
from control_plane.privacy.consent import consent_version_and_sha


def test_production_container_registers_real_nerve_providers_fail_closed(
    tmp_path,
) -> None:
    household_id = "10000000-0000-4000-8000-000000000001"
    config = replace(
        ControlPlaneConfig.for_test(tmp_path),
        real_email_enabled=True,
        real_email_household_allowlist=frozenset({household_id}),
        nerve_base_url="https://nerve.example.test",
        nerve_admin_key="synthetic-admin-key",
        nerve_platform_org_id="20000000-0000-4000-8000-000000000001",
        nerve_platform_domain_id="30000000-0000-4000-8000-000000000001",
    )

    with ControlPlaneContainer.build(config) as container:
        assert container.providers.health() == {
            "dry-run-runtime": "configured",
            "fake-channel": "configured",
            "fake-cleanup": "configured",
            "fake-email": "configured",
            "fake-whatsapp": "configured",
            "google-oauth": "configured",
            "nerve-byo-domain": "configured",
            "nerve-managed": "configured",
        }
        assert container.onboarding.email_provider == "nerve-managed"
        assert container.onboarding.byo_domain_provider == "nerve-byo-domain"
        assert container.onboarding.allow_real_email_domains



def test_the_real_email_brake_leaves_no_nerve_provider_to_reach(tmp_path) -> None:
    """What `ABROLIA_REAL_EMAIL_ENABLED=0` actually does, asserted.

    `docs/onboarding-runbook.md` advertises this as the incident brake for
    managed and BYO, so what it does needs a test rather than a paragraph. It is
    stronger than a refusal: the Nerve adapters and their admin client are never
    CONSTRUCTED, so there is no object holding the admin key and nothing for a
    queued `nerve-managed` job to reach. Both options route to `fake-email`, so
    onboarding keeps working synthetically instead of dead-ending.

    It is also boot-scoped, not call-time, and that is the honest difference
    from the per-option switches: it is a config value, and the whole config is
    read once at startup. Flipping the variable in a running process changes
    nothing until the process restarts — which is what `fly secrets set` does.
    """

    config = ControlPlaneConfig.for_test(tmp_path)
    assert not config.real_email_enabled, "for_test must default to the braked state"

    with ControlPlaneContainer.build(config) as container:
        health = container.providers.health()
        assert "nerve-managed" not in health
        assert "nerve-byo-domain" not in health

        # Not merely unregistered: unreachable. A job whose durable `provider`
        # column still says `nerve-managed` — queued before the brake went on —
        # cannot resolve one.
        with pytest.raises(Exception) as resolution:
            container.providers.get("nerve-managed")
        assert "nerve-managed" in str(resolution.value)

        # And onboarding stays usable rather than dead-ending.
        assert container.onboarding.email_provider == "fake-email"
        assert container.onboarding.byo_domain_provider == "fake-email"
        assert not container.onboarding.allow_real_email_domains


def test_real_email_rollout_rejects_non_allowlisted_household(cp_stack) -> None:
    cp_stack.complete_profile()
    cp_stack.service.real_email_enabled = True
    # The container derives the providers from this flag; a test that moves
    # only the flag builds a configuration production cannot have.
    cp_stack.service.email_provider = "nerve-managed"
    cp_stack.service.real_email_household_allowlist = frozenset()

    with pytest.raises(InvalidTransition, match="not enabled for this household"):
        cp_stack.service.select(
            cp_stack.household.id,
            StepKind.EMAIL,
            {"kind": "abrolia_managed", "local_part": "family-agent"},
            context=cp_stack.context(),
        )


def test_real_email_rollout_requires_content_restriction_receipt(cp_stack) -> None:
    cp_stack.complete_profile()
    cp_stack.service.real_email_enabled = True
    cp_stack.service.email_provider = "nerve-managed"
    cp_stack.service.real_email_household_allowlist = frozenset({
        cp_stack.household.id
    })

    with pytest.raises(InvalidTransition, match="content restriction receipt"):
        cp_stack.service.select(
            cp_stack.household.id,
            StepKind.EMAIL,
            {"kind": "abrolia_managed", "local_part": "family-agent"},
            context=cp_stack.context(),
        )

    with pytest.raises(InvalidTransition, match="content restriction receipt"):
        cp_stack.service.select(
            cp_stack.household.id,
            StepKind.EMAIL,
            {
                "kind": "abrolia_managed",
                "local_part": "family-agent",
                "special_category_restriction_receipt_id": "10000000-0000-4000-8000-000000000005",
            },
            context=cp_stack.context(),
        )


def test_real_email_rollout_does_not_route_gmail_to_nerve(
    cp_stack, monkeypatch
) -> None:
    # `gmail_agent` is behind a fail-closed kill switch, so a test that selects
    # it turns it on. The routing contract below is what is under test here;
    # the switch itself is asserted in `test_email_option_flags.py`.
    monkeypatch.setenv("ABROLIA_GMAIL_ENABLED", "1")
    cp_stack.complete_profile()
    cp_stack.service.real_email_enabled = True
    cp_stack.service.gmail_provider = "google-oauth"
    cp_stack.service.real_email_household_allowlist = frozenset({
        cp_stack.household.id
    })

    restriction_version, restriction_sha = consent_version_and_sha(
        "special_category_content_restriction"
    )
    consent_version, consent_sha = consent_version_and_sha(
        "special_category_household_content"
    )
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        {
            "kind": "gmail_agent",
            "separate_agent_account_acknowledged": True,
            "special_category_restriction_acknowledged": True,
            "special_category_restriction_receipt_id": "10000000-0000-4000-8000-000000000006",
            "special_category_restriction_text_version": restriction_version,
            "special_category_restriction_text_sha256": restriction_sha,
            "special_category_household_consent": True,
            "special_category_household_receipt_id": "10000000-0000-4000-8000-000000000016",
            "special_category_household_text_version": consent_version,
            "special_category_household_text_sha256": consent_sha,
        },
        context=cp_stack.context(),
    )

    job = cp_stack.database.query_one(
        "SELECT id, provider FROM provisioning_jobs WHERE kind = 'email_identity'"
    )
    assert job["provider"] == "google-oauth"
    provider_selection = cp_stack.jobs.request(job["id"])["selection"]
    assert provider_selection == {
        "kind": "gmail_agent",
        "separate_agent_account_acknowledged": True,
    }
    receipts = cp_stack.database.query(
        "SELECT purpose, text_version FROM consent_receipts"
        " WHERE household_id = ? ORDER BY purpose",
        (cp_stack.household.id,),
    )
    assert [dict(receipt) for receipt in receipts] == [
        {
            "purpose": "special_category_content_restriction",
            "text_version": consent_version_and_sha(
            "special_category_content_restriction"
        )[0],
        },
        {
            "purpose": "special_category_household_content",
            "text_version": consent_version_and_sha(
            "special_category_household_content"
        )[0],
        },
    ]

    with cp_stack.database.write() as connection:
        connection.execute(
            "UPDATE consent_receipts SET text_sha256 = ? WHERE id = ?",
            ("0" * 64, "10000000-0000-4000-8000-000000000006"),
        )
        with pytest.raises(IdempotencyConflict):
            cp_stack.service._record_email_consent_receipt(
                connection,
                parsed={
                    "special_category_restriction_acknowledged": True,
                    "special_category_restriction_receipt_id": (
                        "10000000-0000-4000-8000-000000000006"
                    ),
                    "special_category_restriction_text_version": restriction_version,
                    "special_category_restriction_text_sha256": restriction_sha,
                },
                household_id=cp_stack.household.id,
                account_id=cp_stack.account.id,
                now=10.0,
                purpose="special_category_content_restriction",
                prefix="special_category_restriction",
                accepted_field="special_category_restriction_acknowledged",
                mismatch_error="restriction text version does not match",
            )

from __future__ import annotations

from pathlib import Path

import pytest

from control_plane.config import ControlPlaneConfig
from control_plane.container import ControlPlaneContainer
from control_plane.models import ProfileInput, StepKind
from control_plane.onboarding.contracts import CommandContext
from control_plane.onboarding.provision import (
    RUNTIME_BOOTSTRAP_SECRET,
    TableWrite,
    main,
    plan_onboarding,
)
from control_plane.privacy.consent import consent_version_and_sha
from control_plane.provisioning.fly import FlyRuntimeProvisioner
from tests.control_plane.conftest import BASE_TIME

_RESTRICTION_VERSION, _RESTRICTION_SHA = consent_version_and_sha(
    "special_category_content_restriction"
)
EMAIL_SELECTION = {
    "kind": "abrolia_managed",
    "local_part": "family-agent",
    "special_category_restriction_acknowledged": True,
    "special_category_restriction_receipt_id": "10000000-0000-4000-8000-000000000021",
    "special_category_restriction_text_version": _RESTRICTION_VERSION,
    "special_category_restriction_text_sha256": _RESTRICTION_SHA,
}
WHATSAPP_SELECTION = {
    "kind": "shared_abrolia",
    "member_phone_test_ref": "synthetic-phone:dry-run-owner",
    "privacy_notice_receipt_id": "synthetic-dry-run-consent",
}
CHANNEL_SELECTION = {
    "kind": "telegram",
    "actor_id": "synthetic-dry-run-owner",
    "chat_id": "synthetic-dry-run-chat",
}
# Owner principal per household, so each command carries the real session.
_PRINCIPALS: dict[str, tuple[str, str]] = {}


@pytest.fixture
def container(tmp_path: Path) -> ControlPlaneContainer:
    active = ControlPlaneContainer.build(ControlPlaneConfig.for_test(tmp_path))
    try:
        yield active
    finally:
        active.close()


def _context(
    active: ControlPlaneContainer,
    household_id: str,
    key: str,
    *,
    account_id: str,
    session_id: str,
) -> CommandContext:
    workflow = active.onboarding_repository.workflow_for_household(household_id)
    return CommandContext(
        account_id=account_id,
        session_id=session_id,
        request_id=f"dry-run-request-{key}",
        idempotency_key=f"dry-run-key-{key}",
        expected_version=workflow.version,
    )


def _household_with_profile(active: ControlPlaneContainer) -> str:
    account = active.accounts.create_verified("dry-run@family.test", now=BASE_TIME)
    household = active.households.create_for_owner(account.id, now=BASE_TIME)
    session = active.sessions.issue(account.id, now=BASE_TIME)
    _PRINCIPALS[household.id] = (account.id, session.id)
    active.onboarding.save_profile(
        household.id,
        ProfileInput.model_validate({
            "first_name": "Dry",
            "last_name": "Run",
            "family_language": "en",
            "timezone": "Europe/Prague",
            "country_code": "CZ",
            "residency_mode": "eu-app",
        }),
        context=_context(
            active,
            household.id,
            "profile",
            account_id=account.id,
            session_id=session.id,
        ),
        now=BASE_TIME + 1,
    )
    assert active.worker.run_once().status == "succeeded"
    return household.id


def _verify_all_steps(active: ControlPlaneContainer, household_id: str) -> None:
    account_id, session_id = _PRINCIPALS[household_id]
    for index, (kind, selection) in enumerate((
        (StepKind.EMAIL, EMAIL_SELECTION),
        (StepKind.WHATSAPP, WHATSAPP_SELECTION),
        (StepKind.PRIMARY_CHANNEL, CHANNEL_SELECTION),
    )):
        active.onboarding.select(
            household_id,
            kind,
            selection,
            context=_context(
                active,
                household_id,
                f"select-{index}",
                account_id=account_id,
                session_id=session_id,
            ),
        )
        assert active.worker.run_once().status == "succeeded"


def test_dry_run_lists_exact_writes_and_commits_nothing(container) -> None:
    household_id = _household_with_profile(container)
    _verify_all_steps(container, household_id)
    revisions_before = container.database.query("SELECT id FROM config_revisions")

    plan = plan_onboarding(container, household_id)

    assert plan.committed is False
    assert plan.blocked_by is None
    assert plan.unverified_steps == []
    durable = container.database.query_one(
        "SELECT COALESCE(MAX(revision), 0) AS revision FROM config_revisions"
        " WHERE household_id = ?",
        (household_id,),
    )["revision"]
    assert plan.config_revision["current"] == durable
    assert plan.config_revision["next"] == durable + 1
    assert len(plan.config_revision["manifest_sha256"]) == 64
    assert TableWrite("config_revisions", "insert") in plan.table_writes
    assert {resource["stable_name"] for resource in plan.runtime_resources} >= {
        FlyRuntimeProvisioner.stable_app_name(household_id),
        FlyRuntimeProvisioner.stable_volume_name(),
        FlyRuntimeProvisioner.stable_machine_name(),
    }
    assert RUNTIME_BOOTSTRAP_SECRET in {secret["name"] for secret in plan.secrets}
    # The rehearsal must leave durable state byte-identical.
    assert container.database.query("SELECT id FROM config_revisions") == revisions_before


def test_dry_run_reports_what_still_blocks_the_pilot(container) -> None:
    household_id = _household_with_profile(container)

    plan = plan_onboarding(container, household_id)

    assert plan.committed is False
    assert plan.blocked_by is not None
    assert plan.config_revision == {}
    assert plan.unverified_steps == [
        "email_identity",
        "whatsapp_identity",
        "primary_channel",
    ]
    assert container.database.query("SELECT id FROM config_revisions") == []


def test_dry_run_never_reports_a_secret_value(container) -> None:
    household_id = _household_with_profile(container)
    _verify_all_steps(container, household_id)

    payload = plan_onboarding(container, household_id).public_dict()

    assert payload["mode"] == "dry-run"
    for secret in payload["secrets"]:
        assert set(secret) == {"name", "target", "lifecycle"}


def test_provision_cli_refuses_to_run_without_dry_run() -> None:
    with pytest.raises(SystemExit, match="--dry-run"):
        main(["--household", "10000000-0000-4000-8000-000000000031"])

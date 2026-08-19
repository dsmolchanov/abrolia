from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from control_plane.config import ControlPlaneConfig
from control_plane.container import ControlPlaneContainer
from control_plane.db import ControlPlaneDatabase
from control_plane.models import ProfileInput, StepKind
from control_plane.onboarding.contracts import CommandContext
from control_plane.onboarding.provision import (
    RUNTIME_BOOTSTRAP_SECRET,
    RUNTIME_DSAR_SECRET,
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
        "SELECT revision, manifest_sha256 FROM config_revisions"
        " WHERE household_id = ? ORDER BY revision DESC LIMIT 1",
        (household_id,),
    )
    # The reported revision is the one the worker already issued, not a
    # hypothetical next one the real onboarding would never provision.
    assert plan.config_revision["revision"] == durable["revision"]
    assert plan.config_revision["manifest_sha256"] == durable["manifest_sha256"]
    assert plan.pending_runtime_job["desired_revision"] == durable["revision"]
    # And NOTHING hypothetical is traced. Once a revision is issued the pending
    # runtime job provisions THAT revision and performs no planning write;
    # calling the planner anyway minted a revision N+1 and reported its insert
    # into `config_revisions` as the onboarding's "exact writes" — a write the
    # real onboarding will never make, sourced from an operation this very run
    # discarded.
    assert plan.table_writes == []
    assert "already issued" in plan.rehearsal
    assert plan.uncommitted_revision_delta == 0
    assert {resource["stable_name"] for resource in plan.runtime_resources} >= {
        FlyRuntimeProvisioner.stable_app_name(household_id),
        FlyRuntimeProvisioner.stable_volume_name(),
        FlyRuntimeProvisioner.stable_machine_name(),
    }
    assert {secret["name"] for secret in plan.secrets} >= {
        RUNTIME_BOOTSTRAP_SECRET,
        RUNTIME_DSAR_SECRET,
    }
    # The rehearsal must leave durable state byte-identical.
    assert container.database.query("SELECT id FROM config_revisions") == revisions_before


def test_dry_run_reports_what_still_blocks_the_pilot(container) -> None:
    household_id = _household_with_profile(container)

    plan = plan_onboarding(container, household_id)

    assert plan.committed is False
    assert plan.blocked_by is not None
    assert plan.config_revision == {}
    assert plan.pending_runtime_job == {}
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


def test_dry_run_traces_the_planning_pass_that_has_not_happened_yet(container) -> None:
    """The trace is meaningful exactly once: before a revision exists.

    The counterpart to the assertion above — with nothing issued, the planning
    pass is the operation the real onboarding will perform, so its rolled-back
    write set is the honest answer rather than a discarded one.
    """
    household_id = _household_with_profile(container)
    _verify_all_steps(container, household_id)
    with container.database.write() as connection:
        connection.execute(
            "DELETE FROM config_revisions WHERE household_id = ?", (household_id,)
        )

    plan = plan_onboarding(container, household_id)

    assert plan.config_revision == {}
    assert "planning pass" in plan.rehearsal
    assert TableWrite("config_revisions", "insert") in plan.table_writes
    assert plan.committed is False
    assert container.database.query(
        "SELECT id FROM config_revisions WHERE household_id = ?", (household_id,)
    ) == []


def test_dry_run_reads_one_consistent_snapshot(container) -> None:
    """Revision and pending job must describe the same instant.

    Read piecemeal, the API worker could commit the revision and the runtime job
    between two queries, and the report would pair an empty or stale
    `config_revision` with the new job that provisions it — a state that never
    existed.
    """
    household_id = _household_with_profile(container)
    _verify_all_steps(container, household_id)

    plan = plan_onboarding(container, household_id)

    assert plan.config_revision["revision"] == plan.pending_runtime_job[
        "desired_revision"
    ]


def test_dry_run_refuses_a_database_with_pending_migrations(container) -> None:
    """Building the container used to migrate on the way in.

    Those writes commit before the command's own transaction opens, so they sit
    outside its rollback and outside the check that reports `committed`: a
    command whose entire promise is "mutates nothing" was altering the schema.
    """
    from control_plane.onboarding.provision import pending_migrations

    assert pending_migrations(container.database) == []

    with container.database.write() as connection:
        connection.execute(
            "DELETE FROM schema_migrations WHERE name = ("
            " SELECT name FROM schema_migrations ORDER BY name DESC LIMIT 1)"
        )

    assert pending_migrations(container.database) != []


def test_the_cli_never_migrates_the_database_it_rehearses_against(
    tmp_path: Path, monkeypatch
) -> None:
    """The command's whole promise is that it mutates nothing.

    `ControlPlaneContainer.build` applies migrations by default, committing
    schema changes and `schema_migrations` rows before this command's own
    transaction opens — outside its rollback and outside the check that reports
    `committed`. This drives the real entry point, because that is the only
    place the container is built.
    """
    config = ControlPlaneConfig.for_test(tmp_path)
    monkeypatch.setattr(ControlPlaneConfig, "from_env", staticmethod(lambda: config))

    with pytest.raises(SystemExit, match="pending migrations"):
        main(["--dry-run", "--household", "10000000-0000-4000-8000-000000000031"])

    # Not merely "it refused" — it must have left the schema untouched.
    database = ControlPlaneDatabase(config.database_path)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            database.query("SELECT name FROM schema_migrations")
    finally:
        database.close()

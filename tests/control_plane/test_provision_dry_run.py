from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from control_plane.config import ControlPlaneConfig
from control_plane.container import ControlPlaneContainer
from control_plane.db import ControlPlaneDatabase
from control_plane.models import ProfileInput, StepKind
from control_plane.onboarding import provision as provision_module
from control_plane.onboarding.contracts import CommandContext
from control_plane.onboarding.provision import (
    COMPENSATION_WRITES,
    CONTENT_RESTRICTION_BLOCK_WRITES,
    RUNTIME_BOOTSTRAP_SECRET,
    RUNTIME_DSAR_SECRET,
    RUNTIME_WRITES_BY_WORKFLOW_STATE,
    TableWrite,
    _parser,
    _record_writes,
    _runtime_resources,
    main,
    plan_onboarding,
)
from control_plane.privacy.consent import consent_version_and_sha
from control_plane.provisioning.fly import FlyRuntimeProvisioner
from control_plane.provisioning.secrets import InMemorySecretSink
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


def _household_with_profile(
    active: ControlPlaneContainer, *, email: str = "dry-run@family.test"
) -> str:
    """`email` is the account's unique key, so a second household needs its own."""
    account = active.accounts.create_verified(email, now=BASE_TIME)
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


def _verify_all_steps(
    active: ControlPlaneContainer, household_id: str, *, label: str = "select"
) -> None:
    """`label` distinguishes a RECONNECT from a replay of the first attempt.

    The request id is the idempotency key, so selecting the same step again with
    the same id returns the earlier result and enqueues nothing — correct
    behaviour, and it silently turns a reconnect into a no-op in a test.
    """
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
                f"{label}-{index}",
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
    # The tables reported are the pending RUNTIME operation's, not a planner
    # trace. Once a revision is issued the runtime job provisions THAT revision:
    # calling the planner anyway minted a revision N+1 and reported its INSERT
    # into `config_revisions` as the onboarding's "exact writes" — a write the
    # real onboarding never makes, from an operation this very run discarded.
    # The real operation UPDATES the issued revision.
    assert TableWrite("config_revisions", "update") in plan.table_writes
    assert TableWrite("config_revisions", "insert") not in plan.table_writes
    # The success path PLUS the compensation a mid-call cancel triggers: the
    # set is an upper bound on what the pending call can write, and a
    # cancellation landing inside `provider.prepare` is one of the things it
    # can do.
    assert set(plan.table_writes) == set(
        RUNTIME_WRITES_BY_WORKFLOW_STATE["runtime_provisioning"]
    ) | set(COMPENSATION_WRITES)
    assert "already issued" in plan.rehearsal
    assert plan.uncommitted_revision_delta == 0
    # And the config_revision diff the Phase E1 plan requires, key paths only.
    diff = plan.config_revision["diff"]
    assert diff["previous_revision"] is None
    assert "email.agent_inbox" in diff["added"]
    assert diff["removed"] == [] and diff["changed"] == []
    # The resources the CONFIGURED provider creates. `dry-run-runtime` is the
    # allowed default here and creates one synthetic reference — reporting a Fly
    # app, volume and Machine under its name described three resources that
    # would never exist.
    assert {resource["stable_name"] for resource in plan.runtime_resources} == {
        f"synthetic-runtime:{household_id}"
    }
    assert {resource["provider"] for resource in plan.runtime_resources} == {
        container.config.runtime_provider
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

    assert "planning pass" in plan.rehearsal
    assert TableWrite("config_revisions", "insert") in plan.table_writes
    assert plan.committed is False
    assert container.database.query(
        "SELECT id FROM config_revisions WHERE household_id = ?", (household_id,)
    ) == []

    # The diff the Step E1 contract asks for. This branch is the one where the
    # planning pass actually runs, so it is the one place the revision exists to
    # be described — and taking only `.spec` from the planner threw it away,
    # leaving the field empty exactly where it was owed. This test previously
    # asserted the empty object, which pinned the gap rather than the contract.
    assert plan.config_revision, "the rehearsed revision was discarded"
    assert plan.config_revision["revision"]
    assert plan.config_revision["manifest_sha256"]
    assert "rolled back" in plan.config_revision["source"]
    assert "diff" in plan.config_revision
    # Key paths, never values: this prints to an operator's scrollback.
    rendered = json.dumps(plan.config_revision["diff"])
    assert "@abrolia" not in rendered and "dry-run@family.test" not in rendered


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
    # A real but unmigrated database: the missing-file refusal is a separate
    # case, tested below.
    sqlite3.connect(config.database_path).close()
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


def test_a_reset_workflow_is_not_reported_as_already_planned(container) -> None:
    """`reset_from` retains the rows and marks them revoked/cancelled.

    Reading the highest revision regardless of status made a reset workflow look
    planned: it reported a cancelled job as pending and advertised the runtime
    write set while the steps that work depends on were unverified — a plan for
    an onboarding that is not going to happen next.
    """
    household_id = _household_with_profile(container)
    _verify_all_steps(container, household_id)
    account_id, session_id = _PRINCIPALS[household_id]
    container.onboarding.reset_from(
        household_id,
        StepKind.EMAIL,
        context=_context(
            container, household_id, "reset", account_id=account_id,
            session_id=session_id,
        ),
    )

    plan = plan_onboarding(container, household_id)

    assert plan.config_revision == {}
    assert plan.pending_runtime_job == {}
    assert plan.unverified_steps
    # `blocked_by` is deliberately NOT asserted here. A reset queues its own
    # cleanup jobs, and the report now names the one the worker will lease
    # instead of repeating the planner's prerequisite error — which is the
    # point: this test is about not claiming a PLAN, not about the shape of the
    # explanation.
    assert "already issued" not in plan.rehearsal
    assert set(plan.table_writes) != set(
        RUNTIME_WRITES_BY_WORKFLOW_STATE["runtime_provisioning"]
    )
    assert plan.committed is False


def test_the_cli_leaves_a_non_wal_database_in_its_own_journal_mode(
    tmp_path: Path, monkeypatch
) -> None:
    """`PRAGMA journal_mode=WAL` is persistent, so opening the file is a write.

    On a database in another mode — an imported or restored SQLite file — the
    connection rewrote the header and created `-wal`/`-shm` sidecars before any
    transaction existed, so no rollback could undo it. The command promises to
    mutate nothing; that has to hold for this database state too.
    """
    config = ControlPlaneConfig.for_test(tmp_path)
    seed = ControlPlaneDatabase(config.database_path)
    seed.migrate()
    seed.connection.execute("PRAGMA journal_mode=DELETE")
    assert seed.query_one("PRAGMA journal_mode")[0].lower() == "delete"
    seed.close()
    monkeypatch.setattr(ControlPlaneConfig, "from_env", staticmethod(lambda: config))

    # Exits on the missing household — after the container has been built,
    # which is where the mutation used to happen.
    with pytest.raises(SystemExit):
        main(["--dry-run", "--household", "10000000-0000-4000-8000-000000000031"])

    check = ControlPlaneDatabase(config.database_path, preserve_journal_mode=True)
    try:
        assert check.query_one("PRAGMA journal_mode")[0].lower() == "delete"
    finally:
        check.close()
    assert not config.database_path.with_name(
        f"{config.database_path.name}-wal"
    ).exists()


class _BrokenSecretSink(InMemorySecretSink):
    """Makes the runtime job stop at `secret_install_unknown`, as in production."""

    def __init__(self) -> None:
        super().__init__()
        self.fail = True

    def install(self, runtime_ref, material):
        if self.fail:
            raise RuntimeError("secret install outcome unknown")
        return super().install(runtime_ref, material)


@contextmanager
def _tracing_writes(observed: set[TableWrite]):
    original = ControlPlaneDatabase.write

    @contextmanager
    def traced(self):
        with original(self) as connection:
            recorder: list[TableWrite] = []
            connection.set_trace_callback(lambda sql: _record_writes(sql, recorder))
            try:
                yield connection
            finally:
                connection.set_trace_callback(None)
                observed.update(recorder)

    ControlPlaneDatabase.write = traced
    try:
        yield
    finally:
        ControlPlaneDatabase.write = original


def test_the_declared_write_set_matches_a_real_run_from_runtime_provisioning(
    container,
) -> None:
    """First attempt: the operation does the full insert/transition sequence."""
    household_id = _household_with_profile(container)
    _verify_all_steps(container, household_id)

    observed: set[TableWrite] = set()
    with _tracing_writes(observed):
        while container.worker.run_once() is not None:
            pass

    assert observed == set(RUNTIME_WRITES_BY_WORKFLOW_STATE["runtime_provisioning"])


def test_the_declared_write_set_matches_a_real_run_from_activating(container) -> None:
    """Reconcile after a partial activation writes far less.

    The workflow has already moved to `activating` and the external resource
    already exists, so `_finish_runtime` skips the transition entirely and only
    updates. Validating the declaration against the first-attempt path alone
    checked the happy case against itself and would never have caught this.
    """
    household_id = _household_with_profile(container)
    _verify_all_steps(container, household_id)
    sink = _BrokenSecretSink()
    container.worker.secret_sink = sink

    stalled = container.worker.run_once()
    assert stalled.status == "outcome_unknown"
    assert stalled.error_code == "secret_install_unknown"
    assert container.database.query_one(
        "SELECT state FROM onboarding_workflows WHERE household_id = ?",
        (household_id,),
    )["state"] == "activating"

    sink.fail = False
    observed: set[TableWrite] = set()
    with _tracing_writes(observed):
        reconciled = container.worker.reconcile(stalled.job_id)

    assert reconciled.status == "succeeded"
    assert observed == set(RUNTIME_WRITES_BY_WORKFLOW_STATE["activating"])


def test_the_two_states_are_genuinely_different(container) -> None:
    """If they were equal, one set would have done and this design is wrong."""
    first = set(RUNTIME_WRITES_BY_WORKFLOW_STATE["runtime_provisioning"])
    reconcile = set(RUNTIME_WRITES_BY_WORKFLOW_STATE["activating"])
    assert reconcile < first
    assert TableWrite("onboarding_transitions", "insert") in first - reconcile


def test_an_unknown_runtime_outcome_is_reported_as_uncertain(container) -> None:
    """Some states have no single answer, and saying so is the honest report.

    A job left `outcome_unknown` is reconciled next, and
    `ProvisioningWorker._reconcile` branches on `provider.inspect()` — settle,
    fail, or re-run preparation. Which branch is taken depends on an answer only
    the provider has, and asking is precisely what this command must not do.
    Reporting the first-attempt set here would promise a bootstrap token, a
    household update and a workflow transition that may never happen: false
    precision, which is worse for an operator than being told to go and look.
    """
    household_id = _household_with_profile(container)
    _verify_all_steps(container, household_id)
    sink = _BrokenSecretSink()
    container.worker.secret_sink = sink
    stalled = container.worker.run_once()
    assert stalled.error_code == "secret_install_unknown"

    plan = plan_onboarding(container, household_id)

    assert "outcome_unknown" in plan.rehearsal
    assert "reconcile" in plan.rehearsal
    assert plan.table_writes == []
    assert plan.blocked_by is not None
    assert plan.committed is False


def test_the_cli_refuses_a_missing_database_without_creating_one(
    tmp_path: Path, monkeypatch
) -> None:
    """`sqlite3.connect` creates a missing file, and opening makes directories.

    So asking about pending migrations against a wrong path left an empty
    database behind — a mutation, from the one command that promises none, and
    one an operator could easily cause with a typo.
    """
    config = ControlPlaneConfig.for_test(tmp_path / "absent" / "control-plane.db")
    monkeypatch.setattr(ControlPlaneConfig, "from_env", staticmethod(lambda: config))

    with pytest.raises(SystemExit, match="no database"):
        main(["--dry-run", "--household", "10000000-0000-4000-8000-000000000031"])

    assert not config.database_path.exists()
    assert not config.database_path.parent.exists()


def test_committed_reports_only_rows_this_rehearsal_minted(container) -> None:
    """The worker can commit the instant the rehearsal rolls back.

    Comparing COUNTS across the rollback made that legitimate commit look like
    the dry-run's own write, and the CLI then refused to print a report that was
    in fact correct. The verdict is now about ids this rehearsal minted, which a
    concurrent commit cannot forge.
    """
    household_id = _household_with_profile(container)
    _verify_all_steps(container, household_id)

    # A revision the WORKER committed, present before the rehearsal begins.
    foreign = container.database.query(
        "SELECT id FROM config_revisions WHERE household_id = ?", (household_id,)
    )
    assert foreign

    plan = plan_onboarding(container, household_id)

    # Nothing was rehearsed here (the revision is issued), so nothing is owned.
    assert plan.uncommitted_revision_delta == 0
    assert plan.committed is False
    # The worker's rows are still there and are not the dry-run's business.
    assert container.database.query(
        "SELECT id FROM config_revisions WHERE household_id = ?", (household_id,)
    ) == foreign


def test_a_rehearsed_revision_is_owned_and_rolled_back(container) -> None:
    """The other half: a row the rehearsal DID mint, and did not keep."""
    household_id = _household_with_profile(container)
    _verify_all_steps(container, household_id)
    with container.database.write() as connection:
        connection.execute(
            "DELETE FROM provisioning_jobs WHERE household_id = ? AND kind = 'runtime'",
            (household_id,),
        )
        connection.execute(
            "DELETE FROM config_revisions WHERE household_id = ?", (household_id,)
        )

    plan = plan_onboarding(container, household_id)

    assert plan.uncommitted_revision_delta == 1
    assert plan.committed is False
    assert container.database.query(
        "SELECT id FROM config_revisions WHERE household_id = ?", (household_id,)
    ) == []


def test_a_fly_configured_deployment_reports_fly_resources(container) -> None:
    """The counterpart: Fly names must still appear when Fly is configured.

    Otherwise "report the configured provider" would be satisfied by reporting
    nothing useful at all. Exercised directly against `_runtime_resources`,
    because driving a full onboarding through the real Fly provisioner would
    make network calls the rehearsal exists to avoid.
    """
    household_id = _household_with_profile(container)
    _verify_all_steps(container, household_id)
    revision = container.database.query_one(
        "SELECT revision FROM config_revisions WHERE household_id = ?"
        " ORDER BY revision DESC LIMIT 1",
        (household_id,),
    )["revision"]
    spec = container.configs.manifest(household_id, revision)

    fly = FlyRuntimeProvisioner(
        api_token="fly-api-secret-canary",
        org_slug="synthetic-org",
        image_digest="registry.example.test/abrolia@sha256:" + "a" * 64,
        bootstrap_url="https://app.example.test",
    )

    class _FlyConfigured:
        config = SimpleNamespace(runtime_provider="fly-runtime")
        providers = SimpleNamespace(get=lambda _name: fly)
        configs = container.configs

    resources = _runtime_resources(_FlyConfigured(), household_id, spec)

    assert {resource["stable_name"] for resource in resources} == {
        FlyRuntimeProvisioner.stable_app_name(household_id),
        FlyRuntimeProvisioner.stable_volume_name(),
        FlyRuntimeProvisioner.stable_machine_name(),
    }
    assert {resource["provider"] for resource in resources} == {"fly-runtime"}


def test_an_unplannable_provider_asserts_nothing_rather_than_guessing() -> None:
    """No plan and not Fly: say so, do not borrow Fly's resource list."""

    class _Opaque:
        config = SimpleNamespace(runtime_provider="some-future-runtime")
        providers = SimpleNamespace(get=lambda _name: object())

    resources = _runtime_resources(_Opaque(), "household-1", None)

    assert len(resources) == 1
    assert resources[0]["kind"] == "unknown"
    assert "some-future-runtime" in resources[0]["summary"]
    assert FlyRuntimeProvisioner.stable_volume_name() not in str(resources)


def test_a_succeeded_runtime_job_is_not_reported_as_pending(container) -> None:
    """`_settle_runtime_ready` settles the job while the workflow is `activating`.

    A terminal row was still selected, labelled `pending_runtime_job`, and used
    to advertise a reconcile write set — but no `ensure_runtime` operation
    remains at that point; the next writes belong to bootstrap activation, which
    this command says nothing about.
    """
    household_id = _household_with_profile(container)
    _verify_all_steps(container, household_id)
    while container.worker.run_once() is not None:
        pass
    settled = container.database.query_one(
        "SELECT status FROM provisioning_jobs WHERE household_id = ?"
        " AND kind = 'runtime' AND operation = 'ensure_runtime'"
        " ORDER BY created_at DESC LIMIT 1",
        (household_id,),
    )
    assert settled["status"] == "succeeded"

    plan = plan_onboarding(container, household_id)

    assert plan.pending_runtime_job == {}
    assert plan.table_writes == []
    assert "bootstrap activation" in plan.rehearsal


def test_the_write_set_is_labelled_as_the_success_path(container) -> None:
    """It is an upper bound on a provider result, not a prediction of one.

    A first attempt can be rate-limited, rejected or time out, and those
    branches write a subset. Presenting the nine-table mapping as "the exact
    writes" told an operator one possible future as fact.
    """
    household_id = _household_with_profile(container)
    _verify_all_steps(container, household_id)

    plan = plan_onboarding(container, household_id)

    assert plan.pending_runtime_job
    assert "SUCCESS PATH" in plan.rehearsal
    assert "subset" in plan.rehearsal
    # The success path PLUS the compensation a mid-call cancel triggers: the
    # set is an upper bound on what the pending call can write, and a
    # cancellation landing inside `provider.prepare` is one of the things it
    # can do.
    assert set(plan.table_writes) == set(
        RUNTIME_WRITES_BY_WORKFLOW_STATE["runtime_provisioning"]
    ) | set(COMPENSATION_WRITES)


def test_the_secret_target_is_the_configured_provider_s_reference(container) -> None:
    """`_finish_runtime` installs against the provider's runtime reference.

    Reporting a Fly app name under `dry-run-runtime` — the allowed default —
    made the rehearsal contradict its own resource section, which reports the
    synthetic reference, and misstated where the real operation puts secrets.
    """
    household_id = _household_with_profile(container)
    _verify_all_steps(container, household_id)

    plan = plan_onboarding(container, household_id)

    runtime_secrets = [
        secret
        for secret in plan.secrets
        if secret["name"] in {RUNTIME_BOOTSTRAP_SECRET, RUNTIME_DSAR_SECRET}
    ]
    assert len(runtime_secrets) == 2
    for secret in runtime_secrets:
        assert secret["target"] == f"synthetic-runtime:{household_id}"
        assert secret["target"] != FlyRuntimeProvisioner.stable_app_name(household_id)
    # The rehearsal must not contradict itself.
    assert {resource["stable_name"] for resource in plan.runtime_resources} == {
        runtime_secrets[0]["target"]
    }


def test_a_failed_runtime_job_is_reported_as_a_failure(container) -> None:
    """The absence of a pending job does not mean success.

    `_mark_step_problem` leaves the revision issued and the workflow in
    `runtime_provisioning`, so inferring "awaiting activation" from a missing
    pending row reported a failed onboarding as normal progress — hiding the one
    state that actually needs a human.
    """
    household_id = _household_with_profile(container)
    _verify_all_steps(container, household_id)
    with container.database.write() as connection:
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'failed', settled_at = 1.0,"
            " error_code = 'provider_rejected' WHERE household_id = ?"
            " AND kind = 'runtime' AND operation = 'ensure_runtime'",
            (household_id,),
        )

    plan = plan_onboarding(container, household_id)

    assert plan.pending_runtime_job == {}
    assert plan.table_writes == []
    assert "failed" in plan.rehearsal
    assert plan.blocked_by is not None and "failed" in plan.blocked_by
    assert "bootstrap activation" not in plan.rehearsal


def test_a_completed_household_is_not_reported_as_awaiting_activation(
    container,
) -> None:
    """`complete` means activation already committed.

    Folding it into the activating case told an operator that every fully
    onboarded household was still waiting for a step that had finished — a false
    next action on the state they see most often.
    """
    household_id = _household_with_profile(container)
    _verify_all_steps(container, household_id)
    while container.worker.run_once() is not None:
        pass
    with container.database.write() as connection:
        connection.execute(
            "UPDATE onboarding_workflows SET state = 'complete' WHERE household_id = ?",
            (household_id,),
        )

    plan = plan_onboarding(container, household_id)

    assert "complete" in plan.rehearsal
    assert "waiting on bootstrap activation" not in plan.rehearsal
    assert plan.table_writes == []


def test_an_expired_lease_is_reported_as_a_reconciliation(container) -> None:
    """A reclaimed `running` job is inspected, not retried.

    `_run_once` calls `provider.inspect()` for it — the same provider-dependent
    branch as `outcome_unknown`, which can deprovision the resource and insert a
    bootstrap-cleanup job. Those writes fall outside the advertised set
    entirely, so the guarantee attached to it would have been false.
    """
    household_id = _household_with_profile(container)
    _verify_all_steps(container, household_id)
    with container.database.write() as connection:
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'running', lease_until = 1.0"
            " WHERE household_id = ? AND kind = 'runtime'"
            " AND operation = 'ensure_runtime'",
            (household_id,),
        )

    plan = plan_onboarding(container, household_id)

    assert "expired lease" in plan.rehearsal
    assert "reconcile" in plan.rehearsal
    assert plan.table_writes == []
    assert plan.blocked_by is not None


def test_a_held_lease_is_still_reported_as_the_success_path(container) -> None:
    """The counterpart: an unexpired running job is an ordinary first attempt."""
    household_id = _household_with_profile(container)
    _verify_all_steps(container, household_id)
    with container.database.write() as connection:
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'running',"
            " lease_until = ? WHERE household_id = ? AND kind = 'runtime'"
            " AND operation = 'ensure_runtime'",
            (time.time() + 3600, household_id),
        )

    plan = plan_onboarding(container, household_id)

    assert "SUCCESS PATH" in plan.rehearsal
    # The success path PLUS the compensation a mid-call cancel triggers: the
    # set is an upper bound on what the pending call can write, and a
    # cancellation landing inside `provider.prepare` is one of the things it
    # can do.
    assert set(plan.table_writes) == set(
        RUNTIME_WRITES_BY_WORKFLOW_STATE["runtime_provisioning"]
    ) | set(COMPENSATION_WRITES)


def test_the_cli_leaves_the_live_database_files_byte_identical(
    tmp_path: Path, monkeypatch
) -> None:
    """The structural property, tested structurally.

    Every earlier attempt closed one way SQLite writes — migrating on build,
    creating a missing file, rewriting a persistent journal mode — and another
    remained: closing the last connection CHECKPOINTS committed WAL frames into
    the main file and removes the sidecars, which no rollback undoes and no flag
    prevents. Rehearsing against a copy makes the property structural rather
    than a list of closed holes, so this asserts the whole directory instead of
    any single mechanism.
    """
    config = ControlPlaneConfig.for_test(tmp_path)
    # Explicitly closed. Left to garbage collection, this connection can outlive
    # the statement, and while ANY connection is open the rehearsal is not the
    # last one — so the checkpoint-on-close being guarded against never fires
    # and the test passes against a read-write source.
    seed = ControlPlaneDatabase(config.database_path)
    seed.migrate()
    seed.close()

    # A writer that DIES, which is the only way to leave committed frames in the
    # WAL with no live connection. Closing a connection cleanly checkpoints, and
    # holding one open means the rehearsal is never the last connection — either
    # way the failure being guarded against cannot occur, and an earlier version
    # of this test made both mistakes in turn and passed against a read-write
    # source that would have checkpointed in production.
    writer = textwrap.dedent(
        f"""
        import os, sqlite3
        c = sqlite3.connect({str(config.database_path)!r}, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("CREATE TABLE canary (id TEXT PRIMARY KEY)")
        c.execute("INSERT INTO canary VALUES ('uncheckpointed')")
        os._exit(0)
        """
    )
    subprocess.run([sys.executable, "-c", writer], check=True)

    wal = config.database_path.with_name(f"{config.database_path.name}-wal")
    assert wal.exists() and wal.stat().st_size > 0, (
        "the fixture must leave uncheckpointed frames, or this proves nothing"
    )

    def fingerprint() -> dict[str, bytes]:
        # `-shm` is deliberately excluded and that exclusion is the whole
        # subtlety. It is the shared-memory index SQLite must map to read a WAL
        # database at all, it holds no durable data, and it is rebuilt from the
        # WAL — so touching it is not a mutation of the database. Every other
        # file, including the WAL itself, must be byte-identical.
        return {
            path.name: hashlib.sha256(path.read_bytes()).digest()
            for path in sorted(tmp_path.iterdir())
            if path.is_file() and not path.name.endswith("-shm")
        }

    before = fingerprint()
    monkeypatch.setattr(ControlPlaneConfig, "from_env", staticmethod(lambda: config))

    with pytest.raises(SystemExit):
        main(["--dry-run", "--household", "10000000-0000-4000-8000-000000000031"])

    assert fingerprint() == before
    # And the durable pair specifically, named so a regression is unambiguous.
    for suffix in ("", "-wal"):
        name = f"{config.database_path.name}{suffix}"
        assert fingerprint().get(name) == before.get(name), name


@pytest.mark.parametrize("journal_mode", ["WAL", "DELETE"])
def test_the_rehearsal_snapshot_survives_a_concurrent_writer(
    tmp_path: Path, monkeypatch, journal_mode: str
) -> None:
    """The rehearsal snapshot holds up while another connection is committing.

    WHAT THIS DOES NOT DO: it does not reproduce the race that motivated the
    change. Restoring the old `shutil.copy2` loop still passes this test, even
    with checkpoint churn and twelve iterations — a torn copy is real but rare
    enough that a short test does not hit it. Saying so here because a test that
    looks like a regression guard and is not one is worse than no test.

    What it does establish: the online-backup path opens cleanly and reports a
    committed state under sustained concurrent writes in BOTH journal modes,
    including DELETE mode, where the old loop would not have copied a hot
    `-journal` at all. The correctness argument for the change rests on SQLite's
    documented guarantee — independently copied database and sidecar files are
    not a valid snapshot — not on this reproduction.
    """
    config = ControlPlaneConfig.for_test(tmp_path)
    seeded = ControlPlaneDatabase(config.database_path)
    seeded.migrate()
    with seeded.write() as connection:
        connection.execute("CREATE TABLE churn (id INTEGER PRIMARY KEY, blob TEXT)")
    seeded.connection.execute(f"PRAGMA journal_mode={journal_mode}")
    monkeypatch.setattr(ControlPlaneConfig, "from_env", staticmethod(lambda: config))

    stop = threading.Event()
    failures: list[BaseException] = []

    def churn() -> None:
        writer = sqlite3.connect(config.database_path, isolation_level=None)
        try:
            row = 0
            while not stop.is_set():
                row += 1
                writer.execute(
                    "INSERT INTO churn VALUES (?, ?)", (row, "x" * 4000)
                )
                if row % 3 == 0:
                    # Checkpointing is precisely when the main file and the WAL
                    # diverge, so a non-atomic copy is most likely to catch them
                    # in different generations here.
                    writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except BaseException as error:  # surfaced below, not swallowed
            failures.append(error)
        finally:
            writer.close()

    thread = threading.Thread(target=churn, daemon=True)
    thread.start()
    try:
        for _ in range(12):
            # Exits on the missing household — after the snapshot, which is the
            # step under test.
            with pytest.raises(SystemExit) as exit_info:
                main([
                    "--dry-run",
                    "--household",
                    "10000000-0000-4000-8000-000000000031",
                ])
            message = str(exit_info.value)
            assert "could not snapshot" not in message, message
            assert "malformed" not in message and "corrupt" not in message, message
    finally:
        stop.set()
        thread.join(timeout=10)

    assert not failures, failures
    seeded.close()


def _seed_receipt(container, household_id: str, name: str) -> str:
    """Attach a receipt to the household's newest email job.

    Seeded rather than provisioned: the synthetic provider never installs a
    secret, so a fixture that onboards normally cannot exercise this path at
    all. The JOB and its request are real, which is what the correlation reads.
    """
    job_id = container.database.query_one(
        "SELECT id FROM provisioning_jobs WHERE household_id = ?"
        " AND kind = 'email_identity' ORDER BY created_at DESC LIMIT 1",
        (household_id,),
    )["id"]
    with container.database.write() as connection:
        connection.execute(
            "INSERT INTO email_secret_installs (job_id, household_id, secret_name,"
            " namespace_ref, installed_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, household_id, name, "synthetic-namespace", 1.0, 1.0),
        )
    return job_id


def _reported_secrets(container, household_id: str) -> set[str]:
    return {secret["name"] for secret in plan_onboarding(container, household_id).secrets}


def test_only_the_current_identitys_binding_is_reported_as_installed(container) -> None:
    """A retained receipt must not be attributed to a replacement identity.

    The receipt outlives its binding by design: `_delete_email_cleanup_secret`
    removes the secret from the sink and deliberately keeps the
    `email_secret_installs` row as an audit record.

    The previous filter correlated by HOUSEHOLD — "is there still an identity in
    a credential-holding state" — which is a different question. Reset deletes
    the identity and its binding but leaves the succeeded job and receipt alone,
    so once a REPLACEMENT identity reaches verified, the old receipt's job is
    still not cancelled and the unrelated new identity satisfies the check.
    Both names came back as installed.

    This drives the real path. Nothing is edited by hand — in particular the
    succeeded job is left succeeded, which is what the previous test got wrong
    by marking it `cancelled`, something reset never does.
    """
    household_id = _household_with_profile(container)
    _verify_all_steps(container, household_id)
    account_id, session_id = _PRINCIPALS[household_id]
    first_job = _seed_receipt(container, household_id, "ABROLIA_NERVE_EMAIL_CREDENTIALS")
    assert "ABROLIA_NERVE_EMAIL_CREDENTIALS" in _reported_secrets(container, household_id)

    container.onboarding.reset_from(
        household_id,
        StepKind.EMAIL,
        context=_context(
            container, household_id, "reset", account_id=account_id,
            session_id=session_id,
        ),
    )
    for _ in range(10):
        if container.worker.run_once() is None:
            break
    # The reset leaves the job that installed the secret alone. If a later
    # change starts cancelling it, this test would pass for the wrong reason.
    assert container.database.query_one(
        "SELECT status FROM provisioning_jobs WHERE id = ?", (first_job,)
    )["status"] == "succeeded"

    # Reconnect: a replacement identity reaches verified with its own binding.
    _verify_all_steps(container, household_id, label="reconnect")
    second_job = _seed_receipt(container, household_id, "ABROLIA_NERVE_EMAIL_REPLACEMENT")
    assert second_job != first_job

    reported = _reported_secrets(container, household_id)

    assert "ABROLIA_NERVE_EMAIL_REPLACEMENT" in reported, "the live binding is missing"
    assert "ABROLIA_NERVE_EMAIL_CREDENTIALS" not in reported, (
        "the superseded identity's binding is still reported as installed"
    )
    # And the audit trail is intact: the report is shaped by filtering, never by
    # deleting a receipt.
    retained = {
        row["secret_name"]
        for row in container.database.query(
            "SELECT secret_name FROM email_secret_installs WHERE household_id = ?",
            (household_id,),
        )
    }
    assert retained == {
        "ABROLIA_NERVE_EMAIL_CREDENTIALS",
        "ABROLIA_NERVE_EMAIL_REPLACEMENT",
    }


def test_a_cancelled_workflow_advertises_no_future_work(container) -> None:
    """`cancel` leaves verified steps behind; the planner cannot tell.

    Cancelling after every step verified revokes the issued revision and cancels
    the queued job but keeps the step results, so the rehearsal fell through to
    the planning branch, successfully rehearsed a revision N+1 and reported no
    blocker — a ready-to-provision report for an onboarding that can never
    resume.
    """
    household_id = _household_with_profile(container)
    _verify_all_steps(container, household_id)
    account_id, session_id = _PRINCIPALS[household_id]
    container.onboarding.cancel(
        household_id,
        context=_context(
            container, household_id, "cancel", account_id=account_id,
            session_id=session_id,
        ),
    )

    plan = plan_onboarding(container, household_id)

    # The cancellation ends the ONBOARDING, which is why nothing
    # future-tense is claimed. Whatever teardown it queued is listed as
    # fact in `pending_step_jobs`; the report does not predict what the
    # worker does with it.
    assert plan.blocked_by is not None and "cancelled" in plan.blocked_by
    assert plan.table_writes == []
    assert plan.runtime_resources == []
    assert plan.secrets == []
    assert plan.uncommitted_revision_delta == 0, "a revision N+1 was rehearsed"
    assert plan.committed is False


def test_no_state_advertises_resources_it_is_not_rehearsing(container) -> None:
    """`runtime_resources` and `secrets` were assigned unconditionally.

    So a household told "there is no pending operation to rehearse" was handed,
    in the same report, a runtime reference to create and a bootstrap token to
    install — for a complete household whose token had already been cleaned up.
    An operator acting on that prepares or rotates credentials for work that
    does not exist.

    One flag now decides it for every state, which is why this asserts the
    property across states rather than patching the one that was reported.
    """
    # `awaiting_activation`: the runtime job has settled and the next writes
    # belong to bootstrap activation, which this command does not model. It is
    # the state Codex named — a household told nothing is pending while being
    # handed a runtime reference to create and a bootstrap token to install,
    # the latter already cleaned up.
    household_id = _household_with_profile(container)
    _verify_all_steps(container, household_id)
    while container.worker.run_once() is not None:
        pass
    settled = plan_onboarding(container, household_id)

    # ...and then `cancelled`, where the verified step results survive so
    # nothing else marks the workflow terminal. One household reaches both, in
    # order, because the consent receipt ids in the fixture selection are unique
    # per household.
    account_id, session_id = _PRINCIPALS[household_id]
    container.onboarding.cancel(
        household_id,
        context=_context(
            container, household_id, "cancel", account_id=account_id,
            session_id=session_id,
        ),
    )
    cancelled = plan_onboarding(container, household_id)

    # Neither describes future onboarding work. `operation_pending` may be true
    # for either if a teardown or bootstrap-cleanup job is queued — that is the
    # worker's business, not the onboarding's — so the assertion that matters is
    # about what is ADVERTISED.
    for plan in (settled, cancelled):
        assert plan.runtime_resources == [], plan.rehearsal
        assert plan.secrets == [], plan.rehearsal
        assert plan.table_writes == [], plan.rehearsal


def test_a_database_path_with_uri_syntax_is_rehearsed_not_created(
    tmp_path: Path, monkeypatch
) -> None:
    """`?` and `#` are legal in a pathname and meaningful in a SQLite URI.

    Interpolating the path into `file:{path}?mode=ro` made
    `/data/control?plane.db` parse as the filename `/data/control` with the rest
    read as URI syntax. `mode=ro` was then not the parameter it looked like, so
    SQLite opened read-write-CREATE: it created `/data/control`, and the
    rehearsal snapshotted an EMPTY database while the real one was never read.
    Two guarantees broken by one missing encode — "mutates nothing" and
    "rehearses the database you named".
    """
    volume = tmp_path / "data"
    volume.mkdir()
    for name in ("control?plane.db", "control#plane.db"):
        database_path = volume / name
        seed = ControlPlaneDatabase(database_path)
        seed.migrate()
        seed.close()
        before = {
            path.name: path.read_bytes() for path in volume.iterdir() if path.is_file()
        }

        config = replace(
            ControlPlaneConfig.for_test(tmp_path), database_path=database_path
        )
        monkeypatch.setattr(
            ControlPlaneConfig, "from_env", staticmethod(lambda config=config: config)
        )
        # The household does not exist, so the rehearsal refuses AFTER taking
        # its snapshot — which is the part under test. What must not happen is a
        # file appearing, or the real database going unread.
        with pytest.raises(SystemExit):
            main(["--dry-run", "--household", "10000000-0000-4000-8000-000000000031"])

        # The direct evidence: with the bug SQLite parsed `/data/control` as
        # the filename and opened it read-write-CREATE, so this file appeared
        # and the named database was never read at all.
        assert not (volume / "control").exists(), f"{name}: a prefix file was created"
        assert database_path.read_bytes() == before[name], f"{name}: the database moved"
        # Sidecars are the documented exception, as they are for the
        # byte-identical test above: a read-only open of a WAL database creates
        # `-shm`, and `-wal` with it. Neither holds durable data. What must not
        # appear is another DATABASE.
        created = {
            path.name
            for path in volume.iterdir()
            if path.is_file()
            and path.name not in before
            and not path.name.endswith(("-wal", "-shm"))
        }
        assert created == set(), f"{name}: the dry-run created {created}"


def test_a_household_under_deletion_advertises_no_onboarding(container) -> None:
    """Deletion is terminal in a way the WORKFLOW cannot show.

    `DeletionService.delete` sets `households.status` and deliberately preserves
    a leased `ensure_runtime` job, leaving the workflow in
    `runtime_provisioning`. Every classification therefore read a live
    provisioning operation and advertised the runtime success path and its
    secret installs — a provisioning plan for a household whose only valid path
    is deletion. The status lives on `households`, which this query never
    joined.
    """
    household_id = _household_with_profile(container)
    _verify_all_steps(container, household_id)
    with container.database.write() as connection:
        connection.execute(
            "UPDATE households SET status = 'deleting' WHERE id = ?", (household_id,)
        )

    plan = plan_onboarding(container, household_id)

    assert plan.blocked_by is not None and "deleting" in plan.blocked_by
    assert plan.table_writes == []
    assert plan.runtime_resources == []
    assert plan.secrets == []
    assert plan.uncommitted_revision_delta == 0


def test_a_provider_configuration_mismatch_is_reported_as_blocked(container) -> None:
    """A job's provider is durable; the secret SINK is not.

    `ControlPlaneContainer.build` selects one sink from the current
    configuration, so after a restart under a different
    `ABROLIA_RUNTIME_PROVIDER` the worker dispatches the job's own provider and
    then hands its reference to the configured provider's sink —
    `synthetic-runtime:<household>` to `FlySecretSink.install`, which shells out
    to `fly secrets import --app` and settles the job `outcome_unknown`.

    An earlier round made the report FOLLOW the job's provider, which is right
    about what the worker dispatches and silent about this: the operator got a
    runnable-looking plan for an onboarding that stalls at secret installation.
    The provider and its sink are usable only as a durable pair, and this
    command cannot repair the pairing — so it refuses to describe the operation
    as pending, and names both sides so the mismatch is actionable.
    """
    household_id = _household_with_profile(container)
    _verify_all_steps(container, household_id)
    configured = container.config.runtime_provider
    other = "fake-channel"
    assert other != configured
    with container.database.write() as connection:
        connection.execute(
            "UPDATE provisioning_jobs SET provider = ?"
            " WHERE household_id = ? AND kind = 'runtime'"
            " AND operation = 'ensure_runtime'"
            " AND status IN ('pending','running','waiting_user','outcome_unknown')",
            (other, household_id),
        )

    plan = plan_onboarding(container, household_id)

    # PENDING, and not executable. The job is still `pending` and the worker can
    # still lease it, so reporting "nothing is pending" told an operator there
    # was no live work while the worker was about to stall on it. Two facts,
    # two flags.
    assert plan.blocked_by is not None
    # Both sides named: which is queued, and which the deployment is set to.
    assert other in plan.blocked_by and configured in plan.blocked_by
    # And nothing future-tense, because the operation cannot run as recorded.
    assert plan.runtime_resources == []
    assert plan.secrets == []
    assert plan.table_writes == []
    # The job is still reported, so an operator can see what to restore.
    assert plan.pending_runtime_job["provider"] == other
    assert [job["provider"] for job in plan.unresolved_runtime_jobs] == [other]


def test_a_job_naming_an_unregistered_provider_is_blocked_too(container) -> None:
    """An unregistered provider is a mismatch by the same rule.

    It cannot equal the configured one, so it takes the same path — which is
    stricter than the previous behaviour of reporting an "unavailable" resource
    entry, and correct for the same reason: the deployment cannot run the job as
    recorded.
    """
    household_id = _household_with_profile(container)
    _verify_all_steps(container, household_id)
    with container.database.write() as connection:
        connection.execute(
            "UPDATE provisioning_jobs SET provider = 'a-provider-nobody-registers'"
            " WHERE household_id = ? AND kind = 'runtime'"
            " AND operation = 'ensure_runtime'",
            (household_id,),
        )

    plan = plan_onboarding(container, household_id)

    assert plan.runtime_resources == []
    assert plan.secrets == []
    assert "a-provider-nobody-registers" in (plan.blocked_by or "")


def test_the_planning_pass_uses_the_configured_provider(container) -> None:
    """No job yet means nothing to disagree with, so configuration is right.

    The fix must not overcorrect: before a revision is issued there is no
    durable operation, and the next one will be created under the provider this
    deployment is configured with.
    """
    household_id = _household_with_profile(container)

    plan = plan_onboarding(container, household_id)

    assert plan.pending_runtime_job == {}
    if plan.runtime_resources:
        assert {resource["provider"] for resource in plan.runtime_resources} == {
            container.config.runtime_provider
        }


def test_the_snapshot_touches_only_the_shared_memory_index(
    tmp_path: Path, monkeypatch
) -> None:
    """The guarantee, fingerprinted, including the case that has no `-shm` yet.

    A crashed writer leaves committed `-wal` frames and no `-shm`, and reading a
    WAL database requires one — so the rehearsal CREATES it. That is a change to
    the directory, and the plan's unconditional "mutates nothing" did not admit
    it. Both documents now say `-shm` is outside the guarantee; this pins what
    is inside it: the database and its `-wal`, byte for byte.
    """
    config = ControlPlaneConfig.for_test(tmp_path)
    seed = ControlPlaneDatabase(config.database_path)
    seed.migrate()
    seed.close()

    # A writer that DIES, so committed frames stay in the WAL with no live
    # connection — the only way to reach this state.
    writer = textwrap.dedent(
        f"""
        import os, sqlite3
        c = sqlite3.connect({str(config.database_path)!r}, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("CREATE TABLE canary (id TEXT PRIMARY KEY)")
        c.execute("INSERT INTO canary VALUES ('uncheckpointed')")
        os._exit(0)
        """
    )
    subprocess.run([sys.executable, "-c", writer], check=True)

    shm = config.database_path.with_name(f"{config.database_path.name}-shm")
    wal = config.database_path.with_name(f"{config.database_path.name}-wal")
    shm.unlink(missing_ok=True)
    assert wal.exists() and wal.stat().st_size > 0, (
        "the fixture must leave uncheckpointed frames, or this proves nothing"
    )
    assert not shm.exists(), "the fixture must start with no shared-memory index"

    covered = {
        path.name: path.read_bytes()
        for path in sorted(tmp_path.iterdir())
        if path.is_file() and not path.name.endswith("-shm")
    }

    monkeypatch.setattr(ControlPlaneConfig, "from_env", staticmethod(lambda: config))
    with pytest.raises(SystemExit):
        main(["--dry-run", "--household", "10000000-0000-4000-8000-000000000031"])

    after = {
        path.name: path.read_bytes()
        for path in sorted(tmp_path.iterdir())
        if path.is_file() and not path.name.endswith("-shm")
    }
    assert after == covered, "the database or its WAL changed"
    # And the one file the guarantee excludes is named, so a future reader of
    # this test knows the exclusion is deliberate rather than an oversight.
    assert shm.exists(), (
        "the rehearsal did not create the index it needs; if SQLite stops"
        " requiring one, the documented exclusion can be removed"
    )


def test_a_blocked_planning_pass_advertises_no_future_work(container) -> None:
    """Blocked means there is no operation to describe.

    `planner.issue` raises `ValueError` for any unverified step or missing
    prerequisite, and that handler set `blocked_by` while leaving
    `operation_pending` true — so the gate below still emitted runtime resources
    and both runtime secrets for an operation the same report said cannot run.
    The one branch that reaches the gate by raising kept the contradiction the
    flag exists to prevent.
    """
    household_id = _household_with_profile(container)

    plan = plan_onboarding(container, household_id)

    assert plan.blocked_by is not None
    assert plan.unverified_steps
    assert plan.runtime_resources == []
    assert plan.secrets == []
    assert plan.table_writes == []


@pytest.mark.parametrize(
    "value",
    [
        "../x",
        "not-a-uuid",
        "10000000-0000-4000-8000-000000000031 ",
        # Real hex letters, so uppercasing is not a no-op the way it is for an
        # all-digit UUID — which is what made an earlier version of this case
        # pass while proving nothing.
        "abcdef00-0000-4000-8000-0000000000ab".upper(),
        "{10000000-0000-4000-8000-000000000031}",
        "urn:uuid:10000000-0000-4000-8000-000000000031",
    ],
)
def test_a_malformed_household_argument_is_rejected_at_the_boundary(value) -> None:
    """A typo must not come back as "household has no onboarding workflow".

    Unvalidated, every one of these reached the database and produced the same
    message a genuinely missing workflow produces — which is the diagnosis an
    operator acts on. Canonical form specifically: `UUID()` accepts braces, urn
    prefixes and mixed case, and all of them compare unequal to the stored
    identifier, so accepting them would reintroduce the same confusion by
    another route.
    """
    with pytest.raises(SystemExit):
        _parser().parse_args(["--dry-run", "--household", value])


def test_a_canonical_household_argument_is_accepted() -> None:
    household_id = "10000000-0000-4000-8000-000000000031"
    args = _parser().parse_args(["--dry-run", "--household", household_id])
    assert args.household == household_id


def test_every_unresolved_runtime_intent_is_inventoried(container) -> None:
    """`LIMIT 1` answered "what runs next" and hid what is still outstanding.

    A reset preserves a running runtime job as `outcome_unknown` with
    `reset_requires_reconciliation` — deliberately, because it may have created
    provider state. The owner can then complete the steps again and mint a NEWER
    job, and a single-row view showed only that one. An operator reading the
    report sees a clean pending job and proceeds past a provider effect nobody
    has reconciled.
    """
    household_id = _household_with_profile(container)
    _verify_all_steps(container, household_id)
    account_id, session_id = _PRINCIPALS[household_id]
    quarantined = container.database.query_one(
        "SELECT id FROM provisioning_jobs WHERE household_id = ?"
        " AND kind = 'runtime' AND operation = 'ensure_runtime'"
        " ORDER BY created_at DESC LIMIT 1",
        (household_id,),
    )["id"]
    # The state a reset leaves behind for a job that had already started.
    with container.database.write() as connection:
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'outcome_unknown',"
            " error_code = 'reset_requires_reconciliation' WHERE id = ?",
            (quarantined,),
        )
    container.onboarding.reset_from(
        household_id,
        StepKind.EMAIL,
        context=_context(
            container, household_id, "reset", account_id=account_id,
            session_id=session_id,
        ),
    )
    # The reset schedules an email cleanup, and reconnecting before it finishes
    # is refused — drain it, as a real operator's worker would.
    for _ in range(10):
        if container.worker.run_once() is None:
            break
    _verify_all_steps(container, household_id, label="again")
    newest = container.database.query_one(
        "SELECT id FROM provisioning_jobs WHERE household_id = ?"
        " AND kind = 'runtime' AND operation = 'ensure_runtime'"
        " AND id != ? ORDER BY created_at DESC LIMIT 1",
        (household_id, quarantined),
    )
    assert newest is not None, "the fixture did not mint a second runtime job"

    plan = plan_onboarding(container, household_id)

    reported = {job["job_id"] for job in plan.unresolved_runtime_jobs}
    assert quarantined in reported, (
        "a runtime intent still awaiting reconciliation was hidden by a newer job"
    )
    assert newest["id"] in reported
    # And the quarantine reason travels with it, so the operator knows which
    # command settles it.
    quarantine = next(
        job for job in plan.unresolved_runtime_jobs if job["job_id"] == quarantined
    )
    assert quarantine["error_code"] == "reset_requires_reconciliation"


def test_an_older_unresolved_intent_blocks_the_newer_plan(container) -> None:
    """Inventorying an outstanding intent is not the same as refusing to plan on it.

    The previous round listed every unresolved runtime job and went on
    classifying from the newest, so a reset-quarantined job — which may already
    have created a runtime — was reported alongside a clean pending operation.
    An operator provisioning on top of that creates exactly the conflict the
    quarantine exists to prevent.
    """
    household_id = _household_with_profile(container)
    _verify_all_steps(container, household_id)
    account_id, session_id = _PRINCIPALS[household_id]
    quarantined = container.database.query_one(
        "SELECT id FROM provisioning_jobs WHERE household_id = ?"
        " AND kind = 'runtime' AND operation = 'ensure_runtime'"
        " ORDER BY created_at DESC LIMIT 1",
        (household_id,),
    )["id"]
    with container.database.write() as connection:
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'outcome_unknown',"
            " error_code = 'reset_requires_reconciliation' WHERE id = ?",
            (quarantined,),
        )
    container.onboarding.reset_from(
        household_id,
        StepKind.EMAIL,
        context=_context(
            container, household_id, "reset", account_id=account_id,
            session_id=session_id,
        ),
    )
    for _ in range(10):
        if container.worker.run_once() is None:
            break
    _verify_all_steps(container, household_id, label="again")

    plan = plan_onboarding(container, household_id)

    assert len(plan.unresolved_runtime_jobs) > 1, "the fixture left only one intent"
    assert quarantined in (plan.blocked_by or "")
    # Nothing future-tense while an earlier effect is outstanding.
    assert plan.runtime_resources == []
    assert plan.secrets == []
    assert plan.table_writes == []
    # And the NARRATIVE, which emptying the three inventories does not settle.
    # This classifier cleared `models_an_operation` and was then left out of the
    # exclusive-state guard, so the planning pass ran anyway and overwrote
    # `rehearsal` with the newest job's ordinary success path. The report then
    # said in one field that an earlier intent blocks everything and in another
    # that the replacement job runs cleanly — which reads as "the quarantined
    # predecessor is compatible with this", the exact conclusion the quarantine
    # exists to stop an operator reaching.
    assert "unresolved" in (plan.rehearsal or ""), plan.rehearsal
    assert "SUCCESS PATH" not in (plan.rehearsal or ""), plan.rehearsal


def test_a_reconciliation_asserts_no_resource_or_secret_lifecycle(container) -> None:
    """`table_writes` was cleared here and the other two were not.

    After a `prepare` times out, or a secret install returns unknown, the next
    operation is a reconcile whose write set depends on a provider inspection
    this command does not perform. The report still described resources as
    though they will be created and both secrets as though they will be
    installed — when the inspection may find the resource already exists,
    failed, or needs cleanup. One uncertainty, three fields.
    """
    household_id = _household_with_profile(container)
    _verify_all_steps(container, household_id)
    with container.database.write() as connection:
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'outcome_unknown',"
            " error_code = 'outcome_unknown' WHERE household_id = ?"
            " AND kind = 'runtime' AND operation = 'ensure_runtime'",
            (household_id,),
        )

    plan = plan_onboarding(container, household_id)

    assert "reconcile" in plan.rehearsal
    assert plan.table_writes == []
    assert plan.runtime_resources == [], plan.rehearsal
    assert plan.secrets == [], plan.rehearsal


def test_a_pending_step_job_is_not_reported_as_no_work(container) -> None:
    """"Nothing is pending" was said about a worker with work in hand.

    The inventory asked only about `ensure_runtime`, so after `save_profile`
    queues `ensure_secret_namespace` — or while a selected email, WhatsApp or
    channel job is still pending — it found nothing. `planner.issue` then failed
    on unverified steps, and the report declared no pending operation and an
    empty inventory while `JobsRepository.lease` could execute the omitted job
    immediately.
    """
    account = container.accounts.create_verified("pending@family.test", now=BASE_TIME)
    household = container.households.create_for_owner(account.id, now=BASE_TIME)
    session = container.sessions.issue(account.id, now=BASE_TIME)
    _PRINCIPALS[household.id] = (account.id, session.id)
    container.onboarding.save_profile(
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
            container, household.id, "profile",
            account_id=account.id, session_id=session.id,
        ),
        now=BASE_TIME + 1,
    )
    # Deliberately NOT drained: the namespace job the profile step queues is
    # exactly the work the report used to omit.
    queued = container.database.query(
        "SELECT id FROM provisioning_jobs WHERE household_id = ?"
        " AND status IN ('pending','running','waiting_user','outcome_unknown')",
        (household.id,),
    )
    assert queued, "the fixture queued nothing to be omitted"

    plan = plan_onboarding(container, household.id)

    assert plan.pending_step_jobs, "a leasable job was reported as no work"
    assert {job["job_id"] for job in plan.pending_step_jobs} == {
        row["id"] for row in queued
    }
    # NOT blocked. The planner refusing for unverified steps is the normal state
    # while those steps' own jobs are still queued — that is work in progress.
    # Marking it blocked made the report contradict itself: a job listed with
    # `executable: true` in the same document the flag declared unable to
    # proceed.
    # Still nothing future-tense: no revision is planned yet, so there is no
    # runtime operation to describe resources or secrets for.
    assert plan.runtime_resources == []
    assert plan.secrets == []


@pytest.mark.parametrize(
    ("make", "message"),
    [
        pytest.param(
            lambda path: path.mkdir(parents=True),
            "not a regular file",
            id="directory",
        ),
        pytest.param(
            lambda path: path.write_bytes(b"this is not a database"),
            "could not",
            id="not-a-database",
        ),
    ],
)
def test_an_unusable_database_path_fails_with_the_commands_own_diagnostic(
    tmp_path: Path, monkeypatch, make, message
) -> None:
    """A traceback from three layers down is not a diagnostic.

    A directory passes `exists()` and then fails inside SQLite; anything else
    SQLite cannot open did the same, because the source connection sat outside
    the guarded block. Every operator-supplied boundary fails with this
    command's own message.
    """
    database_path = tmp_path / "data" / "control-plane.db"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    make(database_path)
    config = replace(
        ControlPlaneConfig.for_test(tmp_path), database_path=database_path
    )
    monkeypatch.setattr(ControlPlaneConfig, "from_env", staticmethod(lambda: config))

    with pytest.raises(SystemExit, match=message):
        main(["--dry-run", "--household", "10000000-0000-4000-8000-000000000031"])


def test_a_quarantined_runtime_intent_keeps_the_operation_pending(container) -> None:
    """Reset a leased runtime job, drain, and rehearse before re-verifying.

    The revoked revision forces the planning branch, the planner raises for the
    unverified steps, and considering only the STEP jobs reported "nothing is
    pending" while `unresolved_runtime_jobs` was listing an intent that still
    needs reconciling.
    """
    household_id = _household_with_profile(container)
    _verify_all_steps(container, household_id)
    account_id, session_id = _PRINCIPALS[household_id]
    with container.database.write() as connection:
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'outcome_unknown',"
            " error_code = 'reset_requires_reconciliation' WHERE household_id = ?"
            " AND kind = 'runtime' AND operation = 'ensure_runtime'",
            (household_id,),
        )
    container.onboarding.reset_from(
        household_id,
        StepKind.EMAIL,
        context=_context(
            container, household_id, "reset", account_id=account_id,
            session_id=session_id,
        ),
    )
    for _ in range(10):
        if container.worker.run_once() is None:
            break

    plan = plan_onboarding(container, household_id)

    assert plan.unresolved_runtime_jobs, "the fixture settled the quarantined intent"
    assert plan.runtime_resources == []
    assert plan.secrets == []


def test_the_snapshot_carries_the_live_worker_pause(tmp_path, monkeypatch) -> None:
    """`backup()` copies SQLite DATA; the pause is a sibling FILE.

    Redirecting `database_path` at the scratch copy pointed
    `worker_pause_path` at a directory where no marker exists, so the report
    labelled SQL-leasable jobs executable while the live repository, paused,
    returns nothing at all. A restored database stays paused until
    `resume-jobs` — which is exactly when an operator reaches for this command.
    """
    config = ControlPlaneConfig.for_test(tmp_path)
    seed = ControlPlaneDatabase(config.database_path)
    seed.migrate()
    seed.pause_workers()
    seed.close()
    assert config.database_path.with_name(
        f"{config.database_path.name}.workers-paused"
    ).is_file()

    seen: dict[str, bool] = {}
    real_rehearse = provision_module._rehearse

    def record(rehearsal_config, household_id):
        seen["paused"] = ControlPlaneDatabase(
            rehearsal_config.database_path
        ).workers_paused
        return real_rehearse(rehearsal_config, household_id)

    monkeypatch.setattr(ControlPlaneConfig, "from_env", staticmethod(lambda: config))
    monkeypatch.setattr(provision_module, "_rehearse", record)
    with pytest.raises(SystemExit):
        main(["--dry-run", "--household", "10000000-0000-4000-8000-000000000031"])

    assert seen.get("paused") is True, (
        "the rehearsal read an unpaused snapshot of a paused deployment"
    )


def test_an_unresolved_intent_is_reported_with_its_reason(container) -> None:
    """The fact, not a ranking.

    An earlier version asserted that the quarantined intent OUTRANKED the
    planner's prerequisite in `blocked_by` — a precedence claim, and one of
    several the report used to make about which action mattered most. Those
    claims are what needed relitigating every review round.

    What an operator needs is durable and unambiguous: the intent is listed with
    the status and error code that name the command settling it. The report says
    what is true and leaves the ordering to the person who can see the whole
    deployment.
    """
    household_id = _household_with_profile(container)
    _verify_all_steps(container, household_id)
    account_id, session_id = _PRINCIPALS[household_id]
    quarantined = container.database.query_one(
        "SELECT id FROM provisioning_jobs WHERE household_id = ?"
        " AND kind = 'runtime' AND operation = 'ensure_runtime'"
        " ORDER BY created_at DESC LIMIT 1",
        (household_id,),
    )["id"]
    with container.database.write() as connection:
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'outcome_unknown',"
            " error_code = 'reset_requires_reconciliation' WHERE id = ?",
            (quarantined,),
        )
    container.onboarding.reset_from(
        household_id,
        StepKind.EMAIL,
        context=_context(
            container, household_id, "reset", account_id=account_id,
            session_id=session_id,
        ),
    )
    for _ in range(10):
        if container.worker.run_once() is None:
            break

    plan = plan_onboarding(container, household_id)

    reported = {job["job_id"]: job for job in plan.unresolved_runtime_jobs}
    assert quarantined in reported, "the unresolved intent was not reported"
    assert reported[quarantined]["error_code"] == "reset_requires_reconciliation"
    assert reported[quarantined]["status"] == "outcome_unknown"
    # And nothing future-tense is advertised over it.
    assert plan.runtime_resources == []
    assert plan.secrets == []


def test_a_deterministic_step_job_carries_its_own_write_set(container) -> None:
    """A fact about the code, sitting beside the job it describes.

    The Step E1 contract asks for the pending operation's tables where durable
    state determines them. An earlier version satisfied that by CHOOSING a job
    for the top-level `table_writes` — which meant deciding which one runs next
    and defending that against `lease` in every edge case. What a given kind
    writes is static; attached per job, nothing is ranked and nothing is
    predicted.
    """
    household_id = _household_with_profile(container)
    workflow = container.database.query_one(
        "SELECT id FROM onboarding_workflows WHERE household_id = ?", (household_id,)
    )
    with container.database.write() as connection:
        cleanup_id, _created = container.jobs.create(
            connection,
            household_id=household_id,
            workflow_id=workflow["id"],
            kind="bootstrap_cleanup",
            operation="delete_bootstrap_secret",
            intent_key=f"{household_id}:late-bootstrap-cleanup:probe",
            request={"name": "HERMES_BOOTSTRAP_TOKEN"},
            provider="internal-secret-sink",
            now=BASE_TIME,
        )

    plan = plan_onboarding(container, household_id)

    cleanup = next(
        job for job in plan.pending_step_jobs if job["job_id"] == cleanup_id
    )
    assert cleanup["table_writes"] == [
        {"table": "provisioning_jobs", "operation": "update"}
    ]
    # A provider-backed job states no set, and the absence IS the answer.
    provider_backed = [
        job for job in plan.pending_step_jobs if job["kind"] == "email_identity"
    ]
    assert all(job["table_writes"] == [] for job in provider_backed)


def test_a_stale_restriction_receipt_blocks_the_runtime_report(container) -> None:
    """`_run_once` checks this BEFORE dispatching any provider.

    With the receipt revoked or stale, it fails the queued runtime job, revokes
    the revision and the bootstrap tokens, and returns the workflow to email.
    Reporting the ordinary success path over that describes an operation whose
    first act is to undo itself. This is durable state, not a prediction about
    scheduling — the receipt either is current or is not — and the report asks
    it with the worker's own SQL.
    """
    household_id = _household_with_profile(container)
    _verify_all_steps(container, household_id)
    healthy = plan_onboarding(container, household_id)
    assert healthy.table_writes, "the fixture reported no write set to lose"

    with container.database.write() as connection:
        connection.execute(
            "UPDATE consent_receipts SET revoked_at = ? WHERE household_id = ?"
            " AND purpose = 'special_category_content_restriction'",
            (BASE_TIME, household_id),
        )

    plan = plan_onboarding(container, household_id)

    assert plan.blocked_by is not None
    assert "content restriction" in plan.blocked_by
    # The block's OWN writes, on the JOB. The worker never reaches a provider
    # here, so what happens to that job is fully determined — and an operator
    # told "no write set" would not know the revision and the bootstrap tokens
    # are about to be revoked. Top-level stays empty: which gated job the
    # worker reaches first is not something this report decides.
    assert plan.table_writes == []
    assert plan.unresolved_runtime_jobs
    gated = plan.unresolved_runtime_jobs[0]
    assert gated["table_writes"] == [
        {"table": write.table, "operation": write.operation}
        for write in CONTENT_RESTRICTION_BLOCK_WRITES["runtime"]
    ]
    assert "bootstrap tokens" in gated["blocked_by"]
    assert plan.runtime_resources == []
    assert plan.secrets == []


def _requeue_under_another_provider(container, household_id: str) -> None:
    """Make the newest runtime intent name a provider this deployment is not."""
    other = "fake-channel"
    assert other != container.config.runtime_provider
    with container.database.write() as connection:
        connection.execute(
            "UPDATE provisioning_jobs SET provider = ?"
            " WHERE household_id = ? AND kind = 'runtime'"
            " AND operation = 'ensure_runtime'"
            " AND status IN ('pending','running','waiting_user','outcome_unknown')",
            (other, household_id),
        )


@pytest.mark.parametrize(
    "kind, operation",
    [
        ("runtime", "ensure_runtime"),
        ("runtime", "ensure_secret_namespace"),
        ("email_identity", "provision_email_identity"),
        ("whatsapp_identity", "provision_whatsapp_identity"),
        ("channel_binding", "bind_primary_channel"),
    ],
)
def test_a_cancellation_does_not_bury_an_unsettled_provider_intent(
    container, kind, operation
) -> None:
    """`_supersede_unsettled_jobs` preserves that job ON PURPOSE.

    A job past its provider boundary becomes
    `outcome_unknown/cancel_requires_reconciliation` — the service is saying it
    does not know what exists upstream. The cancellation branch then overwrote
    that with "start a new one rather than provisioning from this", which is
    advice to provision over provider state that may already be there. Any
    kind of job can be in that position, not only `ensure_runtime`.
    """
    household_id = _household_with_profile(container)
    _verify_all_steps(container, household_id)
    account_id, session_id = _PRINCIPALS[household_id]
    workflow = container.database.query_one(
        "SELECT id FROM onboarding_workflows WHERE household_id = ?", (household_id,)
    )
    with container.database.write() as connection:
        # Past the provider boundary when the cancel arrives, which is the whole
        # precondition: `pending` jobs are simply cancelled and need no
        # reconciliation.
        container.jobs.create(
            connection,
            household_id=household_id,
            workflow_id=workflow["id"],
            kind=kind,
            operation=operation,
            intent_key=f"{household_id}:{operation}:mid-flight",
            request={},
            provider=container.config.runtime_provider,
            now=BASE_TIME,
        )
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'running' WHERE household_id = ?"
            " AND operation = ?",
            (household_id, operation),
        )

    container.onboarding.cancel(
        household_id,
        context=_context(
            container, household_id, "cancel", account_id=account_id,
            session_id=session_id,
        ),
    )

    plan = plan_onboarding(container, household_id)

    quarantined = container.database.query(
        "SELECT id FROM provisioning_jobs WHERE household_id = ?"
        " AND error_code = 'cancel_requires_reconciliation'",
        (household_id,),
    )
    assert quarantined, "the fixture produced nothing needing reconciliation"
    assert "reconcile" in (plan.blocked_by or "").lower(), plan.blocked_by
    assert "start a new one" not in (plan.blocked_by or ""), (
        "the cancellation label buried an intent that reached its provider"
    )
    assert any(str(row["id"]) in (plan.blocked_by or "") for row in quarantined), (
        "the report does not name the job an operator has to reconcile"
    )
    assert plan.table_writes == []
    assert plan.runtime_resources == []
    assert plan.secrets == []


def test_deletion_outranks_a_provider_mismatch(container) -> None:
    """A configuration diagnostic is not a fact about the world.

    `DeletionService.delete` preserves a leased `ensure_runtime` job, which
    names the provider it was queued under. Restarting under a different
    `ABROLIA_RUNTIME_PROVIDER` then made the mismatch branch — which ran after
    the deletion branch and simply assigned over it — tell the operator to
    "cancel and re-plan" a household whose only valid path is deletion.
    """
    household_id = _household_with_profile(container)
    _verify_all_steps(container, household_id)
    with container.database.write() as connection:
        connection.execute(
            "UPDATE households SET status = 'deleting' WHERE id = ?", (household_id,)
        )
    # The job's provider is durable and the configuration is not, so the
    # mismatch is made by moving the job — which is what a restart under a
    # different `ABROLIA_RUNTIME_PROVIDER` amounts to. `ControlPlaneConfig` is
    # frozen; rewriting the row is both possible and closer to the real thing.
    _requeue_under_another_provider(container, household_id)

    plan = plan_onboarding(container, household_id)

    assert "deleting" in (plan.blocked_by or ""), plan.blocked_by
    assert "re-plan" not in (plan.blocked_by or ""), (
        "a deleting household was described as re-plannable"
    )
    assert "provider mismatch" not in (plan.blocked_by or "")
    assert plan.table_writes == []


def test_an_older_unresolved_intent_outranks_a_provider_mismatch(container) -> None:
    """Reconciliation first, whatever the configuration now says."""
    household_id = _household_with_profile(container)
    _verify_all_steps(container, household_id)
    workflow = container.database.query_one(
        "SELECT id FROM onboarding_workflows WHERE household_id = ?", (household_id,)
    )
    with container.database.write() as connection:
        container.jobs.create(
            connection,
            household_id=household_id,
            workflow_id=workflow["id"],
            kind="runtime",
            operation="ensure_runtime",
            intent_key=f"{household_id}:ensure_runtime:a-second-intent",
            request={},
            provider=container.config.runtime_provider,
            now=BASE_TIME + 1,
        )
    _requeue_under_another_provider(container, household_id)

    plan = plan_onboarding(container, household_id)

    assert len(plan.unresolved_runtime_jobs) > 1, "the fixture left only one intent"
    assert "earlier runtime intent" in (plan.blocked_by or ""), plan.blocked_by
    assert "provider mismatch" not in (plan.blocked_by or ""), (
        "a configuration diagnostic displaced an outstanding provider effect"
    )
    assert plan.table_writes == []


def _revoke_restriction(container, household_id: str) -> None:
    with container.database.write() as connection:
        connection.execute(
            "UPDATE consent_receipts SET revoked_at = ? WHERE household_id = ?"
            " AND purpose = 'special_category_content_restriction'",
            (BASE_TIME, household_id),
        )


def test_the_declared_content_restriction_writes_match_a_real_run(container) -> None:
    """Measured, and the provider registry proves it was never consulted.

    The gate runs before any provider is resolved, so this set is not a guess
    about a provider answer — it is the whole of what happens. Declaring it by
    hand would let it drift from `_block_for_missing_content_restriction`; this
    traces an actual worker call instead.
    """
    household_id = _household_with_profile(container)
    _verify_all_steps(container, household_id)
    _revoke_restriction(container, household_id)
    asked: list[str] = []
    real_get = container.providers.get

    def get(name):
        asked.append(name)
        return real_get(name)

    container.providers.get = get

    observed: set[TableWrite] = set()
    with _tracing_writes(observed):
        result = container.worker.run_once()

    assert result.error_code == "content_restriction_receipt_required"
    assert asked == [], f"a provider was resolved before the gate: {asked}"
    assert observed == set(CONTENT_RESTRICTION_BLOCK_WRITES["runtime"])


def test_an_email_sibling_is_gated_by_the_same_receipt(container) -> None:
    """The gate covers every non-`fake-email` email-identity job.

    The report asked the receipt question only about an already-planned runtime
    job, so an operator with a queued `nerve-managed` email job and a revoked
    receipt was told the write set was provider-dependent — when the worker's
    next act is to fail that job on the receipt and never call a provider at
    all.
    """
    household_id = _household_with_profile(container)
    workflow = container.database.query_one(
        "SELECT id FROM onboarding_workflows WHERE household_id = ?", (household_id,)
    )
    _revoke_restriction(container, household_id)
    with container.database.write() as connection:
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'cancelled', settled_at = ?"
            " WHERE household_id = ? AND status IN"
            " ('pending','running','waiting_user','outcome_unknown')",
            (BASE_TIME, household_id),
        )
        container.jobs.create(
            connection,
            household_id=household_id,
            workflow_id=workflow["id"],
            kind="email_identity",
            operation="provision_email_identity",
            intent_key=f"{household_id}:provision_email_identity:managed",
            request={},
            provider="nerve-managed",
            now=BASE_TIME,
        )

    plan = plan_onboarding(container, household_id)

    assert "content restriction" in (plan.blocked_by or ""), plan.blocked_by
    assert plan.table_writes == []
    gated = [job for job in plan.pending_step_jobs if job.get("blocked_by")]
    assert len(gated) == 1, plan.pending_step_jobs
    assert gated[0]["table_writes"] == [
        {"table": write.table, "operation": write.operation}
        for write in CONTENT_RESTRICTION_BLOCK_WRITES["step"]
    ]
    # `_mark_step_problem` revokes NOTHING. The shared sentence claimed the
    # revision and the bootstrap tokens were revoked, which is the runtime
    # branch — a false credential-lifecycle diagnosis for an email job.
    assert "no revision or token is revoked" in gated[0]["blocked_by"]
    assert "bootstrap tokens" not in (plan.blocked_by or "")
    assert plan.runtime_resources == []
    assert plan.secrets == []


def test_a_fake_email_job_is_not_gated(container) -> None:
    """`fake-email` is exempt in the worker, so it must be exempt here.

    Reporting a block the worker will not perform is the mirror of the defect
    above, and a restatement of the predicate would eventually make one of the
    two mistakes.
    """
    household_id = _household_with_profile(container)
    workflow = container.database.query_one(
        "SELECT id FROM onboarding_workflows WHERE household_id = ?", (household_id,)
    )
    _revoke_restriction(container, household_id)
    with container.database.write() as connection:
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'cancelled', settled_at = ?"
            " WHERE household_id = ? AND status IN"
            " ('pending','running','waiting_user','outcome_unknown')",
            (BASE_TIME, household_id),
        )
        container.jobs.create(
            connection,
            household_id=household_id,
            workflow_id=workflow["id"],
            kind="email_identity",
            operation="provision_email_identity",
            intent_key=f"{household_id}:provision_email_identity:fake",
            request={},
            provider="fake-email",
            now=BASE_TIME,
        )

    plan = plan_onboarding(container, household_id)

    assert "content restriction" not in (plan.blocked_by or ""), plan.blocked_by


def test_the_receipt_gate_outranks_a_provider_mismatch(container) -> None:
    """Durable state before configuration diagnostics.

    An operator with a revoked receipt AND a job queued under another provider
    was told to restore the configuration. Restoring it changes nothing: the
    worker still fails that job on the receipt before it resolves any provider.
    """
    household_id = _household_with_profile(container)
    _verify_all_steps(container, household_id)
    _revoke_restriction(container, household_id)
    _requeue_under_another_provider(container, household_id)

    plan = plan_onboarding(container, household_id)

    assert "content restriction" in (plan.blocked_by or ""), plan.blocked_by
    assert "provider mismatch" not in (plan.blocked_by or "")
    assert plan.table_writes == []
    assert plan.unresolved_runtime_jobs[0]["table_writes"] == [
        {"table": write.table, "operation": write.operation}
        for write in CONTENT_RESTRICTION_BLOCK_WRITES["runtime"]
    ]


@pytest.mark.parametrize("email_provider", ["nerve-managed", "nerve-byo-domain"])
def test_an_older_ungated_job_is_not_displaced_by_a_gated_one(
    container, email_provider
) -> None:
    """`lease` takes the OLDEST job, and this report does not rank them.

    `save_profile` queues `ensure_secret_namespace` before the email job.
    Revoke the receipt and both sit unsettled — but the namespace job is older,
    is not gated, and runs normally with a real provider call. An aggregate
    "some gated job exists" test then assigned the email block's write set as
    the next deterministic action, which is the ordering prediction the
    narrowing removed, returning in another form.
    """
    household_id = _household_with_profile(container)
    workflow = container.database.query_one(
        "SELECT id FROM onboarding_workflows WHERE household_id = ?", (household_id,)
    )
    _revoke_restriction(container, household_id)
    with container.database.write() as connection:
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'cancelled', settled_at = ?"
            " WHERE household_id = ? AND status IN"
            " ('pending','running','waiting_user','outcome_unknown')",
            (BASE_TIME, household_id),
        )
        namespace_id, _created = container.jobs.create(
            connection,
            household_id=household_id,
            workflow_id=workflow["id"],
            kind="runtime",
            operation="ensure_secret_namespace",
            intent_key=f"{household_id}:ensure_secret_namespace:first",
            request={},
            provider=container.config.runtime_provider,
            now=BASE_TIME,
        )
        email_id, _also = container.jobs.create(
            connection,
            household_id=household_id,
            workflow_id=workflow["id"],
            kind="email_identity",
            operation="provision_email_identity",
            intent_key=f"{household_id}:provision_email_identity:{email_provider}",
            request={},
            provider=email_provider,
            now=BASE_TIME + 1,
        )

    plan = plan_onboarding(container, household_id)

    by_id = {job["job_id"]: job for job in plan.pending_step_jobs}
    assert {namespace_id, email_id} <= set(by_id), by_id
    # The gated one carries the block. The older, ungated one does not, and
    # nothing at the top level claims either is next.
    assert by_id[email_id]["blocked_by"], by_id[email_id]
    assert not by_id[namespace_id].get("blocked_by"), by_id[namespace_id]
    assert plan.table_writes == []


def test_the_declared_step_block_writes_match_a_real_run(container) -> None:
    """Measured for the email shape too, and no provider is reached.

    The runtime set was traced and the step set was written down beside it. A
    declared set nobody measured is the drift this file exists to prevent.
    """
    household_id = _household_with_profile(container)
    workflow = container.database.query_one(
        "SELECT id FROM onboarding_workflows WHERE household_id = ?", (household_id,)
    )
    # Run the real steps first, so there IS an identity row and a step row for
    # the block to mark. Without them `_mark_step_problem` skips both branches
    # and the trace measures a third of the real write set — declaring a set
    # the code does not produce.
    _verify_all_steps(container, household_id)
    identity = container.database.query_one(
        "SELECT id FROM email_identities WHERE household_id = ?", (household_id,)
    )
    assert identity is not None, "the fixture produced no email identity"
    _revoke_restriction(container, household_id)
    with container.database.write() as connection:
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'cancelled', settled_at = ?"
            " WHERE household_id = ? AND status IN"
            " ('pending','running','waiting_user','outcome_unknown')",
            (BASE_TIME, household_id),
        )
        container.jobs.create(
            connection,
            household_id=household_id,
            workflow_id=workflow["id"],
            kind="email_identity",
            operation="provision_email_identity",
            intent_key=f"{household_id}:provision_email_identity:measured",
            request={
                # The STEP KIND as the enum spells it. "email" is not one of
                # them, and `_mark_step_problem` skips the step update for an
                # unrecognised kind — so the trace measured a smaller set than
                # the code writes and the declaration would have been wrong in
                # the safe-looking direction.
                "step_kind": StepKind.EMAIL.value,
                "email_identity_id": identity["id"],
            },
            provider="nerve-managed",
            now=BASE_TIME,
        )
        connection.execute(
            "UPDATE onboarding_steps SET status = 'provisioning'"
            " WHERE workflow_id = ? AND kind = ?",
            (workflow["id"], StepKind.EMAIL.value),
        )
    asked: list[str] = []
    real_get = container.providers.get
    container.providers.get = lambda name: (asked.append(name), real_get(name))[1]

    observed: set[TableWrite] = set()
    with _tracing_writes(observed):
        result = container.worker.run_once()

    assert result.error_code == "content_restriction_receipt_required"
    assert asked == [], f"a provider was resolved before the gate: {asked}"
    assert observed == set(CONTENT_RESTRICTION_BLOCK_WRITES["step"])

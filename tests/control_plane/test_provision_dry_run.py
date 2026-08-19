from __future__ import annotations

import hashlib
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from control_plane.config import ControlPlaneConfig
from control_plane.container import ControlPlaneContainer
from control_plane.db import ControlPlaneDatabase
from control_plane.models import ProfileInput, StepKind
from control_plane.onboarding.contracts import CommandContext
from control_plane.onboarding.provision import (
    RUNTIME_BOOTSTRAP_SECRET,
    RUNTIME_DSAR_SECRET,
    RUNTIME_WRITES_BY_WORKFLOW_STATE,
    TableWrite,
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
    # The tables reported are the pending RUNTIME operation's, not a planner
    # trace. Once a revision is issued the runtime job provisions THAT revision:
    # calling the planner anyway minted a revision N+1 and reported its INSERT
    # into `config_revisions` as the onboarding's "exact writes" — a write the
    # real onboarding never makes, from an operation this very run discarded.
    # The real operation UPDATES the issued revision.
    assert TableWrite("config_revisions", "update") in plan.table_writes
    assert TableWrite("config_revisions", "insert") not in plan.table_writes
    assert set(plan.table_writes) == set(
        RUNTIME_WRITES_BY_WORKFLOW_STATE["runtime_provisioning"]
    )
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
    assert plan.blocked_by is not None
    assert plan.unverified_steps
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
    assert set(plan.table_writes) == set(
        RUNTIME_WRITES_BY_WORKFLOW_STATE["runtime_provisioning"]
    )


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
    assert set(plan.table_writes) == set(
        RUNTIME_WRITES_BY_WORKFLOW_STATE["runtime_provisioning"]
    )


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
    seeded = ControlPlaneDatabase(config.database_path)
    seeded.migrate()
    with seeded.write() as connection:
        connection.execute("CREATE TABLE canary (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO canary VALUES ('uncheckpointed')")
    # Leave committed frames in the WAL, as a stopped or crashed writer does.
    assert config.database_path.with_name(
        f"{config.database_path.name}-wal"
    ).exists()

    def fingerprint() -> dict[str, bytes]:
        return {
            path.name: hashlib.sha256(path.read_bytes()).digest()
            for path in sorted(tmp_path.iterdir())
            if path.is_file()
        }

    before = fingerprint()
    monkeypatch.setattr(ControlPlaneConfig, "from_env", staticmethod(lambda: config))

    with pytest.raises(SystemExit):
        main(["--dry-run", "--household", "10000000-0000-4000-8000-000000000031"])

    assert fingerprint() == before

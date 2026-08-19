"""Operator rehearsal for a pilot onboarding: list durable writes, mutate nothing.

The operator runs this before every real pilot onboarding. It reports the
config revision and runtime job the onboarding will actually provision — both
read from durable state, since the worker issues them when the primary step
verifies — and then rehearses one planning pass to enumerate the tables such a
transaction touches, rolling that rehearsal back. No provider is called and no
row is committed.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from control_plane.config import ControlPlaneConfig
from control_plane.container import ControlPlaneContainer
from control_plane.db import MIGRATIONS_DIR
from control_plane.models import USER_STEPS
from control_plane.provisioning.fly import FlyRuntimeProvisioner

# Both are installed into the runtime sink by ProvisioningWorker._finish_runtime;
# the bootstrap token is then deleted by the bootstrap cleanup job.
RUNTIME_BOOTSTRAP_SECRET = "HERMES_BOOTSTRAP_TOKEN"
RUNTIME_DSAR_SECRET = "HERMES_RUNTIME_DSAR_TOKEN"

_WRITE_STATEMENT = re.compile(
    r"^\s*(?P<operation>INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM)"
    r"\s+(?P<table>[a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)


def pending_migrations(database: Any) -> list[str]:
    """Migration files this database has not applied, without applying any.

    Deliberately local and read-only. `ControlPlaneDatabase.migrate()` is the
    only other implementation and it WRITES, which is the thing this command
    must not do. (Phase E9 adds an equivalent method on the database; collapse
    onto it once that lands — the duplication is here so this branch stays
    independent of that one.)
    """
    try:
        applied = {
            row["name"]
            for row in database.query("SELECT name FROM schema_migrations")
        }
    except sqlite3.DatabaseError:
        # No `schema_migrations` at all: nothing has ever been applied.
        applied = set()
    return [
        script.name
        for script in sorted(MIGRATIONS_DIR.glob("*.sql"))
        if script.name not in applied
    ]


class _DryRunRollback(Exception):
    """Sentinel that makes ControlPlaneDatabase.write roll the rehearsal back."""


@dataclass(frozen=True)
class TableWrite:
    table: str
    operation: str


#: Tables the pending runtime operation updates, BY THE WORKFLOW STATE it runs
#: from. One set for the whole operation was wrong: `_finish_runtime` branches
#: on that state, and the two branches touch very different tables.
#:
#: From `runtime_provisioning` it is a first attempt — it issues the bootstrap
#: token, inserts the external resource, stamps the runtime ref on the
#: household, moves the workflow to `activating` and records the transition.
#:
#: From `activating` it is a RECONCILE after a partial activation, such as an
#: `outcome_unknown` on `secret_install_unknown`. The workflow has already
#: moved, the resource already exists, and the operation only updates them. An
#: operator reading the first set in this state would be told to expect writes
#: that are not going to happen.
#:
#: Note `config_revisions` is an UPDATE in the first set and absent from the
#: second — never an INSERT. That is the difference from the planning pass, and
#: reporting a planner trace here claimed an insert the real onboarding never
#: performs.
#:
#: Both sets are measured, not read off the worker, and
#: `test_the_declared_runtime_write_set_matches_a_real_run` re-measures each by
#: tracing an actual run in that state. They cannot drift.
RUNTIME_WRITES_BY_WORKFLOW_STATE: dict[str, tuple[TableWrite, ...]] = {
    "runtime_provisioning": (
        TableWrite("bootstrap_tokens", "insert"),
        TableWrite("bootstrap_tokens", "update"),
        TableWrite("config_revisions", "update"),
        TableWrite("external_resources", "insert"),
        TableWrite("external_resources", "update"),
        TableWrite("households", "update"),
        TableWrite("onboarding_transitions", "insert"),
        TableWrite("onboarding_workflows", "update"),
        TableWrite("provisioning_jobs", "update"),
    ),
    "activating": (
        TableWrite("external_resources", "update"),
        TableWrite("provisioning_jobs", "update"),
    ),
}


@dataclass
class ProvisionPlan:
    household_id: str
    workflow_state: str
    workflow_version: int
    steps: list[dict[str, Any]] = field(default_factory=list)
    unverified_steps: list[str] = field(default_factory=list)
    table_writes: list[TableWrite] = field(default_factory=list)
    config_revision: dict[str, Any] = field(default_factory=dict)
    pending_runtime_job: dict[str, Any] = field(default_factory=dict)
    runtime_resources: list[dict[str, str]] = field(default_factory=list)
    secrets: list[dict[str, str]] = field(default_factory=list)
    blocked_by: str | None = None
    #: What, if anything, this run rehearsed — so `table_writes` is never
    #: read as the write set of an operation that was not rehearsed.
    rehearsal: str = ""
    uncommitted_revision_delta: int = 0
    committed: bool = False

    def public_dict(self) -> dict[str, Any]:
        return {
            "mode": "dry-run",
            "committed": self.committed,
            **asdict(self),
        }


def _record_writes(sql: str, observed: list[TableWrite]) -> None:
    """Keep only the verb and table name — statement text can carry values."""
    match = _WRITE_STATEMENT.match(sql)
    if match is None:
        return
    operation = match.group("operation").split()[0].lower()
    write = TableWrite(match.group("table").lower(), operation)
    if write not in observed:
        observed.append(write)


def _runtime_resources(
    container: ControlPlaneContainer, household_id: str, spec: Any | None
) -> list[dict[str, str]]:
    provider_name = container.config.runtime_provider
    if spec is not None:
        provider = container.providers.get(provider_name)
        planner = getattr(provider, "plan", None)
        if callable(planner):
            return [
                {
                    "provider": provider_name,
                    "kind": planned.kind,
                    "stable_name": planned.stable_name,
                    "summary": planned.summary,
                }
                for planned in planner(spec)
            ]
    return [
        {
            "provider": provider_name,
            "kind": "app",
            "stable_name": FlyRuntimeProvisioner.stable_app_name(household_id),
            "summary": "ensure synthetic Fly app",
        },
        {
            "provider": provider_name,
            "kind": "volume",
            "stable_name": FlyRuntimeProvisioner.stable_volume_name(),
            "summary": "ensure encrypted ams volume",
        },
        {
            "provider": provider_name,
            "kind": "machine",
            "stable_name": FlyRuntimeProvisioner.stable_machine_name(),
            "summary": "ensure pinned runtime Machine",
        },
    ]


def _secret_names(
    container: ControlPlaneContainer, household_id: str
) -> list[dict[str, str]]:
    """Secret *names* only. Values never leave the sink, and never appear here."""
    app = FlyRuntimeProvisioner.stable_app_name(household_id)
    secrets = [
        {
            "name": RUNTIME_BOOTSTRAP_SECRET,
            "target": app,
            "lifecycle": "installed for activation, deleted by bootstrap cleanup",
        },
        {
            "name": RUNTIME_DSAR_SECRET,
            "target": app,
            "lifecycle": "installed with the bootstrap token, kept for runtime DSAR",
        },
    ]
    installed = container.database.query(
        "SELECT secret_name, namespace_ref FROM email_secret_installs"
        " WHERE household_id = ? ORDER BY secret_name",
        (household_id,),
    )
    secrets.extend(
        {
            "name": row["secret_name"],
            "target": row["namespace_ref"],
            "lifecycle": "already installed, receipt recorded",
        }
        for row in installed
    )
    return secrets


def _flatten(document: Any, prefix: str = "") -> dict[str, Any]:
    """Leaf paths of a manifest, so a diff can name what moved."""
    if isinstance(document, dict):
        flat: dict[str, Any] = {}
        for key, value in document.items():
            flat.update(_flatten(value, f"{prefix}.{key}" if prefix else str(key)))
        return flat
    if isinstance(document, (list, tuple)):
        flat = {}
        for index, value in enumerate(document):
            flat.update(_flatten(value, f"{prefix}[{index}]"))
        return flat
    return {prefix: document}


def _revision_diff(
    container: ControlPlaneContainer, household_id: str, revision: int
) -> dict[str, Any]:
    """How this household's configuration changes relative to the prior revision.

    KEY PATHS ONLY, never values. The manifest carries provider bindings and
    inbox addresses, and this command prints to an operator's terminal and
    scrollback; "what moved" is what the rehearsal is for, and "to what" is
    available to someone who needs it through the manifest itself.

    A first revision has no predecessor, which is reported as such rather than
    as a diff against nothing.
    """
    try:
        current = _flatten(container.configs.manifest(household_id, revision))
    except KeyError:
        return {"unavailable": "manifest for the issued revision is not readable"}
    if revision <= 1:
        return {
            "previous_revision": None,
            "added": sorted(current),
            "removed": [],
            "changed": [],
        }
    try:
        previous = _flatten(container.configs.manifest(household_id, revision - 1))
    except KeyError:
        return {
            "previous_revision": revision - 1,
            "unavailable": "prior manifest is no longer readable",
        }
    return {
        "previous_revision": revision - 1,
        "added": sorted(set(current) - set(previous)),
        "removed": sorted(set(previous) - set(current)),
        "changed": sorted(
            key
            for key in set(current) & set(previous)
            if current[key] != previous[key]
        ),
    }


def plan_onboarding(
    container: ControlPlaneContainer, household_id: str
) -> ProvisionPlan:
    """Report what a real onboarding will do, from ONE consistent snapshot.

    Every read below happens inside a single write transaction, and the
    transaction is always rolled back. Reading piecemeal produced impossible
    reports: the API worker could commit the revision and the runtime job
    between two of these queries, so a run could print an empty or stale
    `config_revision` beside the new job that provisions it — a plan describing
    a state that never existed.
    """
    observed: list[TableWrite] = []
    spec = None
    plan: ProvisionPlan | None = None
    revisions_before = 0

    try:
        with container.database.write() as connection:
            workflow = connection.execute(
                "SELECT id, state, version FROM onboarding_workflows"
                " WHERE household_id = ?",
                (household_id,),
            ).fetchone()
            if workflow is None:
                raise KeyError(household_id)
            step_rows = connection.execute(
                "SELECT kind, status, ordinal FROM onboarding_steps"
                " WHERE workflow_id = ? ORDER BY ordinal",
                (workflow["id"],),
            ).fetchall()
            plan = ProvisionPlan(
                household_id=household_id,
                workflow_state=workflow["state"],
                workflow_version=workflow["version"],
                steps=[
                    {
                        "kind": row["kind"],
                        "status": row["status"],
                        "ordinal": row["ordinal"],
                    }
                    for row in step_rows
                ],
                unverified_steps=[
                    row["kind"]
                    for row in step_rows
                    if row["kind"] in {step.value for step in USER_STEPS}
                    and row["status"] != "verified"
                ],
            )

            # The revision the real onboarding provisions is issued by the
            # worker when the primary step verifies, so it is read from durable
            # state, never minted here.
            # Status matters, not merely existence. `reset_from` RETAINS the
            # revision and runtime-job rows and marks them `revoked` and
            # `cancelled`; reading the highest revision regardless of status
            # made a reset workflow look already-planned, reported a cancelled
            # job as pending, and advertised the runtime write set while the
            # steps it depends on were unverified.
            issued = connection.execute(
                "SELECT revision, status, manifest_sha256 FROM config_revisions"
                " WHERE household_id = ?"
                " AND status IN ('planned','issued','claimed','active')"
                " ORDER BY revision DESC LIMIT 1",
                (household_id,),
            ).fetchone()
            if issued is not None:
                plan.config_revision = {
                    "revision": issued["revision"],
                    "status": issued["status"],
                    "manifest_sha256": issued["manifest_sha256"],
                    "source": "issued by the worker when the primary step verified",
                    "diff": _revision_diff(
                        container, household_id, issued["revision"]
                    ),
                }
            runtime_job = connection.execute(
                "SELECT id, status, desired_revision FROM provisioning_jobs"
                " WHERE household_id = ? AND workflow_id = ? AND kind = 'runtime'"
                " AND operation = 'ensure_runtime'"
                " AND status NOT IN ('cancelled','failed')"
                " ORDER BY created_at DESC LIMIT 1",
                (household_id, workflow["id"]),
            ).fetchone()
            if runtime_job is not None:
                plan.pending_runtime_job = {
                    "job_id": runtime_job["id"],
                    "status": runtime_job["status"],
                    "desired_revision": runtime_job["desired_revision"],
                }

            revisions_before = connection.execute(
                "SELECT COUNT(*) AS count FROM config_revisions WHERE household_id = ?",
                (household_id,),
            ).fetchone()["count"]

            # Only rehearse a planning pass that has not already happened.
            #
            # Once a revision is issued, the pending runtime job provisions THAT
            # revision and performs no planning write at all. Calling the planner
            # anyway mints a hypothetical N+1 and traces its insert into
            # `config_revisions` — a write the real onboarding will never make.
            # Reporting that as the "exact writes" described an operation this
            # run had just discarded.
            # Both must be current AND agree with each other. A revision with
            # no live job for this workflow is not a plan the operator can rely
            # on, so the rehearsal below runs instead and reports whatever still
            # blocks it.
            already_planned = (
                issued is not None
                and runtime_job is not None
                and runtime_job["desired_revision"] == issued["revision"]
            )
            if already_planned:
                state = str(workflow["state"])
                writes = RUNTIME_WRITES_BY_WORKFLOW_STATE.get(state)
                if writes is None:
                    # A state the runtime operation does not run from. Say so
                    # rather than reporting another state's tables.
                    plan.rehearsal = (
                        f"revision {issued['revision']} is already issued, and the"
                        f" workflow is `{state}`, which the runtime operation does"
                        " not run from; no write set applies"
                    )
                else:
                    plan.rehearsal = (
                        f"revision {issued['revision']} is already issued, so the"
                        " planning pass is done; table_writes is the write set of"
                        f" the pending runtime operation running from `{state}`,"
                        " which UPDATES that revision rather than inserting another"
                    )
                    observed.extend(writes)
            else:
                plan.rehearsal = (
                    "planning pass for the next revision, rolled back;"
                    " table_writes is the set of tables it touched"
                )
                connection.set_trace_callback(
                    lambda sql: _record_writes(sql, observed)
                )
                try:
                    spec = container.planner.issue(
                        connection, household_id=household_id
                    ).spec
                except ValueError as error:
                    plan.blocked_by = str(error)
                finally:
                    connection.set_trace_callback(None)

            revisions_after = connection.execute(
                "SELECT COUNT(*) AS count FROM config_revisions WHERE household_id = ?",
                (household_id,),
            ).fetchone()["count"]
            plan.uncommitted_revision_delta = revisions_after - revisions_before

            plan.table_writes = observed
            plan.runtime_resources = _runtime_resources(container, household_id, spec)
            plan.secrets = _secret_names(container, household_id)
            raise _DryRunRollback
    except _DryRunRollback:
        pass

    assert plan is not None
    # After the rollback, not before: the count inside the transaction sees the
    # rehearsal's own uncommitted insert, which proves nothing about what was
    # committed. This reads the database as everyone else now sees it.
    plan.committed = (
        container.database.query_one(
            "SELECT COUNT(*) AS count FROM config_revisions WHERE household_id = ?",
            (household_id,),
        )["count"]
        != revisions_before
    )
    return plan


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="abrolia-provision",
        description="rehearse a pilot onboarding without writing anything",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--household", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.dry_run:
        raise SystemExit("provision refuses to run without --dry-run")
    # No process lock: the rehearsal only ever rolls back, so it must not block
    # the single writer that is serving the pilot.
    #
    # And NO MIGRATIONS. `ControlPlaneContainer.build` applies them by default,
    # committing schema changes and `schema_migrations` rows before this
    # command's own transaction opens — outside its rollback, and outside the
    # check that reports `committed`. A command whose entire promise is "mutates
    # nothing" was altering the schema of the database it was asked to rehearse
    # against.
    with ControlPlaneContainer.build(
        ControlPlaneConfig.from_env(),
        acquire_process_lock=False,
        apply_migrations=False,
        # `PRAGMA journal_mode=WAL` is persistent: on a database in another
        # mode — an imported or restored file — opening the connection rewrites
        # the header and creates sidecars before any transaction exists, so the
        # rollback cannot undo it. The rehearsal works in any journal mode, so
        # it simply leaves the mode alone.
        preserve_journal_mode=True,
    ) as active:
        pending = pending_migrations(active.database)
        if pending:
            # Refuse rather than rehearse: against a stale schema the plan would
            # describe an onboarding that cannot happen. Migrating is a separate,
            # deliberate operator action.
            raise SystemExit(
                "dry-run refuses a database with pending migrations: "
                + ", ".join(pending)
            )
        try:
            plan = plan_onboarding(active, args.household)
        except KeyError:
            raise SystemExit("household has no onboarding workflow") from None
        if plan.committed:
            raise SystemExit("dry-run observed a committed write and refuses to report")
    print(json.dumps(plan.public_dict(), sort_keys=True, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

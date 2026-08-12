"""Operator rehearsal for a pilot onboarding: list durable writes, mutate nothing.

The operator runs this before every real pilot onboarding. It opens one write
transaction, replays the desired-spec planner against durable state, records the
tables that transaction would touch, and then rolls back. No provider is called
and no row is committed.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from control_plane.config import ControlPlaneConfig
from control_plane.container import ControlPlaneContainer
from control_plane.models import USER_STEPS
from control_plane.provisioning.fly import FlyRuntimeProvisioner

# Installed for the activation handshake and deleted by the bootstrap cleanup
# job; see ProvisioningWorker._finish_runtime_activation.
RUNTIME_BOOTSTRAP_SECRET = "HERMES_BOOTSTRAP_TOKEN"

_WRITE_STATEMENT = re.compile(
    r"^\s*(?P<operation>INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM)"
    r"\s+(?P<table>[a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)


class _DryRunRollback(Exception):
    """Sentinel that makes ControlPlaneDatabase.write roll the rehearsal back."""


@dataclass(frozen=True)
class TableWrite:
    table: str
    operation: str


@dataclass
class ProvisionPlan:
    household_id: str
    workflow_state: str
    workflow_version: int
    steps: list[dict[str, Any]] = field(default_factory=list)
    unverified_steps: list[str] = field(default_factory=list)
    table_writes: list[TableWrite] = field(default_factory=list)
    config_revision: dict[str, Any] = field(default_factory=dict)
    runtime_resources: list[dict[str, str]] = field(default_factory=list)
    secrets: list[dict[str, str]] = field(default_factory=list)
    blocked_by: str | None = None
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
    secrets = [
        {
            "name": RUNTIME_BOOTSTRAP_SECRET,
            "target": FlyRuntimeProvisioner.stable_app_name(household_id),
            "lifecycle": "installed for activation, deleted by bootstrap cleanup",
        }
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


def plan_onboarding(
    container: ControlPlaneContainer, household_id: str
) -> ProvisionPlan:
    workflow = container.onboarding_repository.workflow_for_household(household_id)
    step_rows = container.database.query(
        "SELECT kind, status, ordinal FROM onboarding_steps WHERE workflow_id = ?"
        " ORDER BY ordinal",
        (workflow.id,),
    )
    plan = ProvisionPlan(
        household_id=household_id,
        workflow_state=workflow.state,
        workflow_version=workflow.version,
        steps=[
            {"kind": row["kind"], "status": row["status"], "ordinal": row["ordinal"]}
            for row in step_rows
        ],
        unverified_steps=[
            row["kind"]
            for row in step_rows
            if row["kind"] in {step.value for step in USER_STEPS}
            and row["status"] != "verified"
        ],
    )

    revisions_before = container.database.query_one(
        "SELECT COUNT(*) AS count FROM config_revisions WHERE household_id = ?",
        (household_id,),
    )["count"]

    observed: list[TableWrite] = []
    spec = None
    try:
        with container.database.write() as connection:
            connection.set_trace_callback(lambda sql: _record_writes(sql, observed))
            try:
                planned = container.planner.issue(
                    connection, household_id=household_id
                )
                spec = planned.spec
                plan.config_revision = {
                    "current": planned.revision.revision - 1,
                    "next": planned.revision.revision,
                    "manifest_sha256": planned.revision.manifest_sha256,
                }
            except ValueError as error:
                plan.blocked_by = str(error)
            finally:
                connection.set_trace_callback(None)
            raise _DryRunRollback
    except _DryRunRollback:
        pass

    revisions_after = container.database.query_one(
        "SELECT COUNT(*) AS count FROM config_revisions WHERE household_id = ?",
        (household_id,),
    )["count"]
    plan.committed = revisions_after != revisions_before

    plan.table_writes = observed
    plan.runtime_resources = _runtime_resources(container, household_id, spec)
    plan.secrets = _secret_names(container, household_id)
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
    with ControlPlaneContainer.build(
        ControlPlaneConfig.from_env(), acquire_process_lock=False
    ) as active:
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

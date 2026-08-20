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
import os
import re
import sqlite3
import tempfile
import time
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from control_plane.config import ControlPlaneConfig
from control_plane.container import ControlPlaneContainer
from control_plane.db import MIGRATIONS_DIR, ControlPlaneDatabase
from control_plane.models import USER_STEPS, WorkflowState
from control_plane.privacy.consent import (
    CURRENT_RESTRICTION_RECEIPT_SQL,
    consent_version_and_sha,
)
from control_plane.provisioning.contracts import ProviderRejected
from control_plane.provisioning.fly import FlyRuntimeProvisioner

#: The purpose whose receipt `_run_once` requires before dispatching a
#: runtime job. Spelled once here, matching the worker's own literal.
CONTENT_RESTRICTION_PURPOSE = "special_category_content_restriction"

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
#: The `activating` entry is measured and validated but deliberately NOT
#: reported: reaching it means the job is `outcome_unknown`, and the reconcile
#: that follows branches on `provider.inspect()` — settle, fail, or re-run
#: preparation — so this set is only one of the possible outcomes. The rehearsal
#: reports that uncertainty instead. The entry stays because the validation test
#: is what proves the two states differ at all, which is the reason the
#: uncertainty has to be reported rather than guessed away.
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

#: What a job of this kind writes, where durable state determines it.
#:
#: Attached to EACH inventoried job rather than chosen for the top-level
#: `table_writes`. The choosing is what generated findings — it meant deciding
#: which job runs next, and defending that against `lease` in every edge case.
#: What a given kind writes is a static fact about the code, and a fact can sit
#: beside the job it describes without anything being ranked.
#:
#: Only the internal kinds are here. A provider-backed job's write set depends
#: on an answer the provider has not given, and this report states uncertainty
#: rather than guessing at it.
STEP_WRITES_BY_KIND: dict[str, tuple[TableWrite, ...]] = {
    "bootstrap_cleanup": (TableWrite("provisioning_jobs", "update"),),
}

#: Tables the COMPENSATION path writes, which any in-flight call can reach.
#:
#: If the API cancels or resets while the worker is inside `provider.prepare`,
#: `_run_once` re-reads the job after the provider returns and routes even an
#: ordinary first attempt through `_cleanup_cancelled_result` — which records
#: the resource as deleting, and on an inconclusive deprovision or a failed
#: bootstrap-secret deletion inserts a cleanup job. Those writes fell outside
#: the advertised set, so a set described as the success-path UPPER BOUND was
#: not one. It is a bound over what the pending call can write, and a
#: cancellation landing mid-call is one of the things it can do.
COMPENSATION_WRITES: tuple[TableWrite, ...] = (
    TableWrite("external_resources", "insert"),
    TableWrite("external_resources", "update"),
    TableWrite("provisioning_jobs", "insert"),
    TableWrite("provisioning_jobs", "update"),
)


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
    #: Whether the deployment's workers are paused. `lease` returns nothing
    #: at all while this is set, which an operator reading a queue of
    #: pending jobs needs to know.
    workers_paused: bool = False
    #: Unsettled jobs that run BEFORE `ensure_runtime` — the namespace job, and
    #: each step's own provider job — reported as the durable FACTS they are:
    #: status, error code, `not_before`, `lease_until`, provider.
    #:
    #: This used to carry an `executable` verdict per job, and a pair of derived
    #: booleans summarised the household's state above. Every one of them was a
    #: claim about what the worker would do next, and each had to agree with
    #: `JobsRepository.lease` in every edge case — future `not_before`, held
    #: lease, paused workers, a provider the deployment no longer registers, a
    #: kind dispatched without one. Eleven review rounds went into reconciling
    #: those claims, and the count rose rather than fell.
    #:
    #: So the command reports what is durably true and stops predicting. An
    #: operator reading `status`, `not_before` and `lease_until` can see what
    #: the worker will take; a field asserting it can be wrong, and was.
    pending_step_jobs: list[dict[str, Any]] = field(default_factory=list)
    #: Every unsettled runtime intent, not only the newest. A reset preserves a
    #: running job as `outcome_unknown` needing reconciliation, and the owner
    #: can then complete the steps again and mint a NEWER job — so a single-row
    #: view showed the new one and hid a provider effect still outstanding.
    unresolved_runtime_jobs: list[dict[str, Any]] = field(default_factory=list)
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
    container: ControlPlaneContainer,
    household_id: str,
    spec: Any | None,
    provider_name: str | None = None,
) -> list[dict[str, str]]:
    """What the operation about to run will create, per ITS provider.

    `provider_name` is the pending job's durable provider, not the current
    configuration. They can differ, and the worker follows the job: queue
    `ensure_runtime` under `dry-run-runtime`, restart with
    `ABROLIA_RUNTIME_PROVIDER=fly-runtime`, and `_dispatch` still routes through
    `job.provider` to the synthetic provider while this reported Fly app, volume
    and Machine names — and targeted both runtime secrets at a Fly app that will
    never exist. An operator preparing from that prepares the wrong resources
    for the exact operation queued.

    The configured provider is correct only for a planning pass that has not
    happened yet, because there is no job to disagree with.
    """
    provider_name = provider_name or container.config.runtime_provider
    try:
        provider = container.providers.get(provider_name)
    except ProviderRejected:
        # `ProviderRegistry.get` RAISES for an unregistered name rather than
        # returning None, and this command must never propagate that: the
        # rehearsal's job is to describe the situation, including the one where
        # a durable job names a provider this deployment has disabled.
        provider = None
    if provider is None:
        # A durable job naming a provider this deployment does not register: the
        # operation cannot run as recorded, and guessing from configuration is
        # how the wrong resources got reported in the first place.
        return [
            {
                "provider": provider_name,
                "kind": "unavailable",
                "stable_name": "",
                "summary": (
                    f"the pending operation is recorded against `{provider_name}`,"
                    " which this deployment does not register; nothing can be"
                    " asserted about what it would create"
                ),
            }
        ]
    planner = getattr(provider, "plan", None)
    if spec is not None and callable(planner):
        return [
            {
                "provider": provider_name,
                "kind": planned.kind,
                "stable_name": planned.stable_name,
                "summary": planned.summary,
            }
            for planned in planner(spec)
        ]
    if not isinstance(provider, FlyRuntimeProvisioner):
        # The fallback below describes FLY resources. Printing them under
        # another provider's name invents an app, a volume and a Machine that
        # will never exist — and `dry-run-runtime`, the allowed default, creates
        # exactly one synthetic reference and no Fly resources at all. Say what
        # is not known rather than describe the wrong provider.
        return [
            {
                "provider": provider_name,
                "kind": "unknown",
                "stable_name": "",
                "summary": (
                    f"{provider_name} does not describe its resources without a"
                    " planned revision; nothing is asserted about what it creates"
                ),
            }
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


def _runtime_secret_target(
    container: ControlPlaneContainer, household_id: str, resources: list[dict[str, str]]
) -> str:
    """Where the configured provider will install the runtime secrets.

    `_finish_runtime` installs both tokens against the runtime reference the
    provider returns, so the answer belongs to the provider — not to Fly. With
    `dry-run-runtime`, the allowed default, that reference is
    `synthetic-runtime:<household-id>` and no Fly app exists at all; naming one
    made the rehearsal contradict its own resource section, which now reports
    the synthetic reference correctly.
    """
    for resource in resources:
        if resource["kind"] in {"runtime_reference", "app"} and resource["stable_name"]:
            return resource["stable_name"]
    return ""


def _secret_names(
    container: ControlPlaneContainer,
    household_id: str,
    resources: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Secret *names* only. Values never leave the sink, and never appear here."""
    app = _runtime_secret_target(container, household_id, resources)
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
    # Live bindings only, correlated to the identity that holds them.
    #
    # The receipt is an audit record and is RETAINED when the binding is
    # deleted: `_delete_email_cleanup_secret` removes the secret from the sink
    # and deliberately leaves the row. An unfiltered query therefore reported a
    # deleted credential as "already installed".
    #
    # The first fix correlated by HOUSEHOLD — "does this household still have an
    # identity in a credential-holding state" — which is not the same question.
    # After a real provider job installs a secret, `reset_from(email)` deletes
    # that identity and its binding but leaves the succeeded job and its receipt
    # alone; once a replacement identity reaches `verified`, the old job still
    # passes `status NOT IN ('cancelled','failed')` and the UNRELATED new
    # identity satisfies the `EXISTS`. Both names came back as installed. The
    # test missed it because it made the old job `cancelled` by hand, which
    # reset never does.
    #
    # So ask the question that was meant: does the identity this receipt was
    # installed FOR still exist? The installing job's request carries
    # `email_identity_id`, and reading it is a decrypt of a handful of rows on a
    # report path — no schema change, and nothing written, which the no-mutation
    # guarantee requires. The receipt is never erased; shaping a report is not a
    # reason to destroy an audit trail.
    live_identities = {
        str(row["id"])
        for row in container.database.query(
            "SELECT id FROM email_identities WHERE household_id = ?"
            " AND status IN ('verified','activating','active','needs_attention')",
            (household_id,),
        )
    }
    candidates = container.database.query(
        "SELECT s.secret_name, s.namespace_ref, s.job_id FROM email_secret_installs s"
        " JOIN provisioning_jobs j ON j.id = s.job_id"
        " WHERE s.household_id = ?"
        " AND j.status NOT IN ('cancelled','failed')"
        " ORDER BY s.secret_name",
        (household_id,),
    )
    installed = []
    for row in candidates:
        try:
            request = container.jobs.request(row["job_id"])
        except Exception:
            # An unreadable request is not evidence of a live binding. Leaving
            # it out under-reports; leaving it in tells an operator a credential
            # is installed that may have been deleted, which is the failure this
            # filter exists to prevent.
            continue
        identity_id = request.get("email_identity_id")
        if isinstance(identity_id, str) and identity_id in live_identities:
            installed.append(row)
    secrets.extend(
        {
            "name": row["secret_name"],
            "target": row["namespace_ref"],
            "lifecycle": "already installed, receipt recorded",
        }
        for row in installed
    )
    return secrets


def _holds_current_restriction(
    container: ControlPlaneContainer, household_id: str
) -> bool:
    """The worker's own precondition, asked with the worker's own SQL."""
    version, sha256 = consent_version_and_sha(CONTENT_RESTRICTION_PURPOSE)
    return container.database.query_one(
        CURRENT_RESTRICTION_RECEIPT_SQL,
        (household_id, CONTENT_RESTRICTION_PURPOSE, version, sha256),
    ) is not None


def _now_seconds() -> float:
    """Wall clock, isolated so a test can pin an expired lease."""
    return time.time()


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


#: `OnboardingService._supersede_unsettled_jobs` writes
#: `<reason>_requires_reconciliation` for any job that had already reached its
#: provider when a cancel or reset arrived. That intent outlives the workflow
#: label: the job may have created upstream state, and only an inspection can
#: say. Matched by SUFFIX so a new reason needs no change here.
RECONCILE_SUFFIX = "_requires_reconciliation"


def _describe_jobs(jobs: list[dict[str, Any]]) -> str:
    """`<id> (<status>/<error_code>)`, joined — the operator's handle on them."""
    return ", ".join(
        f"{job['job_id']} ({job['status']}"
        + (f"/{job['error_code']}" if job.get("error_code") else "")
        + ")"
        for job in jobs
    )


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
    # Whether this run modelled a real operation. Internal: it decides whether
    # the future-tense fields are emitted, and is not itself reported — a
    # derived claim about the household's state is what the report stopped
    # making.
    models_an_operation = True
    plan: ProvisionPlan | None = None
    rehearsed_revision_ids: set[str] = set()

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
            # ALL of them, newest first. `LIMIT 1` answered "what runs next"
            # and silently dropped the rest: a reset preserves a running job as
            # `outcome_unknown` with `reset_requires_reconciliation`, the owner
            # can complete the steps again and mint a newer job, and the older
            # one still represents provider state nobody has reconciled. An
            # operator shown only the new job proceeds past it.
            # Whether the WORKERS are paused is a fact about the deployment and
            # is reported as one. `lease` returns nothing at all while it is
            # set, so an operator reading a queue of pending jobs needs to know
            # — but the report says "the workers are paused", not "this job will
            # not run", which is a prediction it kept getting wrong.
            plan.workers_paused = container.database.workers_paused
            unresolved = connection.execute(
                "SELECT id, status, desired_revision, lease_until, not_before,"
                " provider, error_code FROM provisioning_jobs"
                " WHERE household_id = ? AND workflow_id = ? AND kind = 'runtime'"
                " AND operation = 'ensure_runtime'"
                " AND status IN ('pending','running','waiting_user','outcome_unknown')"
                " ORDER BY created_at DESC, id DESC",
                (household_id, workflow["id"]),
            ).fetchall()
            plan.unresolved_runtime_jobs = [
                {
                    "job_id": row["id"],
                    "status": row["status"],
                    "desired_revision": row["desired_revision"],
                    "provider": row["provider"],
                    "not_before": row["not_before"],
                    "lease_until": row["lease_until"],
                    "error_code": row["error_code"],
                }
                for row in unresolved
            ]
            runtime_job = unresolved[0] if unresolved else None
            # The step and namespace jobs that run BEFORE `ensure_runtime`.
            # This query asked only about `ensure_runtime`, so after
            # `save_profile` queues `ensure_secret_namespace`, or while a
            # selected email/WhatsApp/channel job is still pending, it found
            # nothing — the planner then failed on unverified steps and the
            # report said `operation_pending=false` with an empty inventory,
            # while `JobsRepository.lease` could execute the omitted job
            # immediately. "No pending work" was said about a worker with work
            # in hand.
            plan.pending_step_jobs = [
                {
                    "job_id": row["id"],
                    "kind": row["kind"],
                    "operation": row["operation"],
                    "status": row["status"],
                    "provider": row["provider"],
                    "error_code": row["error_code"],
                    # Whether the worker can pick this up next, asked with
                    # `JobsRepository.lease`'s OWN predicate rather than a
                    # restatement of it. The restatement disagreed in three
                    # ways: a `pending` job with a future `not_before` is not
                    # leasable and was called executable, a due
                    # `waiting_user/inspect` DNS job IS leasable and was called
                    # blocked, and `outcome_unknown` is never selected at all.
                    # A report about what the worker will do has to ask the
                    # worker's question.
                    "not_before": row["not_before"],
                    "lease_until": row["lease_until"],
                    # Where the work is internal and deterministic, what it
                    # writes is knowable and is stated. Where it depends on a
                    # provider, the list is empty and the absence is the answer.
                    "table_writes": [
                        {"table": write.table, "operation": write.operation}
                        for write in STEP_WRITES_BY_KIND.get(str(row["kind"]), ())
                    ],
                }
                for row in connection.execute(
                    "SELECT id, kind, operation, status, provider, error_code,"
                    " not_before, lease_until FROM provisioning_jobs"
                    " WHERE household_id = ? AND workflow_id = ?"
                    " AND NOT (kind = 'runtime' AND operation = 'ensure_runtime')"
                    " AND status IN ('pending','running','waiting_user','outcome_unknown')"
                    " ORDER BY created_at, id",
                    (household_id, workflow["id"]),
                ).fetchall()
            ]
            # More than one unresolved intent means an EARLIER provider effect
            # is still outstanding. The previous round inventoried them and went
            # on classifying from the newest, so a reset-quarantined job that
            # may already have created a runtime was reported alongside a clean
            # pending operation — and an operator provisioning on top of it
            # creates the conflict the quarantine exists to prevent.
            older_intent_unresolved = len(unresolved) > 1
            older = _describe_jobs(plan.unresolved_runtime_jobs[1:])
            # A job the service QUARANTINED — `_supersede_unsettled_jobs` moves
            # anything past the provider boundary to `outcome_unknown` with a
            # `<reason>_requires_reconciliation` code — and it applies to every
            # kind, not just `ensure_runtime`. That intent stays the controlling
            # recovery action until a reconcile settles it, whatever the
            # workflow label says about itself.
            quarantined = _describe_jobs(
                [
                    job
                    for job in (
                        *plan.unresolved_runtime_jobs,
                        *plan.pending_step_jobs,
                    )
                    if str(job.get("error_code") or "").endswith(RECONCILE_SUFFIX)
                ]
            )
            if runtime_job is not None:
                plan.pending_runtime_job = {
                    "job_id": runtime_job["id"],
                    "status": runtime_job["status"],
                    "desired_revision": runtime_job["desired_revision"],
                    # Reported because it is what the worker will dispatch
                    # through, and it can differ from the configured provider.
                    "provider": runtime_job["provider"],
                }

            # IDs, not a count. The API worker can be waiting on this
            # `BEGIN IMMEDIATE` and commit a legitimate revision the moment the
            # rehearsal rolls back — a count taken afterwards would differ and
            # the command would accuse itself of writing, refusing to print a
            # report that was in fact correct. An id the rehearsal minted is
            # unambiguous: a concurrent commit produces a different one.
            revision_ids_before = {
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM config_revisions WHERE household_id = ?",
                    (household_id,),
                ).fetchall()
            }

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
            # Issued, and no runtime job left to run: provisioning is done and
            # the household is waiting on bootstrap activation. Neither branch
            # below fits — rehearsing the planner would mint a revision N+1 the
            # real flow never creates, and the reconcile set describes an
            # operation that has already finished. The next writes belong to
            # activation, which this command does not model, so it says that.
            # A job that FAILED also leaves no pending row, and
            # `_mark_step_problem` leaves the revision issued with the workflow
            # still in `runtime_provisioning`. Inferring success from the mere
            # absence of a pending job therefore reported a failed onboarding as
            # waiting for activation — hiding the one state that needs a human.
            settled = connection.execute(
                "SELECT status FROM provisioning_jobs WHERE household_id = ?"
                " AND workflow_id = ? AND kind = 'runtime'"
                " AND operation = 'ensure_runtime'"
                " ORDER BY created_at DESC LIMIT 1",
                (household_id, workflow["id"]),
            ).fetchone()
            settled_status = str(settled["status"]) if settled else ""
            awaiting_activation = (
                issued is not None
                and runtime_job is None
                and settled_status == "succeeded"
                and str(workflow["state"]) == "activating"
            )
            # `complete` means bootstrap activation already committed. Folding
            # it in reported every fully onboarded household as still waiting
            # for a step that has finished — a false next action on the state an
            # operator sees most often.
            already_onboarded = (
                issued is not None
                and runtime_job is None
                and settled_status == "succeeded"
                and str(workflow["state"]) == "complete"
            )
            # A terminal workflow has no next operation, and the planner does
            # not know that. `cancel()` revokes the issued revision and cancels
            # the queued job but leaves the verified step results intact, so a
            # household cancelled after every step verified fell through to the
            # planning branch below, successfully rehearsed a revision N+1 and
            # reported no blocker at all — a ready-to-provision report for an
            # onboarding that can never resume.
            # A household under deletion is terminal in a way the workflow
            # cannot show. `DeletionService.delete` sets `households.status` and
            # deliberately preserves a leased `ensure_runtime` job, leaving the
            # workflow in `runtime_provisioning` — so every classification below
            # read a live provisioning operation and advertised the runtime
            # success path and its secret installs for a household whose only
            # valid path is deletion. The status lives on `households`, which
            # this query never joined.
            household = connection.execute(
                "SELECT status FROM households WHERE id = ?", (household_id,)
            ).fetchone()
            household_status = str((household or {})["status"] or "") if household else ""
            being_deleted = household_status in {"deleting", "deleted"}

            # A job's provider is durable; the SECRET SINK is not. `build`
            # selects one sink from the current configuration, so after a
            # restart under a different `ABROLIA_RUNTIME_PROVIDER` the worker
            # dispatches the job's own provider and then hands its reference to
            # the configured provider's sink — `synthetic-runtime:<household>`
            # to `FlySecretSink.install`, which shells out to
            # `fly secrets import --app` and settles the job `outcome_unknown`.
            #
            # The previous round made the report follow the job's provider,
            # which is right about what the worker dispatches and silent about
            # this: the operator got a runnable-looking plan for an onboarding
            # that stalls at secret installation. The provider and its sink are
            # only usable as a durable PAIR, and this command cannot repair the
            # pairing — it can refuse to describe the operation as pending.
            configured_provider = container.config.runtime_provider
            job_provider = (
                str(runtime_job["provider"]) if runtime_job is not None else ""
            )
            provider_mismatch = (
                runtime_job is not None and job_provider != configured_provider
            )
            cancelled_workflow = str(workflow["state"]) == WorkflowState.CANCELLED.value
            failed_runtime = (
                issued is not None
                and runtime_job is None
                and settled_status in {"failed", "cancelled"}
            )

            # ONE ordered, mutually exclusive classifier.
            #
            # These used to be a run of independent `if` blocks that each
            # assigned `rehearsal` and `blocked_by`, so the LAST one to match
            # won regardless of which mattered. Cancelling a household whose job
            # had crossed the provider boundary replaced "reconcile this
            # quarantined intent" with "start a new one"; restarting under a
            # different `ABROLIA_RUNTIME_PROVIDER` replaced an outstanding
            # reconciliation, or a deletion, with advice to cancel and re-plan.
            # Each overwrite pointed an operator at an action that provisions
            # over provider state which may already exist.
            #
            # Strongest first: an unresolved provider effect and a household
            # being deleted are facts about the world, and a workflow label or a
            # configuration mismatch is not.
            if being_deleted:
                models_an_operation = False
                plan.rehearsal = (
                    f"the household is `{household_status}`; there is no"
                    " onboarding operation to rehearse. Any runtime job still"
                    " recorded is preserved for reconciliation by the deletion,"
                    " not for provisioning."
                )
                plan.blocked_by = (
                    f"the household is {household_status}; it cannot be"
                    " provisioned"
                )
            elif older_intent_unresolved:
                models_an_operation = False
                plan.rehearsal = (
                    f"an earlier runtime intent is unresolved ({older}), so"
                    " nothing about the newest job is rehearsed. Reconciliation"
                    " comes first: the outstanding intent may already have"
                    " created a runtime, and what the newest job would do"
                    " depends on what that one did."
                )
                plan.blocked_by = (
                    f"an earlier runtime intent is still unresolved: {older}."
                    " Reconcile it with `abrolia-control-plane reconcile"
                    " <job-id>` before provisioning on top of it."
                )
            elif quarantined:
                models_an_operation = False
                plan.rehearsal = (
                    f"an intent that reached its provider is unsettled"
                    f" ({quarantined}); the service preserved it deliberately."
                    " Nothing is rehearsed: what that job did upstream is not"
                    " in durable state, and only an inspection can say."
                )
                plan.blocked_by = (
                    f"reconcile the unsettled intent first: {quarantined}."
                    " `abrolia-control-plane reconcile <job-id>`. Starting again"
                    " provisions over provider state that may already exist."
                )
            elif cancelled_workflow:
                models_an_operation = False
                plan.rehearsal = (
                    "the onboarding workflow is `cancelled`; there is no pending"
                    " operation to rehearse. Verified step results survive a"
                    " cancellation, so the steps above describe what WAS done,"
                    " not work that will resume."
                )
                plan.blocked_by = (
                    "the onboarding workflow is cancelled; start a new one"
                    " rather than provisioning from this"
                )
            elif provider_mismatch:
                # Pending, and NOT executable. The job is still `pending`, the
                # worker can still lease it, and saying "nothing is pending"
                # told an operator there was no live work while the worker was
                # about to stall on it. The two facts are reported separately.
                models_an_operation = False
                plan.rehearsal = (
                    f"the pending runtime job is recorded against"
                    f" `{job_provider}` and this deployment is configured for"
                    f" `{configured_provider}`. The worker dispatches the job's"
                    " provider but installs secrets through the configured"
                    " provider's sink, so the operation would stall at secret"
                    " installation. Nothing is rehearsed and no write set,"
                    " resource or secret is claimed."
                )
                plan.blocked_by = (
                    f"runtime provider mismatch: the queued job is"
                    f" `{job_provider}` and ABROLIA_RUNTIME_PROVIDER is"
                    f" `{configured_provider}`. Restore the configuration the"
                    " job was queued under, or cancel and re-plan it."
                )
            elif failed_runtime:
                models_an_operation = False
                plan.blocked_by = (
                    f"the runtime operation for revision {issued['revision']} is"
                    f" `{settled_status}`; onboarding cannot proceed until it is"
                    " retried or reset"
                )
                plan.rehearsal = (
                    f"revision {issued['revision']} is issued but its runtime"
                    f" operation is `{settled_status}` — a terminal state needing"
                    " intervention, not a stage this command can rehearse past."
                    " No write set is claimed."
                )
            elif awaiting_activation:
                models_an_operation = False
                plan.rehearsal = (
                    f"revision {issued['revision']} is issued and its runtime"
                    " operation has settled; the household is waiting on"
                    " bootstrap activation, whose writes this command does not"
                    " model. Nothing is rehearsed and no write set is claimed."
                )
            elif already_onboarded:
                models_an_operation = False
                plan.rehearsal = (
                    f"revision {issued['revision']} is active and onboarding is"
                    " complete; there is no pending operation to rehearse"
                )

            # Every state that rehearses nothing skips the planning pass and
            # reaches the same gate below. One decision, one place: the previous
            # shape had the cancelled case return through a second mechanism, so
            # "does this state advertise future work" was answered twice.
            if not models_an_operation:
                pass
            elif already_planned and not _holds_current_restriction(
                container, household_id
            ):
                # `_run_once` checks this BEFORE dispatching any provider: with
                # the restriction receipt revoked or stale, it fails the job,
                # revokes the revision and the bootstrap tokens, and returns the
                # workflow to email. Reporting the ordinary success path over
                # that described an operation whose first act is to undo itself.
                #
                # This is durable state, not a prediction about scheduling —
                # the receipt either is current or is not — and the predicate is
                # the worker's own.
                models_an_operation = False
                plan.rehearsal = (
                    f"revision {issued['revision']} is issued, and the"
                    " special-category content restriction receipt is not"
                    " current. The runtime job is blocked on that before any"
                    " provider call, so no write set is claimed."
                )
                plan.blocked_by = (
                    "the household does not currently hold the special-category"
                    " content restriction; the queued runtime job cannot proceed"
                    " until it is accepted again"
                )
            elif already_planned:
                state = str(workflow["state"])
                job_status = str((runtime_job or {})["status"])
                # EVERY status here is pending on a provider call whose result
                # is not in durable state. `_run_once` can be rate-limited,
                # rejected or time out, taking branches that write a subset of
                # the success path; `_reconcile` branches on `provider.inspect()`
                # and may settle, fail, or re-run preparation. Selecting the
                # success mapping presented one possible future as the fact.
                #
                # The set is still worth reporting — it is the upper bound, and
                # what an operator wants to know is which tables can be touched
                # — but it is labelled as the success path, not as the answer.
                # A `running` job whose lease has expired is reclaimed, and
                # `_run_once` then calls `provider.inspect()` — the same
                # provider-dependent branch as `outcome_unknown`, which can
                # settle, fail, or run `_cleanup_cancelled_result` and
                # deprovision the resource plus insert a bootstrap-cleanup job.
                # Those writes fall outside the advertised set entirely, so the
                # guarantee attached to it would be false.
                reclaimed = (
                    job_status == "running"
                    and (runtime_job["lease_until"] or 0) <= _now_seconds()
                )
                if job_status == "outcome_unknown" or reclaimed:
                    # The next operation is a RECONCILE, and
                    # `ProvisioningWorker._reconcile` branches on
                    # `provider.inspect()` — it may settle, fail, or re-run
                    # preparation. Which branch is taken is not knowable from
                    # durable state; it depends on an answer only the provider
                    # has, and asking is exactly what this command must not do.
                    #
                    # So it reports the uncertainty instead of picking a branch.
                    # An operator told "these tables, exactly" when the real
                    # answer is "one of these, depending" has been given false
                    # precision, which is worse than being told to go and look.
                    plan.rehearsal = (
                        f"revision {issued['revision']} is issued and its runtime"
                        f" job is `{job_status}`"
                        + (" with an expired lease" if reclaimed else "")
                        + ": the next operation is a"
                        " reconcile whose write set depends on a provider"
                        " inspection this command does not perform. Inspect with"
                        " `abrolia-control-plane reconcile <job-id>` before"
                        " relying on a write set."
                    )
                    plan.blocked_by = (
                        "runtime job outcome is unknown; reconcile it before"
                        " onboarding"
                    )
                    # `table_writes` was cleared here and the resource and
                    # secret sections were not, so the report still described
                    # resources as though they will be created and both secrets
                    # as though they will be installed — when an inspection may
                    # find the resource already exists, failed, or needs
                    # cleanup. The same uncertainty governs all three.
                    models_an_operation = False
                    writes = None
                else:
                    writes = RUNTIME_WRITES_BY_WORKFLOW_STATE.get(state)
                    if writes is not None:
                        # Union, deduplicated, order preserved: the bound has to
                        # cover a cancellation arriving mid-call.
                        merged = list(writes)
                        merged.extend(
                            write
                            for write in COMPENSATION_WRITES
                            if write not in merged
                        )
                        writes = tuple(merged)
                if writes is None:
                    if plan.rehearsal == "":
                        # A state the runtime operation does not run from. Say
                        # so rather than reporting another state's tables.
                        plan.rehearsal = (
                            f"revision {issued['revision']} is already issued, and"
                            f" the workflow is `{state}`, which the runtime"
                            " operation does not run from; no write set applies"
                        )
                else:
                    plan.rehearsal = (
                        f"revision {issued['revision']} is already issued, so the"
                        " planning pass is done. table_writes is the SUCCESS PATH"
                        f" of the pending runtime operation from `{state}`, which"
                        " UPDATES that revision rather than inserting another. A"
                        " provider that rate-limits, rejects or times out takes a"
                        " branch writing a subset of these; none writes outside"
                        " them. The set also covers the COMPENSATION a cancel or"
                        " reset arriving mid-call triggers, which records the"
                        " resource as deleting and can insert a cleanup job."
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
                    planned = container.planner.issue(
                        connection, household_id=household_id
                    )
                    spec = planned.spec
                    # The rehearsal just built the revision inside the
                    # transaction it is about to roll back, so the diff it was
                    # created to report is right here — and `.spec` alone threw
                    # the rest away, leaving `config_revision` empty for the one
                    # branch where the planning pass actually runs. The Step E1
                    # contract asks for the household `config_revision` diff;
                    # this is where it exists.
                    plan.config_revision = {
                        "revision": planned.revision.revision,
                        "status": planned.revision.status,
                        "manifest_sha256": planned.revision.manifest_sha256,
                        "source": (
                            "rehearsed by this planning pass and rolled back;"
                            " the real onboarding issues it when the primary"
                            " step verifies"
                        ),
                        "diff": _revision_diff(
                            container, household_id, planned.revision.revision
                        ),
                    }
                except ValueError as error:
                    # A prerequisite, reported as one. No claim about what runs
                    # next follows from it — the inventory above says what is
                    # queued and an operator reads it.
                    plan.blocked_by = str(error)
                    models_an_operation = False
                finally:
                    connection.set_trace_callback(None)

            rehearsed_revision_ids = {
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM config_revisions WHERE household_id = ?",
                    (household_id,),
                ).fetchall()
            } - revision_ids_before
            plan.uncommitted_revision_delta = len(rehearsed_revision_ids)

            plan.table_writes = observed
            if spec is None and issued is not None:
                # The planning pass was skipped because a revision is already
                # issued — but its manifest describes the same household, so the
                # provider can still say what it will create. Better than
                # reporting nothing for the ordinary case.
                try:
                    spec = container.configs.manifest(
                        household_id, issued["revision"]
                    )
                except KeyError:
                    spec = None
            # `spec is not None` as well: these two fields describe the RUNTIME
            # operation, and before a revision is planned or issued there is no
            # runtime operation to describe. Reporting a provider entry that
            # says "nothing is asserted about what it creates" is noise dressed
            # as a plan, and the rule everywhere else in this report is that a
            # field describes the pending operation or is empty.
            if spec is not None and models_an_operation:
                plan.runtime_resources = _runtime_resources(
                    container,
                    household_id,
                    spec,
                    # The pending job's provider when there is one; the
                    # configured provider only for a planning pass that has not
                    # happened yet and so has no job to disagree with. A job
                    # whose provider DIFFERS from the configuration never
                    # reaches here — it is blocked above, because its sink and
                    # its provider do not pair.
                    job_provider or None,
                )
                plan.secrets = _secret_names(
                    container, household_id, plan.runtime_resources
                )
            else:
                # These were assigned unconditionally, so a household that had
                # just been told "there is no pending operation to rehearse"
                # was handed, in the same report, a runtime reference to create
                # and a bootstrap token to install — for a complete household
                # whose token had already been cleaned up, or a fresh workflow
                # blocked on email with an empty secret target. An operator
                # acting on that prepares or rotates credentials for work that
                # does not exist. One flag decides it for every state, so the
                # `rehearsal` sentence and the fields beneath it cannot
                # contradict each other again.
                plan.table_writes = []
            raise _DryRunRollback
    except _DryRunRollback:
        pass

    assert plan is not None
    # After the rollback, and asking only about rows THIS rehearsal minted.
    # Anything else in the table belongs to the worker and is none of this
    # command's business to report as its own.
    plan.committed = any(
        container.database.query_one(
            "SELECT 1 FROM config_revisions WHERE id = ?", (revision_id,)
        )
        is not None
        for revision_id in rehearsed_revision_ids
    )
    return plan


def _household_argument(value: str) -> str:
    """A canonical household UUID, rejected at the boundary if it is not one.

    Unvalidated, `--household ../x` or an uppercase UUID reached the database
    and came back as "household has no onboarding workflow" — conflating a typo
    with a real missing workflow, which is the diagnosis an operator acts on.
    Canonical form specifically: `UUID()` accepts braces, urn prefixes and mixed
    case, all of which compare unequal to the stored identifier and would
    produce the same misleading answer.
    """
    try:
        canonical = str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a household UUID"
        ) from None
    if canonical != value:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not canonical; use {canonical}"
        )
    return canonical


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="abrolia-provision",
        description="rehearse a pilot onboarding without writing anything",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--household", required=True, type=_household_argument)
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
    # REHEARSE AGAINST A COPY. Every previous attempt at "mutates nothing"
    # enumerated a way SQLite writes and closed it — migrations on build, a
    # created file, a persistent `journal_mode` pragma — and each time another
    # remained. Closing the last connection checkpoints committed WAL frames
    # into the main file and removes the sidecars, which no rollback undoes and
    # no flag prevents.
    #
    # Copying the database and its sidecars first makes the property structural
    # rather than a list: the rehearsal cannot touch the live files because it
    # never opens them. Anything it does is discarded with the copy.
    #
    # The copy is taken by SQLite, not by the filesystem. A `shutil.copy2` loop
    # over the database and its sidecars is not atomic: with the API appending
    # or checkpointing, the main file can come from one generation and the
    # `-wal` from another, and a DELETE-mode database can have a hot `-journal`
    # that such a loop does not copy at all. SQLite cannot promise that
    # independently copied files form a valid snapshot, so the rehearsal could
    # fail on corruption or — worse — report a state that never existed.
    #
    # An online backup from a READ-ONLY source solves both halves: the source
    # cannot be written, so no checkpoint-on-close and no journal-mode rewrite;
    # and the backup runs under a read transaction, so the copy is one committed
    # state including WAL frames not yet checkpointed.
    #
    # What this does touch is `-shm`, the shared-memory index, which SQLite must
    # map to read a WAL database at all. It holds no durable data and is rebuilt
    # from the WAL, so the guarantee is precise rather than absolute: the
    # database and its WAL are left byte-identical; the transient index may be
    # updated.
    with tempfile.TemporaryDirectory(prefix="abrolia-dry-run-") as scratch:
        live = ControlPlaneConfig.from_env()
        rehearsal_path = Path(scratch) / live.database_path.name
        if not live.database_path.exists():
            raise SystemExit(
                f"dry-run found no database at {live.database_path};"
                " refusing rather than creating one"
            )
        if not live.database_path.is_file():
            # A directory passes `exists()` and then fails inside SQLite. Every
            # operator-supplied boundary should fail with this command's own
            # diagnostic, not a traceback from three layers down.
            raise SystemExit(
                f"{live.database_path} is not a regular file; ABROLIA_CONTROL_PLANE_DB"
                " must name the control-plane database"
            )
        if not os.access(live.database_path, os.R_OK):
            raise SystemExit(f"{live.database_path} is not readable")
        # `as_uri()`, not interpolation. `ABROLIA_CONTROL_PLANE_DB` is a
        # pathname, and `?` and `#` are legal in one: `/data/control?plane.db`
        # interpolated here parses as the filename `/data/control` with
        # `plane.db?mode=ro` as URI syntax. `mode=ro` is then not the parameter
        # it looks like, so SQLite opens read-write-CREATE, creates
        # `/data/control`, and the rehearsal backs up an empty database while
        # the real one is never read. Percent-encoding closes both halves at
        # once — the no-mutation guarantee and the "rehearse the right file"
        # one. `resolve()` because `as_uri()` requires an absolute path.
        try:
            source = sqlite3.connect(
                f"{live.database_path.resolve().as_uri()}?mode=ro", uri=True
            )
        except sqlite3.Error as error:
            # Outside the `try` below, this leaked an `OperationalError`
            # traceback for anything SQLite could not open — an encrypted file,
            # a socket, a path on a filesystem it cannot map. The command has a
            # deliberate diagnostic and should use it for every one of them.
            raise SystemExit(
                f"dry-run could not open {live.database_path}: {error}"
            ) from None
        try:
            snapshot = sqlite3.connect(rehearsal_path)
            try:
                source.backup(snapshot)
            finally:
                snapshot.close()
        except sqlite3.DatabaseError as error:
            raise SystemExit(
                f"dry-run could not snapshot {live.database_path}: {error}"
            ) from None
        finally:
            source.close()
        # The pause marker travels with the snapshot. `backup()` copies SQLite
        # DATA, and the worker pause is a sibling FILE — so redirecting
        # `database_path` at the scratch copy pointed `worker_pause_path` at a
        # directory where no marker exists, and the rehearsal reported
        # SQL-leasable jobs as executable while the live repository, paused,
        # returns nothing at all. A restored database stays paused until
        # `resume-jobs`, which is exactly when an operator reaches for this
        # command.
        live_pause = ControlPlaneDatabase(live.database_path).worker_pause_path
        if live_pause.is_file():
            (rehearsal_path.parent / live_pause.name).write_bytes(
                live_pause.read_bytes()
            )
        config = replace(live, database_path=rehearsal_path)
        return _rehearse(config, args.household)


def _rehearse(config: ControlPlaneConfig, household_id: str) -> int:
    with ControlPlaneContainer.build(
        config,
        acquire_process_lock=False,
        apply_migrations=False,
        # Still set, so the copy is not converted either: a report derived from
        # a database silently rewritten into another journal mode is a report
        # about something other than what is running.
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
            plan = plan_onboarding(active, household_id)
        except KeyError:
            raise SystemExit("household has no onboarding workflow") from None
        if plan.committed:
            raise SystemExit("dry-run observed a committed write and refuses to report")
    print(json.dumps(plan.public_dict(), sort_keys=True, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

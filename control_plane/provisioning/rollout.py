"""Scheduling a planned revision for deployment.

Planning a revision is not deploying one. `DesiredSpecPlanner.issue` writes a
`config_revisions` row and stops; until something creates a runtime job and
advances `households.current_config_revision`, the household keeps serving the
previous revision. For onboarding, the provisioning worker does both in the
same breath as planning. For a household that has already finished onboarding
— one gaining a member — nothing did, so the binding endpoint reported revision
N while the runtime served N−1 and the gateway could route a new member to a
runtime that had never heard of them.

This lives beside the worker rather than inside the endpoint because it is
provisioning's business, and because it has to be exercised without an HTTP
client: what it schedules is checked by the worker's currency guards, and those
deserve a test that does not have to authenticate first.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from control_plane.provisioning.planner import DesiredSpecPlanner, PlannedRevision
from control_plane.provisioning.worker import REPROVISION_RUNTIME_OPERATION
from control_plane.repositories.bindings import ChannelBindingsRepository
from control_plane.repositories.configs import ConfigRepository
from control_plane.repositories.jobs import JobsRepository
from control_plane.repositories.onboarding import OnboardingRepository


class RolloutNotReady(RuntimeError):
    """A rollout was asked for while the household was still settling."""


def _open_runtime_job(connection: sqlite3.Connection, household_id: str):
    """A runtime job this household is still waiting on, or None.

    "In flight" includes a job that has launched and is waiting for its
    revision to activate: since C3e that job is `pending` with a `not_before`
    at the token's expiry, and a second rollout scheduled beside it would
    overwrite the very `current_config_revision` it is waiting to have
    activated.

    A household with NO such job and a status of `provisioning` is not busy —
    it is STRANDED, which is a different thing that reads the same from the
    household row alone.
    """
    return connection.execute(
        "SELECT id FROM provisioning_jobs WHERE household_id = ?"
        " AND kind = 'runtime'"
        " AND status IN ('pending','running','waiting_user','outcome_unknown')"
        " LIMIT 1",
        (household_id,),
    ).fetchone()


def schedule_runtime_rollout(
    connection: sqlite3.Connection,
    *,
    jobs: JobsRepository,
    onboarding: OnboardingRepository,
    household_id: str,
    planned: PlannedRevision,
    runtime_provider: str,
    now: float | None = None,
) -> str:
    """Queue the work that makes a planned revision the one being served.

    Called in the SAME transaction as the change that motivated the revision,
    so a member is never durable without the rollout that makes them real.

    The onboarding workflow is deliberately left where it is. Its state reaches
    the family through `OnboardingSnapshot`, and a household that finished
    setup months ago must not be shown as mid-setup because somebody added an
    adult. `_workflow_states_for` in the worker is the other half of that
    decision: it expects `complete` to persist for this operation, where an
    onboarding rollout expects the states onboarding actually moves through.
    """
    now = time.time() if now is None else now
    workflow = onboarding.workflow_for_household(household_id)
    state = connection.execute(
        "SELECT status FROM households WHERE id = ?", (household_id,)
    ).fetchone()
    # A rollout may not be scheduled while another is IN FLIGHT: a second one
    # enqueues revision N+1, overwrites the household's single
    # `current_config_revision`, and strands both jobs — the original no longer
    # matches the revision, the new one no longer matches the workflow. Two
    # stranded jobs and, after prepare, a cleanup that can take the shared
    # runtime with it.
    #
    # That is the rule. `status != 'active'` was an APPROXIMATION of it, and
    # the approximation is what made a stranded household unrepairable: such a
    # household sits at `provisioning` with no job left to run, so there is
    # nothing to collide with and nothing to wait for, and refusing it declined
    # the one action that would fix it. `_open_runtime_job` asks the question
    # the comment above was always describing.
    #
    # Statuses outside these two are refused whatever the queue says: a
    # `draft`, `onboarding`, `deleting` or `deleted` household is not one a
    # rollout belongs to at all.
    if (
        state is None
        or state["status"] not in {"active", "provisioning"}
        or workflow.state != "complete"
        or _open_runtime_job(connection, household_id) is not None
    ):
        raise RolloutNotReady(
            "the household is not in a settled state a rollout can be planned against"
        )
    revision = planned.revision.revision
    job_id, _ = jobs.create(
        connection,
        household_id=household_id,
        workflow_id=workflow.id,
        kind="runtime",
        operation=REPROVISION_RUNTIME_OPERATION,
        # Same shape as onboarding's, and deliberately so: the revision is what
        # makes the intent unique, so a retry of the same rollout is the same
        # job rather than a second one.
        intent_key=f"{household_id}:runtime:{revision}",
        desired_revision=revision,
        request={
            "step_kind": "runtime",
            "manifest": planned.spec.model_dump(mode="json"),
        },
        provider=runtime_provider,
        now=now,
    )
    # The worker's currency guards read this pair on every phase, so it moves
    # with the job rather than after it.
    connection.execute(
        "UPDATE households SET status = 'provisioning', current_config_revision = ?,"
        " updated_at = ? WHERE id = ?",
        (revision, now, household_id),
    )
    return job_id


# --- reconciling a household whose runtime is serving a stale binding set ---


@dataclass(frozen=True)
class StaleHousehold:
    """One household whose deployed manifest disagrees with the binding table."""

    household_id: str
    served_revision: int
    #: `(channel, actor_id, chat_id)` triples, sorted, as the table says now.
    bindings_now: tuple[tuple[str, str, str], ...]
    #: The same, as the revision the runtime is actually serving says.
    bindings_served: tuple[tuple[str, str, str], ...]
    #: Why a rollout cannot be scheduled for this household right now, or None.
    blocked_by: str | None

    def public_dict(self) -> dict[str, object]:
        """Identifiers and counts only. A binding is a channel identity."""
        return {
            "household_id": self.household_id,
            "served_revision": self.served_revision,
            "bindings_now": len(self.bindings_now),
            "bindings_served": len(self.bindings_served),
            "revoked": len(set(self.bindings_served) - set(self.bindings_now)),
            "added": len(set(self.bindings_now) - set(self.bindings_served)),
            "blocked_by": self.blocked_by,
        }


def _projected(bindings: tuple) -> tuple[tuple[str, str, str], ...]:
    return tuple(sorted(
        (record.channel, record.actor_id, record.chat_id) for record in bindings
    ))


def _served(manifest: dict) -> tuple[tuple[str, str, str], ...]:
    return tuple(sorted(
        (str(entry.get("channel")), str(entry.get("actor_id")), str(entry.get("chat_id")))
        for entry in manifest.get("channel_bindings", ())
    ))


def find_stale_bindings(
    connection: sqlite3.Connection,
    *,
    configs: ConfigRepository,
    bindings: ChannelBindingsRepository,
    onboarding: OnboardingRepository,
) -> tuple[StaleHousehold, ...]:
    """Households whose runtime authorizes a binding set the table no longer has.

    The control plane's table is authoritative, and the runtime does not read
    it: `Household.knows_binding` answers from the manifest the runtime booted
    with. So anything that changes `channel_bindings` WITHOUT issuing a
    revision leaves the two disagreeing, and the disagreement is invisible —
    every layer the control plane can see reports success while the runtime
    goes on authorizing somebody it has been told not to.

    Migration 0010 is the instance this was written for: it retires bindings
    whose identities were never provenanced, and nothing re-plans afterwards.
    `ControlPlaneDatabase.migrate` does not call `DesiredSpecPlanner.issue`,
    and neither does the startup path, so "the next revision will fix it" is
    true only if something happens to issue one.

    Staleness is DERIVED rather than recorded, which is why this finds drift
    from any cause and not only from that migration. A revocation table would
    have to be written by everything that can revoke, and the one thing that
    already knows the answer is the comparison itself: what the table says now
    against what the SERVED revision says.

    The served revision is the one `config_revisions` marks `active`, and this
    originally read `households.current_config_revision` instead. That was
    wrong and it hid the households this sweep matters most for.
    `schedule_runtime_rollout` advances that column when a job is QUEUED, so a
    rollout that then died left the household pointing at a revision nothing
    ever served — and comparing the table against THAT manifest found no
    divergence, because the dead revision is exactly the one that contains the
    change. The sweep reported "nothing to do" for the household whose runtime
    was furthest out of date.

    `BootstrapService.activate` is the sole writer of `status = 'active'`, and
    it supersedes the previous one in the same transaction, so there is at most
    one per household.
    """
    rows = connection.execute(
        "SELECT h.id AS id, h.status AS status,"
        " active.revision AS current_config_revision"
        " FROM households AS h"
        " JOIN config_revisions AS active ON active.household_id = h.id"
        "  AND active.status = 'active'"
        " ORDER BY h.id"
    ).fetchall()
    stale: list[StaleHousehold] = []
    for row in rows:
        household_id = str(row["id"])
        revision = int(row["current_config_revision"])
        try:
            manifest = configs.manifest(household_id, revision)
        except KeyError:
            # The household points at a revision that is not stored. That is a
            # different fault and not one a binding sweep should paper over by
            # planning a fresh revision on top of it.
            stale.append(StaleHousehold(
                household_id=household_id,
                served_revision=revision,
                bindings_now=_projected(
                    bindings.verified(connection, household_id=household_id)
                ),
                bindings_served=(),
                blocked_by="served_revision_missing",
            ))
            continue
        verified = bindings.verified(connection, household_id=household_id)
        now_bindings = _projected(verified)
        served = _served(manifest)
        if now_bindings == served:
            continue
        stale.append(StaleHousehold(
            household_id=household_id,
            served_revision=revision,
            bindings_now=now_bindings,
            bindings_served=served,
            blocked_by=_blocked_by(
                connection,
                row,
                onboarding,
                household_id,
                has_owner_binding=any(
                    record.role == "owner" for record in verified
                ),
            ),
        ))
    return tuple(stale)


def _blocked_by(
    connection: sqlite3.Connection,
    row,
    onboarding: OnboardingRepository,
    household_id: str,
    *,
    has_owner_binding: bool,
) -> str | None:
    """The reason this household cannot be re-planned right now, or None.

    The first two come from `schedule_runtime_rollout`, read from the same two
    facts that function checks so the report cannot promise a rollout the
    scheduler then declines. They are stated as reasons rather than a boolean
    because "still settling" and "never finished setup" are different operator
    problems.

    The third is this sweep's own, and it is a refusal to UNDO a revocation.
    `DesiredSpecPlanner.issue` seeds the owner row from the onboarding result
    on every run — correct while onboarding is the thing that proved the
    channel, and wrong here: migration 0010 retires an owner binding precisely
    when its identity cannot be trusted, and re-planning would write that same
    identity straight back from the step it came from. For the shape 0010
    retires — two households recorded under one owner actor — it would also
    re-create the collision, so the second household's `_reject_foreign_holder`
    would refuse and (before the savepoint below) take the whole sweep with it.

    A household with no verified owner binding is therefore reported and left
    alone. It cannot be repaired without inventing the identity that was taken
    away; what it needs is re-onboarding, which captures one.
    """
    if not has_owner_binding:
        return "owner_binding_retired"
    if row["status"] not in {"active", "provisioning"}:
        return f"household_status_{row['status']}"
    # The distinction this sweep exists to make, and the one it could not make
    # before: a household at `provisioning` is either BUSY or STRANDED, and the
    # household row says the same thing for both.
    #
    # Busy means a rollout is still in flight and a second would collide with
    # it. Stranded means the rollout died without handing the household back —
    # `_restore_settled_household` declines once the provider has been touched,
    # deliberately, because no database-only write can say what the machine is
    # carrying. Those households were reported `household_status_provisioning`
    # and skipped, so the mechanism built to detect this divergence was blind
    # to precisely the households that had it.
    #
    # Repair is a roll FORWARD: planning the next revision and deploying it
    # converges the machine whatever it holds now, which is a claim this side
    # can honestly make where "you are back on N-1" is not.
    if _open_runtime_job(connection, household_id) is not None:
        return "rollout_in_flight"
    try:
        workflow = onboarding.workflow_for_household(household_id)
    except Exception:
        return "workflow_missing"
    if workflow.state != "complete":
        return f"workflow_{workflow.state}"
    return None


def reconcile_stale_bindings(
    connection: sqlite3.Connection,
    *,
    planner: DesiredSpecPlanner,
    jobs: JobsRepository,
    onboarding: OnboardingRepository,
    configs: ConfigRepository,
    bindings: ChannelBindingsRepository,
    runtime_provider: str,
    apply: bool = False,
    now: float | None = None,
) -> tuple[dict[str, object], ...]:
    """Report every stale household, and — only with `apply` — re-plan them.

    Dry run is the default because this schedules real deployments for
    households nobody asked about. The report is the same shape either way, so
    what an operator reads is what the apply run acts on.

    A blocked household is REPORTED and skipped, never forced. The states
    `schedule_runtime_rollout` refuses are the states where forcing strands
    jobs — a second rollout while the first is in flight overwrites the single
    `current_config_revision` and leaves both unable to match it.

    Skipping is per household in the transaction as well as in the report: the
    planner can refuse a household this cannot ask about in advance, and one
    such refusal must not roll back the rollouts already scheduled for the
    others. The dry run asks the planner the same question inside a savepoint
    it always rolls back, so both modes classify a household identically and
    the report keeps its promise.
    """
    results: list[dict[str, object]] = []
    for household in find_stale_bindings(
        connection, configs=configs, bindings=bindings, onboarding=onboarding
    ):
        entry = household.public_dict()
        if household.blocked_by is not None:
            entry["action"] = "skipped"
            results.append(entry)
            continue
        # THE DRY RUN TAKES THE SAME BRANCH, and that is why the planner runs
        # in both modes. `_blocked_by` cannot answer for it: `issue` refuses on
        # an incomplete profile, an unverified provider result, a missing
        # account owner and a consent receipt that is absent, revoked or
        # superseded, and asking those questions here would be a second copy of
        # the planner's preconditions that drifts from the first. Asking the
        # planner itself and rolling the answer back is what keeps the promise
        # this report makes — the branch it describes is the branch the apply
        # run takes — instead of reporting `would_reconcile` for a household
        # that then refuses.
        #
        # One household per SAVEPOINT, because this loop visits households that
        # have nothing to do with each other. `cli.main` holds a single write
        # transaction around the whole sweep, so before this an exception at
        # either call below rolled back the revisions and jobs already created
        # for EARLIER households and abandoned every later one — one
        # unplannable household leaving every other stale runtime authorizing
        # its revoked bindings.
        savepoint = f"reconcile_{len(results)}"
        connection.execute(f"SAVEPOINT {savepoint}")
        try:
            planned = planner.issue(connection, household_id=household.household_id)
            if apply:
                schedule_runtime_rollout(
                    connection,
                    jobs=jobs,
                    onboarding=onboarding,
                    household_id=household.household_id,
                    planned=planned,
                    runtime_provider=runtime_provider,
                    now=now,
                )
        except (ValueError, PermissionError, RolloutNotReady) as error:
            connection.execute(f"ROLLBACK TO {savepoint}")
            connection.execute(f"RELEASE {savepoint}")
            # The message is the planner's own constant text — "household
            # profile is incomplete", "desired spec can only be built from
            # verified results" — which names a precondition and no identity.
            entry["action"] = "skipped"
            entry["blocked_by"] = f"planner_refused: {error}"
            results.append(entry)
            continue
        if not apply:
            # Everything the planner wrote goes back: a dry run must leave the
            # database as it found it, including the `config_revisions` row
            # `issue` inserts to answer the question.
            connection.execute(f"ROLLBACK TO {savepoint}")
            connection.execute(f"RELEASE {savepoint}")
            entry["action"] = "would_reconcile"
            results.append(entry)
            continue
        connection.execute(f"RELEASE {savepoint}")
        entry["action"] = "reconciled"
        entry["planned_revision"] = planned.revision.revision
        results.append(entry)
    return tuple(results)

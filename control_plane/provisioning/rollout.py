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

from control_plane.provisioning.planner import PlannedRevision
from control_plane.provisioning.worker import REPROVISION_RUNTIME_OPERATION
from control_plane.repositories.jobs import JobsRepository
from control_plane.repositories.onboarding import OnboardingRepository


class RolloutNotReady(RuntimeError):
    """A rollout was asked for while the household was still settling."""


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
    # A rollout may only be scheduled against the settled state its currency
    # guards require. `_workflow_states_for` accepts `complete` and nothing
    # else for this operation, so scheduling one while the FIRST rollout is
    # still in flight enqueues revision N+1, overwrites the household's single
    # `current_config_revision`, and strands both jobs: the original
    # `ensure_runtime` no longer matches the revision, and the new one no
    # longer matches the workflow. Two stranded jobs and, after prepare, a
    # cleanup that can take the shared runtime with it.
    #
    # Refusing here is not a limitation to work around later — the owner can
    # add the member once setup finishes, and until then there is nothing to
    # roll out onto.
    if state is None or state["status"] != "active" or workflow.state != "complete":
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

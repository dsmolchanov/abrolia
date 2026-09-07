"""An operator can move a settled household onto the pinned runtime image.

Every path that plans a revision hangs off a family action — verifying a
binding, finishing onboarding — so a runtime pinned before a fix stayed
pinned until somebody in that household happened to do something. On
2026-09-07 both pilot households were still serving an image built on
2026-08-09, whose consent catalogue predates
`special-category-content-restriction-v2` and knows nothing of the Art.
9(2)(a) purpose: every one of them answered `/readyz` with 503
`content_restriction_not_current`, so `runtime-health` parked their email
identities in `needs_attention` and there was no command to fix it.
"""

from __future__ import annotations

import json

import pytest

from control_plane.cli import main
from control_plane.config import ControlPlaneConfig
from control_plane.container import ControlPlaneContainer
from tests.control_plane.test_art9_household_consent import complete_onboarding
from tests.control_plane.test_consent_withdrawal import drain


def _settled(cp_stack) -> None:
    """What activation leaves behind, which this harness does not host.

    `provisioning/bootstrap.py` performs exactly these two writes when the
    runtime claims its bootstrap token; a rollout may only be planned against
    a settled household, and in production both pilot households were
    `active` when this command was needed.
    """
    complete_onboarding(cp_stack)
    drain(cp_stack)
    with cp_stack.database.write() as connection:
        connection.execute(
            "UPDATE households SET status = 'active' WHERE id = ?",
            (cp_stack.household.id,),
        )
        connection.execute(
            "UPDATE onboarding_workflows SET state = 'complete' WHERE household_id = ?",
            (cp_stack.household.id,),
        )
        # Activation also settles the runtime job it was waiting on. Left
        # open, it reads as a rollout already in flight — which is the right
        # refusal for a real in-flight job and the wrong one here.
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'succeeded', settled_at = 1.0"
            " WHERE household_id = ? AND kind = 'runtime'"
            " AND status NOT IN ('succeeded','cancelled')",
            (cp_stack.household.id,),
        )


def _cli(cp_stack, monkeypatch, *args: str) -> int:
    monkeypatch.setattr(
        ControlPlaneConfig, "from_env", staticmethod(lambda: cp_stack.config)
    )
    return main(list(args))


def test_it_queues_a_runtime_job_for_the_new_revision(cp_stack, monkeypatch, capsys) -> None:
    _settled(cp_stack)

    before = cp_stack.database.query_one(
        "SELECT current_config_revision AS revision FROM households WHERE id = ?",
        (cp_stack.household.id,),
    )["revision"]

    assert _cli(cp_stack, monkeypatch, "roll-runtime", cp_stack.household.id) == 0
    printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert printed["household_id"] == cp_stack.household.id
    assert printed["revision"] > before
    # The job the worker will run is a runtime job for THIS household — that is
    # what carries the pinned image onto the Machine.
    job = cp_stack.database.query_one(
        "SELECT kind, operation, status, household_id FROM provisioning_jobs WHERE id = ?",
        (printed["job_id"],),
    )
    assert job["kind"] == "runtime"
    assert job["household_id"] == cp_stack.household.id
    assert job["status"] == "pending"


def test_it_runs_while_the_service_holds_the_writer_lock(cp_stack, monkeypatch) -> None:
    """`serve` owns the flock for the life of the process.

    Moving a household onto a fixed image must not require stopping
    production — the same rule as `reconcile`, `invite` and
    `withdraw-consent`.
    """
    _settled(cp_stack)
    monkeypatch.setattr(
        ControlPlaneConfig, "from_env", staticmethod(lambda: cp_stack.config)
    )
    with ControlPlaneContainer.build(cp_stack.config, acquire_process_lock=True):
        assert main(["roll-runtime", cp_stack.household.id]) == 0


def test_a_household_that_cannot_be_rolled_says_so(cp_stack, monkeypatch) -> None:
    """A refusal, not a traceback and not a silent success.

    An unknown household and one that never finished onboarding are both
    states an operator can act on; either arriving as an unhandled exception
    would say the system broke instead.
    """
    with pytest.raises(SystemExit) as exit_info:
        _cli(cp_stack, monkeypatch, "roll-runtime", "00000000-0000-4000-8000-000000000000")
    assert "cannot be rolled" in str(exit_info.value)

    # Profile saved, onboarding not finished: the planner refuses.
    cp_stack.complete_profile()
    with pytest.raises(SystemExit) as unfinished:
        _cli(cp_stack, monkeypatch, "roll-runtime", cp_stack.household.id)
    assert "cannot be rolled" in str(unfinished.value)


def test_a_second_roll_while_one_is_in_flight_is_refused(cp_stack, monkeypatch) -> None:
    """Two rollouts strand both: one revision, two jobs, neither matching."""
    _settled(cp_stack)
    assert _cli(cp_stack, monkeypatch, "roll-runtime", cp_stack.household.id) == 0

    with pytest.raises(SystemExit) as exit_info:
        _cli(cp_stack, monkeypatch, "roll-runtime", cp_stack.household.id)
    assert "already in flight" in str(exit_info.value)

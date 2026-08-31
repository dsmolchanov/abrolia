"""The predicate that decides whether a control plane may be deployed onto.

Production deploys were dead for nine days on a loop this file exists to keep
closed. `/readyz` reports `backup_stale` once the newest archive is older than
26 hours; the archive is written at CONTAINER START and, while the service
runs, cannot be written at all — `abrolia-control-plane backup` takes the
process lock and refuses with "Stop the service first."

So the only thing that refreshes the backup is a deploy, and the deploy gate
required a fresh backup. Last successful deploy 2026-08-22 09:34Z; first
failure 26.4 hours later; fourteen consecutive failures after that, none of
which reached `flyctl` at all.

The filter is read from the same file `.github/workflows/deploy-production.yml`
passes to `jq`, so a change to one is a change to both. Asserting a copy of the
expression here would pin a duplicate and let the real gate drift — the C5a
lesson, one layer out.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
FILTER = REPOSITORY / "deploy" / "control-plane" / "readyz-deploy-gate.jq"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="the deploy gate is evaluated by jq"
)


def _deployable(payload: dict) -> bool:
    """Exactly what the workflow runs: `jq -e -f <filter>`."""
    result = subprocess.run(
        ["jq", "-e", "-f", str(FILTER)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )
    assert result.returncode in (0, 1), result.stderr
    return result.returncode == 0


def test_a_ready_control_plane_is_deployable() -> None:
    assert _deployable({"status": "ready", "blockers": []})


def test_a_stale_backup_alone_does_not_hold_the_deploy() -> None:
    """The loop, as one case.

    This is the exact shape production served throughout the outage, and the
    deploy that would have fixed it is the deploy this used to refuse.
    """
    assert _deployable({"status": "not_ready", "blockers": ["backup_stale"]})


@pytest.mark.parametrize(
    "blockers",
    [
        ["database_unavailable"],
        ["volume_full"],
        ["workers_paused"],
        # The one that matters most: a stale backup does NOT excuse the others
        # travelling with it.
        ["backup_stale", "database_unavailable"],
        ["database_unavailable", "backup_stale"],
    ],
    ids=["database", "volume", "workers", "stale-plus-database", "database-plus-stale"],
)
def test_every_other_blocker_still_holds_the_deploy(blockers: list[str]) -> None:
    """A deploy makes these worse, not better, so the gate keeps refusing."""
    assert not _deployable({"status": "not_ready", "blockers": blockers})


def test_not_ready_without_a_reason_is_refused() -> None:
    """An incoherent answer is not an affirmative one.

    `not_ready` with nothing to point at means this is not the control plane
    replying, or it is replying with something this gate does not understand.
    Either way it is not a green light.
    """
    assert not _deployable({"status": "not_ready", "blockers": []})
    assert not _deployable({"status": "not_ready"})


def test_the_payload_production_actually_served_is_accepted() -> None:
    """Captured from `https://app.abrolia.com/readyz` on 2026-08-31, verbatim.

    A synthesized fixture would prove the predicate reads the shape this test
    imagines. This one proves it reads the shape that was on the wire while
    fourteen deploys were refused.
    """
    served = {
        "status": "not_ready",
        "mode": "synthetic-only",
        "checks": {
            "database": "ok",
            "volume": "ok",
            "workers": "running",
            "backup": "stale",
        },
        "metrics": {"backup_age_seconds": 815489.4482495785, "pending_jobs": 0},
        "blockers": ["backup_stale"],
    }
    assert _deployable(served)

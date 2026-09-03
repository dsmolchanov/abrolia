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


@pytest.mark.parametrize(
    "payload",
    [
        # No status at all: this is not a readiness answer.
        {"blockers": ["backup_stale"]},
        # A status this gate does not know. `backup_stale` is only benign
        # while the control plane itself says it is merely not ready; a body
        # claiming anything else is claiming something this gate cannot read.
        {"status": "broken", "blockers": ["backup_stale"]},
        {"status": "degraded", "blockers": ["backup_stale"]},
        {"status": None, "blockers": ["backup_stale"]},
        # `ready` is spelled exactly, or not at all.
        {"status": "READY", "blockers": []},
        {"status": "ready ", "blockers": []},
    ],
    ids=[
        "status-missing",
        "status-broken",
        "status-degraded",
        "status-null",
        "ready-wrong-case",
        "ready-trailing-space",
    ],
)
def test_a_body_that_does_not_state_a_readiness_status_is_refused(
    payload: dict,
) -> None:
    """The `backup_stale` exception is not a hole for malformed bodies.

    The exception arm once tested only the blocker list, so a 503 body with no
    `status` — or with one this gate has never heard of — took the benign
    branch. The post-deploy check that used to reject both was replaced by
    this filter, so the permissiveness would have reached a surface that
    previously refused it.
    """
    assert not _deployable(payload)


def test_both_ends_of_the_deploy_read_this_one_filter() -> None:
    """The reason a single file exists, asserted rather than trusted.

    The pre-deploy gate and the post-deploy verification are two consumers of
    one question. They drifted once — the post-deploy copy kept asking
    `.status == "ready"` behind `curl --fail` after the pre-deploy gate was
    fixed — and a deploy that fully succeeded reported failure for nine days.
    A second spelling appearing in the workflow is that defect returning, so
    the count is pinned here.
    """
    workflow = (
        REPOSITORY / ".github" / "workflows" / "deploy-production.yml"
    ).read_text()
    filter_path = "deploy/control-plane/readyz-deploy-gate.jq"

    assert workflow.count(f"jq -e -f {filter_path}") == 2, (
        "both the pre-deploy gate and the post-deploy verification must "
        "evaluate the shared filter"
    )
    # Comments are excluded deliberately: the workflow explains this exact
    # drift by quoting the predicate it used to spell inline, and the prose
    # that records a defect must not read as the defect.
    executable = "\n".join(
        line
        for line in workflow.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert '.status == "ready"' not in executable, (
        "a readiness predicate spelled inline in the workflow is the drift "
        f"this filter exists to prevent — put it in {filter_path}"
    )


def test_a_broken_backup_writer_does_not_hold_the_deploy_either() -> None:
    """Excused for the same reason as `backup_stale`, not a weaker one.

    A deploy cannot fix a directory the service may not write to. Gating on it
    would repeat the nine-day deadlock exactly: refusing the deploy for a
    condition the deploy was never able to clear.
    """
    assert _deployable(
        {"status": "not_ready", "blockers": ["backup_writer_failed"]}
    )
    assert _deployable(
        {"status": "not_ready", "blockers": ["backup_stale", "backup_writer_failed"]}
    )


def test_a_broken_writer_still_does_not_excuse_a_real_blocker() -> None:
    assert not _deployable(
        {
            "status": "not_ready",
            "blockers": ["backup_writer_failed", "database_unavailable"],
        }
    )


def test_the_excused_set_is_exactly_these_four() -> None:
    """What the gate is allowed to ignore, pinned.

    This gate ignoring a condition is how a real problem becomes invisible:
    `backup_stale` stopped blocking deploys for a good reason, the boot archive
    became non-fatal for a good reason, and the combination meant a writer that
    failed at every boot looked like a healthy system for ten days.

    Every exemption shares one property — a deploy cannot clear it, so refusing
    the deploy would deadlock rather than help — and every one is NAMED in the
    readiness payload, which is what makes it visible despite being excused. An
    exemption added without those two properties would recreate the silence, so
    the set is asserted rather than trusted.

    The two allowlist blockers joined on 2026-09-03: an allowlist secret set to
    email addresses refused the boot and took production down. The boot no
    longer refuses, the condition is named, and a deploy cannot fix a secret.
    """
    excused = [
        "backup_stale",
        "backup_writer_failed",
        "real_email_allowlist_invalid",
        "real_email_allowlist_empty",
    ]
    assert _deployable({"status": "not_ready", "blockers": excused})
    for blocker in excused:
        assert _deployable({"status": "not_ready", "blockers": [blocker]}), blocker

    # Anything else, alone or travelling with an excused one, still refuses.
    for blocker in (
        "database_unavailable",
        "volume_unavailable",
        "workers_paused",
        "stale_worker_leases",
        "provider_outcomes_unknown",
        "expired_bootstrap",
        "provider_registry_unavailable",
    ):
        assert not _deployable(
            {"status": "not_ready", "blockers": [blocker]}
        ), f"{blocker} must hold the deploy"
        assert not _deployable(
            {"status": "not_ready", "blockers": [*excused, blocker]}
        ), f"{blocker} must hold the deploy even beside an excused one"


def test_every_excused_blocker_is_one_readiness_can_actually_emit() -> None:
    """An exemption for a name nothing produces would be dead code hiding a typo."""
    from control_plane.observability import HealthSnapshot

    stale = HealthSnapshot(
        database_ok=True,
        volume_ok=True,
        volume_free_bytes=1,
        workers_paused=False,
        pending_jobs=0,
        stale_leases=0,
        unknown_outcomes=0,
        expired_bootstrap_tokens=0,
        backup_age_seconds=10_000_000.0,
        boot_archive_outcome="failed",
    )
    emitted = set(stale.readiness_blockers(maximum_backup_age_seconds=1.0))
    assert {"backup_stale", "backup_writer_failed"} <= emitted

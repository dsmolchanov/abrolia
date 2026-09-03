"""A broken real-email allowlist is named on /readyz, and the service keeps serving.

The boot used to refuse the whole configuration on one bad allowlist entry.
On 2026-09-03 that turned an operator typo — two email addresses instead of
household ids — into an outage. Now the list fails closed per household, as
it always did, and the condition is a readiness blocker the deploy gate
excuses and the operator can see.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from control_plane.api.app import create_app
from control_plane.config import ControlPlaneConfig
from control_plane.container import ControlPlaneContainer


def _readyz(tmp_path: Path, **overrides) -> dict:
    config = replace(
        ControlPlaneConfig.for_test(tmp_path),
        real_email_enabled=True,
        nerve_base_url="https://nerve.example.test",
        nerve_admin_key="synthetic-admin-key",
        nerve_platform_org_id="20000000-0000-4000-8000-000000000001",
        nerve_platform_domain_id="30000000-0000-4000-8000-000000000001",
        **overrides,
    )
    active = ControlPlaneContainer.build(config)
    try:
        app = create_app(active_container=active)
        with TestClient(app, base_url=config.public_origin) as client:
            return client.get("/readyz").json()
    finally:
        active.close()


def test_an_invalid_or_empty_allowlist_is_a_named_blocker(tmp_path: Path) -> None:
    body = _readyz(
        tmp_path,
        real_email_household_allowlist=frozenset(),
        real_email_household_allowlist_invalid=2,
    )
    assert body["status"] == "not_ready"
    assert "real_email_allowlist_invalid" in body["blockers"]
    assert "real_email_allowlist_empty" in body["blockers"]
    # Still serving: the payload exists, the database and workers report.
    assert body["checks"]["database"] == "ok"


def test_a_good_allowlist_raises_no_blocker(tmp_path: Path) -> None:
    body = _readyz(
        tmp_path,
        real_email_household_allowlist=frozenset({"10000000-0000-4000-8000-000000000001"}),
    )
    assert not any(blocker.startswith("real_email_allowlist") for blocker in body["blockers"])

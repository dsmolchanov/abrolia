from __future__ import annotations

import io
import json
import os
import sqlite3
import time

import pytest

from control_plane.api.app import MAXIMUM_BACKUP_AGE_SECONDS
from control_plane.models import StepKind
from control_plane.observability import (
    HealthReporter,
    StructuredLogger,
    UnsafeTelemetry,
    safe_event,
)

BASE_TIME = 1_800_000_000.0


def test_structured_telemetry_accepts_only_allowlisted_non_pii_fields() -> None:
    event = safe_event(
        "provisioning_settled",
        workflow_id="synthetic-workflow",
        step_kind="email_identity",
        job_id="synthetic-job",
        status="succeeded",
        duration_ms=125,
        attempts=1,
        provider="fake-email",
        config_revision=3,
    )
    assert event["status"] == "succeeded"
    assert set(event) == {
        "event",
        "workflow_id",
        "step_kind",
        "job_id",
        "status",
        "duration_ms",
        "attempts",
        "provider",
        "config_revision",
    }


def test_telemetry_phone_canary_does_not_match_numeric_uuid_fragments() -> None:
    event = safe_event(
        "provisioning_settled",
        workflow_id="899dbb72-c7e5-4078-b879-dc6abdb418c8",
        job_id="12bec823-aca3-4218-b458-28ebfdad2a31",
        status="succeeded",
    )

    assert event["workflow_id"] == "899dbb72-c7e5-4078-b879-dc6abdb418c8"


@pytest.mark.parametrize(
    "fields",
    [
        {"email": "pii-owner@family.test"},
        {"error_code": "pii-owner@family.test"},
        {"error_code": "+999 555 0101"},
        {"error_code": "x" * 48},
        {"refresh_token": "credential-canary"},
        {"provider_body": {"status": "rejected"}},
    ],
)
def test_telemetry_rejects_unknown_pii_phone_and_credential_canaries(fields) -> None:
    canaries = json.dumps(fields, sort_keys=True)
    with pytest.raises(UnsafeTelemetry) as raised:
        safe_event("provider_failure", **fields)
    message = str(raised.value)
    for value in fields.values():
        if isinstance(value, str) and value not in fields:
            assert value not in message
    assert "pii-owner@family.test" not in message
    assert "+999 555 0101" not in message
    assert "credential-canary" not in message
    assert canaries not in message


def test_logger_never_emits_rejected_canary_payload() -> None:
    stream = io.StringIO()
    logger = StructuredLogger(stream)
    logger.emit(
        "job_finished",
        workflow_id="synthetic-workflow",
        job_id="synthetic-job",
        status="succeeded",
    )
    with pytest.raises(UnsafeTelemetry):
        logger.emit(
            "job_failed",
            job_id="synthetic-job",
            error_code="log-canary@family.test",
        )
    output = stream.getvalue()
    assert "job_finished" in output
    assert "log-canary@family.test" not in output
    assert "token" not in output.casefold()


def test_worker_emits_allowlisted_job_metrics_without_request_payload(cp_stack) -> None:
    cp_stack.complete_profile()
    cp_stack.service.select(
        cp_stack.household.id,
        StepKind.EMAIL,
        {"kind": "abrolia_managed", "local_part": "telemetry-payload-canary"},
        context=cp_stack.context(),
        now=BASE_TIME + 1,
    )
    stream = io.StringIO()
    worker = cp_stack.make_worker()
    worker.logger = StructuredLogger(stream)

    result = worker.run_once()

    assert result is not None and result.status == "succeeded"
    event = json.loads(stream.getvalue())
    assert event == {
        "attempts": 1,
        "duration_ms": event["duration_ms"],
        "error_code": None,
        "event": "provisioning_job_finished",
        "job_id": result.job_id,
        "provider": "fake-email",
        "status": "succeeded",
        "step_kind": "email_identity",
        "workflow_id": cp_stack.onboarding.workflow_for_household(
            cp_stack.household.id
        ).id,
    }
    assert event["duration_ms"] >= 0
    assert "telemetry-payload-canary" not in stream.getvalue()


def test_health_reporter_counts_backlog_without_exposing_payloads(cp_stack) -> None:
    workflow = cp_stack.onboarding.workflow_for_household(cp_stack.household.id)
    with cp_stack.database.write() as connection:
        job_ids = []
        for suffix in ("pending", "stale", "unknown"):
            job_id, _ = cp_stack.jobs.create(
                connection,
                household_id=cp_stack.household.id,
                workflow_id=workflow.id,
                kind="email_identity",
                operation="ensure",
                intent_key=f"health-{suffix}",
                request={"opaque": f"health-payload-{suffix}-canary"},
                provider="fake-email",
                now=BASE_TIME,
            )
            job_ids.append(job_id)
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'running', lease_until = ? WHERE id = ?",
            (BASE_TIME - 1, job_ids[1]),
        )
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'outcome_unknown' WHERE id = ?",
            (job_ids[2],),
        )
        manifest = {
            "schema_version": 1,
            "config_revision": 1,
            "household_id": cp_stack.household.id,
        }
        revision = cp_stack.configs.create_revision(
            connection,
            household_id=cp_stack.household.id,
            schema_version=1,
            manifest=manifest,
            manifest_sha256="e" * 64,
            now=BASE_TIME - 100,
        )
        cp_stack.configs.issue_bootstrap(
            connection,
            raw_token="expired-health-bootstrap-token",
            household_id=cp_stack.household.id,
            runtime_ref="synthetic-runtime:health",
            revision=revision.revision,
            manifest_sha256=revision.manifest_sha256,
            expires_at=BASE_TIME - 1,
            now=BASE_TIME - 100,
        )

    snapshot = HealthReporter(cp_stack.database).snapshot(
        backup_completed_at=BASE_TIME - 600,
        now=BASE_TIME,
    )
    assert snapshot.database_ok
    assert snapshot.volume_ok
    assert snapshot.volume_free_bytes and snapshot.volume_free_bytes > 0
    assert not snapshot.workers_paused
    assert snapshot.pending_jobs == 1
    assert snapshot.stale_leases == 1
    assert snapshot.unknown_outcomes == 1
    assert snapshot.expired_bootstrap_tokens == 1
    assert snapshot.backup_age_seconds == 600
    assert snapshot.readiness_blockers(maximum_backup_age_seconds=3600) == (
        "stale_worker_leases",
        "provider_outcomes_unknown",
        "expired_bootstrap",
    )
    encoded = json.dumps(snapshot.__dict__, sort_keys=True)
    assert "health-payload" not in encoded
    assert "expired-health-bootstrap-token" not in encoded


def test_public_http_redirects_but_private_flycast_http_is_routed(api_harness) -> None:
    public = api_harness.client.get(
        "/healthz",
        headers={
            "host": "abrolia-control-plane-synthetic.fly.dev",
            "x-forwarded-proto": "http",
        },
        follow_redirects=False,
    )
    private = api_harness.client.get(
        "/healthz",
        headers={
            "host": "abrolia-control-plane-synthetic.flycast",
            "x-forwarded-proto": "http",
        },
    )

    assert public.status_code == 308
    assert public.headers["location"].startswith(
        "https://abrolia-control-plane-synthetic.fly.dev/"
    )
    assert private.status_code == 200


def test_health_endpoints_report_safe_operational_state(api_harness) -> None:
    healthy = api_harness.client.get("/healthz")
    ready = api_harness.client.get("/readyz")
    assert healthy.status_code == ready.status_code == 200
    assert healthy.json()["status"] == "healthy"
    payload = ready.json()
    assert payload["status"] == "ready"
    assert payload["mode"] == "synthetic-only"
    assert payload["checks"] == {
        "database": "ok",
        "volume": "ok",
        "workers": "running",
        "backup": "not_observed",
        "providers": api_harness.container.providers.health(),
    }
    assert payload["metrics"]["volume_free_bytes"] > 0
    assert payload["metrics"]["backup_age_seconds"] is None
    assert payload["metrics"]["pending_jobs"] == 0
    assert payload["metrics"]["stale_leases"] == 0
    assert payload["metrics"]["unknown_outcomes"] == 0
    assert payload["metrics"]["expired_bootstrap"] == 0
    assert payload["blockers"] == []
    provider_health = api_harness.container.providers.health()
    assert provider_health
    assert set(provider_health.values()) == {"configured"}
    encoded = json.dumps(
        {"healthy": healthy.json(), "ready": ready.json(), "providers": provider_health},
        sort_keys=True,
    )
    assert "provider-body-canary" not in encoded
    assert "credential-value-canary" not in encoded


def test_ready_is_non_200_for_paused_workers_and_stale_backup(api_harness) -> None:
    api_harness.container.database.pause_workers("synthetic restore rehearsal")
    paused = api_harness.client.get("/readyz")
    assert paused.status_code == 503
    assert paused.json()["status"] == "not_ready"
    assert "workers_paused" in paused.json()["blockers"]
    assert api_harness.client.get("/healthz").status_code == 200
    api_harness.container.database.resume_workers()

    backup_directory = api_harness.container.database.path.parent / "backups"
    backup_directory.mkdir()
    archive = backup_directory / "synthetic-health.cpb"
    archive.write_bytes(b"opaque-test-archive")
    stale_time = time.time() - MAXIMUM_BACKUP_AGE_SECONDS - 60
    os.utime(archive, (stale_time, stale_time))

    stale = api_harness.client.get("/readyz")
    assert stale.status_code == 503
    assert stale.json()["checks"]["backup"] == "stale"
    assert stale.json()["metrics"]["backup_age_seconds"] >= MAXIMUM_BACKUP_AGE_SECONDS
    assert "backup_stale" in stale.json()["blockers"]


def test_health_reporter_marks_database_metrics_unknown_on_query_failure(
    cp_stack, monkeypatch
) -> None:
    def fail_query(*_args, **_kwargs):
        raise sqlite3.OperationalError("synthetic database failure")

    monkeypatch.setattr(cp_stack.database, "query_one", fail_query)
    snapshot = HealthReporter(cp_stack.database).snapshot(now=BASE_TIME)
    assert not snapshot.database_ok
    assert snapshot.pending_jobs is None
    assert snapshot.stale_leases is None
    assert snapshot.unknown_outcomes is None
    assert snapshot.expired_bootstrap_tokens is None
    assert "database_unavailable" in snapshot.liveness_blockers()


def test_provider_health_failure_does_not_echo_exception(api_harness, monkeypatch) -> None:
    def fail_health():
        raise RuntimeError("credential-value-canary")

    monkeypatch.setattr(api_harness.container.providers, "health", fail_health)
    response = api_harness.client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["providers"] == {"registry": "unavailable"}
    assert "provider_registry_unavailable" in response.json()["blockers"]
    assert "credential-value-canary" not in response.text

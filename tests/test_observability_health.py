"""Observability E7: /health fields and structured logs without content."""

import io
import json
import logging

import pytest

from hermes_cloud.core.observability import ALERTS, RuntimeStructuredLogger, emit_alert
from hermes_cloud.runtime.service import RuntimeService


def test_the_forwarding_alert_is_registered_and_hashes_the_household(caplog) -> None:
    """An alert that exists only as a dictionary entry is documentation."""
    assert "gmail_forwarding_stale" in ALERTS
    with caplog.at_level(logging.WARNING):
        emit_alert(
            logging.getLogger("test"),
            "gmail_forwarding_stale",
            env={"ABROLIA_HMAC_KEY": "test-key-1234567890abcdef"},
            household_id="hh-1",
            misses="2",
        )
    line = caplog.records[-1].getMessage()
    assert "ALERT gmail_forwarding_stale" in line and "misses=2" in line
    assert "hh-1" not in line and "household_id_hash=" in line


def test_an_unknown_alert_name_still_raises() -> None:
    with pytest.raises(KeyError):
        emit_alert(logging.getLogger("test"), "gmail_forwarding_stal")


def test_runtime_health_fields(tmp_path) -> None:
    service = RuntimeService(env={"HERMES_DB": str(tmp_path / "h.db"), "TELEGRAM_BOT_TOKEN": "123:abc"})
    probe = service.health()
    payload = probe.payload
    for field in ("nerve_key_ok", "telegram_ok", "wa_instance_ok", "google_grant_ok", "db_ok", "backup_age_hours"):
        assert field in payload
    assert payload["telegram_ok"] is True
    assert payload["db_ok"] in (True, False)


def test_structured_logs_have_required_fields_and_no_content(tmp_path) -> None:
    stream = io.StringIO()
    logger = RuntimeStructuredLogger(stream, hmac_key=b"test-key-1234567890abcdef12345678")
    logger.emit(level="info", route="/health", status=200, latency_ms=12, household_id="hh1", request_id="req1")
    line = stream.getvalue().strip()
    payload = json.loads(line)
    for field in ("timestamp", "level", "household_id_hash", "request_id", "route", "status", "latency_ms"):
        assert field in payload
    assert "content" not in payload and "prompt" not in payload and "secret" not in payload
    # household_id is hashed, not raw
    assert payload["household_id_hash"] != "hh1"
    assert len(payload["household_id_hash"]) == 16

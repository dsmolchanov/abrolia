"""Authenticated private export/delete boundary of one household runtime."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from control_plane.privacy.consent import consent_version_and_sha
from hermes_cloud.core.db import open_database
from hermes_cloud.core.dsar import is_deleted
from hermes_cloud.core.runtime_manifest import compute_config_sha256, parse_runtime_manifest
from hermes_cloud.runtime.bootstrap import ActivationState, write_activation_state
from hermes_cloud.runtime.service import (
    ENV_RUNTIME_DSAR_TOKEN,
    RuntimeService,
)

RUNTIME_REF = "abrolia-hh-aaaaaaaaaaaaaaaaaaaaaaaaaa"
DSAR_TOKEN = "synthetic-runtime-dsar-token-canary"


_RESTRICTION_VERSION, _RESTRICTION_SHA = consent_version_and_sha(
    "special_category_content_restriction"
)


def _manifest_toml() -> str:
    body = f'''\
schema_version = 1
household_id = "33333333-3333-4333-8333-333333333333"
config_revision = 4
family_language = "English"
timezone = "Europe/Prague"
country_code = "CZ"
residency_mode = "eu-app"

[actors]
owner = "synthetic-owner"
family = ["synthetic-owner"]
guests = []

[channels]
primary = "telegram"

[[channel_bindings]]
channel = "telegram"
actor_id = "synthetic-owner"
chat_id = "synthetic-chat"
verified = true

[email]
agent_inbox = "runtime@abrolia.test"
fallback = "owner@example.test"

[consent]
authority = "control_plane"
enforcement = "required"
required_purposes = ["special_category_content_restriction"]

[[consent.receipts]]
receipt_id = "10000000-0000-4000-8000-000000000033"
purpose = "special_category_content_restriction"
text_version = "{_RESTRICTION_VERSION}"
text_sha256 = "{_RESTRICTION_SHA}"
'''
    digest = compute_config_sha256(body)
    return body.replace("schema_version = 1\n", f'schema_version = 1\nconfig_sha256 = "{digest}"\n')


def _active_runtime(tmp_path: Path) -> tuple[RuntimeService, Path]:
    manifest_path = tmp_path / "household.toml"
    activation_path = tmp_path / "runtime-activation.json"
    database_path = tmp_path / "hermes.db"
    content = _manifest_toml()
    manifest = parse_runtime_manifest(content)
    manifest_path.write_text(content, encoding="utf-8")
    write_activation_state(
        activation_path,
        ActivationState(
            status="active",
            runtime_ref=RUNTIME_REF,
            household_id=manifest.household_id,
            config_revision=manifest.config_revision,
            config_sha256=manifest.config_sha256,
            updated_at=123.0,
        ),
    )
    with open_database(database_path):
        pass
    service = RuntimeService(
        manifest_path=manifest_path,
        activation_path=activation_path,
        runtime_ref=RUNTIME_REF,
        env={
            ENV_RUNTIME_DSAR_TOKEN: DSAR_TOKEN,
            "HERMES_DB": str(database_path),
        },
    )
    return service, database_path


def _request(
    service: RuntimeService, path: str, *, token: str | None = DSAR_TOKEN
) -> tuple[int, dict]:
    captured: dict[str, object] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        captured["status"] = int(status.split()[0])
        captured["headers"] = dict(headers)

    environ = {"PATH_INFO": path, "REQUEST_METHOD": "POST"}
    if token is not None:
        environ["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    body = b"".join(service(environ, start_response))
    return int(captured["status"]), json.loads(body)


def test_runtime_dsar_requires_exact_bearer_and_never_echoes_it(tmp_path: Path) -> None:
    service, database_path = _active_runtime(tmp_path)

    missing_status, missing = _request(
        service, "/internal/v1/dsar/export", token=None
    )
    wrong_status, wrong = _request(
        service, "/internal/v1/dsar/delete", token="wrong-runtime-token"
    )

    assert missing_status == wrong_status == 401
    assert missing == wrong == {"status": "unauthorized"}
    assert DSAR_TOKEN not in json.dumps({"missing": missing, "wrong": wrong})
    assert not service.deletion_marker.exists()
    with open_database(database_path) as database:
        assert not is_deleted(database)


def test_runtime_export_then_delete_is_idempotent_and_fail_closed(tmp_path: Path) -> None:
    service, database_path = _active_runtime(tmp_path)

    export_status, exported = _request(service, "/internal/v1/dsar/export")
    assert export_status == 200
    assert set(exported["tables"]) >= {"events", "approvals", "effects"}
    assert "token" not in exported["tables"]

    delete_status, deleted = _request(service, "/internal/v1/dsar/delete")
    replay_status, replayed = _request(service, "/internal/v1/dsar/delete")
    gone_status, gone = _request(service, "/internal/v1/dsar/export")

    assert delete_status == replay_status == 200
    assert deleted == replayed == {"state": "absent"}
    assert gone_status == 410
    assert gone == {"status": "runtime_deleted"}
    assert stat.S_IMODE(service.deletion_marker.stat().st_mode) == 0o600
    assert service.readyz().status_code == 503
    assert service.readyz().payload["reason"] == "runtime_deleted"
    with open_database(database_path) as database:
        assert is_deleted(database)

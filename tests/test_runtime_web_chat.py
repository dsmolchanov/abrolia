"""Authenticated /internal/v1/web/chat route: the runtime's own chat turn.

The control plane authenticates the human and proxies here with the same
runtime bearer as DSAR; this side owns the RunContext, the cost cap and the
first ToolLoop a runtime process has ever built.
"""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from control_plane.privacy.consent import consent_version_and_sha
from hermes_cloud.channels.web import DEGRADED_MESSAGE
from hermes_cloud.core.db import open_database
from hermes_cloud.core.runtime_manifest import (
    compute_config_sha256,
    parse_runtime_manifest,
)
from hermes_cloud.core.usage import UsageStore, today_utc
from hermes_cloud.runtime.bootstrap import ActivationState, write_activation_state
from hermes_cloud.runtime.service import ENV_RUNTIME_DSAR_TOKEN, RuntimeService

RUNTIME_REF = "abrolia-hh-aaaaaaaaaaaaaaaaaaaaaaaaaa"
CHAT_TOKEN = "synthetic-runtime-dsar-token-canary"
HOUSEHOLD_ID = "33333333-3333-4333-8333-333333333333"

_RESTRICTION_VERSION, _RESTRICTION_SHA = consent_version_and_sha(
    "special_category_content_restriction"
)


def _manifest_toml() -> str:
    body = f'''\
schema_version = 1
household_id = "{HOUSEHOLD_ID}"
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
    return body.replace(
        "schema_version = 1\n",
        f'schema_version = 1\nconfig_sha256 = "{digest}"\n',
    )


class StubLoop:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []

    def run(self, context, text, *, history=None, cost_guard=None):
        self.calls.append((context.role, context.actor_id, context.chat_id, text))
        # The guard is what accounts now — the channel no longer records after
        # the run, because a turn makes many provider calls and only the guard
        # sees each one.
        if cost_guard is not None:
            cost_guard.record(
                SimpleNamespace(usage=SimpleNamespace(input_tokens=11, output_tokens=7))
            )
        return SimpleNamespace(text="готово", input_tokens=11, output_tokens=7)


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
        env={ENV_RUNTIME_DSAR_TOKEN: CHAT_TOKEN, "HERMES_DB": str(database_path)},
    )
    return service, database_path


def _chat(service: RuntimeService, *, token=None, payload=None) -> tuple[int, dict]:
    captured: dict[str, object] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        captured["status"] = int(status.split()[0])

    body_bytes = json.dumps(payload or {"text": "привет", "role": "owner"}).encode()
    environ = {
        "PATH_INFO": "/internal/v1/web/chat",
        "REQUEST_METHOD": "POST",
        "CONTENT_LENGTH": str(len(body_bytes)),
        "wsgi.input": BytesIO(body_bytes),
    }
    if token is not False:
        environ["HTTP_AUTHORIZATION"] = f"Bearer {CHAT_TOKEN if token is None else token}"
    body = b"".join(service(environ, start_response))
    return int(captured["status"]), json.loads(body)


def test_web_chat_fails_closed_without_exact_bearer(tmp_path: Path) -> None:
    service, database_path = _active_runtime(tmp_path)
    unconfigured = RuntimeService(
        manifest_path=service.manifest_path,
        activation_path=service.activation_path,
        runtime_ref=RUNTIME_REF,
        env={"HERMES_DB": str(database_path)},
    )

    missing_status, missing_payload = _chat(unconfigured, token=False)
    wrong_status, wrong_payload = _chat(service, token="wrong-token")

    # Unconfigured hides the route entirely (404); a configured runtime
    # refuses a wrong bearer with 401.
    assert missing_status == 404
    assert missing_payload == {"status": "not_found"}
    assert wrong_status == 401
    assert wrong_payload == {"status": "unauthorized"}
    assert CHAT_TOKEN not in json.dumps([missing_payload, wrong_payload])


def test_web_chat_validates_text_before_any_model_work(tmp_path: Path) -> None:
    service, _ = _active_runtime(tmp_path)
    stub = StubLoop()
    service._web_chat_loop = lambda database, config: stub

    empty_status, empty_payload = _chat(service, payload={"text": "   ", "role": "owner"})
    long_status, long_payload = _chat(
        service, payload={"text": "а" * 2001, "role": "owner"}
    )
    no_role_status, no_role_payload = _chat(service, payload={"text": "привет"})

    assert empty_status == 400 and empty_payload == {"status": "text_required"}
    assert long_status == 400 and long_payload == {"status": "text_too_long"}
    assert no_role_status == 403
    assert no_role_payload == {"status": "owner_role_required"}
    assert stub.calls == []


def test_web_chat_runs_the_dialogue_loop_and_records_usage(tmp_path: Path) -> None:
    service, database_path = _active_runtime(tmp_path)
    stub = StubLoop()
    service._web_chat_loop = lambda database, config: stub

    status, payload = _chat(service)

    assert status == 200
    assert payload == {"reply": "готово"}
    assert stub.calls == [("owner", "synthetic-owner", "web-chat", "привет")]
    with open_database(database_path) as database:
        row = UsageStore(database).get(HOUSEHOLD_ID, today_utc())
    assert row is not None
    assert (row.prompt_tokens, row.completion_tokens) == (11, 7)


def test_web_chat_degrades_over_budget_without_calling_the_model(tmp_path: Path) -> None:
    service, database_path = _active_runtime(tmp_path)
    stub = StubLoop()
    service._web_chat_loop = lambda database, config: stub
    with open_database(database_path) as database:
        UsageStore(database).record(
            HOUSEHOLD_ID, today_utc(), prompt_tokens=2_000_000, completion_tokens=0
        )

    status, payload = _chat(service)

    assert status == 200
    assert payload == {"reply": DEGRADED_MESSAGE}
    assert stub.calls == []


def test_web_chat_reports_not_ready_without_activation(tmp_path: Path) -> None:
    database_path = tmp_path / "hermes.db"
    with open_database(database_path):
        pass
    service = RuntimeService(
        manifest_path=tmp_path / "missing.toml",
        activation_path=tmp_path / "missing.json",
        runtime_ref=RUNTIME_REF,
        env={ENV_RUNTIME_DSAR_TOKEN: CHAT_TOKEN, "HERMES_DB": str(database_path)},
    )
    stub = StubLoop()
    service._web_chat_loop = lambda database, config: stub

    status, payload = _chat(service)

    assert status == 503
    assert payload == {"status": "runtime_not_ready"}
    assert stub.calls == []

"""Provisioned runtime stays alive but not ready until matching activation."""

from __future__ import annotations

import base64
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cloud.core.db import open_database
from hermes_cloud.core.runtime_manifest import compute_config_sha256, parse_runtime_manifest
from hermes_cloud.runtime import service as runtime_service_module
from hermes_cloud.runtime.bootstrap import (
    ActivationState,
    BootstrapClaim,
    BootstrapError,
    RuntimeBootstrapper,
    atomic_write,
    load_activation_state,
    write_activation_state,
)
from hermes_cloud.runtime.service import RuntimeNotReady, RuntimeService

RUNTIME_REF = "fly:abrolia-hh-test"
TOKEN = "synthetic-bootstrap-token-canary"


def manifest_toml(
    *,
    with_email_binding: bool = False,
    email_provider: str = "nerve-managed",
) -> str:
    body = '''\
schema_version = 1
household_id = "33333333-3333-4333-8333-333333333333"
config_revision = 4
family_language = "English"
timezone = "Europe/Prague"
country_code = "CZ"
residency_mode = "eu-app"

[actors]
owner = "owner-actor"
family = ["owner-actor"]
guests = []

[channels]
primary = "telegram"

[[channel_bindings]]
channel = "telegram"
actor_id = "owner-actor"
chat_id = "telegram-test-chat"
verified = true

[email]
agent_inbox = "runtime@abrolia.test"
fallback = "owner@example.test"
'''
    if with_email_binding:
        binding_ref = (
            "email-identity-1"
            if email_provider == "gmail"
            else '{"org_id":"org-1","inbox_id":"inbox-1"}'
        )
        body += f"""\
provider_kind = "{email_provider}"
provider_binding_ref = '{binding_ref}'
secret_binding_ref = "HERMES_EMAIL_BINDING"
"""
    digest = compute_config_sha256(body)
    return body.replace("schema_version = 1\n", f'schema_version = 1\nconfig_sha256 = "{digest}"\n')


class FakeBootstrapClient:
    def __init__(self, content: str) -> None:
        self.manifest = parse_runtime_manifest(content)
        self.content = content
        self.claims = 0
        self.activations = []
        self.acknowledgements = []

    def claim(
        self,
        token: str,
        *,
        household_id: str,
        runtime_ref: str,
        config_revision: int,
    ) -> BootstrapClaim:
        assert token == TOKEN
        assert household_id == self.manifest.household_id
        assert config_revision == self.manifest.config_revision
        self.claims += 1
        return BootstrapClaim(
            runtime_ref=runtime_ref,
            household_id=self.manifest.household_id,
            config_revision=self.manifest.config_revision,
            config_sha256=self.manifest.config_sha256,
            manifest_toml=self.content,
        )

    def activate(self, token: str, receipt):
        assert token == TOKEN
        self.activations.append(receipt)
        return receipt

    def acknowledge(self, token: str, receipt):
        assert token == TOKEN
        self.acknowledgements.append(receipt)
        return receipt


def _binding(client: FakeBootstrapClient) -> dict[str, object]:
    return {
        "household_id": client.manifest.household_id,
        "config_revision": client.manifest.config_revision,
    }


def test_health_is_live_while_readiness_is_fail_closed(tmp_path: Path) -> None:
    service = RuntimeService(
        manifest_path=tmp_path / "household.toml",
        activation_path=tmp_path / "activation.json",
        runtime_ref=RUNTIME_REF,
        env={},
    )

    assert service.healthz().status_code == 200
    assert service.readyz().status_code == 503
    assert service.can_start_workers is False
    with pytest.raises(RuntimeNotReady):
        service.require_ready()


def test_bootstrap_atomically_activates_matching_manifest(tmp_path: Path) -> None:
    manifest_path = tmp_path / "household.toml"
    activation_path = tmp_path / "activation.json"
    client = FakeBootstrapClient(manifest_toml())
    bootstrapper = RuntimeBootstrapper(
        client,
        runtime_ref=RUNTIME_REF,
        **_binding(client),
        manifest_path=manifest_path,
        activation_path=activation_path,
        env={},
        clock=lambda: 123.0,
    )

    manifest = bootstrapper.run(TOKEN)

    assert client.claims == 1 and len(client.activations) == 1
    assert len(client.acknowledgements) == 1
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(activation_path.stat().st_mode) == 0o600
    assert TOKEN not in manifest_path.read_text()
    assert TOKEN not in activation_path.read_text()
    assert load_activation_state(activation_path).status == "active"
    service = RuntimeService(
        manifest_path=manifest_path,
        activation_path=activation_path,
        runtime_ref=RUNTIME_REF,
        env={},
    )
    assert service.readyz().status_code == 200
    assert service.require_ready() == manifest
    assert bootstrapper.run("") == manifest
    assert bootstrapper.run(TOKEN) == manifest
    assert client.claims == 1
    assert len(client.activations) == 1
    assert len(client.acknowledgements) == 2


def test_activating_state_resumes_activate_without_second_claim(tmp_path: Path) -> None:
    content = manifest_toml()
    manifest = parse_runtime_manifest(content)
    manifest_path = atomic_write(tmp_path / "household.toml", content.encode())
    activation_path = tmp_path / "activation.json"
    write_activation_state(
        activation_path,
        ActivationState(
            status="activating",
            runtime_ref=RUNTIME_REF,
            household_id=manifest.household_id,
            config_revision=manifest.config_revision,
            config_sha256=manifest.config_sha256,
            updated_at=1.0,
        ),
    )
    client = FakeBootstrapClient(content)

    RuntimeBootstrapper(
        client,
        runtime_ref=RUNTIME_REF,
        manifest_path=manifest_path,
        activation_path=activation_path,
        env={},
    ).run(TOKEN)

    assert client.claims == 0
    assert len(client.activations) == 1
    assert len(client.acknowledgements) == 1
    assert load_activation_state(activation_path).status == "active"


def test_revision_mismatch_never_becomes_ready(tmp_path: Path) -> None:
    content = manifest_toml()
    manifest = parse_runtime_manifest(content)
    manifest_path = atomic_write(tmp_path / "household.toml", content.encode())
    activation_path = tmp_path / "activation.json"
    write_activation_state(
        activation_path,
        ActivationState(
            status="active",
            runtime_ref=RUNTIME_REF,
            household_id=manifest.household_id,
            config_revision=manifest.config_revision + 1,
            config_sha256=manifest.config_sha256,
            updated_at=1.0,
        ),
    )

    service = RuntimeService(
        manifest_path=manifest_path,
        activation_path=activation_path,
        runtime_ref=RUNTIME_REF,
        env={},
    )

    assert service.readyz().status_code == 503
    assert service.readyz().payload["reason"] == "revision_mismatch"
    with pytest.raises(RuntimeNotReady):
        service.require_ready()


def test_readiness_materializes_public_email_binding_without_exposing_secrets(
    tmp_path: Path,
) -> None:
    content = manifest_toml(with_email_binding=True)
    manifest = parse_runtime_manifest(content)
    manifest_path = atomic_write(tmp_path / "household.toml", content.encode())
    activation_path = tmp_path / "activation.json"
    write_activation_state(
        activation_path,
        ActivationState(
            status="active",
            runtime_ref=RUNTIME_REF,
            household_id=manifest.household_id,
            config_revision=manifest.config_revision,
            config_sha256=manifest.config_sha256,
            updated_at=1.0,
        ),
    )
    service = RuntimeService(
        manifest_path=manifest_path,
        activation_path=activation_path,
        runtime_ref=RUNTIME_REF,
        env={
            "HERMES_DB": str(tmp_path / "hermes.db"),
            "HERMES_EMAIL_BINDING": json.dumps(
                {"api_key": "synthetic-key", "webhook_signing_key": "synthetic-signing"}
            ),
        },
    )

    probe = service.readyz()

    assert probe.status_code == 200
    assert probe.payload["email_provider"] == "nerve-managed"
    assert probe.payload["email_binding_revision"] == manifest.config_revision
    assert "runtime@abrolia.test" not in str(probe.payload)
    assert "HERMES_EMAIL_BINDING" not in str(probe.payload)


def test_nerve_readiness_fails_closed_without_credential_bundle(tmp_path: Path) -> None:
    content = manifest_toml(with_email_binding=True)
    manifest = parse_runtime_manifest(content)
    manifest_path = atomic_write(tmp_path / "household.toml", content.encode())
    activation_path = tmp_path / "activation.json"
    write_activation_state(
        activation_path,
        ActivationState(
            status="active",
            runtime_ref=RUNTIME_REF,
            household_id=manifest.household_id,
            config_revision=manifest.config_revision,
            config_sha256=manifest.config_sha256,
            updated_at=1.0,
        ),
    )
    service = RuntimeService(
        manifest_path=manifest_path,
        activation_path=activation_path,
        runtime_ref=RUNTIME_REF,
        env={"HERMES_DB": str(tmp_path / "hermes.db")},
    )

    assert service.readyz().status_code == 503
    assert service.readyz().payload["reason"] == "email_provider_unavailable"


def test_claim_metadata_must_match_manifest(tmp_path: Path) -> None:
    client = FakeBootstrapClient(manifest_toml())
    original = client.claim

    def wrong_claim(
        token: str,
        *,
        household_id: str,
        runtime_ref: str,
        config_revision: int,
    ) -> BootstrapClaim:
        claim = original(
            token,
            household_id=household_id,
            runtime_ref=runtime_ref,
            config_revision=config_revision,
        )
        return BootstrapClaim(**{**claim.__dict__, "config_revision": 99})

    client.claim = wrong_claim  # type: ignore[method-assign]
    with pytest.raises(BootstrapError, match="metadata"):
        RuntimeBootstrapper(
            client,
            runtime_ref=RUNTIME_REF,
            **_binding(client),
            manifest_path=tmp_path / "household.toml",
            activation_path=tmp_path / "activation.json",
            env={},
        ).run(TOKEN)

    assert not (tmp_path / "household.toml").exists()


def test_wsgi_surface_exposes_only_health_and_readiness(tmp_path: Path) -> None:
    service = RuntimeService(
        manifest_path=tmp_path / "missing.toml",
        activation_path=tmp_path / "missing.json",
        env={},
    )
    seen = []
    body = service(
        {"PATH_INFO": "/healthz", "REQUEST_METHOD": "GET"},
        lambda status, headers: seen.append((status, headers)),
    )

    assert seen[0][0] == "200 OK"
    assert json.loads(body[0]) == {"status": "ok"}


def test_serve_runtime_starts_probe_server_while_bootstrap_is_pending(
    tmp_path: Path,
    monkeypatch,
) -> None:
    seen: dict[str, object] = {}

    class FakeServer:
        def __init__(self, application) -> None:
            self.application = application

        def serve_forever(self) -> None:
            statuses = []
            body = self.application(
                {"PATH_INFO": "/healthz", "REQUEST_METHOD": "GET"},
                lambda status, _headers: statuses.append(status),
            )
            seen["status"] = statuses[0]
            seen["body"] = json.loads(body[0])

        def server_close(self) -> None:
            seen["closed"] = True

    def fake_make_server(host, port, application, *, handler_class):
        seen.update(host=host, port=port, handler_class=handler_class)
        return FakeServer(application)

    monkeypatch.setattr(runtime_service_module, "make_server", fake_make_server)
    monkeypatch.setattr(
        runtime_service_module,
        "_gmail_worker_until_stopped",
        lambda _service, _source, _stop: seen.update(gmail_worker_started=True),
    )
    runtime_service_module.serve_runtime(
        env={
            "HERMES_RUNTIME_HOST": "127.0.0.1",
            "HERMES_RUNTIME_PORT": "8089",
            "HERMES_HOUSEHOLD": str(tmp_path / "missing.toml"),
            "HERMES_ACTIVATION_STATE": str(tmp_path / "missing-activation.json"),
            "HERMES_REQUIRE_MANIFEST": "1",
            "HERMES_BOOTSTRAP_RETRY_SECONDS": "0.1",
        }
    )

    assert seen["host"] == "127.0.0.1" and seen["port"] == 8089
    assert seen["status"] == "200 OK"
    assert seen["body"] == {"status": "ok"}
    assert seen["gmail_worker_started"] is True
    assert seen["closed"] is True


class FakeRuntimeGmailClient:
    def __init__(self) -> None:
        self.profile_id = "100"
        self.history_pages: list[dict] = []
        self.messages: dict[str, dict] = {}
        self.history_starts: list[str] = []
        self.closed = False

    def profile(self):
        return {"historyId": self.profile_id}

    def history(self, start_history_id, page_token=None):
        self.history_starts.append(start_history_id)
        if self.history_pages:
            return self.history_pages.pop(0)
        return {"historyId": start_history_id, "history": []}

    def message(self, message_id):
        return self.messages[message_id]

    def list_inbox(self, page_token=None, *, max_results):
        return {"messages": []}

    def close(self):
        self.closed = True


def _gmail_secret_bundle() -> str:
    return json.dumps(
        {
            "client_id": "client-id.apps.googleusercontent.com",
            "client_secret": "client-secret-canary",
            "refresh_credential": "refresh-credential-canary",
            "provider_subject": "google-subject-1",
            "scopes": [
                "openid",
                "email",
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/gmail.send",
            ],
            "wrapping_key": base64.urlsafe_b64encode(b"k" * 32).rstrip(b"=").decode(),
        }
    )


def test_runtime_gmail_worker_baselines_ingests_and_resumes_after_restart(
    tmp_path: Path,
    monkeypatch,
) -> None:
    content = manifest_toml(with_email_binding=True, email_provider="gmail")
    manifest = parse_runtime_manifest(content)
    manifest_path = atomic_write(tmp_path / "household.toml", content.encode())
    activation_path = tmp_path / "activation.json"
    write_activation_state(
        activation_path,
        ActivationState(
            status="active",
            runtime_ref=RUNTIME_REF,
            household_id=manifest.household_id,
            config_revision=manifest.config_revision,
            config_sha256=manifest.config_sha256,
            updated_at=1.0,
        ),
    )
    env = {
        "HERMES_DB": str(tmp_path / "hermes.db"),
        "HERMES_EMAIL_BINDING": _gmail_secret_bundle(),
    }
    first_client = FakeRuntimeGmailClient()
    first = RuntimeService(
        manifest_path=manifest_path,
        activation_path=activation_path,
        runtime_ref=RUNTIME_REF,
        env=env,
        gmail_client_factory=lambda *_args, **_kwargs: first_client,
    )

    assert first.run_gmail_once() == 0
    raw = (
        b"From: school@example.test\r\nTo: runtime@abrolia.test\r\n"
        b"Message-ID: <runtime-gmail-1@example.test>\r\nSubject: Test\r\n\r\nBody"
    )
    first_client.history_pages = [
        {
            "historyId": "102",
            "history": [{"messagesAdded": [{"message": {"id": "gmail-message-1"}}]}],
        }
    ]
    first_client.messages["gmail-message-1"] = {
        "id": "gmail-message-1",
        "labelIds": ["INBOX"],
        "raw": base64.urlsafe_b64encode(raw).rstrip(b"=").decode(),
    }
    assert first.run_gmail_once() == 1
    first.close()
    assert first_client.closed is True

    second_client = FakeRuntimeGmailClient()
    second_client.profile_id = "102"
    restarted = RuntimeService(
        manifest_path=manifest_path,
        activation_path=activation_path,
        runtime_ref=RUNTIME_REF,
        env=env,
        gmail_client_factory=lambda *_args, **_kwargs: second_client,
    )
    assert restarted.run_gmail_once() == 0
    assert second_client.history_starts == ["102"]
    with open_database(tmp_path / "hermes.db") as database:
        assert database.query_one("SELECT COUNT(*) AS n FROM events")["n"] == 1
        assert database.query_one("SELECT cursor FROM email_sync_state")["cursor"] == "102"
    revoke_calls = []

    class Revoked:
        status_code = 200

    monkeypatch.setattr(
        runtime_service_module.httpx,
        "post",
        lambda *_args, **_kwargs: revoke_calls.append(True) or Revoked(),
    )
    assert restarted._revoke_google_credential(manifest) is True
    assert restarted._revoke_google_credential(manifest) is True
    assert revoke_calls == [True]
    restarted.close()


def test_python_module_entrypoint_is_wired_without_exposing_configuration() -> None:
    env = os.environ.copy()
    env["HERMES_RUNTIME_PORT"] = "invalid-port"
    process = subprocess.run(
        [sys.executable, "-m", "hermes_cloud.runtime.service"],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert process.returncode == 1
    assert process.stdout == ""
    assert process.stderr.strip() == "runtime service failed (BootstrapError)"

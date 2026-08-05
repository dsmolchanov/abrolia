"""Provisioned runtime stays alive but not ready until matching activation."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

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


def manifest_toml(*, with_email_binding: bool = False) -> str:
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
        body += '''\
provider_kind = "nerve-managed"
provider_binding_ref = "identity-1"
secret_binding_ref = "HERMES_EMAIL_BINDING"
'''
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


def test_readiness_materializes_public_email_binding_without_secrets(
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
        env={"HERMES_DB": str(tmp_path / "hermes.db")},
    )

    probe = service.readyz()

    assert probe.status_code == 200
    assert probe.payload["email_provider"] == "nerve-managed"
    assert probe.payload["email_binding_revision"] == manifest.config_revision
    assert "runtime@abrolia.test" not in str(probe.payload)
    assert "HERMES_EMAIL_BINDING" not in str(probe.payload)


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
    assert seen["closed"] is True


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

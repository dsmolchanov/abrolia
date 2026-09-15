"""Provisioned runtime stays alive but not ready until matching activation."""

from __future__ import annotations

import base64
import json
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from control_plane.privacy.consent import consent_version_and_sha
from hermes_cloud.core.runtime_manifest import compute_config_sha256, parse_runtime_manifest
from hermes_cloud.runtime import bootstrap as runtime_bootstrap_module
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


_RESTRICTION_VERSION, _RESTRICTION_SHA = consent_version_and_sha(
    "special_category_content_restriction"
)
_HOUSEHOLD_VERSION, _HOUSEHOLD_SHA = consent_version_and_sha(
    "special_category_household_content"
)


def manifest_toml(
    *,
    revision: int = 4,
    with_email_binding: bool = False,
    email_provider: str = "nerve-managed",
    with_content_restriction: bool = True,
    with_household_consent: bool = True,
    content_restriction_sha: str = _RESTRICTION_SHA,
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
    if with_content_restriction:
        # A real email provider owes the Art 9(2)(a) consent too, and the
        # runtime derives that from the provider rather than reading it back
        # from the manifest — so a fixture naming a real provider must carry it,
        # exactly as a manifest the control plane issues would.
        real_content = email_provider not in {"fake-email", "synthetic"}
        purposes = ['"special_category_content_restriction"']
        if real_content and with_household_consent:
            purposes.append('"special_category_household_content"')
        body += f"""\

[consent]
authority = "control_plane"
enforcement = "required"
required_purposes = [{", ".join(purposes)}]

[[consent.receipts]]
receipt_id = "10000000-0000-4000-8000-000000000031"
purpose = "special_category_content_restriction"
text_version = "{_RESTRICTION_VERSION}"
text_sha256 = "{content_restriction_sha}"
"""
        if real_content and with_household_consent:
            body += f"""\

[[consent.receipts]]
receipt_id = "10000000-0000-4000-8000-000000000034"
purpose = "special_category_household_content"
text_version = "{_HOUSEHOLD_VERSION}"
text_sha256 = "{_HOUSEHOLD_SHA}"
"""
    body = body.replace("config_revision = 4", f"config_revision = {revision}")
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


@pytest.fixture
def active_rollout(tmp_path: Path):
    paths = {
        "manifest_path": tmp_path / "household.toml",
        "activation_path": tmp_path / "activation.json",
    }
    old = FakeBootstrapClient(manifest_toml(revision=1))
    RuntimeBootstrapper(old, runtime_ref=RUNTIME_REF, **_binding(old), **paths, env={}).run(TOKEN)

    def target(revision=2):
        client = FakeBootstrapClient(manifest_toml(revision=revision))
        env = {
            "HERMES_HOUSEHOLD_ID": client.manifest.household_id,
            "HERMES_CONFIG_REVISION": str(revision),
            "HERMES_CONFIG_SHA256": client.manifest.config_sha256,
        }
        bootstrapper = RuntimeBootstrapper(client, runtime_ref=RUNTIME_REF, **paths, env=env)
        service = RuntimeService(runtime_ref=RUNTIME_REF, **paths, env=env)
        return client, bootstrapper, service

    return target


@pytest.mark.parametrize("revision", [2, 3])
@pytest.mark.parametrize("old_status", ["active", "activating"])
def test_runtime_rolls_forward_to_desired_revision(active_rollout, revision, old_status) -> None:
    client, bootstrapper, service = active_rollout(revision)
    state = load_activation_state(bootstrapper.activation_path)
    write_activation_state(
        bootstrapper.activation_path,
        ActivationState(**{**state.__dict__, "status": old_status}),
    )
    assert service.readyz().status_code == 503
    assert not service.can_start_workers
    manifest = bootstrapper.run(TOKEN)
    assert manifest.config_sha256 == client.manifest.config_sha256
    assert service.readyz().status_code == 200
    assert service.require_ready().config_revision == revision
    assert client.claims == 1
    assert [r.config_revision for r in client.activations] == [revision]
    assert [r.config_revision for r in client.acknowledgements] == [revision]
    assert load_activation_state(bootstrapper.activation_path).config_revision == revision
    assert bootstrapper.run("") == manifest
    assert bootstrapper.run(TOKEN) == manifest
    assert client.claims == 1
    service.close()


@pytest.mark.parametrize("failure", ["token", "claim", "manifest", "activating", "activate", "active", "acknowledge"])
def test_rollout_resumes_each_interrupted_boundary(active_rollout, monkeypatch, failure) -> None:
    client, bootstrapper, service = active_rollout()
    before = (bootstrapper.manifest_path.read_bytes(), bootstrapper.activation_path.read_bytes())

    def interrupted(*args, **kwargs):
        raise OSError("synthetic interruption")

    with monkeypatch.context() as patch:
        if failure in {"claim", "activate", "acknowledge"}:
            patch.setattr(client, failure, interrupted)
        elif failure == "manifest":
            patch.setattr(runtime_bootstrap_module, "atomic_write", interrupted)
        elif failure in {"activating", "active"}:
            write = runtime_bootstrap_module.write_activation_state

            def interrupt_state(path, state):
                if state.status == failure:
                    interrupted()
                write(path, state)

            patch.setattr(runtime_bootstrap_module, "write_activation_state", interrupt_state)
        with pytest.raises((BootstrapError, OSError)):
            bootstrapper.run("" if failure == "token" else TOKEN)

    if failure in {"token", "claim", "manifest"}:
        assert (bootstrapper.manifest_path.read_bytes(), bootstrapper.activation_path.read_bytes()) == before
    if failure != "acknowledge":
        assert service.readyz().status_code == 503
        assert not service.can_start_workers
    # A new process must recover from the durable files, including the window
    # where the manifest is new and the activation receipt still names N.
    restarted = RuntimeBootstrapper(
        client,
        runtime_ref=RUNTIME_REF,
        manifest_path=bootstrapper.manifest_path,
        activation_path=bootstrapper.activation_path,
        env=bootstrapper.env,
    )
    assert restarted.run(TOKEN).config_sha256 == client.manifest.config_sha256
    assert service.readyz().status_code == 200
    assert [r.config_revision for r in client.activations] == ([2, 2] if failure == "active" else [2])
    assert [r.config_revision for r in client.acknowledgements] == [2]
    service.close()


@pytest.mark.parametrize("change", ["household_id", "runtime_ref", "status", "downgrade", "same_revision_hash"])
def test_rollout_rejects_invalid_durable_binding(active_rollout, change) -> None:
    client, bootstrapper, service = active_rollout()
    state = load_activation_state(bootstrapper.activation_path)
    changes = {
        "household_id": {"household_id": "another-household"},
        "runtime_ref": {"runtime_ref": "fly:another-runtime"},
        "status": {"status": "unsupported"},
        "downgrade": {"config_revision": 3},
        "same_revision_hash": {"config_revision": 2, "config_sha256": "0" * 64},
    }
    write_activation_state(
        bootstrapper.activation_path,
        ActivationState(**{**state.__dict__, **changes[change]}),
    )
    before = (bootstrapper.manifest_path.read_bytes(), bootstrapper.activation_path.read_bytes())
    with pytest.raises(BootstrapError):
        bootstrapper.run(TOKEN)
    assert client.claims == 0
    assert client.activations == client.acknowledgements == []
    assert (bootstrapper.manifest_path.read_bytes(), bootstrapper.activation_path.read_bytes()) == before
    assert service.readyz().status_code == 503
    service.close()


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


def test_active_legacy_manifest_without_content_restriction_is_suspended(
    tmp_path: Path,
) -> None:
    content = manifest_toml(with_content_restriction=False)
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
        env={},
    )

    assert service.readyz().status_code == 503
    assert service.readyz().payload["reason"] == "content_restriction_not_current"
    assert service.can_start_workers is False
    with pytest.raises(RuntimeNotReady, match="content_restriction_not_current"):
        service.require_ready()


def test_active_manifest_with_stale_content_restriction_is_suspended(
    tmp_path: Path,
) -> None:
    content = manifest_toml(content_restriction_sha="0" * 64)
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
        env={},
    )

    assert service.readyz().payload == {
        "status": "not_ready",
        "reason": "content_restriction_not_current",
    }
    with pytest.raises(RuntimeNotReady, match="content_restriction_not_current"):
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


def test_fly_runtime_listener_is_dual_stack() -> None:
    host, server_class = runtime_service_module._runtime_server_binding("0.0.0.0")

    assert host == "::"
    assert server_class is runtime_service_module._DualStackWSGIServer
    assert server_class.address_family == socket.AF_INET6
    assert runtime_service_module._runtime_server_binding("127.0.0.1") == (
        "127.0.0.1",
        None,
    )


class FakeRuntimeGmailClient:
    """A send-only client: the runtime may refresh the grant and send, nothing else."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.verified = 0
        self.closed = False

    def verify_access(self):
        self.verified += 1
        if self.error is not None:
            raise self.error

    def send_raw(self, raw):
        return {"id": "gmail-message-1"}

    def close(self):
        self.closed = True


def _gmail_secret_bundle(*, scopes=None) -> str:
    return json.dumps(
        {
            "client_id": "client-id.apps.googleusercontent.com",
            "client_secret": "client-secret-canary",
            "refresh_credential": "refresh-credential-canary",
            "provider_subject": "google-subject-1",
            "scopes": list(
                scopes
                or (
                    "openid",
                    "email",
                    "https://www.googleapis.com/auth/gmail.send",
                )
            ),
            "wrapping_key": base64.urlsafe_b64encode(b"k" * 32).rstrip(b"=").decode(),
        }
    )


def _active_gmail_runtime(tmp_path: Path, client) -> tuple[RuntimeService, object]:
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
    service = RuntimeService(
        manifest_path=manifest_path,
        activation_path=activation_path,
        runtime_ref=RUNTIME_REF,
        env={
            "HERMES_DB": str(tmp_path / "hermes.db"),
            "HERMES_EMAIL_BINDING": _gmail_secret_bundle(),
        },
        gmail_client_factory=lambda *_args, **_kwargs: client,
    )
    return service, manifest


def test_gmail_activation_health_is_the_grant_refresh_and_never_a_read(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Send-only Gmail: activation proves the grant refreshes, readiness shows
    the grant, and the runtime never starts a Gmail poller or calls a read
    method — the send-only scope set would answer one with 403."""
    client = FakeRuntimeGmailClient()
    service, manifest = _active_gmail_runtime(tmp_path, client)

    assert service.email_activation_health(manifest) == ("healthy", "healthy")
    assert client.verified == 1
    assert client.closed is True
    assert not hasattr(service, "run_gmail_once")
    assert not hasattr(runtime_service_module, "_gmail_worker_until_stopped")

    ready = service.readyz()
    assert ready.status_code == 200
    assert ready.payload["email_provider"] == "gmail"
    assert ready.payload["email_health"] == {"status": "send_only"}

    revoke_calls = []

    class Revoked:
        status_code = 200

    monkeypatch.setattr(
        runtime_service_module.httpx,
        "post",
        lambda *_args, **_kwargs: revoke_calls.append(True) or Revoked(),
    )
    assert service._revoke_google_credential(manifest) is True
    assert service._revoke_google_credential(manifest) is True
    assert revoke_calls == [True]
    revoked = service.readyz()
    assert revoked.status_code == 503
    assert revoked.payload["reason"] == "email_state_unavailable"
    service.close()


def test_gmail_activation_fails_closed_on_a_revoked_grant(tmp_path: Path) -> None:
    from hermes_cloud.email.google_client import GmailAuthRevoked

    client = FakeRuntimeGmailClient(error=GmailAuthRevoked("gmail_auth_revoked"))
    service, manifest = _active_gmail_runtime(tmp_path, client)

    assert service.email_activation_health(manifest) == ("failed", "failed")
    assert client.closed is True


def _google_that(*, refresh, send):
    """Google's token endpoint answers `refresh`, Gmail's send answers `send`."""
    import httpx

    from hermes_cloud.email.google_client import GMAIL_URL, TOKEN_URL

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            return httpx.Response(refresh[0], json=refresh[1])
        assert str(request.url) == f"{GMAIL_URL}/messages/send"
        return httpx.Response(send[0], json=send[1])

    return handler


def _real_gmail_client(handler):
    import httpx

    from hermes_cloud.email.google_client import GmailHttpClient

    return lambda store, **kwargs: GmailHttpClient(
        store, http=httpx.Client(transport=httpx.MockTransport(handler)), **kwargs
    )


def _active_gmail_runtime_with_google(tmp_path: Path, handler):
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
    service = RuntimeService(
        manifest_path=manifest_path,
        activation_path=activation_path,
        runtime_ref=RUNTIME_REF,
        env={
            "HERMES_DB": str(tmp_path / "hermes.db"),
            "HERMES_EMAIL_BINDING": _gmail_secret_bundle(),
        },
        gmail_client_factory=_real_gmail_client(handler),
    )
    return service, manifest


OK_TOKEN = (200, {"access_token": "access-canary", "expires_in": 3600})
DEAD_TOKEN = (400, {"error": "invalid_grant"})


@pytest.mark.parametrize(
    ("refresh", "send"),
    [
        pytest.param(DEAD_TOKEN, (200, {"id": "m1"}), id="refresh-invalid_grant"),
        pytest.param(OK_TOKEN, (401, {"error": {"code": 401}}), id="send-401"),
        pytest.param(OK_TOKEN, (403, {"error": {"errors": [{"reason": "forbidden"}]}}), id="send-403"),
    ],
)
def test_a_revocation_google_proves_closes_readiness_durably(
    tmp_path: Path, refresh, send
) -> None:
    """After the family revokes access in Google, the next refresh or send is
    the only place the runtime learns of it. Without the poller writing
    `auth_revoked`, `/readyz` would keep answering `send_only` with 200 from
    the locally unrevoked row; the grant is zeroed where the revocation is
    observed, so readiness fails closed until a reconnect."""
    from hermes_cloud.email.google_client import (
        GmailAuthRevoked,
        build_gmail_client,
    )

    service, manifest = _active_gmail_runtime_with_google(
        tmp_path, _google_that(refresh=refresh, send=send)
    )
    assert service.readyz().payload["email_health"] == {"status": "send_only"}

    if refresh is DEAD_TOKEN:
        assert service.email_activation_health(manifest) == ("failed", "failed")
    else:
        assert service.email_activation_health(manifest) == ("healthy", "healthy")
        binding = service._sync_email_binding(manifest)
        with runtime_service_module.open_database(service.database_path) as database:
            client = build_gmail_client(
                database,
                binding,
                service._gmail_bundle(binding),
                client_factory=service.gmail_client_factory,
            )
            with pytest.raises(GmailAuthRevoked):
                client.send_raw("raw")

    revoked = service.readyz()
    assert revoked.status_code == 503
    assert revoked.payload["reason"] == "email_state_unavailable"
    with runtime_service_module.open_database(service.database_path) as database:
        row = database.query_one("SELECT revoked_at, encrypted_refresh_credential FROM oauth_grants")
    assert row["revoked_at"] is not None
    assert bytes(row["encrypted_refresh_credential"]) == b""


def test_a_gmail_usage_limit_leaves_readiness_open(tmp_path: Path) -> None:
    from hermes_cloud.email.google_client import GmailQuotaExceeded, build_gmail_client

    service, manifest = _active_gmail_runtime_with_google(
        tmp_path,
        _google_that(
            refresh=OK_TOKEN,
            send=(403, {"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}),
        ),
    )
    binding = service._sync_email_binding(manifest)
    with runtime_service_module.open_database(service.database_path) as database:
        client = build_gmail_client(
            database, binding, service._gmail_bundle(binding), client_factory=service.gmail_client_factory
        )
        with pytest.raises(GmailQuotaExceeded):
            client.send_raw("raw")
    assert service.readyz().payload["email_health"] == {"status": "send_only"}


def test_gmail_activation_fails_closed_without_the_send_scope(tmp_path: Path) -> None:
    """A bundle the control plane would never write, refused here as well:
    the runtime does not activate a Gmail household it cannot send from."""
    from hermes_cloud.email.google_client import GmailConfigurationError

    client = FakeRuntimeGmailClient()
    service, manifest = _active_gmail_runtime(tmp_path, client)
    service.env["HERMES_EMAIL_BINDING"] = _gmail_secret_bundle(scopes=("openid", "email"))

    with pytest.raises(RuntimeNotReady) as raised:
        service.email_activation_health(manifest)
    assert isinstance(raised.value.__cause__, GmailConfigurationError)
    assert client.verified == 0


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

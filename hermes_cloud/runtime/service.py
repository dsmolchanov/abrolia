"""Health/readiness boundary that keeps runtime work disabled until activation."""

from __future__ import annotations

import base64
import hmac
import json
import os
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from wsgiref.simple_server import WSGIRequestHandler, make_server

import httpx

from hermes_cloud.core.config import DEFAULT_DB_PATH, ENV_DB
from hermes_cloud.core.db import open_database
from hermes_cloud.core.dsar import export_household, is_deleted, wipe_household
from hermes_cloud.core.runtime_manifest import (
    ENV_CONFIG_REVISION,
    ENV_HOUSEHOLD_FILE,
    ENV_HOUSEHOLD_ID,
    ManifestError,
    RuntimeManifest,
    load_runtime_manifest,
)
from hermes_cloud.email.contracts import EmailBinding
from hermes_cloud.email.google_client import GmailHttpClient
from hermes_cloud.email.google_grant import GoogleGrantError, GoogleGrantStore
from hermes_cloud.email.nerve_client import (
    DEFAULT_REST_URL,
    DEFAULT_RUNTIME_URL,
    NerveEmailClient,
)
from hermes_cloud.email.receipts import EmailBindingStore
from hermes_cloud.email.service import EmailRuntimeService
from hermes_cloud.ingest.nerve_webhook import (
    MAX_WEBHOOK_BYTES,
    NerveAttachmentWorker,
    NerveWebhookReceiver,
    NerveWebhookRejected,
    NerveWebhookStore,
)
from hermes_cloud.runtime.bootstrap import (
    DEFAULT_ACTIVATION_STATE,
    DEFAULT_MANIFEST_PATH,
    ENV_ACTIVATION_STATE,
    ENV_BOOTSTRAP_TOKEN,
    ENV_CONTROL_PLANE_URL,
    ENV_RUNTIME_REF,
    BootstrapError,
    ControlPlaneBootstrapClient,
    RuntimeBootstrapper,
    atomic_write,
    load_activation_state,
    state_matches_manifest,
)

ENV_RUNTIME_HOST = "HERMES_RUNTIME_HOST"
ENV_RUNTIME_PORT = "HERMES_RUNTIME_PORT"
ENV_BOOTSTRAP_RETRY_SECONDS = "HERMES_BOOTSTRAP_RETRY_SECONDS"
ENV_RUNTIME_DSAR_TOKEN = "HERMES_RUNTIME_DSAR_TOKEN"
ENV_RUNTIME_DELETION_MARKER = "HERMES_RUNTIME_DELETION_MARKER"
ENV_NERVE_RUNTIME_URL = "ABROLIA_NERVE_RUNTIME_URL"
ENV_NERVE_REST_URL = "ABROLIA_NERVE_REST_URL"
ENV_NERVE_WORKER_SECONDS = "ABROLIA_NERVE_WORKER_SECONDS"


class RuntimeNotReady(RuntimeError):
    """Model, channel, or ingress work was requested before activation."""


@dataclass(frozen=True)
class Probe:
    status_code: int
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class NerveRuntimeConfig:
    org_id: str
    inbox_id: str
    api_key: str
    webhook_signing_key: str
    runtime_url: str
    rest_url: str


class RuntimeService:
    def __init__(
        self,
        *,
        manifest_path: Path | str | None = None,
        activation_path: Path | str | None = None,
        runtime_ref: str | None = None,
        env: Mapping[str, str] | None = None,
        nerve_client_factory: Callable[..., Any] = NerveEmailClient,
    ) -> None:
        self.env = dict(os.environ if env is None else env)
        self.manifest_path = Path(manifest_path or self.env.get(ENV_HOUSEHOLD_FILE) or DEFAULT_MANIFEST_PATH)
        self.activation_path = Path(
            activation_path or self.env.get(ENV_ACTIVATION_STATE) or DEFAULT_ACTIVATION_STATE
        )
        self.runtime_ref = runtime_ref or self.env.get(ENV_RUNTIME_REF) or ""
        self.nerve_client_factory = nerve_client_factory
        self.database_path = Path(self.env.get(ENV_DB) or DEFAULT_DB_PATH)
        self.deletion_marker = Path(
            self.env.get(ENV_RUNTIME_DELETION_MARKER)
            or self.activation_path.with_name("runtime-deleted.json")
        )

    def healthz(self) -> Probe:
        # Liveness deliberately does not depend on bootstrap/control-plane state.
        return Probe(200, {"status": "ok"})

    def _ready_manifest(self) -> tuple[RuntimeManifest | None, str]:
        if self.deletion_marker.is_file():
            return None, "runtime_deleted"
        try:
            manifest = load_runtime_manifest(self.manifest_path, env=self.env)
        except ManifestError:
            return None, "manifest_missing_or_invalid"
        try:
            state = load_activation_state(self.activation_path)
        except BootstrapError:
            return None, "activation_invalid"
        if state is None or state.status != "active":
            return None, "activation_pending"
        if self.runtime_ref and state.runtime_ref != self.runtime_ref:
            return None, "runtime_ref_mismatch"
        if not state_matches_manifest(state, manifest):
            return None, "revision_mismatch"
        return manifest, "active"

    def readyz(self) -> Probe:
        manifest, reason = self._ready_manifest()
        if manifest is None:
            return Probe(503, {"status": "not_ready", "reason": reason})
        try:
            binding = self._sync_email_binding(manifest)
        except Exception:
            return Probe(503, {"status": "not_ready", "reason": "email_state_unavailable"})
        if binding is not None and binding.provider.startswith("nerve"):
            try:
                self._nerve_config(manifest)
            except RuntimeNotReady:
                return Probe(
                    503,
                    {"status": "not_ready", "reason": "email_provider_unavailable"},
                )
        payload = {
            "status": "ready",
            "household_id": manifest.household_id,
            "config_revision": manifest.config_revision,
        }
        if binding is not None:
            payload.update(
                email_provider=binding.provider,
                email_binding_revision=binding.revision,
            )
            if binding.provider.startswith("nerve"):
                try:
                    with open_database(self.database_path) as database:
                        payload.update(email_health=NerveWebhookStore(database).health(binding))
                except Exception:
                    payload.update(email_health={"status": "unavailable"})
            elif binding.provider == "gmail":
                try:
                    with open_database(self.database_path) as database:
                        health = EmailRuntimeService(database).health()
                        payload.update(
                            email_health={
                                "status": health.status,
                                "last_success_at": health.last_success_at,
                            }
                        )
                except Exception:
                    payload.update(email_health={"status": "unavailable"})
        return Probe(
            200,
            payload,
        )

    @property
    def can_start_workers(self) -> bool:
        return self.readyz().status_code == 200

    def require_ready(self) -> RuntimeManifest:
        manifest, reason = self._ready_manifest()
        if manifest is None:
            raise RuntimeNotReady(f"runtime is not active ({reason})")
        try:
            binding = self._sync_email_binding(manifest)
            if binding is not None and binding.provider.startswith("nerve"):
                self._nerve_config(manifest)
        except Exception as error:
            raise RuntimeNotReady("runtime is not active (email_state_unavailable)") from error
        return manifest

    def _sync_email_binding(self, manifest: RuntimeManifest) -> EmailBinding | None:
        identity_id = manifest.email.provider_binding_ref
        if not identity_id:
            return None
        binding = EmailBinding(
            identity_id=identity_id,
            revision=manifest.config_revision,
            provider=manifest.email.provider_kind,
            address=manifest.email.agent_inbox,
            provider_ref=identity_id,
            secret_names=((manifest.email.secret_binding_ref,) if manifest.email.secret_binding_ref else ()),
        )
        with open_database(self.database_path) as database:
            active = EmailBindingStore(database).activate(binding)
            if active.provider == "gmail":
                self._install_gmail_grant(database, active, manifest)
            return active

    def _gmail_bundle(self, manifest: RuntimeManifest) -> dict[str, Any]:
        secret_name = manifest.email.secret_binding_ref or ""
        try:
            bundle = json.loads(self.env.get(secret_name, ""))
        except (TypeError, json.JSONDecodeError) as error:
            raise RuntimeNotReady("Gmail credential bundle is unavailable") from error
        required = {
            "client_id",
            "client_secret",
            "refresh_credential",
            "provider_subject",
            "scopes",
            "wrapping_key",
        }
        if not isinstance(bundle, dict) or set(bundle) != required:
            raise RuntimeNotReady("Gmail credential bundle is invalid")
        return bundle

    @staticmethod
    def _grant_key(bundle: dict[str, Any]) -> bytes:
        value = str(bundle.get("wrapping_key") or "")
        try:
            key = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        except (ValueError, TypeError) as error:
            raise RuntimeNotReady("Gmail grant key is invalid") from error
        if len(key) != 32:
            raise RuntimeNotReady("Gmail grant key is invalid")
        return key

    def _install_gmail_grant(self, database, binding: EmailBinding, manifest: RuntimeManifest) -> None:
        bundle = self._gmail_bundle(manifest)
        store = GoogleGrantStore(database, {1: self._grant_key(bundle)}, active_version=1)
        try:
            store.load(binding.identity_id, binding.revision)
        except GoogleGrantError as error:
            scopes = bundle.get("scopes")
            if not isinstance(scopes, list) or not all(isinstance(item, str) for item in scopes):
                raise RuntimeNotReady("Gmail scope bundle is invalid") from error
            store.put(
                identity_id=binding.identity_id,
                revision=binding.revision,
                refresh_credential=str(bundle["refresh_credential"]),
                provider_subject=str(bundle["provider_subject"]),
                scopes=tuple(scopes),
            )

    def _revoke_google_credential(self, manifest: RuntimeManifest) -> bool:
        binding = self._sync_email_binding(manifest)
        if binding is None or binding.provider != "gmail":
            return True
        bundle = self._gmail_bundle(manifest)
        with open_database(self.database_path) as database:
            store = GoogleGrantStore(database, {1: self._grant_key(bundle)}, active_version=1)
            try:
                grant = store.load(binding.identity_id, binding.revision)
            except GoogleGrantError:
                return True
            try:
                response = httpx.post(
                    "https://oauth2.googleapis.com/revoke",
                    params={"token": grant.refresh_credential},
                    timeout=20.0,
                )
            except (httpx.TimeoutException, httpx.TransportError):
                return False
            if response.status_code not in {200, 400}:
                return False
            store.revoke(binding.identity_id, binding.revision)
        return True

    def email_activation_health(self, manifest: RuntimeManifest) -> tuple[str, str]:
        provider = manifest.email.provider_kind
        if provider == "synthetic":
            return "healthy", "healthy"
        if provider.startswith("nerve"):
            config = self._nerve_config(manifest)
            client = self.nerve_client_factory(
                api_key=config.api_key,
                runtime_url=config.runtime_url,
                rest_url=config.rest_url,
            )
            try:
                healthy = bool(client.health_check())
            finally:
                close = getattr(client, "close", None)
                if close is not None:
                    close()
            state = "healthy" if healthy else "failed"
            return state, state
        if provider == "gmail":
            binding = self._sync_email_binding(manifest)
            if binding is None:
                return "failed", "failed"
            bundle = self._gmail_bundle(manifest)
            with open_database(self.database_path) as database:
                store = GoogleGrantStore(database, {1: self._grant_key(bundle)}, active_version=1)
                client = GmailHttpClient(
                    store,
                    identity_id=binding.identity_id,
                    revision=binding.revision,
                    client_id=str(bundle["client_id"]),
                    client_secret=str(bundle["client_secret"]),
                )
                profile = client.profile()
                inbound = "healthy" if profile.get("historyId") else "failed"
                scopes = {str(item) for item in bundle.get("scopes", [])}
                outbound = "healthy" if "https://www.googleapis.com/auth/gmail.send" in scopes else "failed"
                return inbound, outbound
        return "failed", "failed"

    def _nerve_config(self, manifest: RuntimeManifest) -> NerveRuntimeConfig:
        if not manifest.email.provider_kind.startswith("nerve"):
            raise RuntimeNotReady("runtime email provider is not Nerve")
        try:
            refs = json.loads(manifest.email.provider_binding_ref or "")
        except (TypeError, json.JSONDecodeError) as error:
            raise RuntimeNotReady("Nerve public binding reference is invalid") from error
        secret_name = manifest.email.secret_binding_ref or ""
        try:
            secrets = json.loads(self.env.get(secret_name, ""))
        except (TypeError, json.JSONDecodeError) as error:
            raise RuntimeNotReady("Nerve credential bundle is unavailable") from error
        if not isinstance(refs, dict) or not isinstance(secrets, dict):
            raise RuntimeNotReady("Nerve runtime configuration is invalid")
        values = {
            "org_id": refs.get("org_id"),
            "inbox_id": refs.get("inbox_id"),
            "api_key": secrets.get("api_key"),
            "webhook_signing_key": secrets.get("webhook_signing_key"),
        }
        if any(not isinstance(value, str) or not value for value in values.values()):
            raise RuntimeNotReady("Nerve runtime configuration is incomplete")
        return NerveRuntimeConfig(
            **values,
            runtime_url=self.env.get(ENV_NERVE_RUNTIME_URL, DEFAULT_RUNTIME_URL),
            rest_url=self.env.get(ENV_NERVE_REST_URL, DEFAULT_REST_URL),
        )

    def receive_nerve_webhook(self, payload: bytes, signature: str):
        manifest = self.require_ready()
        binding = self._sync_email_binding(manifest)
        if binding is None:
            raise RuntimeNotReady("Nerve email binding is unavailable")
        config = self._nerve_config(manifest)
        with open_database(self.database_path) as database:
            return NerveWebhookReceiver(
                NerveWebhookStore(database),
                binding=binding,
                org_id=config.org_id,
                inbox_id=config.inbox_id,
                signing_secret=config.webhook_signing_key,
            ).receive(payload, signature)

    def run_nerve_once(self):
        manifest = self.require_ready()
        config = self._nerve_config(manifest)
        with open_database(self.database_path) as database:
            client = self.nerve_client_factory(
                api_key=config.api_key,
                runtime_url=config.runtime_url,
                rest_url=config.rest_url,
            )
            try:
                return NerveAttachmentWorker(database, client).run_once()
            finally:
                close = getattr(client, "close", None)
                if close is not None:
                    close()

    def __call__(self, environ: Mapping[str, Any], start_response: Callable) -> list[bytes]:
        """Health probes plus an authenticated private DSAR boundary."""
        path = str(environ.get("PATH_INFO") or "")
        method = str(environ.get("REQUEST_METHOD") or "GET").upper()
        if path == "/v1/email/nerve/webhook" and method == "POST":
            probe = self._nerve_webhook(environ)
        elif path in {"/internal/v1/dsar/export", "/internal/v1/dsar/delete"}:
            probe = self._dsar(path, method, str(environ.get("HTTP_AUTHORIZATION") or ""))
        elif path == "/internal/v1/email/google/revoke":
            probe = self._google_revoke(method, str(environ.get("HTTP_AUTHORIZATION") or ""))
        elif method != "GET" or path not in {"/healthz", "/readyz"}:
            probe = Probe(404, {"status": "not_found"})
        else:
            probe = self.healthz() if path == "/healthz" else self.readyz()
        body = json.dumps(probe.payload, sort_keys=True, separators=(",", ":")).encode()
        status_text = {
            200: "200 OK",
            400: "400 Bad Request",
            401: "401 Unauthorized",
            403: "403 Forbidden",
            404: "404 Not Found",
            409: "409 Conflict",
            413: "413 Content Too Large",
            410: "410 Gone",
            503: "503 Service Unavailable",
        }
        start_response(
            status_text[probe.status_code],
            [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
            ],
        )
        return [body]

    def _nerve_webhook(self, environ: Mapping[str, Any]) -> Probe:
        try:
            length = int(str(environ.get("CONTENT_LENGTH") or "0"))
        except ValueError:
            return Probe(400, {"status": "invalid_request"})
        if length < 1:
            return Probe(400, {"status": "invalid_request"})
        if length > MAX_WEBHOOK_BYTES:
            return Probe(413, {"status": "payload_too_large"})
        stream = environ.get("wsgi.input")
        if stream is None or not hasattr(stream, "read"):
            return Probe(400, {"status": "invalid_request"})
        payload = stream.read(length)
        if not isinstance(payload, bytes) or len(payload) != length:
            return Probe(400, {"status": "invalid_request"})
        signature = str(environ.get("HTTP_X_NERVE_SIGNATURE") or "")
        try:
            accepted = self.receive_nerve_webhook(payload, signature)
        except NerveWebhookRejected as error:
            return Probe(error.status_code, {"status": error.code})
        except RuntimeNotReady:
            return Probe(503, {"status": "runtime_not_ready"})
        except Exception:
            return Probe(503, {"status": "webhook_unavailable"})
        return Probe(
            200,
            {
                "status": "accepted" if accepted.created else "duplicate",
                "event_id": accepted.event.id,
            },
        )

    def _dsar(self, path: str, method: str, authorization: str) -> Probe:
        expected = self.env.get(ENV_RUNTIME_DSAR_TOKEN, "")
        if method != "POST" or not expected:
            return Probe(404, {"status": "not_found"})
        prefix = "Bearer "
        supplied = authorization[len(prefix) :] if authorization.startswith(prefix) else ""
        if not supplied or not hmac.compare_digest(supplied, expected):
            return Probe(401, {"status": "unauthorized"})
        if self.deletion_marker.is_file():
            if path.endswith("/delete"):
                return Probe(200, {"state": "absent"})
            return Probe(410, {"status": "runtime_deleted"})
        try:
            self.require_ready()
            with open_database(self.database_path) as database:
                if is_deleted(database):
                    atomic_write(
                        self.deletion_marker,
                        b'{"status":"deleted"}',
                    )
                    if path.endswith("/delete"):
                        return Probe(200, {"state": "absent"})
                    return Probe(410, {"status": "runtime_deleted"})
                if path.endswith("/export"):
                    return Probe(200, export_household(database))
                manifest = self.require_ready()
                if manifest.email.provider_kind == "gmail" and not self._revoke_google_credential(manifest):
                    return Probe(503, {"status": "provider_cleanup_unknown"})
                wipe_household(database)
            atomic_write(self.deletion_marker, b'{"status":"deleted"}')
            return Probe(200, {"state": "absent"})
        except Exception:
            # Runtime payloads and exception strings must never cross this boundary.
            return Probe(503, {"status": "runtime_boundary_unavailable"})

    def _google_revoke(self, method: str, authorization: str) -> Probe:
        expected = self.env.get(ENV_RUNTIME_DSAR_TOKEN, "")
        supplied = authorization[7:] if authorization.startswith("Bearer ") else ""
        if method != "POST" or not expected:
            return Probe(404, {"status": "not_found"})
        if not supplied or not hmac.compare_digest(supplied, expected):
            return Probe(401, {"status": "unauthorized"})
        try:
            manifest = self.require_ready()
            if not self._revoke_google_credential(manifest):
                return Probe(503, {"status": "provider_cleanup_unknown"})
        except Exception:
            return Probe(503, {"status": "provider_cleanup_unknown"})
        return Probe(200, {"state": "absent"})


def bootstrap_from_environment(
    service: RuntimeService,
    *,
    env: Mapping[str, str] | None = None,
) -> RuntimeManifest:
    """Run or resume bootstrap using credentials supplied only by environment."""
    source = dict(os.environ if env is None else env)
    token = source.get(ENV_BOOTSTRAP_TOKEN, "")
    control_plane_url = source.get(ENV_CONTROL_PLANE_URL, "")
    runtime_ref = source.get(ENV_RUNTIME_REF, "")
    household_id = source.get(ENV_HOUSEHOLD_ID, "")
    raw_revision = source.get(ENV_CONFIG_REVISION, "")
    if service.can_start_workers and not all(
        (token, control_plane_url, runtime_ref, household_id, raw_revision)
    ):
        # The durable activation receipt is sufficient for serving. If Fly has
        # already removed the bootstrap secret, there is nothing left to ack.
        return service.require_ready()
    if not all((token, control_plane_url, runtime_ref, household_id, raw_revision)):
        raise BootstrapError("runtime bootstrap environment is incomplete")
    try:
        config_revision = int(raw_revision)
    except ValueError as error:
        raise BootstrapError(f"{ENV_CONFIG_REVISION} must be an integer") from error
    return RuntimeBootstrapper(
        ControlPlaneBootstrapClient(control_plane_url),
        runtime_ref=runtime_ref,
        household_id=household_id,
        config_revision=config_revision,
        manifest_path=service.manifest_path,
        activation_path=service.activation_path,
        env=source,
        email_health_checker=service.email_activation_health,
    ).run(token)


def _bootstrap_until_active(
    service: RuntimeService,
    source: Mapping[str, str],
    stop: threading.Event,
) -> None:
    try:
        retry_seconds = max(float(source.get(ENV_BOOTSTRAP_RETRY_SECONDS, "5")), 0.1)
    except ValueError:
        retry_seconds = 5.0
    last_error: str | None = None
    while not stop.is_set():
        try:
            bootstrap_from_environment(service, env=source)
        except Exception as error:  # keep liveness up while readiness stays fail-closed
            error_name = error.__class__.__name__
            if error_name != last_error:
                print(f"runtime bootstrap pending ({error_name})", file=sys.stderr)
                last_error = error_name
            stop.wait(retry_seconds)
        else:
            return


def _nerve_worker_until_stopped(
    service: RuntimeService,
    source: Mapping[str, str],
    stop: threading.Event,
) -> None:
    try:
        interval = max(float(source.get(ENV_NERVE_WORKER_SECONDS, "2")), 0.1)
    except ValueError:
        interval = 2.0
    while not stop.is_set():
        if service.can_start_workers:
            try:
                result = service.run_nerve_once()
            except RuntimeNotReady:
                pass
            except Exception as error:
                print(
                    f"Nerve ingress pending ({error.__class__.__name__})",
                    file=sys.stderr,
                )
            else:
                if result is not None:
                    continue
        stop.wait(interval)


class _QuietRequestHandler(WSGIRequestHandler):
    def log_message(self, _format: str, *args: object) -> None:
        """Health requests are intentionally absent from application logs."""


def serve_runtime(*, env: Mapping[str, str] | None = None) -> None:
    """Serve probes immediately and bootstrap in the background until active."""
    source = dict(os.environ if env is None else env)
    service = RuntimeService(env=source)
    host = source.get(ENV_RUNTIME_HOST, "0.0.0.0")
    try:
        port = int(source.get(ENV_RUNTIME_PORT, "8080"))
    except ValueError as error:
        raise BootstrapError(f"{ENV_RUNTIME_PORT} must be an integer") from error
    if not 0 <= port <= 65535:
        raise BootstrapError(f"{ENV_RUNTIME_PORT} is outside the valid range")
    stop = threading.Event()
    server = make_server(host, port, service, handler_class=_QuietRequestHandler)
    worker = threading.Thread(
        target=_bootstrap_until_active,
        args=(service, source, stop),
        name="runtime-bootstrap",
        daemon=True,
    )
    nerve_worker = threading.Thread(
        target=_nerve_worker_until_stopped,
        args=(service, source, stop),
        name="nerve-ingress",
        daemon=True,
    )
    worker.start()
    nerve_worker.start()
    try:
        server.serve_forever()
    finally:
        stop.set()
        worker.join(timeout=1.0)
        nerve_worker.join(timeout=1.0)
        server.server_close()


def main() -> int:
    try:
        serve_runtime()
    except (BootstrapError, OSError) as error:
        print(f"runtime service failed ({error.__class__.__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

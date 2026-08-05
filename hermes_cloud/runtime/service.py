"""Health/readiness boundary that keeps runtime work disabled until activation."""

from __future__ import annotations

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


class RuntimeNotReady(RuntimeError):
    """Model, channel, or ingress work was requested before activation."""


@dataclass(frozen=True)
class Probe:
    status_code: int
    payload: Mapping[str, Any]


class RuntimeService:
    def __init__(
        self,
        *,
        manifest_path: Path | str | None = None,
        activation_path: Path | str | None = None,
        runtime_ref: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.env = dict(os.environ if env is None else env)
        self.manifest_path = Path(
            manifest_path or self.env.get(ENV_HOUSEHOLD_FILE) or DEFAULT_MANIFEST_PATH
        )
        self.activation_path = Path(
            activation_path
            or self.env.get(ENV_ACTIVATION_STATE)
            or DEFAULT_ACTIVATION_STATE
        )
        self.runtime_ref = runtime_ref or self.env.get(ENV_RUNTIME_REF) or ""
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
        return Probe(
            200,
            {
                "status": "ready",
                "household_id": manifest.household_id,
                "config_revision": manifest.config_revision,
            },
        )

    @property
    def can_start_workers(self) -> bool:
        return self.readyz().status_code == 200

    def require_ready(self) -> RuntimeManifest:
        manifest, reason = self._ready_manifest()
        if manifest is None:
            raise RuntimeNotReady(f"runtime is not active ({reason})")
        return manifest

    def __call__(self, environ: Mapping[str, Any], start_response: Callable) -> list[bytes]:
        """Health probes plus an authenticated private DSAR boundary."""
        path = str(environ.get("PATH_INFO") or "")
        method = str(environ.get("REQUEST_METHOD") or "GET").upper()
        if path in {"/internal/v1/dsar/export", "/internal/v1/dsar/delete"}:
            probe = self._dsar(path, method, str(environ.get("HTTP_AUTHORIZATION") or ""))
        elif method != "GET" or path not in {"/healthz", "/readyz"}:
            probe = Probe(404, {"status": "not_found"})
        else:
            probe = self.healthz() if path == "/healthz" else self.readyz()
        body = json.dumps(probe.payload, sort_keys=True, separators=(",", ":")).encode()
        status_text = {
            200: "200 OK",
            401: "401 Unauthorized",
            404: "404 Not Found",
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
                wipe_household(database)
            atomic_write(self.deletion_marker, b'{"status":"deleted"}')
            return Probe(200, {"state": "absent"})
        except Exception:
            # Runtime payloads and exception strings must never cross this boundary.
            return Probe(503, {"status": "runtime_boundary_unavailable"})


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
    worker.start()
    try:
        server.serve_forever()
    finally:
        stop.set()
        worker.join(timeout=1.0)
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

"""Two-phase, crash-resumable bootstrap for a dedicated household runtime."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from hermes_cloud.core.runtime_manifest import (
    ENV_CONFIG_REVISION,
    ENV_HOUSEHOLD_ID,
    ManifestError,
    RuntimeManifest,
    load_runtime_manifest,
    parse_runtime_manifest,
)

ENV_BOOTSTRAP_TOKEN = "HERMES_BOOTSTRAP_TOKEN"
ENV_CONTROL_PLANE_URL = "HERMES_CONTROL_PLANE_URL"
ENV_RUNTIME_REF = "HERMES_RUNTIME_REF"
ENV_ACTIVATION_STATE = "HERMES_ACTIVATION_STATE"
DEFAULT_MANIFEST_PATH = "/data/household.toml"
DEFAULT_ACTIVATION_STATE = "/data/runtime-activation.json"
MAX_BOOTSTRAP_RESPONSE_BYTES = 1_048_576


class BootstrapError(RuntimeError):
    """Bootstrap failed without exposing credentials or provider response bodies."""


class BootstrapOutcomeUnknown(BootstrapError):
    """The activation request may have reached the control plane."""


@dataclass(frozen=True)
class BootstrapClaim:
    runtime_ref: str
    household_id: str
    config_revision: int
    config_sha256: str
    manifest_toml: str


@dataclass(frozen=True)
class ActivationReceipt:
    runtime_ref: str
    household_id: str
    config_revision: int
    config_sha256: str
    email_inbound_check: str = "healthy"
    email_outbound_check: str = "healthy"
    email_receipt_digest: str = ""


@dataclass(frozen=True)
class ActivationState:
    status: str
    runtime_ref: str
    household_id: str
    config_revision: int
    config_sha256: str
    updated_at: float
    email_inbound_check: str = "healthy"
    email_outbound_check: str = "healthy"
    email_receipt_digest: str = ""

    def receipt(self) -> ActivationReceipt:
        return ActivationReceipt(
            runtime_ref=self.runtime_ref,
            household_id=self.household_id,
            config_revision=self.config_revision,
            config_sha256=self.config_sha256,
            email_inbound_check=self.email_inbound_check,
            email_outbound_check=self.email_outbound_check,
            email_receipt_digest=self.email_receipt_digest,
        )


class BootstrapClient(Protocol):
    def claim(
        self,
        token: str,
        *,
        household_id: str,
        runtime_ref: str,
        config_revision: int,
    ) -> BootstrapClaim: ...

    def activate(self, token: str, receipt: ActivationReceipt) -> ActivationReceipt: ...

    def acknowledge(self, token: str, receipt: ActivationReceipt) -> ActivationReceipt: ...


class BootstrapHTTPTransport(Protocol):
    """Injectable byte transport used by tests without weakening the wire client."""

    def __call__(
        self,
        url: str,
        *,
        data: bytes,
        headers: Mapping[str, str],
        timeout: float,
    ) -> tuple[int, bytes]: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        # Never forward the one-time bearer credential to a redirected host.
        return None


def _urllib_transport(
    url: str,
    *,
    data: bytes,
    headers: Mapping[str, str],
    timeout: float,
) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url,
        data=data,
        headers=dict(headers),
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read(MAX_BOOTSTRAP_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        return error.code, b""


class ControlPlaneBootstrapClient:
    """Small stdlib HTTP client; bearer values never enter URLs or exceptions."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 20.0,
        transport: BootstrapHTTPTransport | None = None,
    ) -> None:
        parsed_url = urllib.parse.urlsplit(base_url)
        private_flycast_http = (
            parsed_url.scheme == "http"
            and parsed_url.hostname is not None
            and parsed_url.hostname.endswith(".flycast")
            and parsed_url.port in {None, 80}
        )
        if (
            (parsed_url.scheme != "https" and not private_flycast_http)
            or (parsed_url.scheme == "https" and parsed_url.port not in {None, 443})
            or (
                not parsed_url.hostname
                or parsed_url.username is not None
                or parsed_url.password is not None
                or parsed_url.query
                or parsed_url.fragment
                or parsed_url.path not in {"", "/"}
            )
        ):
            raise BootstrapError("control-plane URL must use HTTPS or private Flycast HTTP")
        self.base_url = urllib.parse.urlunsplit((parsed_url.scheme, parsed_url.netloc, "", "", ""))
        self.timeout = timeout
        self.transport = transport or _urllib_transport

    def _post(
        self,
        path: str,
        token: str,
        payload: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        data = json.dumps(payload, separators=(",", ":")).encode()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        try:
            status_code, raw = self.transport(
                self.base_url + path,
                data=data,
                headers=headers,
                timeout=self.timeout,
            )
        except (OSError, TimeoutError) as error:
            raise BootstrapOutcomeUnknown("control plane did not return a response") from error
        if not 200 <= status_code < 300:
            if status_code in {408, 425, 429} or status_code >= 500:
                raise BootstrapOutcomeUnknown(
                    f"control plane bootstrap outcome is unknown (HTTP {status_code})"
                )
            raise BootstrapError(f"control plane rejected bootstrap (HTTP {status_code})")
        if len(raw) > MAX_BOOTSTRAP_RESPONSE_BYTES:
            raise BootstrapError("control plane bootstrap response is too large")
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise BootstrapError("control plane returned invalid JSON") from error
        if not isinstance(parsed, dict):
            raise BootstrapError("control plane returned an invalid response")
        return parsed

    def claim(
        self,
        token: str,
        *,
        household_id: str,
        runtime_ref: str,
        config_revision: int,
    ) -> BootstrapClaim:
        body = self._post(
            "/internal/v1/bootstrap/claim",
            token,
            {
                "household_id": household_id,
                "runtime_ref": runtime_ref,
                "config_revision": config_revision,
            },
        )
        try:
            return BootstrapClaim(
                runtime_ref=str(body["runtime_ref"]),
                household_id=str(body["household_id"]),
                config_revision=int(body["config_revision"]),
                config_sha256=str(body["config_sha256"]),
                manifest_toml=str(body["manifest_toml"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise BootstrapError("claim response is missing required fields") from error

    def _activate(
        self,
        token: str,
        receipt: ActivationReceipt,
        *,
        acknowledge_receipt: bool,
    ) -> ActivationReceipt:
        body = self._post(
            "/internal/v1/bootstrap/activate",
            token,
            asdict(receipt),
            extra_headers=(
                {"X-Hermes-Runtime-Receipt-Acknowledged": "true"} if acknowledge_receipt else None
            ),
        )
        try:
            activated = ActivationReceipt(
                runtime_ref=str(body["runtime_ref"]),
                household_id=str(body["household_id"]),
                config_revision=int(body["config_revision"]),
                config_sha256=str(body["config_sha256"]),
                email_inbound_check=receipt.email_inbound_check,
                email_outbound_check=receipt.email_outbound_check,
                email_receipt_digest=receipt.email_receipt_digest,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise BootstrapError("activation response is missing required fields") from error
        if body.get("status") != "active" or activated != receipt:
            raise BootstrapError("activation response does not match requested revision")
        expected_cleanup = "pending" if acknowledge_receipt else "awaiting_runtime_receipt"
        if body.get("bootstrap_cleanup") != expected_cleanup:
            raise BootstrapError("activation response has an invalid cleanup state")
        return activated

    def activate(self, token: str, receipt: ActivationReceipt) -> ActivationReceipt:
        return self._activate(token, receipt, acknowledge_receipt=False)

    def acknowledge(self, token: str, receipt: ActivationReceipt) -> ActivationReceipt:
        return self._activate(token, receipt, acknowledge_receipt=True)


def atomic_write(path: Path | str, payload: bytes, *, mode: int = 0o600) -> Path:
    """Write, fsync, chmod, rename, then fsync the containing directory."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".partial"
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        os.chmod(target, mode)
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()
    return target


def write_activation_state(path: Path | str, state: ActivationState) -> None:
    atomic_write(
        path,
        json.dumps(asdict(state), sort_keys=True, separators=(",", ":")).encode(),
    )


def load_activation_state(path: Path | str) -> ActivationState | None:
    file = Path(path)
    if not file.is_file():
        return None
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
        return ActivationState(
            status=str(data["status"]),
            runtime_ref=str(data["runtime_ref"]),
            household_id=str(data["household_id"]),
            config_revision=int(data["config_revision"]),
            config_sha256=str(data["config_sha256"]),
            email_inbound_check=str(data.get("email_inbound_check", "healthy")),
            email_outbound_check=str(data.get("email_outbound_check", "healthy")),
            email_receipt_digest=str(data.get("email_receipt_digest", "")),
            updated_at=float(data["updated_at"]),
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise BootstrapError("activation state is invalid") from error


def state_matches_manifest(state: ActivationState, manifest: RuntimeManifest) -> bool:
    return (
        state.household_id == manifest.household_id
        and state.config_revision == manifest.config_revision
        and state.config_sha256 == manifest.config_sha256
    )


class RuntimeBootstrapper:
    """Claim, atomically install, activate, and persist the activation receipt."""

    def __init__(
        self,
        client: BootstrapClient,
        *,
        runtime_ref: str,
        household_id: str | None = None,
        config_revision: int | None = None,
        manifest_path: Path | str = DEFAULT_MANIFEST_PATH,
        activation_path: Path | str = DEFAULT_ACTIVATION_STATE,
        env: Mapping[str, str] | None = None,
        email_health_checker: Callable[[RuntimeManifest], tuple[str, str]] | None = None,
        clock=time.time,
    ) -> None:
        if not runtime_ref:
            raise BootstrapError("runtime_ref is required")
        self.client = client
        self.runtime_ref = runtime_ref
        self.manifest_path = Path(manifest_path)
        self.activation_path = Path(activation_path)
        self.env = dict(os.environ if env is None else env)
        self.household_id = (household_id or self.env.get(ENV_HOUSEHOLD_ID) or "").strip()
        raw_revision: str | int | None = config_revision
        if raw_revision is None:
            raw_revision = self.env.get(ENV_CONFIG_REVISION)
        try:
            self.config_revision = int(raw_revision) if raw_revision is not None else 0
        except (TypeError, ValueError) as error:
            raise BootstrapError(f"{ENV_CONFIG_REVISION} must be an integer") from error
        if self.config_revision < 0:
            raise BootstrapError(f"{ENV_CONFIG_REVISION} must be positive")
        self.clock = clock
        self.email_health_checker = email_health_checker or (lambda _manifest: ("healthy", "healthy"))

    def _load_installed(self) -> RuntimeManifest:
        try:
            return load_runtime_manifest(self.manifest_path, env=self.env)
        except ManifestError as error:
            raise BootstrapError(f"installed manifest is invalid: {error}") from error

    def _activate(
        self, token: str, manifest: RuntimeManifest, *, state: ActivationState | None = None
    ) -> RuntimeManifest:
        inbound_check, outbound_check = self.email_health_checker(manifest)
        if inbound_check not in {"healthy", "failed"} or outbound_check not in {
            "healthy",
            "failed",
        }:
            raise BootstrapError("email activation checker returned an invalid state")
        receipt = ActivationReceipt(
            runtime_ref=self.runtime_ref,
            household_id=manifest.household_id,
            config_revision=manifest.config_revision,
            config_sha256=manifest.config_sha256,
            email_inbound_check=inbound_check,
            email_outbound_check=outbound_check,
            email_receipt_digest=hashlib.sha256(
                (
                    f"email-health:{manifest.email.provider_kind}:"
                    f"{manifest.config_revision}:{manifest.config_sha256}:"
                    f"{inbound_check}:{outbound_check}"
                ).encode()
            ).hexdigest(),
        )
        activating = state or ActivationState(status="activating", updated_at=self.clock(), **asdict(receipt))
        try:
            write_activation_state(self.activation_path, activating)
        except OSError as error:
            raise BootstrapError("cannot persist activation state") from error
        activated = self.client.activate(token, receipt)
        if activated != receipt:
            raise BootstrapError("activation receipt does not match installed manifest")
        try:
            write_activation_state(
                self.activation_path,
                ActivationState(status="active", updated_at=self.clock(), **asdict(receipt)),
            )
        except OSError as error:
            raise BootstrapError("cannot persist active revision receipt") from error
        acknowledged = self.client.acknowledge(token, receipt)
        if acknowledged != receipt:
            raise BootstrapError("cleanup acknowledgement does not match active revision")
        return manifest

    def run(self, token: str) -> RuntimeManifest:
        state = load_activation_state(self.activation_path)
        if state is not None and state.runtime_ref != self.runtime_ref:
            raise BootstrapError("activation state belongs to another runtime")

        if state is not None and self.manifest_path.is_file():
            manifest = self._load_installed()
            if not state_matches_manifest(state, manifest):
                raise BootstrapError("activation state does not match installed manifest")
            if state.status == "active":
                if token:
                    acknowledged = self.client.acknowledge(token, state.receipt())
                    if acknowledged != state.receipt():
                        raise BootstrapError("cleanup acknowledgement does not match active revision")
                return manifest
            if state.status == "activating":
                # A crash or unknown response after activate resumes the same
                # callback without consuming another claim or revision.
                if not token:
                    raise BootstrapError("bootstrap token is required to resume activation")
                return self._activate(token, manifest, state=state)
            raise BootstrapError("activation state has an unsupported status")

        if not token:
            raise BootstrapError("bootstrap token is required")
        if not self.household_id or self.config_revision < 1:
            raise BootstrapError(f"fresh bootstrap requires {ENV_HOUSEHOLD_ID} and {ENV_CONFIG_REVISION}")

        claim = self.client.claim(
            token,
            household_id=self.household_id,
            runtime_ref=self.runtime_ref,
            config_revision=self.config_revision,
        )
        if claim.runtime_ref != self.runtime_ref:
            raise BootstrapError("claim is bound to another runtime")
        try:
            manifest = parse_runtime_manifest(claim.manifest_toml, env=self.env, source="bootstrap claim")
        except ManifestError as error:
            raise BootstrapError(f"claimed manifest is invalid: {error}") from error
        if (
            claim.household_id != self.household_id
            or claim.household_id != manifest.household_id
            or claim.config_revision != self.config_revision
            or claim.config_revision != manifest.config_revision
            or claim.config_sha256.casefold() != manifest.config_sha256
        ):
            raise BootstrapError("claim metadata does not match manifest")
        try:
            atomic_write(self.manifest_path, claim.manifest_toml.encode())
        except (OSError, UnicodeError) as error:
            raise BootstrapError("cannot persist claimed manifest") from error
        # Re-read the exact bytes from their durable destination before activation.
        installed = self._load_installed()
        return self._activate(token, installed)

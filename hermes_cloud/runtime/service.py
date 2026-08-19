"""Health/readiness boundary that keeps runtime work disabled until activation."""

from __future__ import annotations

import hmac
import json
import os
import socket
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

import httpx

from control_plane.privacy.consent import consent_version_and_sha
from hermes_cloud.core.config import DEFAULT_DB_PATH, ENV_DB
from hermes_cloud.core.db import open_database
from hermes_cloud.core.dsar import export_household, is_deleted, wipe_household
from hermes_cloud.core.events import EventStore
from hermes_cloud.core.runtime_manifest import (
    ENV_CONFIG_REVISION,
    ENV_HOUSEHOLD_FILE,
    ENV_HOUSEHOLD_ID,
    ManifestError,
    RuntimeManifest,
    load_runtime_manifest,
)
from hermes_cloud.email.contracts import EmailBinding
from hermes_cloud.email.google_client import (
    GmailConfigurationError,
    GmailHttpClient,
    build_gmail_client,
    ensure_gmail_grant,
    load_gmail_credential_bundle,
)
from hermes_cloud.email.google_grant import GoogleGrantError, GoogleGrantStore
from hermes_cloud.email.nerve_client import (
    DEFAULT_REST_URL,
    DEFAULT_RUNTIME_URL,
    NerveEmailClient,
)
from hermes_cloud.email.receipts import EmailBindingStore
from hermes_cloud.email.service import EmailRuntimeService
from hermes_cloud.ingest.gmail_api import GmailHistorySource
from hermes_cloud.ingest.nerve_webhook import (
    MAX_WEBHOOK_BYTES,
    NerveAttachmentWorker,
    NerveWebhookReceiver,
    NerveWebhookRejected,
    NerveWebhookStore,
)
from hermes_cloud.ingest.whatsapp_webhook import (
    MAX_WHATSAPP_WEBHOOK_BYTES,
    WhatsAppWebhookReceiver,
    WhatsAppWebhookRejected,
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
ENV_RUNTIME_CONSENT_MARKER = "HERMES_RUNTIME_CONSENT_MARKER"
ENV_NERVE_RUNTIME_URL = "ABROLIA_NERVE_RUNTIME_URL"
ENV_NERVE_REST_URL = "ABROLIA_NERVE_REST_URL"
ENV_NERVE_WORKER_SECONDS = "ABROLIA_NERVE_WORKER_SECONDS"
ENV_GMAIL_WORKER_SECONDS = "ABROLIA_GMAIL_WORKER_SECONDS"
ENV_WHATSAPP_INSTANCE = "HERMES_WHATSAPP_INSTANCE"
ENV_WHATSAPP_RELAY_SECRET = "HERMES_WHATSAPP_RELAY_SECRET"
REQUIRED_CONTENT_RESTRICTION_PURPOSE = "special_category_content_restriction"


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


@dataclass(frozen=True)
class WhatsAppRuntimeConfig:
    instance: str
    relay_secret: str


class RuntimeService:
    def __init__(
        self,
        *,
        manifest_path: Path | str | None = None,
        activation_path: Path | str | None = None,
        runtime_ref: str | None = None,
        env: Mapping[str, str] | None = None,
        nerve_client_factory: Callable[..., Any] = NerveEmailClient,
        gmail_client_factory: Callable[..., Any] = GmailHttpClient,
    ) -> None:
        self.env = dict(os.environ if env is None else env)
        self.manifest_path = Path(manifest_path or self.env.get(ENV_HOUSEHOLD_FILE) or DEFAULT_MANIFEST_PATH)
        self.activation_path = Path(
            activation_path or self.env.get(ENV_ACTIVATION_STATE) or DEFAULT_ACTIVATION_STATE
        )
        self.runtime_ref = runtime_ref or self.env.get(ENV_RUNTIME_REF) or ""
        self.nerve_client_factory = nerve_client_factory
        self.gmail_client_factory = gmail_client_factory
        self.database_path = Path(self.env.get(ENV_DB) or DEFAULT_DB_PATH)
        self.deletion_marker = Path(
            self.env.get(ENV_RUNTIME_DELETION_MARKER)
            or self.activation_path.with_name("runtime-deleted.json")
        )
        # Withdrawal has to stop a runtime that is ALREADY serving, and the
        # manifest it serves is immutable — the receipt embedded in it stays
        # valid-looking forever. A local marker is the only signal that reaches
        # a running instance, which is why deletion already works this way.
        self.consent_marker = Path(
            self.env.get(ENV_RUNTIME_CONSENT_MARKER)
            or self.activation_path.with_name("consent-withdrawn.json")
        )
        self._gmail_runtime: EmailRuntimeService | None = None
        self._gmail_client: Any | None = None
        self._gmail_database: Any | None = None
        self._gmail_binding_key: tuple[str, int] | None = None

    def health(self) -> Probe:
        """Public /health for pilot observability (E7): no content, safe for logs."""
        try:
            with open_database(self.database_path) as database:
                db_ok = database.query_one("SELECT 1") is not None  # type: ignore[attr-defined]
        except Exception:
            db_ok = False
        # Provider checks are best-effort and do not leak secrets.
        nerve_key_ok = bool(
            self.env.get("HERMES_NERVE_RUNTIME_KEY") or self.env.get("ABROLIA_NERVE_RUNTIME_KEY")
        )
        telegram_ok = bool(self.env.get("TELEGRAM_BOT_TOKEN"))
        wa_ok = bool(
            self.env.get("HERMES_WHATSAPP_INSTANCE") or self.env.get("HERMES_WHATSAPP_RELAY_SECRET")
        )
        google_grant_ok: bool | None = None
        try:
            manifest, _ = self._ready_manifest()
            if manifest is not None:
                # gmail grant presence check
                google_grant_ok = self._gmail_grant_ok()
        except Exception:
            google_grant_ok = None
        # backup age via control-plane marker if runtime db has backup info? pilot uses control-plane db.
        backup_age_hours: float | None = None
        try:
            from control_plane.db import ControlPlaneDatabase
            from control_plane.observability import HealthReporter

            cp_db_path = self.env.get("ABROLIA_CONTROL_PLANE_DB")
            if cp_db_path:
                cp_db = ControlPlaneDatabase(cp_db_path)
                reporter = HealthReporter(cp_db)
                latest = reporter.latest_backup_completed_at()
                if latest is not None:
                    import time as _t

                    backup_age_hours = (_t.time() - latest) / 3600.0
        except Exception:
            pass
        payload: dict[str, Any] = {
            "status": "ok" if db_ok else "degraded",
            "nerve_key_ok": nerve_key_ok,
            "telegram_ok": telegram_ok,
            "wa_instance_ok": wa_ok,
            "google_grant_ok": google_grant_ok,
            "db_ok": db_ok,
            "backup_age_hours": backup_age_hours,
        }
        if backup_age_hours is not None and backup_age_hours > 30 * 24:
            payload["needs_attention"] = True
        return Probe(200 if db_ok else 503, payload)

    def _gmail_grant_ok(self) -> bool | None:
        try:
            with open_database(self.database_path) as database:
                row = database.query_one("SELECT COUNT(*) as c FROM oauth_grants WHERE revoked_at IS NULL")
                return bool(row and row["c"] > 0) if row else False
        except Exception:
            return None

    def healthz(self) -> Probe:
        # Liveness deliberately does not depend on bootstrap/control-plane state.
        return Probe(200, {"status": "ok"})

    def _ready_manifest(self) -> tuple[RuntimeManifest | None, str]:
        if self.deletion_marker.is_file():
            return None, "runtime_deleted"
        if self.consent_marker.is_file():
            # Checked before the manifest is even loaded: the manifest cannot
            # express this state, and the Art 9(2)(a) copy promises withdrawal
            # "stops further processing", not "stops it at the next delivery".
            return None, "consent_withdrawn"
        try:
            manifest = load_runtime_manifest(self.manifest_path, env=self.env)
        except ManifestError:
            return None, "manifest_missing_or_invalid"
        # Enforce EVERY purpose the manifest declares authoritative, not one
        # hard-coded name. The control plane already decides which consents a
        # household owes — including the Art. 9(2)(a) household-content consent
        # for real-email households — and naming a single purpose here meant a
        # runtime stayed ready while any other required consent was revoked or
        # superseded.
        if manifest.consent is None:
            # A manifest with no consent block is the legacy shape, and it is
            # the S5 restriction it is missing — keep the established reason.
            return None, "content_restriction_not_current"
        if REQUIRED_CONTENT_RESTRICTION_PURPOSE not in (
            manifest.consent.required_purposes
        ):
            # The S5 boundary is required of every household, synthetic or not,
            # so a manifest that omits it is malformed rather than permissive.
            return None, "content_restriction_not_current"
        for purpose in manifest.consent.required_purposes:
            version, sha256 = consent_version_and_sha(purpose)
            if not any(
                receipt.purpose == purpose
                and receipt.text_version == version
                and hmac.compare_digest(receipt.text_sha256, sha256)
                for receipt in manifest.consent.receipts
            ):
                if purpose == REQUIRED_CONTENT_RESTRICTION_PURPOSE:
                    return None, "content_restriction_not_current"
                return None, "consent_not_current"
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
        binding = self._email_binding_from_manifest(manifest)
        if binding is None:
            return None
        with open_database(self.database_path) as database:
            active = EmailBindingStore(database).activate(binding)
            if active.provider == "gmail":
                self._install_gmail_grant(database, active)
            return active

    @staticmethod
    def _email_binding_from_manifest(manifest: RuntimeManifest) -> EmailBinding | None:
        identity_id = manifest.email.provider_binding_ref
        if not identity_id:
            return None
        return EmailBinding(
            identity_id=identity_id,
            revision=manifest.config_revision,
            provider=manifest.email.provider_kind,
            address=manifest.email.agent_inbox,
            provider_ref=identity_id,
            secret_names=((manifest.email.secret_binding_ref,) if manifest.email.secret_binding_ref else ()),
        )

    def _gmail_bundle(self, binding: EmailBinding):
        try:
            return load_gmail_credential_bundle(binding, self.env)
        except GmailConfigurationError as error:
            raise RuntimeNotReady("Gmail credential bundle is unavailable") from error

    def _install_gmail_grant(self, database, binding: EmailBinding) -> None:
        try:
            ensure_gmail_grant(database, binding, self._gmail_bundle(binding))
        except GmailConfigurationError as error:
            raise RuntimeNotReady("Gmail grant is unavailable") from error

    def _revoke_google_credential(self, manifest: RuntimeManifest) -> bool:
        binding = self._email_binding_from_manifest(manifest)
        if binding is None or binding.provider != "gmail":
            return True
        bundle = self._gmail_bundle(binding)
        with open_database(self.database_path) as database:
            store = GoogleGrantStore(database, {1: bundle.wrapping_key}, active_version=1)
            row = database.query_one(
                "SELECT revoked_at FROM oauth_grants WHERE binding_identity_id = ?"
                " AND binding_revision = ?",
                (binding.identity_id, binding.revision),
            )
            if row is None or row["revoked_at"] is not None:
                return True
            try:
                grant = store.load(binding.identity_id, binding.revision)
            except GoogleGrantError:
                return False
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
        self._close_gmail_runtime()
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
            bundle = self._gmail_bundle(binding)
            with open_database(self.database_path) as database:
                client = build_gmail_client(
                    database,
                    binding,
                    bundle,
                    client_factory=self.gmail_client_factory,
                )
                try:
                    profile = client.profile()
                    inbound = "healthy" if profile.get("historyId") else "failed"
                    scopes = set(bundle.scopes)
                    outbound = (
                        "healthy"
                        if "https://www.googleapis.com/auth/gmail.send" in scopes
                        else "failed"
                    )
                    return inbound, outbound
                finally:
                    close = getattr(client, "close", None)
                    if close is not None:
                        close()
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

    def _close_gmail_runtime(self) -> None:
        close = getattr(self._gmail_client, "close", None)
        if close is not None:
            close()
        if self._gmail_database is not None:
            self._gmail_database.close()
        self._gmail_runtime = None
        self._gmail_client = None
        self._gmail_database = None
        self._gmail_binding_key = None

    def run_gmail_once(self) -> int:
        manifest = self.require_ready()
        binding = self._sync_email_binding(manifest)
        if binding is None or binding.provider != "gmail":
            self._close_gmail_runtime()
            raise RuntimeNotReady("runtime email provider is not Gmail")
        binding_key = (binding.identity_id, binding.revision)
        if self._gmail_runtime is None or self._gmail_binding_key != binding_key:
            self._close_gmail_runtime()
            database = open_database(self.database_path)
            try:
                bundle = self._gmail_bundle(binding)
                client = build_gmail_client(
                    database,
                    binding,
                    bundle,
                    client_factory=self.gmail_client_factory,
                )
                source = GmailHistorySource(database, binding, client)
                runtime = EmailRuntimeService(database, (source,))
            except Exception:
                database.close()
                raise
            self._gmail_database = database
            self._gmail_client = client
            self._gmail_runtime = runtime
            self._gmail_binding_key = binding_key
        return self._gmail_runtime.run_once()

    def close(self) -> None:
        self._close_gmail_runtime()

    def _whatsapp_config(self, manifest: RuntimeManifest) -> WhatsAppRuntimeConfig:
        if not any(binding.channel == "whatsapp" for binding in manifest.verified_bindings):
            raise RuntimeNotReady("WhatsApp is not bound to this household")
        instance = self.env.get(ENV_WHATSAPP_INSTANCE, "").strip()
        relay_secret = self.env.get(ENV_WHATSAPP_RELAY_SECRET, "").strip()
        if not instance or not relay_secret:
            raise RuntimeNotReady("WhatsApp runtime configuration is incomplete")
        return WhatsAppRuntimeConfig(instance=instance, relay_secret=relay_secret)

    def receive_whatsapp_webhook(self, payload: bytes, signature: str):
        manifest = self.require_ready()
        config = self._whatsapp_config(manifest)
        with open_database(self.database_path) as database:
            return WhatsAppWebhookReceiver(
                EventStore(database),
                signing_secret=config.relay_secret,
                instance=config.instance,
            ).receive(payload, signature)

    def __call__(self, environ: Mapping[str, Any], start_response: Callable) -> list[bytes]:
        """Health probes plus an authenticated private DSAR boundary."""
        import time as _t

        from hermes_cloud.core.observability import RuntimeStructuredLogger

        start = _t.time()
        path = str(environ.get("PATH_INFO") or "")
        method = str(environ.get("REQUEST_METHOD") or "GET").upper()
        if path == "/v1/email/nerve/webhook" and method == "POST":
            probe = self._nerve_webhook(environ)
        elif path in {"/v1/whatsapp/webhook", "/webhooks/whatsapp"} and method == "POST":
            probe = self._whatsapp_webhook(environ)
        elif path == "/internal/v1/consent/revoke":
            probe = self._consent_revoke(method, str(environ.get("HTTP_AUTHORIZATION") or ""))
        elif path in {"/internal/v1/dsar/export", "/internal/v1/dsar/delete"}:
            probe = self._dsar(path, method, str(environ.get("HTTP_AUTHORIZATION") or ""))
        elif path == "/internal/v1/email/google/revoke":
            probe = self._google_revoke(method, str(environ.get("HTTP_AUTHORIZATION") or ""))
        elif path == "/health" and method == "GET":
            probe = self.health()
        elif method != "GET" or path not in {"/healthz", "/readyz"}:
            probe = Probe(404, {"status": "not_found"})
        else:
            probe = self.healthz() if path == "/healthz" else self.readyz()
        # Structured observability: one JSON line per request, no content
        try:
            hmac_key = (
                self.env.get("ABROLIA_HMAC_KEY") or self.env.get("HERMES_HOUSEHOLD_HMAC_KEY") or ""
            ).encode()
            if len(hmac_key) >= 16:
                logger = RuntimeStructuredLogger(sys.stdout, hmac_key=hmac_key)
                latency_ms = int((_t.time() - start) * 1000)
                logger.emit(
                    level="info",
                    route=path,
                    status=probe.status_code,
                    latency_ms=latency_ms,
                    request_id=environ.get("HTTP_X_REQUEST_ID"),
                )
        except Exception:
            pass
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

    def _whatsapp_webhook(self, environ: Mapping[str, Any]) -> Probe:
        try:
            length = int(str(environ.get("CONTENT_LENGTH") or "0"))
        except ValueError:
            return Probe(400, {"status": "invalid_request"})
        if length < 1:
            return Probe(400, {"status": "invalid_request"})
        if length > MAX_WHATSAPP_WEBHOOK_BYTES:
            return Probe(413, {"status": "payload_too_large"})
        stream = environ.get("wsgi.input")
        if stream is None or not hasattr(stream, "read"):
            return Probe(400, {"status": "invalid_request"})
        payload = stream.read(length)
        if not isinstance(payload, bytes) or len(payload) != length:
            return Probe(400, {"status": "invalid_request"})
        signature = str(environ.get("HTTP_X_RELAY_SIGNATURE") or "")
        try:
            accepted = self.receive_whatsapp_webhook(payload, signature)
        except WhatsAppWebhookRejected as error:
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

    def _consent_revoke(self, method: str, authorization: str) -> Probe:
        """Stop serving because a consent behind this runtime was withdrawn.

        Deliberately unconditional once authenticated: it must succeed on a
        runtime that is already deleted, not yet active, or serving a manifest
        that cannot be parsed. Withdrawal that fails because the runtime is in
        an awkward state is withdrawal that did not happen, and Art. 7(3) gives
        the family a right to it, not an attempt at it. Idempotent — the marker
        is the state, and re-posting rewrites the same file.
        """
        expected = self.env.get(ENV_RUNTIME_DSAR_TOKEN, "")
        if method != "POST" or not expected:
            return Probe(404, {"status": "not_found"})
        prefix = "Bearer "
        supplied = (
            authorization[len(prefix) :] if authorization.startswith(prefix) else ""
        )
        if not supplied or not hmac.compare_digest(supplied, expected):
            return Probe(401, {"status": "unauthorized"})
        try:
            atomic_write(self.consent_marker, b'{"status":"consent_withdrawn"}')
        except OSError:
            # The control plane retries; reporting success here would record a
            # withdrawal that never reached disk and would not survive a restart.
            return Probe(503, {"status": "consent_marker_unavailable"})
        return Probe(200, {"state": "consent_withdrawn"})

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
            manifest, _reason = self._ready_manifest()
            if manifest is None:
                raise RuntimeNotReady("runtime is not active")
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
            manifest, _reason = self._ready_manifest()
            if manifest is None:
                raise RuntimeNotReady("runtime is not active")
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


def _gmail_worker_until_stopped(
    service: RuntimeService,
    source: Mapping[str, str],
    stop: threading.Event,
) -> None:
    try:
        interval = max(float(source.get(ENV_GMAIL_WORKER_SECONDS, "60")), 0.1)
    except ValueError:
        interval = 60.0
    while not stop.is_set():
        if service.can_start_workers:
            try:
                service.run_gmail_once()
            except RuntimeNotReady:
                pass
            except Exception as error:
                print(
                    f"Gmail ingress pending ({error.__class__.__name__})",
                    file=sys.stderr,
                )
        stop.wait(interval)


class _QuietRequestHandler(WSGIRequestHandler):
    def log_message(self, _format: str, *args: object) -> None:
        """Health requests are intentionally absent from application logs."""


class _DualStackWSGIServer(WSGIServer):
    """Accept Fly 6PN IPv6 and local IPv4 traffic on one runtime socket."""

    address_family = socket.AF_INET6

    def server_bind(self) -> None:
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def _runtime_server_binding(host: str) -> tuple[str, type[WSGIServer] | None]:
    if host in {"0.0.0.0", "::"}:
        return "::", _DualStackWSGIServer
    return host, None


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
    server_host, server_class = _runtime_server_binding(host)
    if server_class is None:
        server = make_server(
            server_host,
            port,
            service,
            handler_class=_QuietRequestHandler,
        )
    else:
        server = make_server(
            server_host,
            port,
            service,
            server_class=server_class,
            handler_class=_QuietRequestHandler,
        )
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
    gmail_worker = threading.Thread(
        target=_gmail_worker_until_stopped,
        args=(service, source, stop),
        name="gmail-history",
        daemon=True,
    )
    worker.start()
    nerve_worker.start()
    gmail_worker.start()
    try:
        server.serve_forever()
    finally:
        stop.set()
        worker.join(timeout=1.0)
        nerve_worker.join(timeout=1.0)
        gmail_worker.join(timeout=1.0)
        service.close()
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

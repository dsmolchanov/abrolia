"""Fail-closed Nerve webhook journal and asynchronous RFC822 materializer."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import PurePath
from typing import Any, Protocol

from hermes_cloud.core.db import Database, new_id
from hermes_cloud.core.dsar import is_deleted
from hermes_cloud.core.events import DEFAULT_LEASE_SECONDS, DEFAULT_MAX_ATTEMPTS, EventStore
from hermes_cloud.email.contracts import EmailBinding
from hermes_cloud.email.nerve_client import NerveCredentialRevoked
from hermes_cloud.ingest.rfc822 import ingest_rfc822

DEFAULT_MAX_SKEW_SECONDS = 5 * 60
MAX_WEBHOOK_BYTES = 64 * 1024
MAX_ATTACHMENT_COUNT = 10
MAX_ATTACHMENT_BYTES = 10 << 20
MAX_ATTACHMENT_TOTAL_BYTES = 20 << 20
ATTACHMENT_RETENTION_DAYS = 30
ATTACHMENT_CLASSIFICATION = "email_attachment"
ALLOWED_ATTACHMENT_TYPES = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "image/jpeg",
        "image/png",
        "image/webp",
        "text/plain",
    }
)
_HEX_SHA256 = re.compile(r"[0-9a-f]{64}")


class NerveWebhookRejected(ValueError):
    status_code = 400
    code = "invalid_webhook"


class NerveWebhookUnauthorized(NerveWebhookRejected):
    status_code = 401
    code = "invalid_signature"


class NerveWebhookReplay(NerveWebhookRejected):
    status_code = 409
    code = "signature_replay"


class NerveWebhookTenantMismatch(NerveWebhookRejected):
    status_code = 403
    code = "tenant_mismatch"


@dataclass(frozen=True)
class NerveWebhookEvent:
    id: str
    identity_id: str
    binding_revision: int
    org_id: str
    inbox_id: str
    thread_id: str
    message_id: str
    attachment_count: int
    state: str
    attempts: int
    received_at: float


@dataclass(frozen=True)
class NerveWebhookAccepted:
    event: NerveWebhookEvent
    created: bool


@dataclass(frozen=True)
class NerveMaterialized:
    nerve_event_id: str
    canonical_event_id: str
    created: bool
    attachment_count: int


class NerveInboundClient(Protocol):
    def get_thread(self, inbox_id: str, thread_id: str) -> dict[str, Any]: ...

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes: ...


def _signature_parts(value: str) -> tuple[int, str]:
    parts: dict[str, str] = {}
    for item in value.split(","):
        key, separator, raw = item.strip().partition("=")
        if not separator or key in parts:
            raise NerveWebhookUnauthorized("malformed signature")
        parts[key] = raw
    if set(parts) != {"t", "v1"} or not _HEX_SHA256.fullmatch(parts["v1"]):
        raise NerveWebhookUnauthorized("malformed signature")
    try:
        timestamp = int(parts["t"])
    except ValueError as error:
        raise NerveWebhookUnauthorized("malformed signature") from error
    return timestamp, parts["v1"]


def verify_nerve_signature(
    *,
    payload: bytes,
    signature: str,
    secret: str,
    now: float | None = None,
    max_skew_seconds: int = DEFAULT_MAX_SKEW_SECONDS,
) -> tuple[int, str]:
    if not secret or not signature or len(payload) > MAX_WEBHOOK_BYTES:
        raise NerveWebhookUnauthorized("signature cannot be verified")
    timestamp, supplied = _signature_parts(signature)
    now = time.time() if now is None else now
    if abs(now - timestamp) > max_skew_seconds:
        raise NerveWebhookUnauthorized("signature timestamp expired")
    expected = hmac.new(
        secret.encode(), str(timestamp).encode() + b"." + payload, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, supplied):
        raise NerveWebhookUnauthorized("signature mismatch")
    # Replay identity is the signed tuple, not the caller's whitespace/casing
    # representation of the header.
    digest = hashlib.sha256(f"{timestamp}:{supplied}".encode()).hexdigest()
    return timestamp, digest


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise NerveWebhookRejected(f"invalid {key}")
    return value.strip()


def _required_provider_id(payload: dict[str, Any], key: str) -> str:
    value = _required_text(payload, key)
    try:
        uuid.UUID(value)
    except ValueError as error:
        raise NerveWebhookRejected(f"invalid {key}") from error
    return value


def _event_from_row(row: Any) -> NerveWebhookEvent:
    return NerveWebhookEvent(
        id=row["id"],
        identity_id=row["binding_identity_id"],
        binding_revision=row["binding_revision"],
        org_id=row["org_id"],
        inbox_id=row["inbox_id"],
        thread_id=row["thread_id"],
        message_id=row["message_id"],
        attachment_count=row["attachment_count"],
        state=row["state"],
        attempts=row["attempts"],
        received_at=row["received_at"],
    )


class NerveWebhookStore:
    def __init__(self, database: Database, *, clock=time.time) -> None:
        self.db = database
        self.clock = clock

    def append(
        self,
        *,
        binding: EmailBinding,
        expected_org_id: str,
        expected_inbox_id: str,
        payload: bytes,
        signature: str,
        signing_secret: str,
        received_at: float | None = None,
    ) -> NerveWebhookAccepted:
        now = self.clock() if received_at is None else received_at
        timestamp, signature_digest = verify_nerve_signature(
            payload=payload,
            signature=signature,
            secret=signing_secret,
            now=now,
        )
        try:
            decoded = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise NerveWebhookRejected("invalid JSON") from error
        if not isinstance(decoded, dict) or decoded.get("event") != "email.received":
            raise NerveWebhookRejected("unsupported event")
        org_id = _required_provider_id(decoded, "org_id")
        inbox_id = _required_provider_id(decoded, "inbox_id")
        thread_id = _required_provider_id(decoded, "thread_id")
        message_id = _required_provider_id(decoded, "message_id")
        if org_id != expected_org_id or inbox_id != expected_inbox_id:
            raise NerveWebhookTenantMismatch("webhook tenant does not match binding")
        attachment_count = decoded.get("attachment_count", 0)
        if (
            isinstance(attachment_count, bool)
            or not isinstance(attachment_count, int)
            or not 0 <= attachment_count <= MAX_ATTACHMENT_COUNT
        ):
            raise NerveWebhookRejected("invalid attachment count")
        payload_digest = hashlib.sha256(payload).hexdigest()
        event_id = new_id()
        if is_deleted(self.db):
            raise NerveWebhookRejected("household deleted")
        with self.db.write() as connection:
            replay = connection.execute(
                "SELECT nerve_event_id FROM nerve_webhook_signatures"
                " WHERE signature_sha256 = ?",
                (signature_digest,),
            ).fetchone()
            if replay is not None:
                raise NerveWebhookReplay("signature was already consumed")
            prior = connection.execute(
                "SELECT * FROM nerve_webhook_events WHERE binding_identity_id = ?"
                " AND binding_revision = ? AND message_id = ?",
                (binding.identity_id, binding.revision, message_id),
            ).fetchone()
            if prior is not None:
                if prior["payload_sha256"] != payload_digest:
                    raise NerveWebhookReplay("message replay changed payload")
                event_id = prior["id"]
            else:
                connection.execute(
                    "INSERT INTO nerve_webhook_events (id, binding_identity_id,"
                    " binding_revision, org_id, inbox_id, thread_id, message_id,"
                    " attachment_count, payload, payload_sha256, signature_sha256,"
                    " webhook_timestamp, state, received_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
                    (
                        event_id,
                        binding.identity_id,
                        binding.revision,
                        org_id,
                        inbox_id,
                        thread_id,
                        message_id,
                        attachment_count,
                        payload,
                        payload_digest,
                        signature_digest,
                        timestamp,
                        now,
                        now,
                    ),
                )
            connection.execute(
                "INSERT INTO nerve_webhook_signatures"
                " (signature_sha256, nerve_event_id, seen_at) VALUES (?, ?, ?)",
                (signature_digest, event_id, now),
            )
            connection.execute(
                "INSERT INTO nerve_runtime_health (binding_identity_id, binding_revision,"
                " last_webhook_at, credential_state, updated_at) VALUES (?, ?, ?, 'unknown', ?)"
                " ON CONFLICT (binding_identity_id, binding_revision) DO UPDATE SET"
                " last_webhook_at = excluded.last_webhook_at, updated_at = excluded.updated_at",
                (binding.identity_id, binding.revision, now, now),
            )
        row = self.db.query_one("SELECT * FROM nerve_webhook_events WHERE id = ?", (event_id,))
        assert row is not None
        return NerveWebhookAccepted(_event_from_row(row), prior is None)

    def lease(
        self,
        worker_id: str,
        *,
        now: float | None = None,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> NerveWebhookEvent | None:
        now = self.clock() if now is None else now
        with self.db.write() as connection:
            row = connection.execute(
                "SELECT * FROM nerve_webhook_events WHERE state = 'queued'"
                " OR (state = 'processing' AND lease_until <= ?)"
                " ORDER BY received_at, id LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE nerve_webhook_events SET state = 'processing', lease_until = ?,"
                " leased_by = ?, updated_at = ? WHERE id = ?",
                (now + lease_seconds, worker_id, now, row["id"]),
            )
        updated = self.db.query_one(
            "SELECT * FROM nerve_webhook_events WHERE id = ?", (row["id"],)
        )
        assert updated is not None
        return _event_from_row(updated)

    def cached_attachment(self, event_id: str, attachment_id: str) -> Any | None:
        return self.db.query_one(
            "SELECT * FROM nerve_attachments WHERE nerve_event_id = ?"
            " AND provider_attachment_id = ?",
            (event_id, attachment_id),
        )

    def store_attachment(
        self,
        *,
        event: NerveWebhookEvent,
        attachment_id: str,
        filename: str,
        content_type: str,
        expected_size: int | None,
        content: bytes,
        now: float | None = None,
    ) -> None:
        now = self.clock() if now is None else now
        digest = hashlib.sha256(content).hexdigest()
        with self.db.write() as connection:
            existing = connection.execute(
                "SELECT * FROM nerve_attachments WHERE nerve_event_id = ?"
                " AND provider_attachment_id = ?",
                (event.id, attachment_id),
            ).fetchone()
            if existing is not None:
                if existing["content_sha256"] != digest:
                    raise ValueError("attachment identity changed content")
                return
            connection.execute(
                "INSERT INTO nerve_attachments (id, nerve_event_id, message_id,"
                " provider_attachment_id, filename, content_type, expected_size,"
                " actual_size, content_sha256, classification, content, retention_until,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id(),
                    event.id,
                    event.message_id,
                    attachment_id,
                    filename,
                    content_type,
                    expected_size,
                    len(content),
                    digest,
                    ATTACHMENT_CLASSIFICATION,
                    content,
                    now + ATTACHMENT_RETENTION_DAYS * 86_400,
                    now,
                ),
            )

    def mark_materialized(
        self, event: NerveWebhookEvent, canonical_event_id: str, *, now: float | None = None
    ) -> None:
        now = self.clock() if now is None else now
        with self.db.write() as connection:
            connection.execute(
                "UPDATE nerve_webhook_events SET state = 'materialized',"
                " canonical_event_id = ?, lease_until = NULL, leased_by = NULL,"
                " last_error_code = NULL, updated_at = ? WHERE id = ?",
                (canonical_event_id, now, event.id),
            )
            connection.execute(
                "UPDATE nerve_runtime_health SET last_materialized_at = ?,"
                " credential_state = 'valid', last_error_code = NULL, updated_at = ?"
                " WHERE binding_identity_id = ? AND binding_revision = ?",
                (now, now, event.identity_id, event.binding_revision),
            )

    def mark_failed(
        self,
        event: NerveWebhookEvent,
        error_code: str,
        *,
        credential_revoked: bool = False,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        now: float | None = None,
    ) -> str:
        now = self.clock() if now is None else now
        attempts = event.attempts + 1
        state = "dlq" if attempts >= max_attempts else "queued"
        with self.db.write() as connection:
            connection.execute(
                "UPDATE nerve_webhook_events SET state = ?, attempts = ?,"
                " lease_until = NULL, leased_by = NULL, last_error_code = ?,"
                " updated_at = ? WHERE id = ?",
                (state, attempts, error_code[:128], now, event.id),
            )
            connection.execute(
                "UPDATE nerve_runtime_health SET credential_state = ?,"
                " last_error_code = ?, updated_at = ? WHERE binding_identity_id = ?"
                " AND binding_revision = ?",
                (
                    "revoked" if credential_revoked else "unknown",
                    error_code[:128],
                    now,
                    event.identity_id,
                    event.binding_revision,
                ),
            )
        return state

    def health(self, binding: EmailBinding, *, now: float | None = None) -> dict[str, Any]:
        now = self.clock() if now is None else now
        row = self.db.query_one(
            "SELECT * FROM nerve_runtime_health WHERE binding_identity_id = ?"
            " AND binding_revision = ?",
            (binding.identity_id, binding.revision),
        )
        counts = self.db.query_one(
            "SELECT SUM(CASE WHEN state = 'dlq' THEN 1 ELSE 0 END) AS dlq_count,"
            " SUM(CASE WHEN last_error_code LIKE 'attachment_%' THEN 1 ELSE 0 END)"
            " AS attachment_failures FROM nerve_webhook_events"
            " WHERE binding_identity_id = ? AND binding_revision = ?",
            (binding.identity_id, binding.revision),
        )
        last_webhook = row["last_webhook_at"] if row else None
        revoked = bool(row and row["credential_state"] == "revoked")
        dlq_count = int((counts["dlq_count"] if counts else 0) or 0)
        attachment_failures = int((counts["attachment_failures"] if counts else 0) or 0)
        return {
            "status": "degraded" if revoked or dlq_count or attachment_failures else "ready",
            "webhook_age_seconds": max(0.0, now - last_webhook) if last_webhook else None,
            "dlq_count": dlq_count,
            "attachment_failures": attachment_failures,
            "credential_revoked": revoked,
        }


class NerveWebhookReceiver:
    def __init__(
        self,
        store: NerveWebhookStore,
        *,
        binding: EmailBinding,
        org_id: str,
        inbox_id: str,
        signing_secret: str,
    ) -> None:
        self.store = store
        self.binding = binding
        self.org_id = org_id
        self.inbox_id = inbox_id
        self.signing_secret = signing_secret

    def receive(
        self, payload: bytes, signature: str, *, received_at: float | None = None
    ) -> NerveWebhookAccepted:
        return self.store.append(
            binding=self.binding,
            expected_org_id=self.org_id,
            expected_inbox_id=self.inbox_id,
            payload=payload,
            signature=signature,
            signing_secret=self.signing_secret,
            received_at=received_at,
        )


def _address(value: Any) -> str:
    if isinstance(value, dict):
        email = str(value.get("email") or "").strip()
        name = str(value.get("name") or "").strip()
        return f"{name} <{email}>" if name and email else email
    return str(value or "").strip()


def _addresses(value: Any) -> str:
    values = value if isinstance(value, list) else [value]
    return ", ".join(address for item in values if (address := _address(item)))


def _safe_attachment_metadata(raw: Any) -> tuple[str, str, str, int | None]:
    if not isinstance(raw, dict):
        raise ValueError("attachment_invalid_metadata")
    attachment_id = str(raw.get("id") or "").strip()
    filename = str(raw.get("filename") or "").strip()
    content_type = str(raw.get("content_type") or "").strip().casefold()
    expected_size = raw.get("size_bytes")
    try:
        uuid.UUID(attachment_id)
    except ValueError as error:
        raise ValueError("attachment_invalid_id") from error
    if not filename or PurePath(filename).name != filename:
        raise ValueError("attachment_invalid_filename")
    if any(ord(character) < 32 for character in filename):
        raise ValueError("attachment_invalid_filename")
    if content_type not in ALLOWED_ATTACHMENT_TYPES:
        raise ValueError("attachment_type_not_allowed")
    if expected_size is not None and (
        isinstance(expected_size, bool)
        or not isinstance(expected_size, int)
        or not 0 <= expected_size <= MAX_ATTACHMENT_BYTES
    ):
        raise ValueError("attachment_invalid_size")
    return attachment_id, filename, content_type, expected_size


def _find_message(envelope: dict[str, Any], event: NerveWebhookEvent) -> dict[str, Any]:
    thread = envelope.get("thread")
    messages = envelope.get("messages")
    if not isinstance(thread, dict) or thread.get("id") != event.thread_id:
        raise ValueError("thread_identity_mismatch")
    if thread.get("inbox_id") != event.inbox_id or not isinstance(messages, list):
        raise ValueError("thread_identity_mismatch")
    matching = [item for item in messages if isinstance(item, dict) and item.get("id") == event.message_id]
    if len(matching) != 1 or matching[0].get("direction") != "inbound":
        raise ValueError("message_identity_mismatch")
    return matching[0]


def _build_rfc822(event: NerveWebhookEvent, message: dict[str, Any], attachments: list[Any]) -> bytes:
    result = EmailMessage()
    result["From"] = _address(message.get("from"))
    result["To"] = _addresses(message.get("to")) or "undisclosed-recipients:;"
    cc = _addresses(message.get("cc"))
    if cc:
        result["Cc"] = cc
    result["Subject"] = str(message.get("subject") or "(no subject)")
    result["Message-ID"] = f"<nerve-{event.message_id}@abrolia.invalid>"
    created_at = message.get("created_at")
    try:
        parsed_date = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except ValueError:
        parsed_date = datetime.fromtimestamp(event.received_at, UTC)
    result["Date"] = format_datetime(parsed_date)
    text = str(message.get("text") or "")
    html = str(message.get("html") or "")
    result.set_content(text or " ")
    if html:
        result.add_alternative(html, subtype="html")
    for attachment in attachments:
        maintype, subtype = str(attachment["content_type"]).split("/", 1)
        result.add_attachment(
            attachment["content"],
            maintype=maintype,
            subtype=subtype,
            filename=attachment["filename"],
        )
    return result.as_bytes()


class NerveAttachmentWorker:
    def __init__(
        self,
        database: Database,
        client: NerveInboundClient,
        *,
        worker_id: str = "nerve-runtime",
        clock=time.time,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self.db = database
        self.store = NerveWebhookStore(database, clock=clock)
        self.events = EventStore(database, clock=clock)
        self.client = client
        self.worker_id = worker_id
        self.clock = clock
        self.max_attempts = max_attempts

    def run_once(self) -> NerveMaterialized | None:
        event = self.store.lease(self.worker_id)
        if event is None:
            return None
        try:
            envelope = self.client.get_thread(event.inbox_id, event.thread_id)
            message = _find_message(envelope, event)
            raw_attachments = message.get("attachments", [])
            if not isinstance(raw_attachments, list) or len(raw_attachments) != event.attachment_count:
                raise ValueError("attachment_count_mismatch")
            stored: list[Any] = []
            total = 0
            for raw_attachment in raw_attachments:
                attachment_id, filename, content_type, expected_size = _safe_attachment_metadata(
                    raw_attachment
                )
                cached = self.store.cached_attachment(event.id, attachment_id)
                if cached is None:
                    content = self.client.get_attachment(event.message_id, attachment_id)
                    if not content or len(content) > MAX_ATTACHMENT_BYTES:
                        raise ValueError("attachment_invalid_size")
                    if expected_size is not None and len(content) != expected_size:
                        raise ValueError("attachment_hash_or_size_mismatch")
                    self.store.store_attachment(
                        event=event,
                        attachment_id=attachment_id,
                        filename=filename,
                        content_type=content_type,
                        expected_size=expected_size,
                        content=content,
                    )
                    cached = self.store.cached_attachment(event.id, attachment_id)
                    assert cached is not None
                elif (
                    cached["filename"] != filename
                    or cached["content_type"] != content_type
                    or cached["expected_size"] != expected_size
                ):
                    raise ValueError("attachment_metadata_changed")
                total += int(cached["actual_size"])
                if total > MAX_ATTACHMENT_TOTAL_BYTES:
                    raise ValueError("attachment_total_too_large")
                stored.append(cached)
            binding = EmailBinding(
                identity_id=event.identity_id,
                revision=event.binding_revision,
                provider="nerve",
                address="provider-bound",
            )
            ingested = ingest_rfc822(
                self.events,
                source="nerve",
                provider_event_id=event.message_id,
                raw_bytes=_build_rfc822(event, message, stored),
                received_at=event.received_at,
                binding=binding,
            )
            self.store.mark_materialized(event, ingested.event_id)
            return NerveMaterialized(
                event.id, ingested.event_id, ingested.created, len(stored)
            )
        except Exception as error:
            error_code = type(error).__name__
            if error.__class__.__name__.startswith("NerveAttachment"):
                error_code = "attachment_" + error.__class__.__name__.removeprefix(
                    "NerveAttachment"
                ).casefold()
            if isinstance(error, NerveCredentialRevoked):
                error_code = "credential_revoked"
            if isinstance(error, ValueError) and error.args:
                error_code = str(error.args[0])
            self.store.mark_failed(
                event,
                error_code,
                credential_revoked=isinstance(error, NerveCredentialRevoked),
                max_attempts=self.max_attempts,
            )
            raise

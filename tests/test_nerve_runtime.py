from __future__ import annotations

import hashlib
import hmac
import io
import json
import time
from pathlib import Path

import pytest

from hermes_cloud.core.db import open_database
from hermes_cloud.core.runtime_manifest import compute_config_sha256, parse_runtime_manifest
from hermes_cloud.email.contracts import EmailBinding
from hermes_cloud.email.nerve_client import NerveAttachmentPending, NerveCredentialRevoked
from hermes_cloud.ingest.nerve_webhook import (
    NerveAttachmentWorker,
    NerveWebhookReplay,
    NerveWebhookStore,
    NerveWebhookUnauthorized,
)
from hermes_cloud.runtime.bootstrap import ActivationState, atomic_write, write_activation_state
from hermes_cloud.runtime.service import RuntimeService

SECRET = "synthetic-webhook-signing-value"
ORG_ID = "00000000-0000-4000-8000-000000000001"
INBOX_ID = "00000000-0000-4000-8000-000000000002"
THREAD_ID = "00000000-0000-4000-8000-000000000003"
MESSAGE_ID = "00000000-0000-4000-8000-000000000004"
ATTACHMENT_ID = "00000000-0000-4000-8000-000000000005"


def binding(provider: str = "nerve-managed") -> EmailBinding:
    return EmailBinding("identity-1", 7, provider, "agent@abrolia.test")


def payload(*, attachment_count: int = 0) -> bytes:
    return json.dumps(
        {
            "event": "email.received",
            "org_id": ORG_ID,
            "inbox_id": INBOX_ID,
            "thread_id": THREAD_ID,
            "message_id": MESSAGE_ID,
            "from": "school@example.test",
            "subject": "Trip",
            "has_attachments": bool(attachment_count),
            "attachment_count": attachment_count,
            "created_at": "2026-08-05T10:00:00Z",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def signature(body: bytes, timestamp: int) -> str:
    digest = hmac.new(
        SECRET.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    return f"t={timestamp},v1={digest}"


def append(store: NerveWebhookStore, body: bytes, timestamp: int):
    return store.append(
        binding=binding(),
        expected_org_id=ORG_ID,
        expected_inbox_id=INBOX_ID,
        payload=body,
        signature=signature(body, timestamp),
        signing_secret=SECRET,
        received_at=float(timestamp),
    )


@pytest.fixture()
def database(tmp_path: Path):
    with open_database(tmp_path / "runtime.db") as opened:
        yield opened


def test_invalid_expired_and_exact_replay_never_append(database) -> None:
    body = payload()
    store = NerveWebhookStore(database, clock=lambda: 1_000.0)

    with pytest.raises(NerveWebhookUnauthorized):
        store.append(
            binding=binding(),
            expected_org_id=ORG_ID,
            expected_inbox_id=INBOX_ID,
            payload=body,
            signature="t=1000,v1=" + "0" * 64,
            signing_secret=SECRET,
        )
    with pytest.raises(NerveWebhookUnauthorized):
        store.append(
            binding=binding(),
            expected_org_id=ORG_ID,
            expected_inbox_id=INBOX_ID,
            payload=body,
            signature=signature(body, 1),
            signing_secret=SECRET,
        )
    assert database.query_one("SELECT COUNT(*) AS n FROM nerve_webhook_events")["n"] == 0

    accepted = append(store, body, 1_000)
    assert accepted.created is True
    with pytest.raises(NerveWebhookReplay):
        append(store, body, 1_000)
    with pytest.raises(NerveWebhookReplay):
        store.append(
            binding=binding(),
            expected_org_id=ORG_ID,
            expected_inbox_id=INBOX_ID,
            payload=body,
            signature=signature(body, 1_000).replace(",", ", "),
            signing_secret=SECRET,
            received_at=1_000,
        )
    assert database.query_one("SELECT COUNT(*) AS n FROM nerve_webhook_events")["n"] == 1


def test_provider_retry_after_durable_append_is_one_event(database) -> None:
    body = payload()
    store = NerveWebhookStore(database)

    first = append(store, body, 2_000)
    # Model a crash after fsync but before the HTTP response. Nerve retries the
    # same domain event later and therefore signs it with a fresh timestamp.
    retry = append(store, body, 2_001)

    assert first.created is True
    assert retry.created is False
    assert retry.event.id == first.event.id
    assert database.query_one("SELECT COUNT(*) AS n FROM nerve_webhook_events")["n"] == 1
    assert database.query_one("SELECT COUNT(*) AS n FROM nerve_webhook_signatures")["n"] == 2


class FakeInboundClient:
    def __init__(self, *, fail_attachment: bool = False, auth_revoked: bool = False) -> None:
        self.fail_attachment = fail_attachment
        self.auth_revoked = auth_revoked

    def get_thread(self, inbox_id: str, thread_id: str):
        if self.auth_revoked:
            raise NerveCredentialRevoked("revoked")
        assert inbox_id == INBOX_ID and thread_id == THREAD_ID
        return {
            "thread": {"id": THREAD_ID, "inbox_id": INBOX_ID},
            "messages": [
                {
                    "id": MESSAGE_ID,
                    "direction": "inbound",
                    "from": {"name": "School", "email": "school@example.test"},
                    "to": [{"name": "Agent", "email": "agent@abrolia.test"}],
                    "cc": [],
                    "subject": "Trip",
                    "text": "Permission slip attached.",
                    "html": "",
                    "created_at": "2026-08-05T10:00:00Z",
                    "attachments_state": "available",
                    "attachments": [
                        {
                            "id": ATTACHMENT_ID,
                            "filename": "permission.pdf",
                            "content_type": "application/pdf",
                            "size_bytes": 13,
                            "availability": "available",
                        }
                    ],
                }
            ],
        }

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        assert message_id == MESSAGE_ID and attachment_id == ATTACHMENT_ID
        if self.fail_attachment:
            raise NerveAttachmentPending(1)
        return b"synthetic-pdf"


@pytest.mark.parametrize("provider", ["nerve-managed", "nerve-byo-domain"])
def test_managed_and_byo_materialize_the_same_ingress_scenario(database, provider) -> None:
    body = payload(attachment_count=1)
    store = NerveWebhookStore(database)
    accepted = store.append(
        binding=binding(provider),
        expected_org_id=ORG_ID,
        expected_inbox_id=INBOX_ID,
        payload=body,
        signature=signature(body, 3_000),
        signing_secret=SECRET,
        received_at=3_000,
    )

    result = NerveAttachmentWorker(
        database, FakeInboundClient(), clock=lambda: 3_000.0
    ).run_once()

    assert result is not None and result.attachment_count == 1
    assert database.query_one(
        "SELECT state, canonical_event_id FROM nerve_webhook_events WHERE id = ?",
        (accepted.event.id,),
    )["state"] == "materialized"
    attachment = database.query_one("SELECT * FROM nerve_attachments")
    assert attachment["content_sha256"] == hashlib.sha256(b"synthetic-pdf").hexdigest()
    assert attachment["classification"] == "email_attachment"
    assert attachment["retention_until"] == 3_000 + 30 * 86_400


def test_attachment_failure_retries_then_dlq_without_losing_parent(database) -> None:
    body = payload(attachment_count=1)
    accepted = append(NerveWebhookStore(database), body, 4_000)
    worker = NerveAttachmentWorker(
        database, FakeInboundClient(fail_attachment=True), max_attempts=2
    )

    with pytest.raises(NerveAttachmentPending):
        worker.run_once()
    assert database.query_one(
        "SELECT state FROM nerve_webhook_events WHERE id = ?", (accepted.event.id,)
    )["state"] == "queued"
    with pytest.raises(NerveAttachmentPending):
        worker.run_once()
    row = database.query_one(
        "SELECT state, attempts, length(payload) AS payload_size"
        " FROM nerve_webhook_events WHERE id = ?",
        (accepted.event.id,),
    )
    assert (row["state"], row["attempts"]) == ("dlq", 2)
    assert row["payload_size"] > 0
    health = NerveWebhookStore(database).health(binding(), now=4_001)
    assert health["dlq_count"] == 1
    assert health["attachment_failures"] == 1


def test_revoked_credential_is_visible_in_health(database) -> None:
    append(NerveWebhookStore(database), payload(), 5_000)
    with pytest.raises(NerveCredentialRevoked):
        NerveAttachmentWorker(database, FakeInboundClient(auth_revoked=True)).run_once()

    health = NerveWebhookStore(database).health(binding(), now=5_001)
    assert health["status"] == "degraded"
    assert health["credential_revoked"] is True


def _runtime_manifest() -> str:
    refs = json.dumps(
        {
            "household_id": "33333333-3333-4333-8333-333333333333",
            "stable_ref": "stable",
            "org_id": ORG_ID,
            "grant_id": "grant-1",
            "inbox_id": INBOX_ID,
            "key_id": "key-1",
            "webhook_id": "webhook-1",
            "address": "agent@abrolia.test",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    content = f'''\
schema_version = 1
household_id = "33333333-3333-4333-8333-333333333333"
config_revision = 7
family_language = "English"
timezone = "Europe/Prague"
country_code = "CZ"
residency_mode = "eu-app"

[actors]
owner = "owner"
family = ["owner"]
guests = []

[channels]
primary = "telegram"

[[channel_bindings]]
channel = "telegram"
actor_id = "owner"
chat_id = "chat-1"
verified = true

[email]
agent_inbox = "agent@abrolia.test"
fallback = "owner@example.test"
provider_kind = "nerve-managed"
provider_binding_ref = {json.dumps(refs)}
secret_binding_ref = "ABROLIA_NERVE_EMAIL_CREDENTIALS"

[consent]
authority = "control_plane"
enforcement = "required"
required_purposes = ["special_category_content_restriction"]

[[consent.receipts]]
receipt_id = "10000000-0000-4000-8000-000000000032"
purpose = "special_category_content_restriction"
text_version = "special-category-content-restriction-v1"
text_sha256 = "64221529a01cff070f1f614451eecaa5c6ed28a2b7c5d6af6f56e5ef5054e509"
'''
    digest = compute_config_sha256(content)
    return content.replace(
        "schema_version = 1\n", f'schema_version = 1\nconfig_sha256 = "{digest}"\n'
    )


def _active_runtime(tmp_path: Path) -> RuntimeService:
    content = _runtime_manifest()
    manifest = parse_runtime_manifest(content)
    manifest_path = atomic_write(tmp_path / "household.toml", content.encode())
    activation_path = tmp_path / "activation.json"
    write_activation_state(
        activation_path,
        ActivationState(
            status="active",
            runtime_ref="fly:runtime",
            household_id=manifest.household_id,
            config_revision=manifest.config_revision,
            config_sha256=manifest.config_sha256,
            updated_at=1.0,
        ),
    )
    return RuntimeService(
        manifest_path=manifest_path,
        activation_path=activation_path,
        runtime_ref="fly:runtime",
        env={
            "HERMES_DB": str(tmp_path / "runtime.db"),
            "ABROLIA_NERVE_EMAIL_CREDENTIALS": json.dumps(
                {"api_key": "synthetic-api-key", "webhook_signing_key": SECRET}
            ),
        },
    )


def _call_webhook(service: RuntimeService, body: bytes, signed: str) -> tuple[str, dict]:
    seen: list[str] = []
    result = service(
        {
            "PATH_INFO": "/v1/email/nerve/webhook",
            "REQUEST_METHOD": "POST",
            "CONTENT_LENGTH": str(len(body)),
            "CONTENT_TYPE": "application/json",
            "HTTP_X_NERVE_SIGNATURE": signed,
            "wsgi.input": io.BytesIO(body),
        },
        lambda status, _headers: seen.append(status),
    )
    return seen[0], json.loads(result[0])


def test_runtime_webhook_route_acks_only_after_durable_append(tmp_path: Path) -> None:
    service = _active_runtime(tmp_path)
    body = payload()
    timestamp = int(time.time())
    signed = signature(body, timestamp)

    status, response = _call_webhook(service, body, signed)
    assert status == "200 OK" and response["status"] == "accepted"
    with open_database(tmp_path / "runtime.db") as database:
        assert database.query_one("SELECT COUNT(*) AS n FROM nerve_webhook_events")["n"] == 1

    status, response = _call_webhook(service, body, signed)
    assert status == "409 Conflict" and response == {"status": "signature_replay"}
    status, response = _call_webhook(service, body, signature(body, timestamp + 1))
    assert status == "200 OK" and response["status"] == "duplicate"

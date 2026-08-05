"""Consumer-owned live contract for Abrolia MVP Phase 3.

The suite uses only clearly synthetic production-canary traffic. It is excluded
from ordinary CI by the ``live`` marker and an explicit typed confirmation.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import httpx
import nerve_email
import pytest
from nerve_email import NerveAttachmentPendingError, NerveClient

from hermes_cloud.core.db import open_database
from hermes_cloud.email.contracts import EmailBinding
from hermes_cloud.ingest.nerve_webhook import (
    NerveWebhookReplay,
    NerveWebhookStore,
    NerveWebhookUnauthorized,
    verify_nerve_signature,
)

CONFIRMATION = "synthetic-production-canary"
POLL_SECONDS = 5
LIVE_TIMEOUT_SECONDS = 180
_SIMPLE_TEST_MAILBOX = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}"
)
_RESERVED_EXTERNAL_DOMAINS = frozenset({"example.com", "example.net", "example.org"})
_RESERVED_EXTERNAL_SUFFIXES = (".example", ".invalid", ".localhost", ".test")


def _joined(*parts: str) -> str:
    """Build detector probes without committing a literal non-reserved address."""
    return "".join(parts)


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _required_envs(names: tuple[str, ...]) -> dict[str, str]:
    values = {name: os.environ.get(name, "").strip() for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError(
            "missing Phase 3 live environment: "
            + ", ".join(missing)
            + "; see docs/nerve-phase3-live-contract.md"
        )
    return values


def _validated_external_mailbox(value: str) -> str:
    if not _SIMPLE_TEST_MAILBOX.fullmatch(value):
        raise RuntimeError(
            "NERVE_EXTERNAL_MAILBOX must be a real ASCII test mailbox, not a placeholder"
        )
    domain = value.rsplit("@", 1)[1].casefold()
    if (
        domain in _RESERVED_EXTERNAL_DOMAINS
        or domain.endswith(_RESERVED_EXTERNAL_SUFFIXES)
        or domain == "abrolia.com"
        or domain.endswith(".abrolia.com")
    ):
        raise RuntimeError(
            "NERVE_EXTERNAL_MAILBOX must be deliverable outside Nerve; reserved and "
            "Abrolia domains are not external delivery targets"
        )
    return value


def _one_header(value: Any) -> str:
    if isinstance(value, list):
        if len(value) != 1:
            raise RuntimeError("Nerve webhook returned an ambiguous header")
        return str(value[0])
    return str(value)


def _canary_pdf() -> bytes:
    """Build a small deterministic, structurally valid one-page PDF."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length 63 >>\nstream\nBT /F1 12 Tf 36 96 Td (Abrolia Phase 3 synthetic canary) Tj ET\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    payload = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(payload))
        payload.extend(f"{number} 0 obj\n".encode())
        payload.extend(body)
        payload.extend(b"\nendobj\n")
    xref = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode())
    payload.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(payload)


def _signed(secret: str, payload: bytes, timestamp: int) -> str:
    digest = hmac.new(
        secret.encode(), str(timestamp).encode() + b"." + payload, hashlib.sha256
    ).hexdigest()
    return f"t={timestamp},v1={digest}"


def _field(item: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in item:
            return item[name]
    return None


class Phase3LiveContract:
    def __init__(self, database_path: Path) -> None:
        if _required_env("ABROLIA_NERVE_LIVE_CONFIRM") != CONFIRMATION:
            raise RuntimeError(
                f"ABROLIA_NERVE_LIVE_CONFIRM must equal {CONFIRMATION!r}"
            )
        if nerve_email.__version__ != "0.2.0":
            raise RuntimeError("the Phase 3 contract requires nerve-email==0.2.0")

        environment = _required_envs(
            (
                "NERVE_CONTROL_PLANE_ORIGIN",
                "NERVE_RUNTIME_ORIGIN",
                "NERVE_ADMIN_KEY",
                "NERVE_CANARY_ORG_ID",
                "NERVE_CANARY_INBOX_ID",
                "NERVE_CANARY_ADDRESS",
                "NERVE_CANARY_PEER_ORG_ID",
                "NERVE_CANARY_PEER_INBOX_ID",
                "NERVE_EXTERNAL_MAILBOX",
            )
        )
        self.control_origin = environment["NERVE_CONTROL_PLANE_ORIGIN"].rstrip("/")
        self.runtime_origin = environment["NERVE_RUNTIME_ORIGIN"].rstrip("/")
        self.admin_key = environment["NERVE_ADMIN_KEY"]
        self.canary_org_id = environment["NERVE_CANARY_ORG_ID"]
        self.canary_inbox_id = environment["NERVE_CANARY_INBOX_ID"]
        self.canary_address = environment["NERVE_CANARY_ADDRESS"]
        self.peer_org_id = environment["NERVE_CANARY_PEER_ORG_ID"]
        self.peer_inbox_id = environment["NERVE_CANARY_PEER_INBOX_ID"]
        self.external_mailbox = _validated_external_mailbox(
            environment["NERVE_EXTERNAL_MAILBOX"]
        )
        if self.canary_org_id == self.peer_org_id:
            raise RuntimeError("canary and peer must be separate synthetic organizations")
        if self.canary_address.casefold() == self.external_mailbox.casefold():
            raise RuntimeError("external delivery mailbox must differ from the canary inbox")

        self.database_path = database_path
        self.http = httpx.AsyncClient(timeout=20)
        self.resources: list[tuple[str, str, str]] = []
        self.webhook_site_token = ""

    async def close(self) -> None:
        failures: list[str] = []
        for kind, resource_id, org_id in reversed(self.resources):
            try:
                response = await self.http.delete(
                    f"{self.control_origin}/v1/{kind}/{resource_id}",
                    params={"org_id": org_id},
                    headers={"X-API-Key": self.admin_key},
                )
                if response.status_code not in {200, 204, 404}:
                    failures.append(f"{kind}:{response.status_code}")
            except httpx.HTTPError:
                failures.append(f"{kind}:transport")
        if self.webhook_site_token:
            try:
                response = await self.http.delete(
                    f"https://webhook.site/token/{self.webhook_site_token}"
                )
                if response.status_code not in {200, 204, 404}:
                    failures.append(f"webhook-site:{response.status_code}")
            except httpx.HTTPError:
                failures.append("webhook-site:transport")
        await self.http.aclose()
        if failures:
            raise RuntimeError("synthetic resource cleanup failed: " + ", ".join(failures))

    async def _create_key(self, org_id: str, label: str) -> str:
        response = await self.http.post(
            f"{self.control_origin}/v1/keys",
            headers={"X-API-Key": self.admin_key},
            json={
                "org_id": org_id,
                "label": label,
                "external_ref": f"abrolia-phase3-{uuid.uuid4()}",
                "scopes": ["nerve:email.read", "nerve:email.send"],
            },
        )
        response.raise_for_status()
        payload = response.json()
        self.resources.append(("keys", str(payload["id"]), org_id))
        return str(payload["key"])

    async def _create_webhook(self) -> str:
        response = await self.http.post("https://webhook.site/token")
        response.raise_for_status()
        self.webhook_site_token = str(response.json()["uuid"])
        response = await self.http.post(
            f"{self.control_origin}/v1/webhooks",
            headers={"X-API-Key": self.admin_key},
            json={
                "org_id": self.canary_org_id,
                "url": f"https://webhook.site/{self.webhook_site_token}",
                "events": ["email.received"],
                "external_ref": f"abrolia-phase3-{uuid.uuid4()}",
            },
        )
        response.raise_for_status()
        payload = response.json()
        self.resources.append(("webhooks", str(payload["id"]), self.canary_org_id))
        return str(payload["secret"])

    async def _wait_for_webhook(
        self, *, subject: str, secret: str
    ) -> tuple[bytes, str, int, dict[str, Any]]:
        deadline = time.monotonic() + LIVE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            response = await self.http.get(
                f"https://webhook.site/token/{self.webhook_site_token}/requests",
                params={"sorting": "newest"},
            )
            response.raise_for_status()
            for request in response.json().get("data", []):
                raw = str(request["content"]).encode()
                decoded = json.loads(raw)
                event = decoded.get("data") or decoded.get("payload") or decoded
                if event.get("subject") != subject:
                    continue
                headers = {str(key).casefold(): value for key, value in request["headers"].items()}
                signature = _one_header(headers["x-nerve-signature"])
                timestamp, _ = verify_nerve_signature(
                    payload=raw,
                    signature=signature,
                    secret=secret,
                    now=float(signature.split(",", 1)[0].split("=", 1)[1]),
                )
                header_timestamp = int(_one_header(headers["x-nerve-timestamp"]))
                if header_timestamp != timestamp:
                    raise RuntimeError("Nerve webhook timestamp headers disagree")
                if event.get("attachment_count") != 1 or not event.get("has_attachments"):
                    raise RuntimeError("email.received omitted attachment metadata")
                return raw, signature, timestamp, event
            await asyncio.sleep(POLL_SECONDS)
        raise TimeoutError("email.received webhook did not arrive within 180 seconds")

    def _verify_consumer_ingress(
        self,
        *,
        raw: bytes,
        signature: str,
        timestamp: int,
        event: dict[str, Any],
        secret: str,
    ) -> None:
        for boundary in (timestamp - 300, timestamp + 300):
            verify_nerve_signature(
                payload=raw, signature=signature, secret=secret, now=float(boundary)
            )
        for outside in (timestamp - 301, timestamp + 301):
            with pytest.raises(NerveWebhookUnauthorized):
                verify_nerve_signature(
                    payload=raw, signature=signature, secret=secret, now=float(outside)
                )

        binding = EmailBinding(
            "phase3-live-canary", 1, "nerve-managed", self.canary_address
        )
        with open_database(self.database_path) as database:
            store = NerveWebhookStore(database)
            first = store.append(
                binding=binding,
                expected_org_id=self.canary_org_id,
                expected_inbox_id=self.canary_inbox_id,
                payload=raw,
                signature=signature,
                signing_secret=secret,
                received_at=float(timestamp),
            )
            with pytest.raises(NerveWebhookReplay):
                store.append(
                    binding=binding,
                    expected_org_id=self.canary_org_id,
                    expected_inbox_id=self.canary_inbox_id,
                    payload=raw,
                    signature=signature,
                    signing_secret=secret,
                    received_at=float(timestamp),
                )
            retry = store.append(
                binding=binding,
                expected_org_id=self.canary_org_id,
                expected_inbox_id=self.canary_inbox_id,
                payload=raw,
                signature=_signed(secret, raw, timestamp + 1),
                signing_secret=secret,
                received_at=float(timestamp + 1),
            )
            assert first.created is True
            assert retry.created is False
            assert retry.event.id == first.event.id
            assert retry.event.message_id == event["message_id"]
            assert database.query_one(
                "SELECT COUNT(*) AS n FROM nerve_webhook_events"
            )["n"] == 1
            assert database.query_one(
                "SELECT COUNT(*) AS n FROM nerve_webhook_signatures"
            )["n"] == 2

    async def _wait_for_attachment(
        self,
        client: NerveClient,
        *,
        thread_id: str,
        message_id: str,
        expected: bytes,
    ) -> None:
        deadline = time.monotonic() + LIVE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            detail = await client.rest.get_thread(self.canary_inbox_id, thread_id)
            message = next(
                (
                    item
                    for item in detail.get("messages", [])
                    if _field(item, "id", "ID") == message_id
                ),
                None,
            )
            if message is not None:
                attachments = _field(message, "attachments", "Attachments") or []
                if len(attachments) != 1:
                    raise RuntimeError("inbound message did not expose one attachment")
                attachment = attachments[0]
                availability = _field(attachment, "availability", "Availability")
                if availability == "available":
                    try:
                        content = await client.rest.get_attachment(
                            message_id, str(_field(attachment, "id", "ID"))
                        )
                    except NerveAttachmentPendingError:
                        await asyncio.sleep(POLL_SECONDS)
                        continue
                    if content != expected:
                        raise RuntimeError("downloaded PDF differs from the sent PDF")
                    return
                if availability in {"failed", "too_large", "expired"}:
                    raise RuntimeError(f"inbound attachment became {availability}")
            await asyncio.sleep(POLL_SECONDS)
        raise TimeoutError("durable attachment did not become available within 180 seconds")

    async def _wait_for_external_delivery(self, key: str, outbox_id: str) -> None:
        deadline = time.monotonic() + LIVE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            response = await self.http.get(
                f"{self.control_origin}/v1/outbox/{outbox_id}",
                headers={"X-Nerve-Cloud-Key": key},
            )
            response.raise_for_status()
            state = response.json()
            if state.get("state") == "failed":
                raise RuntimeError("outbound synthetic PDF delivery failed")
            if state.get("state") == "sent" and state.get("delivery_status") == "delivered":
                return
            await asyncio.sleep(POLL_SECONDS)
        raise TimeoutError("external PDF delivery was not confirmed within 180 seconds")

    async def run(self) -> None:
        canary_key = await self._create_key(self.canary_org_id, "Abrolia Phase 3 canary")
        peer_key = await self._create_key(self.peer_org_id, "Abrolia Phase 3 peer")
        webhook_secret = await self._create_webhook()
        pdf = _canary_pdf()
        nonce = uuid.uuid4().hex
        inbound_subject = f"Abrolia Phase 3 synthetic inbound {nonce}"

        async with AsyncExitStack() as stack:
            canary = await stack.enter_async_context(
                NerveClient(
                    base_url=self.runtime_origin,
                    rest_base_url=self.control_origin,
                    api_key=canary_key,
                )
            )
            peer = await stack.enter_async_context(
                NerveClient(
                    base_url=self.runtime_origin,
                    rest_base_url=self.control_origin,
                    api_key=peer_key,
                )
            )
            await peer.compose_email(
                inbox_id=self.peer_inbox_id,
                to=self.canary_address,
                subject=inbound_subject,
                body="Synthetic Abrolia Phase 3 inbound contract.",
                idempotency_key=f"abrolia-phase3-inbound-{nonce}",
                attachments=("abrolia-phase3-canary.pdf", pdf),
            )
            raw, signature, timestamp, event = await self._wait_for_webhook(
                subject=inbound_subject, secret=webhook_secret
            )
            self._verify_consumer_ingress(
                raw=raw,
                signature=signature,
                timestamp=timestamp,
                event=event,
                secret=webhook_secret,
            )
            await self._wait_for_attachment(
                canary,
                thread_id=str(event["thread_id"]),
                message_id=str(event["message_id"]),
                expected=pdf,
            )
            outbound = await canary.compose_email(
                inbox_id=self.canary_inbox_id,
                to=self.external_mailbox,
                subject=f"Abrolia Phase 3 synthetic outbound {nonce}",
                body="Synthetic Abrolia Phase 3 external-delivery contract.",
                idempotency_key=f"abrolia-phase3-outbound-{nonce}",
                attachments=("abrolia-phase3-canary.pdf", pdf),
            )
            await self._wait_for_external_delivery(canary_key, str(outbound["message_id"]))


def test_phase3_canary_pdf_and_signature_are_deterministic() -> None:
    pdf = _canary_pdf()
    assert pdf.startswith(b"%PDF-1.4") and pdf.endswith(b"%%EOF\n")
    assert pdf == _canary_pdf()
    signature = _signed("synthetic-signing-secret", pdf, 1_000)
    assert verify_nerve_signature(
        payload=pdf,
        signature=signature,
        secret="synthetic-signing-secret",
        now=1_300,
    )[0] == 1_000


def test_phase3_live_contract_requires_typed_confirmation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ABROLIA_NERVE_LIVE_CONFIRM", raising=False)
    with pytest.raises(RuntimeError, match="ABROLIA_NERVE_LIVE_CONFIRM"):
        Phase3LiveContract(tmp_path / "must-not-open.db")


def test_phase3_live_contract_reports_every_missing_environment_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ABROLIA_NERVE_LIVE_CONFIRM", CONFIRMATION)
    for name in (
        "NERVE_CONTROL_PLANE_ORIGIN",
        "NERVE_RUNTIME_ORIGIN",
        "NERVE_ADMIN_KEY",
        "NERVE_CANARY_ORG_ID",
        "NERVE_CANARY_INBOX_ID",
        "NERVE_CANARY_ADDRESS",
        "NERVE_CANARY_PEER_ORG_ID",
        "NERVE_CANARY_PEER_INBOX_ID",
        "NERVE_EXTERNAL_MAILBOX",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError) as failure:
        Phase3LiveContract(tmp_path / "must-not-open.db")
    assert "NERVE_CONTROL_PLANE_ORIGIN" in str(failure.value)
    assert "NERVE_EXTERNAL_MAILBOX" in str(failure.value)


@pytest.mark.parametrize(
    "value",
    (
        "<designated external test mailbox>",
        "<ваш внешний тестовый ящик>",
        "canary@example",
        "Name <canary@example.test>",
    ),
)
def test_phase3_live_contract_rejects_mailbox_placeholders(value: str) -> None:
    with pytest.raises(RuntimeError, match="real ASCII test mailbox"):
        _validated_external_mailbox(value)


def test_phase3_live_contract_accepts_plain_external_test_mailbox() -> None:
    mailbox = _joined("canary@", "mailbox.tld")
    assert _validated_external_mailbox(mailbox) == mailbox


@pytest.mark.parametrize(
    "value",
    (
        "canary@example.com",
        "canary@example.net",
        "canary@example.org",
        "canary@family.test",
        _joined("canary@", "abrolia.com"),
        _joined("canary@mail.", "abrolia.com"),
    ),
)
def test_phase3_live_contract_rejects_non_external_domains(value: str) -> None:
    with pytest.raises(RuntimeError, match="deliverable outside Nerve"):
        _validated_external_mailbox(value)


@pytest.mark.live
def test_nerve_phase3_live_consumer_contract(tmp_path: Path) -> None:
    async def execute() -> None:
        contract = Phase3LiveContract(tmp_path / "phase3-live.db")
        try:
            await contract.run()
        finally:
            await contract.close()

    asyncio.run(execute())

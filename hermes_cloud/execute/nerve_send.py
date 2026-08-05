"""Nerve compose adapter for the provider-neutral approved-email executor."""

from __future__ import annotations

import base64
import time
from email import policy
from email.parser import BytesParser
from typing import Any, Protocol

from hermes_cloud.email.contracts import EmailDeliveryReceipt, EmailSendRequest
from hermes_cloud.email.nerve_client import (
    NerveCredentialRevoked,
    NerveError,
    NerveTransportUnknown,
)
from hermes_cloud.execute.email_send import EmailOutcomeUnknown, EmailRejected
from hermes_cloud.ingest.nerve_webhook import (
    ALLOWED_ATTACHMENT_TYPES,
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENT_COUNT,
    MAX_ATTACHMENT_TOTAL_BYTES,
)


class NerveComposeClient(Protocol):
    def compose_email(
        self,
        *,
        inbox_id: str,
        to: str,
        subject: str,
        body: str,
        html: str | None,
        idempotency_key: str,
        attachments: list[dict[str, str]],
    ) -> dict[str, Any]: ...


def _mime_parts(raw: bytes) -> tuple[str, str, str, str | None, list[dict[str, str]]]:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    recipient = str(message.get("To") or "").strip()
    subject = str(message.get("Subject") or "").strip()
    text = ""
    html: str | None = None
    attachments: list[dict[str, str]] = []
    total = 0
    for part in message.walk():
        if part.is_multipart():
            continue
        disposition = part.get_content_disposition()
        content_type = part.get_content_type().casefold()
        if disposition == "attachment" or part.get_filename():
            filename = str(part.get_filename() or "").strip()
            content = part.get_payload(decode=True) or b""
            if (
                not filename
                or content_type not in ALLOWED_ATTACHMENT_TYPES
                or not content
                or len(content) > MAX_ATTACHMENT_BYTES
            ):
                raise EmailRejected("outbound attachment is invalid")
            total += len(content)
            if len(attachments) >= MAX_ATTACHMENT_COUNT or total > MAX_ATTACHMENT_TOTAL_BYTES:
                raise EmailRejected("outbound attachment limits exceeded")
            attachments.append(
                {
                    "filename": filename,
                    "content_type": content_type,
                    "content_base64": base64.b64encode(content).decode("ascii"),
                }
            )
        elif content_type == "text/plain" and not text:
            text = str(part.get_content())
        elif content_type == "text/html" and html is None:
            html = str(part.get_content())
    if not recipient or not subject or (not text.strip() and not html):
        raise EmailRejected("Nerve compose MIME is incomplete")
    return recipient, subject, text, html, attachments


class NerveSendProvider:
    provider = "nerve"
    supports_idempotent_reconcile = True

    def __init__(
        self,
        client: NerveComposeClient,
        *,
        inbox_id: str,
        clock=time.time,
    ) -> None:
        if not inbox_id:
            raise ValueError("Nerve inbox ID is required")
        self.client = client
        self.inbox_id = inbox_id
        self.clock = clock

    def send(self, request: EmailSendRequest) -> EmailDeliveryReceipt:
        recipient, subject, body, html, attachments = _mime_parts(request.mime_bytes)
        try:
            result = self.client.compose_email(
                inbox_id=self.inbox_id,
                to=recipient,
                subject=subject,
                body=body,
                html=html,
                idempotency_key=request.effect_id,
                attachments=attachments,
            )
        except (NerveTransportUnknown, TimeoutError) as error:
            raise EmailOutcomeUnknown(
                "Nerve compose outcome is unknown; effect is not replayed blindly"
            ) from error
        except NerveCredentialRevoked:
            raise
        except NerveError as error:
            raise EmailRejected("Nerve rejected the approved email") from error
        provider_ref = str(result.get("message_id") or "").strip()
        status = str(result.get("status") or "").casefold()
        if not provider_ref or status not in {"queued", "accepted", "sent"}:
            raise EmailRejected("Nerve returned no accepted message identity")
        return EmailDeliveryReceipt(
            effect_id=request.effect_id,
            approval_id=request.approval_id,
            message_id=request.message_id,
            provider_ref=provider_ref,
            accepted_at=self.clock(),
            status="accepted",
        )

    def reconcile(self, request: EmailSendRequest) -> EmailDeliveryReceipt:
        # compose_email is idempotent on the child effect_id at Nerve. Calling
        # it again is an inspect-or-return operation, never a second delivery.
        return self.send(request)

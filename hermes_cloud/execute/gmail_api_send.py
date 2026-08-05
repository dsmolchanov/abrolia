"""Gmail API send adapter with exact Message-ID reconciliation."""

from __future__ import annotations

import base64
import time
from typing import Any, Protocol

from hermes_cloud.email.contracts import EmailDeliveryReceipt, EmailSendRequest
from hermes_cloud.execute.email_send import EmailOutcomeUnknown, EmailRejected


class GmailSendApi(Protocol):
    def send_raw(self, raw: str) -> dict[str, Any]: ...
    def search_sent(self, query: str) -> list[dict[str, Any]]: ...


class GmailSendProvider:
    provider = "gmail"
    supports_idempotent_reconcile = False

    def __init__(self, client: GmailSendApi, *, clock=time.time) -> None:
        self.client = client
        self.clock = clock

    def send(self, request: EmailSendRequest) -> EmailDeliveryReceipt:
        raw = base64.urlsafe_b64encode(request.mime_bytes).rstrip(b"=").decode("ascii")
        try:
            result = self.client.send_raw(raw)
        except (TimeoutError, ConnectionError) as error:
            raise EmailOutcomeUnknown("Gmail send outcome is unknown") from error
        provider_ref = str(result.get("id") or "")
        if not provider_ref:
            raise EmailRejected("Gmail returned no accepted message identity")
        return EmailDeliveryReceipt(
            request.effect_id,
            request.approval_id,
            request.message_id,
            provider_ref,
            self.clock(),
            "accepted",
        )

    def reconcile(self, request: EmailSendRequest) -> EmailDeliveryReceipt:
        matches = self.client.search_sent(f"rfc822msgid:{request.message_id}")
        exact = [item for item in matches if str(item.get("rfc822_message_id")) == request.message_id]
        if len(exact) != 1 or not exact[0].get("id"):
            raise EmailOutcomeUnknown("Gmail Sent reconciliation is absent or ambiguous")
        return EmailDeliveryReceipt(
            request.effect_id,
            request.approval_id,
            request.message_id,
            str(exact[0]["id"]),
            self.clock(),
            "accepted",
        )

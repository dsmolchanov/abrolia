"""Gmail API send adapter for a send-only grant.

`users.messages.send` answers synchronously with the accepted message's `id`,
so a 200 is the receipt. What the adapter cannot do is look again: settling a
timed-out send by searching Sent for the Message-ID needs the Gmail read
scope, which the grant no longer carries. A timeout therefore ends as
`outcome_unknown`, and `EmailSender` never replays an unknown outcome.
"""

from __future__ import annotations

import base64
import time
from typing import Any, Protocol

from hermes_cloud.email.contracts import EmailDeliveryReceipt, EmailSendRequest
from hermes_cloud.execute.email_send import EmailOutcomeUnknown, EmailRejected


class GmailSendApi(Protocol):
    def send_raw(self, raw: str) -> dict[str, Any]: ...


class GmailSendProvider:
    provider = "gmail"
    #: No Sent search without a read scope: an unknown outcome stays unknown.
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

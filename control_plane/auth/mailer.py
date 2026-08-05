from __future__ import annotations

import sys
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Protocol, TextIO

import httpx

RESEND_EMAILS_URL = "https://api.resend.com/emails"


class MailDeliveryError(RuntimeError):
    """A magic link could not be handed to the configured delivery provider."""


class Mailer(Protocol):
    def validate_recipient(self, recipient: str) -> None: ...

    def send_magic_link(self, *, recipient: str, url: str, purpose: str) -> None: ...


def require_test_recipient(recipient: str) -> None:
    domain = recipient.rsplit("@", 1)[-1].casefold()
    if not domain.endswith(".test"):
        raise ValueError("Phase 1 mailers accept only reserved .test addresses")


@dataclass(frozen=True)
class SentLink:
    recipient: str
    url: str
    purpose: str


@dataclass
class MemoryMailer:
    sent: list[SentLink] = field(default_factory=list)

    def validate_recipient(self, recipient: str) -> None:
        require_test_recipient(recipient)

    def send_magic_link(self, *, recipient: str, url: str, purpose: str) -> None:
        self.validate_recipient(recipient)
        self.sent.append(SentLink(recipient, url, purpose))


class ConsoleMailer:
    """Test-only operator output. Uses a stream, never the application logger."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream or sys.stdout

    def validate_recipient(self, recipient: str) -> None:
        require_test_recipient(recipient)

    def send_magic_link(self, *, recipient: str, url: str, purpose: str) -> None:
        self.validate_recipient(recipient)
        print(f"synthetic {purpose} link for {recipient}: {url}", file=self.stream)


class ResendMailer:
    """Production magic-link delivery without logging provider response bodies."""

    def __init__(
        self,
        *,
        api_key: str,
        sender: str,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._sender = sender
        self._client = client

    def validate_recipient(self, recipient: str) -> None:
        local, separator, domain = recipient.rpartition("@")
        if not separator or not local or "." not in domain or any(
            character in recipient for character in "\r\n"
        ):
            raise ValueError("recipient must be a valid email address")

    def send_magic_link(self, *, recipient: str, url: str, purpose: str) -> None:
        self.validate_recipient(recipient)
        subject = (
            "Sign in to Abrolia"
            if purpose == "login"
            else "Confirm your Abrolia session"
        )
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Idempotency-Key": "magic-link/" + sha256(url.encode()).hexdigest(),
        }
        payload = {
            "from": self._sender,
            "to": [recipient],
            "subject": subject,
            "text": (
                "Open this secure link to continue with Abrolia:\n\n"
                f"{url}\n\nThis link expires in 15 minutes."
            ),
        }
        try:
            sender = self._client.post if self._client is not None else httpx.post
            response = sender(
                RESEND_EMAILS_URL, headers=headers, json=payload, timeout=10.0
            )
        except httpx.HTTPError:
            raise MailDeliveryError("magic-link provider request failed") from None
        if not response.is_success:
            raise MailDeliveryError(
                f"magic-link provider rejected delivery with status {response.status_code}"
            )

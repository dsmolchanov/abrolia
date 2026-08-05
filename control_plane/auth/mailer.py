from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Protocol, TextIO


class Mailer(Protocol):
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

    def send_magic_link(self, *, recipient: str, url: str, purpose: str) -> None:
        require_test_recipient(recipient)
        self.sent.append(SentLink(recipient, url, purpose))


class ConsoleMailer:
    """Test-only operator output. Uses a stream, never the application logger."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream or sys.stdout

    def send_magic_link(self, *, recipient: str, url: str, purpose: str) -> None:
        require_test_recipient(recipient)
        print(f"synthetic {purpose} link for {recipient}: {url}", file=self.stream)

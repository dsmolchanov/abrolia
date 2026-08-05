"""SDK-free contracts shared by runtime email providers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class EmailBinding:
    identity_id: str
    revision: int
    provider: str
    address: str
    provider_ref: str | None = None
    secret_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class InboundEmail:
    source: str
    provider_event_id: str
    raw: bytes
    received_at: float | None = None


@dataclass(frozen=True)
class EmailSendRequest:
    effect_id: str
    approval_id: str
    binding: EmailBinding
    message_id: str
    mime_bytes: bytes
    request_sha256: str

    @property
    def idempotency_key(self) -> str:
        return self.effect_id


@dataclass(frozen=True)
class EmailDeliveryReceipt:
    effect_id: str
    approval_id: str
    message_id: str
    provider_ref: str | None
    accepted_at: float | None
    status: str


class PollingEmailSource(Protocol):
    provider: str

    def poll(self) -> Sequence[InboundEmail]: ...


class WebhookEmailSource(Protocol):
    provider: str

    def decode_webhook(
        self, *, event_id: str, payload: bytes, received_at: float | None = None
    ) -> InboundEmail: ...


class EmailSendProvider(Protocol):
    provider: str

    def send(self, request: EmailSendRequest) -> EmailDeliveryReceipt: ...

    def reconcile(self, request: EmailSendRequest) -> EmailDeliveryReceipt: ...

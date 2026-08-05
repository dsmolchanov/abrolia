"""Canonical durable ingress seam for every RFC822 email provider."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from hermes_cloud.core.db import new_id
from hermes_cloud.core.events import Accepted, EventStore
from hermes_cloud.email.contracts import EmailBinding
from hermes_cloud.ingest.eml import ParsedEmail, external_id, parse_eml


@dataclass(frozen=True)
class Ingested:
    accepted: Accepted
    parsed: ParsedEmail
    content_sha256: str

    @property
    def created(self) -> bool:
        return self.accepted.created

    @property
    def event_id(self) -> str:
        return self.accepted.event.id


def ingest_rfc822(
    store: EventStore,
    *,
    source: str,
    provider_event_id: str,
    raw_bytes: bytes,
    received_at: float | None = None,
    binding: EmailBinding | None = None,
) -> Ingested:
    """Parse and fsync an email before a provider cursor/ACK may advance.

    The event identity is canonical across providers. Provider event identities
    remain separately unique in ``email_ingress_receipts`` for replay auditing.
    """
    if not source or not provider_event_id:
        raise ValueError("email source and provider_event_id are required")
    parsed = parse_eml(raw_bytes)
    digest = hashlib.sha256(raw_bytes).hexdigest()
    prior = store.db.query_one(
        "SELECT content_sha256, event_id FROM email_ingress_receipts"
        " WHERE source = ? AND provider_event_id = ?",
        (source, provider_event_id),
    )
    if prior is not None:
        if prior["content_sha256"] != digest:
            raise ValueError("provider_event_id was replayed with different content")
        event = store.get(prior["event_id"])
        if event is None:
            raise RuntimeError("email ingress receipt points to a missing event")
        return Ingested(
            accepted=Accepted(event=event, created=False),
            parsed=parsed,
            content_sha256=digest,
        )
    accepted = store.append(
        source=source,
        external_id=external_id(parsed, raw_bytes),
        raw=raw_bytes,
        context_key=parsed.thread_key,
        received_at=received_at,
    )
    now = store.clock() if received_at is None else received_at
    with store.db.write() as connection:
        connection.execute(
            "INSERT INTO email_ingress_receipts (id, binding_identity_id,"
            " binding_revision, source, provider_event_id, canonical_message_id,"
            " content_sha256, event_id, state, received_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'materialized', ?, ?)"
            " ON CONFLICT (source, provider_event_id) DO NOTHING",
            (
                new_id(),
                binding.identity_id if binding else None,
                binding.revision if binding else None,
                source,
                provider_event_id,
                parsed.message_id,
                digest,
                accepted.event.id,
                now,
                now,
            ),
        )
    return Ingested(accepted=accepted, parsed=parsed, content_sha256=digest)

"""Small provider-neutral runtime loop used by Nerve and Gmail adapters."""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass

from hermes_cloud.core.db import Database
from hermes_cloud.core.events import EventStore
from hermes_cloud.email.contracts import EmailBinding, PollingEmailSource
from hermes_cloud.email.receipts import EmailBindingStore
from hermes_cloud.ingest.rfc822 import ingest_rfc822


@dataclass(frozen=True)
class EmailHealth:
    status: str
    provider: str | None
    binding_revision: int | None
    last_success_at: float | None
    last_error: str | None = None


class EmailRuntimeService:
    def __init__(
        self,
        database: Database,
        sources: Sequence[PollingEmailSource] = (),
        *,
        clock=time.time,
    ) -> None:
        self.db = database
        self.events = EventStore(database, clock=clock)
        self.bindings = EmailBindingStore(database, clock=clock)
        self.sources = tuple(sources)
        self.clock = clock
        self._last_success_at: float | None = None
        self._last_error: str | None = None

    def activate(self, binding: EmailBinding) -> EmailBinding:
        return self.bindings.activate(binding)

    def run_once(self) -> int:
        binding = self.bindings.current()
        if binding is None:
            return 0
        accepted = 0
        try:
            for source in self.sources:
                for incoming in source.poll():
                    result = ingest_rfc822(
                        self.events,
                        source=incoming.source,
                        provider_event_id=incoming.provider_event_id,
                        raw_bytes=incoming.raw,
                        received_at=incoming.received_at,
                        binding=binding,
                    )
                    accepted += int(result.created)
                acknowledge = getattr(source, "ack", None)
                if callable(acknowledge):
                    acknowledge()
        except Exception as error:
            self._last_error = type(error).__name__
            raise
        self._last_success_at = self.clock()
        self._last_error = None
        return accepted

    def health(self) -> EmailHealth:
        binding = self.bindings.current()
        if binding is None:
            return EmailHealth("not_configured", None, None, self._last_success_at)
        if binding.provider == "gmail":
            row = self.db.query_one(
                "SELECT last_success_at, health FROM email_sync_state"
                " WHERE binding_identity_id = ? AND binding_revision = ?",
                (binding.identity_id, binding.revision),
            )
            if row is None:
                return EmailHealth("pending", "gmail", binding.revision, None)
            last_success = row["last_success_at"]
            health = str(row["health"])
            if health == "ready" and (last_success is None or self.clock() - last_success > 180):
                health = "stale_cursor"
            return EmailHealth(
                health,
                "gmail",
                binding.revision,
                last_success,
                None if health == "ready" else health,
            )
        return EmailHealth(
            "degraded" if self._last_error else "ready",
            binding.provider,
            binding.revision,
            self._last_success_at,
            self._last_error,
        )

    def run_forever(self, *, idle_sleep: float = 5.0, should_stop=lambda: False) -> None:
        while not should_stop():
            self.run_once()
            time.sleep(idle_sleep)

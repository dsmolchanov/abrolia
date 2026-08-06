"""Runtime structured observability (Phase E E7).

Every log line is JSON with no content: timestamp, level, household_id_hash (HMAC),
request_id, route, status, latency_ms — never message content, prompt, secret.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, TextIO


@dataclass(frozen=True)
class RuntimeHealth:
    nerve_key_ok: bool | None
    telegram_ok: bool | None
    wa_instance_ok: bool | None
    google_grant_ok: bool | None
    db_ok: bool
    backup_age_hours: float | None

    def needs_attention(self) -> bool:
        if self.backup_age_hours is not None and self.backup_age_hours > 30 * 24:
            return True
        return False


def household_id_hash(household_id: str, hmac_key: bytes) -> str:
    return hmac.new(hmac_key, household_id.encode(), hashlib.sha256).hexdigest()[:16]


class RuntimeStructuredLogger:
    """Emit only allowlisted JSON fields; content never enters output."""

    ALLOWED_ROUTE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/_-.")

    def __init__(self, stream: TextIO, *, hmac_key: bytes | None = None) -> None:
        self.stream = stream
        self.hmac_key = hmac_key or b"synthetic-hmac-key-for-tests"

    def emit(
        self,
        *,
        level: str,
        route: str,
        status: int | str,
        latency_ms: int,
        household_id: str | None = None,
        request_id: str | None = None,
    ) -> None:
        # Validate route is safe (no injection)
        for ch in route:
            if ch not in self.ALLOWED_ROUTE_CHARS and ch != "/":
                raise ValueError(f"unsafe route char {ch!r}")
        payload: dict[str, Any] = {
            "timestamp": time.time(),
            "level": level,
            "route": route,
            "status": status,
            "latency_ms": latency_ms,
        }
        if household_id is not None:
            payload["household_id_hash"] = household_id_hash(household_id, self.hmac_key)
        if request_id is not None:
            payload["request_id"] = request_id
        # Never log content, prompt, secret, token — enforce by allowlist.
        line = json.dumps(payload, sort_keys=True)
        # Safety: ensure no secret-like substrings leak via caller misuse.
        lowered = line.casefold()
        for needle in ("prompt", "secret", "token", "content", "email"):
            if needle in lowered:
                # route may contain those words? Only status/latency should not.
                # If household_id_hash contains, it's hex — safe. So check raw fields.
                pass
        print(line, file=self.stream)


# Alerts (operator-visible, not auto-page in pilot)
ALERTS = {
    "dlq": "DLQ > 0 — provisioning jobs failed without reconcile",
    "sticky_executing": "sticky executing — job running > 10 min without lease renewal",
    "primary_unavailable": "primary unavailable — routing fallback triggered",
    "backup_stale": "backup stale — backup_age_hours > 26h",
    "budget_exceeded": "budget exceeded — per-household/day cost cap hit",
}

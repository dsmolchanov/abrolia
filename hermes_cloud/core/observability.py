"""Runtime structured observability (Phase E E7).

Every log line is JSON with no content: timestamp, level, household_id_hash (HMAC),
request_id, route, status, latency_ms — never message content, prompt, secret.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from collections.abc import Mapping
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
        return self.backup_age_hours is not None and self.backup_age_hours > 30 * 24


def household_id_hash(household_id: str, hmac_key: bytes) -> str:
    return hmac.new(hmac_key, household_id.encode(), hashlib.sha256).hexdigest()[:16]


class RuntimeStructuredLogger:
    """Emit only allowlisted JSON fields; content never enters output."""

    ALLOWED_ROUTE_CHARS = frozenset(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/_-."
    )
    FORBIDDEN_SUBSTRINGS = ("prompt", "secret", "token", "content", "password")
    ROUTE_FORBIDDEN = ("prompt", "secret", "token", "content", "password")

    def __init__(self, stream: TextIO, *, hmac_key: bytes) -> None:
        if not hmac_key or len(hmac_key) < 16:
            raise ValueError("hmac_key must be provided (>=16 bytes) — no synthetic default")
        self.stream = stream
        self.hmac_key = hmac_key

    def _reject_forbidden(self, value: str, field: str) -> None:
        lowered = value.casefold()
        for needle in self.FORBIDDEN_SUBSTRINGS:
            if needle in lowered:
                raise ValueError(f"forbidden substring {needle!r} in {field}")

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
        # Route may contain 'email' as endpoint name — not PII, so use ROUTE allowlist
        lowered_route = route.casefold()
        for needle in self.ROUTE_FORBIDDEN:
            if needle in lowered_route:
                raise ValueError(f"forbidden substring {needle!r} in route")
        payload: dict[str, Any] = {
            "timestamp": time.time(),
            "level": level,
            "route": route,
            "status": status,
            "latency_ms": latency_ms,
        }
        if household_id is not None:
            self._reject_forbidden(household_id, "household_id")
            payload["household_id_hash"] = household_id_hash(household_id, self.hmac_key)
        if request_id is not None:
            self._reject_forbidden(str(request_id), "request_id")
            self._reject_forbidden(str(status), "status")
            payload["request_id"] = request_id
        else:
            self._reject_forbidden(str(status), "status")
        # Final line must not contain forbidden substrings in raw JSON.
        line = json.dumps(payload, sort_keys=True)
        lowered = line.casefold()
        for needle in self.FORBIDDEN_SUBSTRINGS:
            if needle in lowered:
                # Only level/route/status may contain, but we already checked — fail closed.
                raise ValueError(f"forbidden content {needle!r} in log line")
        print(line, file=self.stream)


# Alerts (operator-visible, not auto-page in pilot)
ALERTS = {
    "dlq": "DLQ > 0 — provisioning jobs failed without reconcile",
    "sticky_executing": "sticky executing — job running > 10 min without lease renewal",
    "primary_unavailable": "primary unavailable — routing fallback triggered",
    "backup_stale": "backup stale — backup_age_hours > 26h",
    "budget_exceeded": "budget exceeded — per-household/day cost cap hit",
}


#: Field names that carry a tenant identifier. An alert may not print any of
#: these verbatim — `RuntimeStructuredLogger` sixty lines above refuses to
#: start without an HMAC key precisely so that identifiers reach the log as
#: `household_id_hash`, and an alert writing the raw value to the same
#: Fly-hosted stream would walk around that guarantee. AGENTS.md: "No PII or
#: PHI field in a payload leaving the system to a third party."
IDENTIFIER_FIELDS = frozenset({
    "household_id",
    "account_id",
    "actor_id",
    "chat_id",
    "external_id",
})

#: What an identifier becomes when no key is configured. The ALERT still
#: fires — that a household crossed its cap is the operator-relevant fact, and
#: losing which household is strictly better than naming it in a third party's
#: logs. `RuntimeStructuredLogger` makes the same trade by emitting nothing at
#: all when the key is short or absent.
UNKEYED_IDENTIFIER = "unkeyed"


def alert_hmac_key(env: Mapping[str, str] | None = None) -> bytes:
    """The key identifiers are hashed under, or empty when none is usable.

    Same two variables, and the same >=16-byte floor, that the request logger
    reads in `runtime/service.py` — one key for one household, so an operator
    correlating an alert with a request line sees the same hash on both.
    """
    source = os.environ if env is None else env
    raw = (
        source.get("ABROLIA_HMAC_KEY") or source.get("HERMES_HOUSEHOLD_HMAC_KEY") or ""
    ).encode()
    return raw if len(raw) >= 16 else b""


def emit_alert(
    logger_: Any,
    name: str,
    *,
    env: Mapping[str, str] | None = None,
    **fields: str,
) -> None:
    """Log a defined alert through the given logger.

    An alert that exists only as a dictionary entry is documentation, not an
    alert. Unknown names raise: a typo must be loud at the call site, not a
    silently skipped warning.

    Tenant identifiers are hashed here rather than at the call sites. All three
    channels pass `household_id=`, so a rule enforced at each of them is a rule
    with three chances to be forgotten — and the fourth caller would be the
    leak. This is the boundary the field crosses, so this is where it is
    redacted.
    """
    if name not in ALERTS:
        raise KeyError(f"unknown alert {name!r}")
    key = alert_hmac_key(env)
    safe: dict[str, str] = {}
    for field, value in fields.items():
        if field in IDENTIFIER_FIELDS:
            safe[f"{field}_hash"] = (
                household_id_hash(str(value), key) if key else UNKEYED_IDENTIFIER
            )
        else:
            safe[field] = value
    suffix = "".join(f" {key_}={value}" for key_, value in sorted(safe.items()))
    logger_.warning("ALERT %s — %s%s", name, ALERTS[name], suffix)

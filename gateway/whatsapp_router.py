"""Shared gateway narrow multi-tenant relay (Phase E E5).

No model/tools/secrets, only sender→household mapping via channel_bindings
with exact HMAC, durable ingress before ACK, per-household relay-HMAC.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
import sqlite3


@dataclass(frozen=True)
class GatewayResult:
    status: str  # delivered | denied
    code: str  # unknown_sender | ambiguous_sender | hmac_rejected | ok
    household_id: str | None = None


def relay_hmac(household_key: bytes, body: bytes, timestamp: str) -> str:
    return hmac.new(household_key, body + b"|" + timestamp.encode(), hashlib.sha256).hexdigest()


def verify_relay_hmac(household_key: bytes, body: bytes, timestamp: str, signature: str) -> bool:
    expected = relay_hmac(household_key, body, timestamp)
    supplied = signature.removeprefix("sha256=").strip().lower()
    if len(supplied) != len(expected):
        return False
    return hmac.compare_digest(supplied, expected)


class GatewayStore:
    """Durable ingress WAL before ACK."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS gateway_ingress ("
            "id TEXT PRIMARY KEY, payload BLOB NOT NULL, sender TEXT NOT NULL, "
            "received_at REAL NOT NULL, delivered INTEGER NOT NULL DEFAULT 0)"
        )

    def persist_before_ack(self, payload: bytes, sender: str) -> str:
        ingress_id = uuid.uuid4().hex
        self.conn.execute(
            "INSERT INTO gateway_ingress (id, payload, sender, received_at, delivered) VALUES (?, ?, ?, ?, 0)",
            (ingress_id, payload, sender, time.time()),
        )
        self.conn.commit()
        return ingress_id

    def mark_delivered(self, ingress_id: str) -> None:
        self.conn.execute("DELETE FROM gateway_ingress WHERE id = ?", (ingress_id,))
        self.conn.commit()


class WhatsAppGatewayRouter:
    """Narrow relay: sender -> exactly one household via channel_bindings."""

    def __init__(self, db, *, relay_keys: dict[str, bytes] | None = None, ingress_path: Path | str | None = None) -> None:
        self.db = db
        self.relay_keys = relay_keys or {}
        self.store = GatewayStore(ingress_path or Path("data/gateway_ingress.db")) if ingress_path is not None else None

    def route(self, sender: str, channel: str = "whatsapp") -> GatewayResult:
        rows = self.db.query(
            "SELECT household_id FROM channel_bindings WHERE channel = ? AND external_id = ?",
            (channel, sender),
        )
        ids = [r["household_id"] for r in rows]
        if len(ids) == 0:
            return GatewayResult(status="denied", code="unknown_sender")
        if len(ids) > 1:
            return GatewayResult(status="denied", code="ambiguous_sender")
        return GatewayResult(status="delivered", code="ok", household_id=ids[0])

    def handle_webhook(self, payload: bytes, sender: str, *, channel: str = "whatsapp") -> GatewayResult:
        # Durable before ACK
        store = self.store or GatewayStore(Path("data/gateway_ingress.db"))
        ingress_id = store.persist_before_ack(payload, sender)
        result = self.route(sender, channel)
        if result.status == "denied":
            # Keep ingress for reconcile? For pilot, delete denied as well but log.
            store.mark_delivered(ingress_id)
            return result
        # Simulate relay HMAC signing; delivery to runtime would verify.
        household_id = result.household_id
        if household_id and household_id in self.relay_keys:
            body = payload
            ts = str(int(time.time()))
            _sig = relay_hmac(self.relay_keys[household_id], body, ts)
            # In real path, runtime verifies; here we just ensure key exists.
        store.mark_delivered(ingress_id)
        return result

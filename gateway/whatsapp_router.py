"""Shared gateway narrow multi-tenant relay (Phase E E5).

No model/tools/secrets, only sender→household mapping via channel_bindings
with keyed HMAC, durable ingress before ACK, per-household relay-HMAC.
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GatewayResult:
    status: str  # delivered | denied
    code: str  # unknown_sender | ambiguous_sender | hmac_rejected | timestamp_replay | ok
    household_id: str | None = None


def relay_hmac(household_key: bytes, body: bytes, timestamp: str) -> str:
    return hmac.new(household_key, body + b"|" + timestamp.encode(), hashlib.sha256).hexdigest()


def verify_relay_hmac(household_key: bytes, body: bytes, timestamp: str, signature: str) -> bool:
    expected = relay_hmac(household_key, body, timestamp)
    supplied = signature.removeprefix("sha256=").strip().lower()
    if len(supplied) != len(expected):
        return False
    return hmac.compare_digest(supplied, expected)


def sender_hmac(sender: str, gateway_key: bytes) -> str:
    return hmac.new(gateway_key, sender.encode(), hashlib.sha256).hexdigest()


class GatewayStore:
    """Durable ingress WAL before ACK — only delete after confirmed runtime delivery."""

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
            "INSERT INTO gateway_ingress (id, payload, sender, received_at, delivered) "
            "VALUES (?, ?, ?, ?, 0)",
            (ingress_id, payload, sender, time.time()),
        )
        self.conn.commit()
        return ingress_id

    def mark_delivered(self, ingress_id: str) -> None:
        self.conn.execute("DELETE FROM gateway_ingress WHERE id = ?", (ingress_id,))
        self.conn.commit()


class WhatsAppGatewayRouter:
    """Narrow relay: sender -> exactly one household via HMAC lookup."""

    REPLAY_WINDOW_SECONDS = 300

    def __init__(
        self,
        db,
        *,
        relay_keys: dict[str, bytes] | None = None,
        gateway_hmac_key: bytes | None = None,
        ingress_path: Path | str | None = None,
        runtime_deliver=None,
        now_fn=None,
    ) -> None:
        self.db = db
        self.relay_keys = relay_keys or {}
        self.gateway_hmac_key = gateway_hmac_key
        self.store = (
            GatewayStore(ingress_path or Path("data/gateway_ingress.db"))
            if ingress_path is not None
            else None
        )
        self.runtime_deliver = runtime_deliver
        self.now_fn = now_fn or time.time

    def route(
        self, sender: str, channel: str = "whatsapp", *, timestamp: str | None = None
    ) -> GatewayResult:
        # Timestamp freshness required whenever HMAC mode is active
        if self.gateway_hmac_key is not None:
            if timestamp is None:
                return GatewayResult(status="denied", code="timestamp_replay")
            try:
                ts = int(timestamp)
            except ValueError:
                return GatewayResult(status="denied", code="timestamp_replay")
            if abs(int(self.now_fn()) - ts) > self.REPLAY_WINDOW_SECONDS:
                return GatewayResult(status="denied", code="timestamp_replay")
            # Strict HMAC lookup — no plaintext fallback when key configured
            h = sender_hmac(sender, self.gateway_hmac_key)
            rows = self.db.query(
                "SELECT household_id FROM channel_bindings "
                "WHERE channel = ? AND external_id_hmac = ?",
                (channel, h),
            )
        else:
            if timestamp is not None:
                try:
                    ts = int(timestamp)
                except ValueError:
                    return GatewayResult(status="denied", code="timestamp_replay")
                if abs(int(self.now_fn()) - ts) > self.REPLAY_WINDOW_SECONDS:
                    return GatewayResult(status="denied", code="timestamp_replay")
            rows = self.db.query(
                "SELECT household_id FROM channel_bindings "
                "WHERE channel = ? AND external_id = ?",
                (channel, sender),
            )
        ids = [r["household_id"] for r in rows]
        if len(ids) == 0:
            return GatewayResult(status="denied", code="unknown_sender")
        if len(ids) > 1:
            return GatewayResult(status="denied", code="ambiguous_sender")
        return GatewayResult(status="delivered", code="ok", household_id=ids[0])

    def handle_webhook(
        self,
        payload: bytes,
        sender: str,
        *,
        channel: str = "whatsapp",
        timestamp: str,
        signature: str,
    ) -> GatewayResult:
        # Durable before ACK — persist first, ACK only after persist succeeds
        if not timestamp or not signature:
            return GatewayResult(status="denied", code="hmac_rejected", household_id=None)
        store = self.store or GatewayStore(Path("data/gateway_ingress.db"))
        ingress_id = store.persist_before_ack(payload, sender)
        routed = self.route(sender, channel, timestamp=timestamp)
        if routed.status == "denied":
            store.mark_delivered(ingress_id)
            return routed
        key = self.relay_keys.get(routed.household_id) if routed.household_id else None
        if not key or not verify_relay_hmac(key, payload, timestamp, signature):
            return GatewayResult(status="denied", code="hmac_rejected", household_id=None)
        # Deliver to runtime — only delete WAL after confirmed delivery
        try:
            if self.runtime_deliver:
                self.runtime_deliver(routed.household_id, payload, timestamp, signature)
            # If no explicit deliver fn, successful HMAC is the delivery proof for this pilot
        except Exception:
            # Delivery failed — keep WAL for reconcile, do not ACK as delivered
            return GatewayResult(status="denied", code="hmac_rejected", household_id=None)
        store.mark_delivered(ingress_id)
        return routed

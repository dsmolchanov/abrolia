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

from control_plane.feature_flags import is_whatsapp_shared_enabled


@dataclass(frozen=True)
class GatewayResult:
    status: str  # delivered | denied
    #: ok | unknown_sender | ambiguous_sender | timestamp_replay | flag_disabled
    #: | hmac_rejected | relay_key_absent | runtime_unavailable
    #:
    #: The last three are all "denied", and they differ in what happens to the
    #: ingress row: `hmac_rejected` is terminal and drops it, the other two are
    #: retryable and keep it. See `handle_webhook`.
    code: str
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
    """Durable ingress WAL before ACK — only delete after confirmed runtime delivery.

    Every row here is a family's message body at rest, which is why deleting is
    the normal end of a row's life and every path that stops retrying deletes.
    A row is kept only while redelivery could still change its outcome.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS gateway_ingress ("
            "id TEXT PRIMARY KEY, payload BLOB NOT NULL, sender TEXT NOT NULL, "
            "received_at REAL NOT NULL, delivered INTEGER NOT NULL DEFAULT 0)"
        )
        # The three columns the reader needs. Added by ALTER rather than folded
        # into the CREATE above so a WAL file written before this slice keeps
        # the rows it is holding — the whole point of the file is that it
        # survives the process, and a gateway restarting into a schema that
        # cannot read its own backlog would lose exactly what it persisted.
        #
        # This is not the control plane's migration chain: the file belongs to
        # a gateway process, is created on demand, and has no `schema_version`
        # to advance. `PRAGMA table_info` is the whole mechanism it needs.
        existing = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(gateway_ingress)")
        }
        for column, ddl in (
            ("attempts", "attempts INTEGER NOT NULL DEFAULT 0"),
            ("next_attempt_at", "next_attempt_at REAL NOT NULL DEFAULT 0"),
            ("last_error", "last_error TEXT"),
        ):
            if column not in existing:
                self.conn.execute(f"ALTER TABLE gateway_ingress ADD COLUMN {ddl}")
        self.conn.commit()

    def persist_before_ack(
        self, payload: bytes, sender: str, *, now: float | None = None
    ) -> str:
        # `now` comes from the router's clock rather than being read here.
        # `received_at` is only meaningful against the clock the redeliver
        # worker ages it with, and this used to call `time.time()` directly
        # while the router took an injectable `now_fn` — two clocks for one
        # comparison, which makes `MAX_AGE_SECONDS` mean nothing wherever they
        # differ and silently disables expiry under any injected clock.
        now = time.time() if now is None else now
        ingress_id = uuid.uuid4().hex
        self.conn.execute(
            "INSERT INTO gateway_ingress (id, payload, sender, received_at, delivered,"
            " attempts, next_attempt_at) VALUES (?, ?, ?, ?, 0, 0, ?)",
            (ingress_id, payload, sender, now, now),
        )
        self.conn.commit()
        return ingress_id

    def mark_delivered(self, ingress_id: str) -> None:
        self.conn.execute("DELETE FROM gateway_ingress WHERE id = ?", (ingress_id,))
        self.conn.commit()

    def defer(self, ingress_id: str, *, error: str, not_before: float) -> None:
        """Leave the row for the reader, one attempt older."""
        self.conn.execute(
            "UPDATE gateway_ingress SET attempts = attempts + 1,"
            " next_attempt_at = ?, last_error = ? WHERE id = ?",
            (not_before, error, ingress_id),
        )
        self.conn.commit()

    def due(self, *, now: float, limit: int = 100) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT id, payload, sender, received_at, attempts, last_error"
                " FROM gateway_ingress WHERE next_attempt_at <= ?"
                " ORDER BY received_at LIMIT ?",
                (now, limit),
            )
        )


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
        # Phase F fail-closed kill switch — read at call time, not startup.
        # Through the shared accessor: this used to spell the variable itself,
        # which made two readers of one switch and no way to see from
        # `feature_flags` that the switch was live at all.
        if not is_whatsapp_shared_enabled():
            return GatewayResult(status="denied", code="flag_disabled", household_id=None)
        # Durable before ACK — persist first, ACK only after persist succeeds
        if not timestamp or not signature:
            return GatewayResult(status="denied", code="hmac_rejected", household_id=None)
        store = self.store or GatewayStore(Path("data/gateway_ingress.db"))
        ingress_id = store.persist_before_ack(payload, sender, now=self.now_fn())
        routed = self.route(sender, channel, timestamp=timestamp)
        if routed.status == "denied":
            store.mark_delivered(ingress_id)
            return routed
        # `hmac_rejected` used to be the answer to four different questions,
        # and the WAL row's fate was decided by which `return` happened to run.
        # The question the reader actually has to ask is not "was the HMAC bad"
        # but CAN THIS EVER BE DELIVERED, so that is what the code answers.
        key = self.relay_keys.get(routed.household_id) if routed.household_id else None
        if not key:
            # No key for this household YET. Retryable, and the reason is C5c:
            # the relay-key provisioning path does not exist, so a binding can
            # be routable while its key is not yet installed. This is the case
            # `tests/test_gateway_routing.py` has always described as "WAL kept
            # for reconcile" — the reconcile that is now written.
            store.defer(
                ingress_id,
                error="relay_key_absent",
                not_before=self.now_fn() + GatewayRedeliverWorker.BASE_BACKOFF_SECONDS,
            )
            return GatewayResult(
                status="denied", code="relay_key_absent", household_id=None
            )
        if not verify_relay_hmac(key, payload, timestamp, signature):
            # A present key rejected these bytes. TERMINAL: the payload cannot
            # become valid under a key that already exists, so retrying is only
            # a way of storing somebody's message body forever. Deleted for the
            # same reason a denied route is.
            store.mark_delivered(ingress_id)
            return GatewayResult(status="denied", code="hmac_rejected", household_id=None)
        # Deliver to runtime — only delete WAL after confirmed delivery
        try:
            if self.runtime_deliver:
                self.runtime_deliver(routed.household_id, payload, timestamp, signature)
            # If no explicit deliver fn, successful HMAC is the delivery proof for this pilot
        except Exception:
            # The runtime may come back. Retryable, and named for what happened
            # rather than reported as an HMAC failure it has nothing to do with.
            store.defer(
                ingress_id,
                error="runtime_unavailable",
                not_before=self.now_fn() + GatewayRedeliverWorker.BASE_BACKOFF_SECONDS,
            )
            return GatewayResult(
                status="denied", code="runtime_unavailable", household_id=None
            )
        store.mark_delivered(ingress_id)
        return routed


@dataclass(frozen=True)
class RedeliverReport:
    """Counts only. A gateway report never carries a payload or a sender."""

    delivered: int = 0
    deferred: int = 0
    dropped_exhausted: int = 0
    dropped_expired: int = 0
    dropped_undeliverable: int = 0

    def public_dict(self) -> dict[str, int]:
        return {
            "delivered": self.delivered,
            "deferred": self.deferred,
            "dropped_exhausted": self.dropped_exhausted,
            "dropped_expired": self.dropped_expired,
            "dropped_undeliverable": self.dropped_undeliverable,
        }


class GatewayRedeliverWorker:
    """The reader `GatewayStore` was built for and never had.

    `persist_before_ack` has always written a row before the webhook is ACKed,
    and `mark_delivered` has always deleted it once the runtime confirmed. In
    between there was nothing: a row a delivery failure left behind stayed
    until somebody deleted the file. The durability was real and it was never
    spent, which is the same as not having it — a message the gateway accepted
    responsibility for was lost the moment the first attempt failed.

    Rows are RE-ROUTED rather than delivered to the household the first attempt
    resolved. That matters most for the case this was written for: a row kept
    because no relay key existed is retried after C5c installs one, and by then
    the binding may also have changed. Trusting the old answer would deliver a
    message to a household that no longer holds the sender.
    """

    #: First retry this far out, doubling per attempt. A runtime that is down
    #: stays down for longer than a webhook takes, and retrying hot would spend
    #: the outage sending it traffic it cannot take.
    BASE_BACKOFF_SECONDS = 30.0
    #: After this many attempts the row is dropped and COUNTED. A gateway WAL
    #: is not a dead-letter store: it holds message bodies, and the alternative
    #: to dropping is keeping a family's messages indefinitely for an operator
    #: who has no interface to read them. What is retained is the fact.
    MAX_ATTEMPTS = 8
    #: And bounded in the other direction, because attempts are not the only
    #: way a row goes stale. A WhatsApp message delivered three days late is not
    #: a repair, and holding it that long is the retention problem either way.
    MAX_AGE_SECONDS = 24 * 60 * 60

    def __init__(self, router: WhatsAppGatewayRouter, *, store: GatewayStore | None = None):
        self.router = router
        self.store = store or router.store
        if self.store is None:
            raise ValueError("a redeliver worker needs an ingress store to read")

    def run_once(self, *, limit: int = 100) -> RedeliverReport:
        now = self.router.now_fn()
        # One stamp for the whole pass, and it is not cosmetic: it is what the
        # re-route below is checked against and what each redelivery is signed
        # with, so a row cannot be routed against one instant and signed for
        # another.
        timestamp = str(int(now))
        delivered = deferred = exhausted = expired = undeliverable = 0
        for row in self.store.due(now=now, limit=limit):
            ingress_id = row["id"]
            if now - row["received_at"] > self.MAX_AGE_SECONDS:
                self.store.mark_delivered(ingress_id)
                expired += 1
                continue
            # A FRESH timestamp, not the one the message arrived with. `route`
            # refuses `None` outright in strict mode and refuses anything older
            # than `REPLAY_WINDOW_SECONDS` in either — so passing the original
            # would make every redelivery answer `timestamp_replay` and drop
            # the entire backlog as undeliverable, which is the opposite of
            # what this worker is for. The window guards the freshness of an
            # inbound webhook; a redelivery's own freshness is now.
            routed = self.router.route(row["sender"], timestamp=timestamp)
            if routed.status == "denied":
                # Nobody to deliver to. `unknown_sender` here is not the same
                # event as at ingress: the binding was resolvable once and is
                # not now, which is a revocation taking effect on a message in
                # flight — the right outcome, and a terminal one.
                self.store.mark_delivered(ingress_id)
                undeliverable += 1
                continue
            key = self.router.relay_keys.get(routed.household_id)
            if not key:
                # Still no key. This is the wait C5c ends.
                kept = self._defer(row, now, "relay_key_absent")
                deferred += kept
                exhausted += 1 - kept
                continue
            try:
                if self.router.runtime_deliver:
                    # Re-signed under the household's CURRENT key, over the
                    # same body, with this pass's stamp: the runtime enforces
                    # the replay window C5a gave it, so a redelivery carrying
                    # the original hour-old signature would be refused by the
                    # very freshness check that makes the scheme safe. The BODY
                    # is unchanged, so this is the same message and not a new
                    # one — the runtime's ingest dedupes on the relay
                    # provenance it carries, which is why this slice does not
                    # add a second idempotency mechanism beside it.
                    self.router.runtime_deliver(
                        routed.household_id,
                        row["payload"],
                        timestamp,
                        relay_hmac(key, row["payload"], timestamp),
                    )
            except Exception:
                kept = self._defer(row, now, "runtime_unavailable")
                deferred += kept
                exhausted += 1 - kept
                continue
            self.store.mark_delivered(ingress_id)
            delivered += 1
        return RedeliverReport(
            delivered=delivered,
            deferred=deferred,
            dropped_exhausted=exhausted,
            dropped_expired=expired,
            dropped_undeliverable=undeliverable,
        )

    def _defer(self, row, now: float, error: str) -> int:
        """Keep the row for another attempt, or drop it once attempts run out.

        Returns 1 if the row was kept and 0 if it was dropped, so the caller
        counts one or the other from a single answer.
        """
        attempts = int(row["attempts"]) + 1
        if attempts >= self.MAX_ATTEMPTS:
            self.store.mark_delivered(row["id"])
            return 0
        self.store.defer(
            row["id"],
            error=error,
            not_before=now + self.BASE_BACKOFF_SECONDS * (2 ** (attempts - 1)),
        )
        return 1

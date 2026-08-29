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

from control_plane.crypto import (  # one implementation, both ends
    RELAY_KEY_LABEL,
    derive_relay_key,
    sender_hmac,
)
from control_plane.feature_flags import is_whatsapp_shared_enabled

__all__ = [
    "RELAY_KEY_LABEL",
    "GatewayRedeliverWorker",
    "GatewayResult",
    "GatewayStore",
    "RedeliverReport",
    "WhatsAppGatewayRouter",
    "derive_relay_key",
    "relay_hmac",
    "sender_hmac",
    "verify_relay_hmac",
]


@dataclass(frozen=True)
class GatewayResult:
    status: str  # delivered | denied
    #: ok | unknown_sender | ambiguous_sender | timestamp_replay | flag_disabled
    #: | hmac_rejected | relay_key_absent | runtime_unavailable
    #:
    #: The last three are all "denied", and they differ in what happens to the
    #: ingress row: `hmac_rejected` is terminal and drops it, the other two are
    #: retryable and keep it. See `handle_webhook`.
    #:
    #: A retained row keeps the timestamp and signature it arrived with, so the
    #: redeliver worker can prove it came from the relay before signing it with
    #: the real key. Retaining without that proof is what would let a forged
    #: body be laundered into the runtime once a key appeared.
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
            # The routing context the row was accepted UNDER. A redelivery that
            # re-derives these instead of remembering them answers a different
            # question than the one the message arrived with: the channel
            # defaults to whatsapp, so a telegram row becomes undeliverable,
            # and the household is whoever holds the sender NOW, so a sender
            # rebound from A to B carries A's message into B.
            ("channel", "channel TEXT"),
            ("household_id", "household_id TEXT"),
            # And the credentials it arrived with. Kept so redelivery can PROVE
            # the payload came from the relay before signing it with the real
            # key — see `GatewayRedeliverWorker.run_once`.
            ("origin_timestamp", "origin_timestamp TEXT"),
            ("origin_signature", "origin_signature TEXT"),
        ):
            if column not in existing:
                self.conn.execute(f"ALTER TABLE gateway_ingress ADD COLUMN {ddl}")
        self.conn.commit()

    def persist_before_ack(
        self,
        payload: bytes,
        sender: str,
        *,
        channel: str = "whatsapp",
        household_id: str | None = None,
        timestamp: str | None = None,
        signature: str | None = None,
        now: float | None = None,
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
            " attempts, next_attempt_at, channel, household_id, origin_timestamp,"
            " origin_signature) VALUES (?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?)",
            (
                ingress_id, payload, sender, now, now, channel, household_id,
                timestamp, signature,
            ),
        )
        self.conn.commit()
        return ingress_id

    def mark_delivered(self, ingress_id: str) -> None:
        self.conn.execute("DELETE FROM gateway_ingress WHERE id = ?", (ingress_id,))
        self.conn.commit()

    def defer(
        self,
        ingress_id: str,
        *,
        error: str,
        not_before: float,
        household_id: str | None = None,
        spend_attempt: bool = True,
    ) -> None:
        """Leave the row for the reader, normally one attempt older.

        `spend_attempt=False` is for a hold that is nobody's failure — the
        kill switch being off is an operator decision, and letting it burn the
        backlog's attempts would mean the brake destroyed the work it was
        pulled to protect.
        """
        self.conn.execute(
            "UPDATE gateway_ingress SET attempts = attempts + ?,"
            " next_attempt_at = ?, last_error = ?,"
            " household_id = COALESCE(?, household_id) WHERE id = ?",
            (1 if spend_attempt else 0, not_before, error, household_id, ingress_id),
        )
        self.conn.commit()

    def due(self, *, now: float, limit: int = 100) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT id, payload, sender, received_at, attempts, last_error,"
                " channel, household_id, origin_timestamp, origin_signature"
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
        self._ingress_path = Path(ingress_path or "data/gateway_ingress.db")
        self._store: GatewayStore | None = None
        self.runtime_deliver = runtime_deliver
        self.now_fn = now_fn or time.time

    @property
    def store(self) -> GatewayStore:
        """The one ingress store, opened on first use.

        `self.store` used to be None whenever no `ingress_path` was given,
        while `handle_webhook` quietly opened a throwaway store on the default
        path to write to. That made two stores for one WAL: the router wrote
        retryable failures into `data/gateway_ingress.db` and the redeliver
        worker, constructed the normal way from that router, raised because
        `router.store` was None — so on the default configuration, which is the
        deployed one, nothing was ever retried.

        Lazy rather than eager so constructing a router still creates no
        directory until something actually persists.
        """
        if self._store is None:
            self._store = GatewayStore(self._ingress_path)
        return self._store

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
                # `published_revision IS NOT NULL` is C3c: a binding is
                # routable only once the revision carrying it has ACTIVATED.
                # Without it a member verified during a rollout was routed to a
                # runtime still serving the previous revision, whose manifest
                # had no pair for them — the runtime denied the turn and their
                # message went nowhere.
                "SELECT household_id FROM channel_bindings "
                "WHERE channel = ? AND external_id_hmac = ? "
                "AND published_revision IS NOT NULL",
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
                # See the strict branch above: staged bindings do not route.
                "SELECT household_id FROM channel_bindings "
                "WHERE channel = ? AND external_id = ? "
                "AND published_revision IS NOT NULL",
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
        if not timestamp or not signature:
            return GatewayResult(status="denied", code="hmac_rejected", household_id=None)
        store = self.store
        # ROUTE FIRST, then persist the row COMPLETE. The resolved household is
        # part of what the row was accepted as, and recording it in a later
        # `defer` left a window — from this commit until that update — in which
        # a row on disk had no household. A process killed in that window,
        # which is most likely while `runtime_deliver` is in flight, restarted
        # into a row `_has_provenance` reads as unverifiable and deletes. The
        # write-ahead log would have lost the message on exactly the crash it
        # exists to survive, and both retryable paths shared the window.
        #
        # Routing before persisting does not weaken "durable before ACK":
        # `route` is a local read of `channel_bindings` with no external effect,
        # and the ACK is this function returning. What is now true is that the
        # row is never on disk in a state the reader cannot act on.
        #
        # A denied route also stops writing a row it would immediately delete.
        routed = self.route(sender, channel, timestamp=timestamp)
        if routed.status == "denied":
            return routed
        ingress_id = store.persist_before_ack(
            payload,
            sender,
            channel=channel,
            household_id=routed.household_id,
            timestamp=timestamp,
            signature=signature,
            now=self.now_fn(),
        )
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
    #: Could not be proven to have come from the relay — a legacy row, or one
    #: retained while no key existed and never authenticated since.
    dropped_unverifiable: int = 0
    #: Left alone because the kill switch is off. Not a drop and not a failure.
    held: int = 0

    def public_dict(self) -> dict[str, int]:
        return {
            "delivered": self.delivered,
            "deferred": self.deferred,
            "dropped_exhausted": self.dropped_exhausted,
            "dropped_expired": self.dropped_expired,
            "dropped_undeliverable": self.dropped_undeliverable,
            "dropped_unverifiable": self.dropped_unverifiable,
            "held": self.held,
        }


class GatewayRedeliverWorker:
    """The reader `GatewayStore` was built for and never had.

    `persist_before_ack` has always written a row before the webhook is ACKed,
    and `mark_delivered` has always deleted it once the runtime confirmed. In
    between there was nothing: a row a delivery failure left behind stayed
    until somebody deleted the file. The durability was real and it was never
    spent, which is the same as not having it — a message the gateway accepted
    responsibility for was lost the moment the first attempt failed.

    THE INVARIANTS, written down because not writing them down is what went
    wrong. This function has five cross-cutting concerns, and four review
    generations found them one at a time — each fix locally correct, each
    incomplete, because the rule was being inferred from the last defect rather
    than stated. They are stated here, and `_process` is ordered to satisfy
    them in this order:

    **I1 — Every row is classified before anything is gated.** A row's outcome
    splits in two. TERMINAL outcomes delete the row and are retention or
    security decisions: too old to be worth delivering, no provenance to
    authenticate, nobody to deliver to, a signature that does not verify. Those
    are correct whether or not traffic is flowing, so nothing gates them.
    RETRYABLE outcomes keep the row, and delivery sends it; both are gated.
    The kill switch sits exactly on that seam — it stops sending and retrying,
    never classification, so an incident does not also suspend cleanup.

    **I2 — One clock per row, read when that row is processed.** Expiry, route
    freshness, the runtime signature and every backoff are time-sensitive, and
    a batch is up to `limit` rows with a blocking network call in each. A stamp
    taken at batch start goes stale inside the batch: past
    `REPLAY_WINDOW_SECONDS` the next row routes as `timestamp_replay` and is
    deleted as undeliverable. A backoff scheduled after a delivery attempt is
    measured from AFTER it, because the attempt itself consumed real time.

    **I3 — An attempt is spent only on a genuine try that could succeed later.**
    No key yet and the runtime refusing are attempts. A hold is not: the switch
    being off is an operator decision, and letting it burn `MAX_ATTEMPTS` would
    mean the brake destroyed the work it was pulled to protect. A terminal drop
    spends nothing because there is nothing left to try.

    **I4 — Re-routing decides WHETHER to deliver, never to whom.** A row waits
    across the window in which a binding can change, so it is re-routed on the
    channel it arrived on — and the answer must agree with the household the
    row was accepted for. A sender rebound from A to B would otherwise carry
    A's message into B's runtime, signed with B's key.

    **I5 — Nothing is signed that was not proven to come from the relay.** The
    `relay_key_absent` path retains a row without verifying it, because there
    is no key to verify with. The stored timestamp and signature are checked
    against the key that later appears BEFORE anything is re-signed; a row that
    cannot answer is dropped, never blessed. The gateway's signature is exactly
    the runtime's reason to trust a payload.

    One more, which is structural rather than about the domain: **every row
    yields exactly one outcome, and a row that fails unexpectedly is deferred
    rather than allowed to abandon the batch.** `_process` returns that outcome
    and `run_once` only counts, so a row cannot be both delivered and deferred,
    and one poison row cannot block a backlog forever.
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
        """Drain what is due. Every row gets exactly one outcome; this counts.

        The selection clock is the only one taken here — see I2: each row reads
        its own, because the delivery inside the loop blocks and a batch-wide
        stamp goes stale between rows.
        """
        counted: dict[str, int] = {}
        for row in self.store.due(now=self.router.now_fn(), limit=limit):
            try:
                outcome = self._process(row)
            except Exception:
                # A row whose processing throws must not abandon the batch, and
                # must not spin: it is deferred like any other retryable
                # failure, so a poison row backs off and is eventually dropped
                # by `MAX_ATTEMPTS` instead of blocking every row behind it.
                outcome = self._defer(row, self.router.now_fn(), "worker_error")
            counted[outcome] = counted.get(outcome, 0) + 1
        return RedeliverReport(
            delivered=counted.get("delivered", 0),
            deferred=counted.get("deferred", 0),
            dropped_exhausted=counted.get("exhausted", 0),
            dropped_expired=counted.get("expired", 0),
            dropped_undeliverable=counted.get("undeliverable", 0),
            dropped_unverifiable=counted.get("unverifiable", 0),
            held=counted.get("held", 0),
        )

    def _process(self, row) -> str:
        """One row, one outcome. Ordered by the invariants in the class docstring."""
        # I2: this row's clock, not the batch's.
        now = self.router.now_fn()
        ingress_id = row["id"]

        # ------------------------------------------------------------------
        # I1, first half: TERMINAL classification. Ungated — these delete the
        # row, and deleting message content is correct during an incident.
        # ------------------------------------------------------------------
        if now - row["received_at"] > self.MAX_AGE_SECONDS:
            # Attempts are not the only way a row goes stale. A message
            # delivered a day late is not a repair, and holding it is the
            # retention problem either way.
            self.store.mark_delivered(ingress_id)
            return "expired"
        if not self._has_provenance(row):
            # I5: a row that cannot say what it arrived as. Written before
            # these columns existed, or by the implementation that retained
            # rows after a REJECTED HMAC. It can never be authenticated, so it
            # is dropped rather than kept for a signature it must not get.
            self.store.mark_delivered(ingress_id)
            return "unverifiable"
        stamp = str(int(now))
        # I4, and on the channel the row ARRIVED on: defaulting to whatsapp
        # resolved a telegram row against the wrong binding set. The stamp is
        # fresh because `route` refuses `None` and anything outside
        # `REPLAY_WINDOW_SECONDS` — that window guards an inbound webhook's
        # freshness, and a redelivery's own freshness is now.
        routed = self.router.route(row["sender"], row["channel"], timestamp=stamp)
        if routed.status == "denied" or routed.household_id != row["household_id"]:
            # Either nobody holds this sender now — a revocation taking effect
            # on a message in flight, which is the right outcome — or somebody
            # else does, which is I4: re-routing decides whether, never to whom.
            self.store.mark_delivered(ingress_id)
            return "undeliverable"
        key = self.router.relay_keys.get(routed.household_id)
        if key is not None and not verify_relay_hmac(
            key, row["payload"], row["origin_timestamp"], row["origin_signature"]
        ):
            # I5. Checked HERE, above the brake, because it is terminal: a body
            # a present key rejects is forged or corrupt and no future state
            # makes it deliverable. The original timestamp is what the check
            # uses because that is what was signed — authenticity is the
            # question, and the fresh stamp below answers freshness instead.
            self.store.mark_delivered(ingress_id)
            return "unverifiable"

        # ------------------------------------------------------------------
        # I1, second half: RETRYABLE work and delivery. Everything below is
        # gated by the brake, and nothing above it is.
        # ------------------------------------------------------------------
        if not is_whatsapp_shared_enabled():
            # Per row rather than per batch: a batch is up to `limit` rows with
            # a blocking call in each, so a switch thrown during the first
            # delivery has to stop the rest. I3 — a hold spends no attempt.
            self._hold(row, now)
            return "held"
        if key is None:
            # The wait C5c ends. I3 — a genuine attempt, so it spends one.
            return self._defer(row, now, "relay_key_absent")
        try:
            if self.router.runtime_deliver:
                # Re-signed under the household's current key, over the same
                # body, with this row's stamp: the runtime enforces the replay
                # window C5a gave it, so the original hour-old signature would
                # be refused by the very freshness check that makes the scheme
                # safe. The BODY is unchanged and now proven authentic, so this
                # is the same message and not a new one — the runtime's ingest
                # dedupes on the relay provenance it carries, which is why this
                # slice adds no second idempotency mechanism beside it.
                self.router.runtime_deliver(
                    routed.household_id,
                    row["payload"],
                    stamp,
                    relay_hmac(key, row["payload"], stamp),
                )
        except Exception:
            # I2: measured from AFTER the attempt. A delivery that blocked for
            # a minute before failing has already spent that minute, and
            # backing off from before it would retry immediately.
            return self._defer(row, self.router.now_fn(), "runtime_unavailable")
        self.store.mark_delivered(ingress_id)
        return "delivered"

    def _hold(self, row, now: float) -> None:
        """Leave a row as it is, for a run when the switch is back on.

        I3: not a drop and not a failure, so it spends no attempt.
        """
        self.store.defer(
            row["id"],
            error="flag_disabled",
            not_before=now + self.BASE_BACKOFF_SECONDS,
            spend_attempt=False,
        )

    @staticmethod
    def _has_provenance(row) -> bool:
        """Whether the row can say what it arrived as, and for whom."""
        return all(
            row[column] is not None
            for column in ("channel", "household_id", "origin_timestamp", "origin_signature")
        )

    def _defer(self, row, now: float, error: str) -> str:
        """Spend an attempt: keep the row, or drop it once attempts run out.

        Returns the outcome name, so the caller reports what happened rather
        than deciding it a second time.
        """
        attempts = int(row["attempts"]) + 1
        if attempts >= self.MAX_ATTEMPTS:
            self.store.mark_delivered(row["id"])
            return "exhausted"
        self.store.defer(
            row["id"],
            error=error,
            not_before=now + self.BASE_BACKOFF_SECONDS * (2 ** (attempts - 1)),
        )
        return "deferred"

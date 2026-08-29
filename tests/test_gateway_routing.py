"""Gateway narrow multi-tenant routing + durable before ACK + HMAC."""

import contextlib
import sqlite3
from pathlib import Path

from control_plane.db import open_control_plane_database
from gateway.whatsapp_router import (
    GatewayRedeliverWorker,
    GatewayResult,
    GatewayStore,
    WhatsAppGatewayRouter,
    relay_hmac,
    sender_hmac,
    verify_relay_hmac,
)


def _ingress_count(path: Path) -> int:
    """How many rows the WAL is holding.

    Read from the file rather than through `GatewayStore`, so a test asserting
    what the WAL kept cannot be satisfied by the store's own bookkeeping.
    """
    connection = sqlite3.connect(path)
    try:
        return int(
            connection.execute("SELECT COUNT(*) FROM gateway_ingress").fetchone()[0]
        )
    finally:
        connection.close()


def test_unknown_and_ambiguous_senders_denied(tmp_path: Path) -> None:
    db = open_control_plane_database(tmp_path / "cp.db")
    # synthetic household IDs and synthetic phone in allowed range (+999 / 555-01xx)
    hid = "00000000-0000-4000-a000-000000000001"
    hid2 = "00000000-0000-4000-a000-000000000002"
    phone = "+999511234567"  # synthetic fixture phone
    db.connection.execute(
        "INSERT INTO households (id, slug, status, created_at, updated_at) VALUES (?, ?, 'draft', 1, 1)",
        (hid, "hh1"),
    )
    db.connection.execute(
        "INSERT INTO households (id, slug, status, created_at, updated_at) VALUES (?, ?, 'draft', 1, 1)",
        (hid2, "hh2"),
    )
    db.connection.commit()
    db.connection.execute(
        "INSERT INTO channel_bindings (id, household_id, channel, external_id, chat_id, actor_id, role, verified_at, verified_by_actor_id) "
        "VALUES (?, ?, 'whatsapp', ?, ?, 'owner1', 'owner', 1, 'owner1')",
        ("b1", hid, phone, phone),
    )
    db.connection.commit()
    router = WhatsAppGatewayRouter(db, ingress_path=tmp_path / "ingress.db")
    assert router.route("unknown") == GatewayResult(status="denied", code="unknown_sender", household_id=None)
    # add second household with same sender -> ambiguous
    db.connection.execute(
        "INSERT INTO channel_bindings (id, household_id, channel, external_id, chat_id, actor_id, role, verified_at, verified_by_actor_id) "
        "VALUES (?, ?, 'whatsapp', ?, ?, 'owner2', 'owner', 1, 'owner2')",
        ("b2", hid2, phone, phone),
    )
    db.connection.commit()
    assert router.route(phone).code == "ambiguous_sender"
    # exact single mapping -> delivered
    db.connection.execute("DELETE FROM channel_bindings WHERE id='b2'")
    db.connection.commit()
    assert router.route(phone).household_id == hid


def test_per_household_relay_hmac_and_durable_before_ack(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ABROLIA_WHATSAPP_SHARED_ENABLED", "1")
    db = open_control_plane_database(tmp_path / "cp.db")
    hid = "00000000-0000-4000-a000-000000000011"
    phone = "+999511234567"
    gateway_key = b"synthetic-gateway-hmac-key-1234567890abcdef"
    phone_hmac = sender_hmac(phone, gateway_key)
    db.connection.execute(
        "INSERT INTO households (id, slug, status, created_at, updated_at) VALUES (?, ?, 'draft', 1, 1)",
        (hid, "hh1"),
    )
    db.connection.execute(
        "INSERT INTO channel_bindings (id, household_id, channel, external_id, chat_id, external_id_hmac, actor_id, role, verified_at, verified_by_actor_id) "
        "VALUES (?, ?, 'whatsapp', ?, ?, ?, 'owner1', 'owner', 1, 'owner1')",
        ("b1", hid, phone, phone, phone_hmac),
    )
    db.connection.commit()
    key = b"household-relay-key-1234567890abcdef"
    router = WhatsAppGatewayRouter(
        db, relay_keys={hid: key}, gateway_hmac_key=gateway_key, ingress_path=tmp_path / "ingress.db"
    )
    body = b'{"message":"hello"}'
    ts = str(int(router.now_fn()))
    sig = relay_hmac(key, body, ts)
    assert verify_relay_hmac(key, body, ts, sig) is True
    assert verify_relay_hmac(key, body, ts, "sha256=" + sig) is True
    assert verify_relay_hmac(key, b"other", ts, sig) is False
    # durable ingress — only delivered after runtime confirm, WAL deleted only then
    delivered: list[tuple[str, bytes, str, str]] = []

    def fake_deliver(household_id, payload, timestamp, signature):
        delivered.append((household_id, payload, timestamp, signature))

    router2 = WhatsAppGatewayRouter(
        db,
        relay_keys={hid: key},
        gateway_hmac_key=gateway_key,
        ingress_path=tmp_path / "ingress2.db",
        runtime_deliver=fake_deliver,
    )
    ts2 = str(int(router2.now_fn()))
    sig2 = relay_hmac(key, body, ts2)
    result = router2.handle_webhook(body, phone, timestamp=ts2, signature=sig2)
    assert result.status == "delivered"
    assert delivered, "runtime_deliver must be called before WAL delete"
    assert delivered[0][2] == ts2 and delivered[0][3] == sig2
    # A present key rejecting these bytes is TERMINAL, and the row goes. This
    # used to answer `hmac_rejected` and keep the row, which is how a payload
    # that can never verify came to be stored indefinitely: the signature
    # cannot become valid under a key that already exists, so the only thing
    # retrying preserved was somebody's message body.
    result_bad = router2.handle_webhook(body, phone, timestamp=ts2, signature="bad")
    assert result_bad.code == "hmac_rejected"
    assert _ingress_count(tmp_path / "ingress2.db") == 0, "a dead payload was kept"
    # Missing signature — fail-closed, and refused before anything is persisted.
    result_nosig = router2.handle_webhook(body, phone, timestamp=ts2, signature="")  # type: ignore[arg-type]
    assert result_nosig.code == "hmac_rejected"
    # Missing key — not delivered, WAL kept for the reconcile that now exists.
    # `relay_key_absent` rather than `hmac_rejected`: nothing is wrong with the
    # signature, the key has not been provisioned yet (C5c), and the two need
    # different answers because they need different fates for the row.
    router3 = WhatsAppGatewayRouter(
        db, relay_keys={}, gateway_hmac_key=gateway_key, ingress_path=tmp_path / "ingress3.db"
    )
    ts3 = str(int(router3.now_fn()))
    sig3 = relay_hmac(key, body, ts3)
    result3 = router3.handle_webhook(body, phone, timestamp=ts3, signature=sig3)
    assert result3.code == "relay_key_absent"
    assert _ingress_count(tmp_path / "ingress3.db") == 1, "the WAL kept nothing to reconcile"


def test_routing_is_by_sender_so_two_members_in_one_chat_still_resolve(
    tmp_path: Path,
) -> None:
    """C3a: `chat_id` may repeat; `external_id` may not, and only it is matched.

    This is the consumer the split was FOR. `gateway/whatsapp_router.py` was
    never wrong — it always meant `external_id` as the sender — and after C3a
    that is the only thing the column means. Two members of one household
    sharing one conversation therefore have two distinct rows the gateway can
    tell apart, and each resolves to their household rather than to
    `ambiguous_sender`.

    The cross-household rule is the half that must NOT relax: an ID held by one
    household is refused to every other, because two matching rows break
    delivery for both, including the household that was there first.
    """
    db = open_control_plane_database(tmp_path / "cp.db")
    hid = "00000000-0000-4000-a000-000000000021"
    other = "00000000-0000-4000-a000-000000000022"
    chat = "-100990000101"
    owner_sender = "990000001"
    adult_sender = "990000002"
    for household, slug in ((hid, "hh1"), (other, "hh2")):
        db.connection.execute(
            "INSERT INTO households (id, slug, status, created_at, updated_at)"
            " VALUES (?, ?, 'draft', 1, 1)",
            (household, slug),
        )
    for row_id, sender, actor, role in (
        ("b1", owner_sender, "owner1", "owner"),
        ("b2", adult_sender, "adult1", "adult"),
    ):
        db.connection.execute(
            "INSERT INTO channel_bindings (id, household_id, channel, external_id,"
            " chat_id, actor_id, role, verified_at, verified_by_actor_id)"
            " VALUES (?, ?, 'telegram', ?, ?, ?, ?, 1, 'owner1')",
            (row_id, hid, sender, chat, actor, role),
        )
    db.connection.commit()

    router = WhatsAppGatewayRouter(db, ingress_path=tmp_path / "ingress.db")
    assert router.route(owner_sender, "telegram").household_id == hid
    assert router.route(adult_sender, "telegram").household_id == hid
    # The shared conversation is not a sender and resolves to nobody.
    assert router.route(chat, "telegram").code == "unknown_sender"

    # The same sender in a second household is still ambiguous, which is why
    # binding one another household holds is refused at bind time.
    db.connection.execute(
        "INSERT INTO channel_bindings (id, household_id, channel, external_id,"
        " chat_id, actor_id, role, verified_at, verified_by_actor_id)"
        " VALUES ('b3', ?, 'telegram', ?, ?, 'owner2', 'owner', 1, 'owner2')",
        (other, adult_sender, "-100990000202"),
    )
    db.connection.commit()
    assert router.route(adult_sender, "telegram").code == "ambiguous_sender"


def test_a_binding_without_a_chat_cannot_be_written(tmp_path: Path) -> None:
    """The NOT NULL that SQLite would not let 0010 declare.

    `ALTER TABLE ADD COLUMN` cannot add a NOT NULL column to a populated table
    without inventing a default, and an empty-string default is a lie that
    reads as data. The requirement is a trigger instead — the same treatment
    `email_identities.domain_lookup_hmac` gets in 0003 — because a NULL
    `chat_id` reaches the manifest as a binding that speaks nowhere.
    """
    import sqlite3

    import pytest

    db = open_control_plane_database(tmp_path / "cp.db")
    hid = "00000000-0000-4000-a000-000000000031"
    db.connection.execute(
        "INSERT INTO households (id, slug, status, created_at, updated_at)"
        " VALUES (?, 'hh1', 'draft', 1, 1)",
        (hid,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="chat_id is required"):
        db.connection.execute(
            "INSERT INTO channel_bindings (id, household_id, channel, external_id,"
            " actor_id, role, verified_at, verified_by_actor_id)"
            " VALUES ('b1', ?, 'telegram', '990000001', 'owner1', 'owner', 1, 'owner1')",
            (hid,),
        )


def _bound_household(tmp_path: Path, *, gateway_key: bytes):
    """One household with one verified WhatsApp binding, and its identifiers."""
    db = open_control_plane_database(tmp_path / "cp.db")
    hid = "00000000-0000-4000-a000-0000000000c5"
    phone = "+999511234599"
    db.connection.execute(
        "INSERT INTO households (id, slug, status, created_at, updated_at)"
        " VALUES (?, ?, 'draft', 1, 1)",
        (hid, "hh-c5b"),
    )
    db.connection.execute(
        "INSERT INTO channel_bindings (id, household_id, channel, external_id,"
        " chat_id, external_id_hmac, actor_id, role, verified_at,"
        " verified_by_actor_id) VALUES (?, ?, 'whatsapp', ?, ?, ?, 'owner1',"
        " 'owner', 1, 'owner1')",
        ("b-c5b", hid, phone, phone, sender_hmac(phone, gateway_key)),
    )
    db.connection.commit()
    return db, hid, phone


def test_a_delivery_failure_is_redelivered_on_the_next_run(
    tmp_path: Path, monkeypatch
) -> None:
    """The WAL is read back, which is the whole of C5b.

    `persist_before_ack` and `mark_delivered` have always been here. Between
    them there was nothing, so a row a failed delivery left behind stayed until
    the file was deleted — durability that was written and never spent.
    """
    monkeypatch.setenv("ABROLIA_WHATSAPP_SHARED_ENABLED", "1")
    gateway_key = b"synthetic-gateway-hmac-key-1234567890abcdef"
    db, hid, phone = _bound_household(tmp_path, gateway_key=gateway_key)
    key = b"household-relay-key-1234567890abcdef"

    delivered: list[tuple[str, bytes]] = []
    runtime_up = False

    def deliver(household_id, payload, timestamp, signature):
        if not runtime_up:
            raise RuntimeError("runtime refused the delivery")
        assert verify_relay_hmac(key, payload, timestamp, signature), (
            "a redelivery must be signed with the scheme C5a settled on"
        )
        delivered.append((household_id, payload))

    clock = [1_000_000.0]
    router = WhatsAppGatewayRouter(
        db,
        relay_keys={hid: key},
        gateway_hmac_key=gateway_key,
        ingress_path=tmp_path / "ingress.db",
        runtime_deliver=deliver,
        now_fn=lambda: clock[0],
    )
    body = b'{"message":"the one that failed"}'
    ts = str(int(clock[0]))
    result = router.handle_webhook(
        body, phone, timestamp=ts, signature=relay_hmac(key, body, ts)
    )
    # Named for what happened. It is not an HMAC problem and reporting it as
    # one is what made the four outcomes indistinguishable.
    assert result.code == "runtime_unavailable"
    assert _ingress_count(tmp_path / "ingress.db") == 1
    assert not delivered

    worker = GatewayRedeliverWorker(router)
    # Still down, and not yet due: backoff is the point.
    assert worker.run_once().public_dict()["delivered"] == 0

    runtime_up = True
    clock[0] += GatewayRedeliverWorker.BASE_BACKOFF_SECONDS + 1
    report = worker.run_once()
    assert report.public_dict()["delivered"] == 1
    assert [payload for _, payload in delivered] == [body], "the same body, not a new one"
    assert _ingress_count(tmp_path / "ingress.db") == 0, "a delivered row was kept"


def test_a_row_kept_for_a_missing_key_is_delivered_once_the_key_arrives(
    tmp_path: Path, monkeypatch
) -> None:
    """The C5c seam, tested with the key simply appearing.

    `relay_key_absent` is retryable precisely because the relay-key
    provisioning path does not exist yet. This does not create a key; it proves
    that the row survives the wait and is delivered when one shows up.
    """
    monkeypatch.setenv("ABROLIA_WHATSAPP_SHARED_ENABLED", "1")
    gateway_key = b"synthetic-gateway-hmac-key-1234567890abcdef"
    db, hid, phone = _bound_household(tmp_path, gateway_key=gateway_key)
    key = b"household-relay-key-1234567890abcdef"

    delivered: list[bytes] = []
    clock = [1_000_000.0]
    router = WhatsAppGatewayRouter(
        db,
        relay_keys={},  # nothing provisioned yet
        gateway_hmac_key=gateway_key,
        ingress_path=tmp_path / "ingress.db",
        runtime_deliver=lambda h, p, t, s: delivered.append(p),
        now_fn=lambda: clock[0],
    )
    body = b'{"message":"waiting for a key"}'
    ts = str(int(clock[0]))
    assert router.handle_webhook(
        body, phone, timestamp=ts, signature=relay_hmac(key, body, ts)
    ).code == "relay_key_absent"
    assert _ingress_count(tmp_path / "ingress.db") == 1

    worker = GatewayRedeliverWorker(router)
    clock[0] += GatewayRedeliverWorker.BASE_BACKOFF_SECONDS + 1
    assert worker.run_once().public_dict()["deferred"] == 1, "still no key, still kept"

    router.relay_keys[hid] = key  # C5c, standing in for itself
    clock[0] += GatewayRedeliverWorker.BASE_BACKOFF_SECONDS * 4
    assert worker.run_once().public_dict()["delivered"] == 1
    assert delivered == [body]
    assert _ingress_count(tmp_path / "ingress.db") == 0


def test_a_row_is_dropped_and_counted_once_it_can_no_longer_be_repaired(
    tmp_path: Path, monkeypatch
) -> None:
    """Bounded in both directions, because the WAL holds message bodies.

    A gateway WAL is not a dead-letter store. The alternative to dropping is
    keeping a family's messages indefinitely for an operator with no interface
    to read them, so what is retained is the COUNT and not the payload.
    """
    monkeypatch.setenv("ABROLIA_WHATSAPP_SHARED_ENABLED", "1")
    gateway_key = b"synthetic-gateway-hmac-key-1234567890abcdef"
    db, hid, phone = _bound_household(tmp_path, gateway_key=gateway_key)
    key = b"household-relay-key-1234567890abcdef"

    clock = [1_000_000.0]

    def always_fails(household_id, payload, timestamp, signature):
        raise RuntimeError("runtime is never coming back")

    router = WhatsAppGatewayRouter(
        db,
        relay_keys={hid: key},
        gateway_hmac_key=gateway_key,
        ingress_path=tmp_path / "ingress.db",
        runtime_deliver=always_fails,
        now_fn=lambda: clock[0],
    )
    body = b'{"message":"never lands"}'
    ts = str(int(clock[0]))
    router.handle_webhook(body, phone, timestamp=ts, signature=relay_hmac(key, body, ts))

    worker = GatewayRedeliverWorker(router)
    backoffs = []
    for _ in range(GatewayRedeliverWorker.MAX_ATTEMPTS + 2):
        clock[0] += GatewayRedeliverWorker.MAX_AGE_SECONDS / 100
        before = _ingress_count(tmp_path / "ingress.db")
        report = worker.run_once()
        if before and not _ingress_count(tmp_path / "ingress.db"):
            assert report.public_dict()["dropped_exhausted"] == 1
            break
        backoffs.append(report.public_dict()["deferred"])
    else:  # pragma: no cover - the loop must reach the drop
        raise AssertionError("the row was retried past MAX_ATTEMPTS")
    assert _ingress_count(tmp_path / "ingress.db") == 0

    # And the other bound: a row still young in attempts but too old to matter.
    fresh = tmp_path / "aged.db"
    store = GatewayStore(fresh)
    store.persist_before_ack(b'{"message":"too late to help"}', phone, now=clock[0])
    aged = WhatsAppGatewayRouter(
        db,
        relay_keys={hid: key},
        gateway_hmac_key=gateway_key,
        ingress_path=fresh,
        runtime_deliver=lambda *a: None,
        now_fn=lambda: clock[0] + GatewayRedeliverWorker.MAX_AGE_SECONDS * 2,
    )
    report = GatewayRedeliverWorker(aged).run_once()
    assert report.public_dict()["dropped_expired"] == 1
    assert _ingress_count(fresh) == 0


def test_a_redelivery_is_re_routed_so_a_revoked_sender_does_not_land(
    tmp_path: Path, monkeypatch
) -> None:
    """The stored sender's old answer is not trusted.

    A row waits precisely across the window in which a binding can change. If
    the sender has been revoked while its message sat in the WAL, delivering it
    to the household that used to hold them is the revocation failing to take
    effect on a message in flight.
    """
    monkeypatch.setenv("ABROLIA_WHATSAPP_SHARED_ENABLED", "1")
    gateway_key = b"synthetic-gateway-hmac-key-1234567890abcdef"
    db, hid, phone = _bound_household(tmp_path, gateway_key=gateway_key)
    key = b"household-relay-key-1234567890abcdef"

    delivered: list[bytes] = []
    clock = [1_000_000.0]
    failing = True

    def deliver(household_id, payload, timestamp, signature):
        if failing:
            raise RuntimeError("not yet")
        delivered.append(payload)

    router = WhatsAppGatewayRouter(
        db,
        relay_keys={hid: key},
        gateway_hmac_key=gateway_key,
        ingress_path=tmp_path / "ingress.db",
        runtime_deliver=deliver,
        now_fn=lambda: clock[0],
    )
    body = b'{"message":"sent before the revocation"}'
    ts = str(int(clock[0]))
    router.handle_webhook(body, phone, timestamp=ts, signature=relay_hmac(key, body, ts))
    assert _ingress_count(tmp_path / "ingress.db") == 1

    # The binding is revoked while the row waits.
    db.connection.execute("DELETE FROM channel_bindings WHERE id = 'b-c5b'")
    db.connection.commit()

    failing = False
    clock[0] += GatewayRedeliverWorker.BASE_BACKOFF_SECONDS + 1
    report = GatewayRedeliverWorker(router).run_once()
    assert report.public_dict()["dropped_undeliverable"] == 1
    assert delivered == [], "a revoked sender's message was delivered anyway"
    assert _ingress_count(tmp_path / "ingress.db") == 0


def test_a_rebound_sender_does_not_carry_the_previous_households_message(
    tmp_path: Path, monkeypatch
) -> None:
    """Re-routing decides WHETHER to deliver, never to whom.

    A row waits precisely across the window in which a binding can change, and
    a sender can move between households as well as disappear. Re-deriving the
    household from the sender at redelivery time meant that a sender rebound
    from A to B carried A's queued message into B's runtime — signed with B's
    key, so B's runtime would accept it — which is one household's content
    delivered to another.

    The row remembers the household it was accepted for, and the re-route has
    to agree with it.
    """
    monkeypatch.setenv("ABROLIA_WHATSAPP_SHARED_ENABLED", "1")
    gateway_key = b"synthetic-gateway-hmac-key-1234567890abcdef"
    db, hid, phone = _bound_household(tmp_path, gateway_key=gateway_key)
    other = "00000000-0000-4000-a000-0000000000b2"
    db.connection.execute(
        "INSERT INTO households (id, slug, status, created_at, updated_at)"
        " VALUES (?, ?, 'draft', 1, 1)",
        (other, "hh-other"),
    )
    db.connection.commit()

    key_a = b"household-a-relay-key-1234567890abcd"
    key_b = b"household-b-relay-key-1234567890abcd"
    delivered: list[tuple[str, bytes]] = []
    failing = True

    def deliver(household_id, payload, timestamp, signature):
        if failing:
            raise RuntimeError("not yet")
        delivered.append((household_id, payload))

    clock = [1_000_000.0]
    router = WhatsAppGatewayRouter(
        db,
        relay_keys={hid: key_a, other: key_b},
        gateway_hmac_key=gateway_key,
        ingress_path=tmp_path / "ingress.db",
        runtime_deliver=deliver,
        now_fn=lambda: clock[0],
    )
    body = b'{"message":"for household A only"}'
    ts = str(int(clock[0]))
    router.handle_webhook(body, phone, timestamp=ts, signature=relay_hmac(key_a, body, ts))
    assert _ingress_count(tmp_path / "ingress.db") == 1

    # The sender is rebound to the other household while the row waits.
    db.connection.execute(
        "UPDATE channel_bindings SET household_id = ? WHERE id = 'b-c5b'", (other,)
    )
    db.connection.commit()

    failing = False
    clock[0] += GatewayRedeliverWorker.BASE_BACKOFF_SECONDS + 1
    report = GatewayRedeliverWorker(router).run_once()
    assert delivered == [], "one household's message was delivered to another"
    assert report.public_dict()["dropped_undeliverable"] == 1
    assert _ingress_count(tmp_path / "ingress.db") == 0


def test_a_payload_retained_without_a_key_is_proven_before_it_is_signed(
    tmp_path: Path, monkeypatch
) -> None:
    """The worker must not bless what nobody ever authenticated.

    `relay_key_absent` retains a row WITHOUT verifying it, because there is no
    key to verify with. If redelivery then signs that body with the real key
    the moment C5c installs one, anyone who could reach the gateway for a known
    sender during the wait would have had a forged payload laundered into the
    runtime's trusted ingest — the gateway's signature is exactly the runtime's
    reason to trust it.

    So the row keeps what it arrived with, and the worker checks that against
    the key before signing anything.
    """
    monkeypatch.setenv("ABROLIA_WHATSAPP_SHARED_ENABLED", "1")
    gateway_key = b"synthetic-gateway-hmac-key-1234567890abcdef"
    db, hid, phone = _bound_household(tmp_path, gateway_key=gateway_key)
    key = b"household-relay-key-1234567890abcdef"

    delivered: list[bytes] = []
    clock = [1_000_000.0]
    router = WhatsAppGatewayRouter(
        db,
        relay_keys={},  # C5c has not run
        gateway_hmac_key=gateway_key,
        ingress_path=tmp_path / "ingress.db",
        runtime_deliver=lambda h, p, t, s: delivered.append(p),
        now_fn=lambda: clock[0],
    )
    forged = b'{"message":"never signed by the relay"}'
    ts = str(int(clock[0]))
    assert router.handle_webhook(
        forged, phone, timestamp=ts, signature="sha256=" + "0" * 64
    ).code == "relay_key_absent"
    assert _ingress_count(tmp_path / "ingress.db") == 1

    router.relay_keys[hid] = key  # the key arrives
    clock[0] += GatewayRedeliverWorker.BASE_BACKOFF_SECONDS + 1
    report = GatewayRedeliverWorker(router).run_once()
    assert delivered == [], "a payload nobody authenticated was signed and delivered"
    assert report.public_dict()["dropped_unverifiable"] == 1
    assert _ingress_count(tmp_path / "ingress.db") == 0


def test_a_row_that_cannot_say_what_it_arrived_as_is_dropped(
    tmp_path: Path, monkeypatch
) -> None:
    """Legacy rows are dropped, not blessed.

    The implementation before this one retained rows after a REJECTED HMAC, and
    any version predating these columns wrote rows with no provenance at all.
    Such a row cannot be authenticated, so the one thing that must not happen is
    signing it with the real key.
    """
    monkeypatch.setenv("ABROLIA_WHATSAPP_SHARED_ENABLED", "1")
    gateway_key = b"synthetic-gateway-hmac-key-1234567890abcdef"
    db, hid, phone = _bound_household(tmp_path, gateway_key=gateway_key)
    key = b"household-relay-key-1234567890abcdef"

    path = tmp_path / "legacy.db"
    store = GatewayStore(path)
    legacy = store.persist_before_ack(
        b'{"message":"from an older gateway"}', phone, now=1_000_000.0
    )
    # Exactly the shape the previous schema left behind: no provenance columns,
    # and `next_attempt_at` at the ALTER's default rather than a real stamp.
    store.conn.execute(
        "UPDATE gateway_ingress SET channel = NULL, household_id = NULL,"
        " origin_timestamp = NULL, origin_signature = NULL, next_attempt_at = 0"
        " WHERE id = ?",
        (legacy,),
    )
    store.conn.commit()

    delivered: list[bytes] = []
    router = WhatsAppGatewayRouter(
        db,
        relay_keys={hid: key},
        gateway_hmac_key=gateway_key,
        ingress_path=path,
        runtime_deliver=lambda h, p, t, s: delivered.append(p),
        now_fn=lambda: 1_000_000.0,
    )
    report = GatewayRedeliverWorker(router).run_once()
    assert delivered == []
    assert report.public_dict()["dropped_unverifiable"] == 1
    assert _ingress_count(path) == 0


def test_the_kill_switch_stops_the_worker_and_the_backlog_survives_it(
    tmp_path: Path, monkeypatch
) -> None:
    """A brake that only stops new webhooks is not a brake.

    `handle_webhook` reads the switch at call time so it can be thrown during
    an incident. The worker did not, so an operator turning the switch off
    stopped new traffic while the worker went on draining the backlog into the
    runtime — the one thing the switch exists to prevent.

    Held, not dropped, and without spending an attempt: the switch being off is
    an operator decision, not the row's failure, and burning `MAX_ATTEMPTS`
    while it is off would mean the brake destroyed the work it was pulled to
    protect.
    """
    monkeypatch.setenv("ABROLIA_WHATSAPP_SHARED_ENABLED", "1")
    gateway_key = b"synthetic-gateway-hmac-key-1234567890abcdef"
    db, hid, phone = _bound_household(tmp_path, gateway_key=gateway_key)
    key = b"household-relay-key-1234567890abcdef"

    delivered: list[bytes] = []
    failing = True

    def deliver(household_id, payload, timestamp, signature):
        if failing:
            raise RuntimeError("not yet")
        delivered.append(payload)

    clock = [1_000_000.0]
    router = WhatsAppGatewayRouter(
        db,
        relay_keys={hid: key},
        gateway_hmac_key=gateway_key,
        ingress_path=tmp_path / "ingress.db",
        runtime_deliver=deliver,
        now_fn=lambda: clock[0],
    )
    body = b'{"message":"queued while the switch was on"}'
    ts = str(int(clock[0]))
    router.handle_webhook(body, phone, timestamp=ts, signature=relay_hmac(key, body, ts))
    assert _ingress_count(tmp_path / "ingress.db") == 1

    worker = GatewayRedeliverWorker(router)
    failing = False
    monkeypatch.setenv("ABROLIA_WHATSAPP_SHARED_ENABLED", "0")
    clock[0] += GatewayRedeliverWorker.BASE_BACKOFF_SECONDS + 1
    report = worker.run_once()
    assert delivered == [], "the brake did not stop already-queued work"
    assert report.public_dict()["held"] == 1
    assert _ingress_count(tmp_path / "ingress.db") == 1, "the brake dropped the backlog"

    monkeypatch.setenv("ABROLIA_WHATSAPP_SHARED_ENABLED", "1")
    clock[0] += GatewayRedeliverWorker.BASE_BACKOFF_SECONDS + 1
    assert worker.run_once().public_dict()["delivered"] == 1
    assert delivered == [body]


def test_the_worker_reads_the_same_wal_the_router_writes_by_default(
    tmp_path: Path, monkeypatch
) -> None:
    """One store, including on the configuration that is actually deployed.

    `router.store` was None whenever no `ingress_path` was given, while
    `handle_webhook` opened a throwaway store on the default path to write to.
    Two stores for one WAL: the router persisted retryable failures and the
    worker, built the normal way from that router, raised because the store was
    None — so on the default configuration nothing was ever retried.
    """
    monkeypatch.setenv("ABROLIA_WHATSAPP_SHARED_ENABLED", "1")
    monkeypatch.chdir(tmp_path)
    gateway_key = b"synthetic-gateway-hmac-key-1234567890abcdef"
    db, hid, phone = _bound_household(tmp_path, gateway_key=gateway_key)
    key = b"household-relay-key-1234567890abcdef"

    delivered: list[bytes] = []
    failing = True

    def deliver(household_id, payload, timestamp, signature):
        if failing:
            raise RuntimeError("not yet")
        delivered.append(payload)

    clock = [1_000_000.0]
    router = WhatsAppGatewayRouter(  # NO ingress_path — the deployed shape
        db,
        relay_keys={hid: key},
        gateway_hmac_key=gateway_key,
        runtime_deliver=deliver,
        now_fn=lambda: clock[0],
    )
    body = b'{"message":"default WAL"}'
    ts = str(int(clock[0]))
    assert router.handle_webhook(
        body, phone, timestamp=ts, signature=relay_hmac(key, body, ts)
    ).code == "runtime_unavailable"

    worker = GatewayRedeliverWorker(router)  # used to raise ValueError
    failing = False
    clock[0] += GatewayRedeliverWorker.BASE_BACKOFF_SECONDS + 1
    assert worker.run_once().public_dict()["delivered"] == 1
    assert delivered == [body]


def test_a_retained_telegram_row_is_rerouted_on_the_channel_it_arrived_on(
    tmp_path: Path, monkeypatch
) -> None:
    """The channel is part of what the row arrived as, not a default.

    `handle_webhook` takes a `channel`, and the row did not record it, so every
    redelivery re-routed as whatsapp. A retained telegram message therefore
    resolved against the wrong binding set, found nobody, and was dropped as
    undeliverable — a message lost by the mechanism built to save it.
    """
    monkeypatch.setenv("ABROLIA_WHATSAPP_SHARED_ENABLED", "1")
    gateway_key = b"synthetic-gateway-hmac-key-1234567890abcdef"
    db = open_control_plane_database(tmp_path / "cp.db")
    hid = "00000000-0000-4000-a000-0000000000t1"[:36]
    sender = "990000777"
    db.connection.execute(
        "INSERT INTO households (id, slug, status, created_at, updated_at)"
        " VALUES (?, ?, 'draft', 1, 1)",
        (hid, "hh-tg"),
    )
    db.connection.execute(
        "INSERT INTO channel_bindings (id, household_id, channel, external_id,"
        " chat_id, external_id_hmac, actor_id, role, verified_at,"
        " verified_by_actor_id) VALUES (?, ?, 'telegram', ?, ?, ?, 'owner1',"
        " 'owner', 1, 'owner1')",
        ("b-tg", hid, sender, "-100990000777", sender_hmac(sender, gateway_key)),
    )
    db.connection.commit()

    key = b"household-relay-key-1234567890abcdef"
    delivered: list[bytes] = []
    failing = True

    def deliver(household_id, payload, timestamp, signature):
        if failing:
            raise RuntimeError("not yet")
        delivered.append(payload)

    clock = [1_000_000.0]
    router = WhatsAppGatewayRouter(
        db,
        relay_keys={hid: key},
        gateway_hmac_key=gateway_key,
        ingress_path=tmp_path / "ingress.db",
        runtime_deliver=deliver,
        now_fn=lambda: clock[0],
    )
    body = b'{"message":"sent on telegram"}'
    ts = str(int(clock[0]))
    assert router.handle_webhook(
        body,
        sender,
        channel="telegram",
        timestamp=ts,
        signature=relay_hmac(key, body, ts),
    ).code == "runtime_unavailable"

    failing = False
    clock[0] += GatewayRedeliverWorker.BASE_BACKOFF_SECONDS + 1
    report = GatewayRedeliverWorker(router).run_once()
    assert report.public_dict()["delivered"] == 1, (
        "a telegram row was re-routed as whatsapp and lost"
    )
    assert delivered == [body]


def test_a_row_is_never_on_disk_without_the_household_it_was_accepted_for(
    tmp_path: Path, monkeypatch
) -> None:
    """The WAL must not lose a message on the crash it exists to survive.

    Recording the household in a later `defer` left a window — from the insert
    until that update — in which a row on disk had no household. A process
    killed in it, which is most likely while `runtime_deliver` is in flight,
    restarted into a row `_has_provenance` reads as unverifiable and deletes.

    Simulated by killing the process where it hurts: `runtime_deliver` raises
    `KeyboardInterrupt`, which no `except Exception` catches, so nothing after
    it runs — exactly what a SIGKILL leaves behind.
    """
    monkeypatch.setenv("ABROLIA_WHATSAPP_SHARED_ENABLED", "1")
    gateway_key = b"synthetic-gateway-hmac-key-1234567890abcdef"
    db, hid, phone = _bound_household(tmp_path, gateway_key=gateway_key)
    key = b"household-relay-key-1234567890abcdef"

    clock = [1_000_000.0]

    def killed(household_id, payload, timestamp, signature):
        raise KeyboardInterrupt("SIGKILL mid-delivery")

    router = WhatsAppGatewayRouter(
        db,
        relay_keys={hid: key},
        gateway_hmac_key=gateway_key,
        ingress_path=tmp_path / "ingress.db",
        runtime_deliver=killed,
        now_fn=lambda: clock[0],
    )
    body = b'{"message":"in flight when the process died"}'
    ts = str(int(clock[0]))
    with contextlib.suppress(KeyboardInterrupt):
        router.handle_webhook(body, phone, timestamp=ts, signature=relay_hmac(key, body, ts))

    row = sqlite3.connect(tmp_path / "ingress.db").execute(
        "SELECT household_id, channel, origin_timestamp, origin_signature"
        " FROM gateway_ingress"
    ).fetchone()
    assert row is not None, "the WAL lost the message it had already committed"
    assert row[0] == hid, "a committed row had no household to be redelivered to"

    # And the restarted worker delivers it rather than dropping it.
    delivered: list[bytes] = []
    restarted = WhatsAppGatewayRouter(
        db,
        relay_keys={hid: key},
        gateway_hmac_key=gateway_key,
        ingress_path=tmp_path / "ingress.db",
        runtime_deliver=lambda h, p, t, s: delivered.append(p),
        now_fn=lambda: clock[0] + GatewayRedeliverWorker.BASE_BACKOFF_SECONDS + 1,
    )
    report = GatewayRedeliverWorker(restarted).run_once()
    assert report.public_dict()["dropped_unverifiable"] == 0
    assert report.public_dict()["delivered"] == 1
    assert delivered == [body]


def test_the_brake_stops_the_rest_of_the_batch_not_just_the_next_one(
    tmp_path: Path, monkeypatch
) -> None:
    """A batch is up to `limit` rows, so once per batch is not often enough.

    Checking the switch only before selecting the batch meant a switch thrown
    while the first `runtime_deliver` was in flight still sent every remaining
    row — up to 99 more messages during the incident the switch was pulled for.
    It is read immediately before each delivery, for the same reason
    `handle_webhook` reads it at call time: the switch exists to be thrown
    DURING the work.
    """
    monkeypatch.setenv("ABROLIA_WHATSAPP_SHARED_ENABLED", "1")
    gateway_key = b"synthetic-gateway-hmac-key-1234567890abcdef"
    db, hid, phone = _bound_household(tmp_path, gateway_key=gateway_key)
    key = b"household-relay-key-1234567890abcdef"

    clock = [1_000_000.0]
    delivered: list[bytes] = []
    failing = True

    def deliver(household_id, payload, timestamp, signature):
        if failing:
            raise RuntimeError("not yet")
        delivered.append(payload)
        # The operator throws the switch while this first delivery is running.
        monkeypatch.setenv("ABROLIA_WHATSAPP_SHARED_ENABLED", "0")

    router = WhatsAppGatewayRouter(
        db,
        relay_keys={hid: key},
        gateway_hmac_key=gateway_key,
        ingress_path=tmp_path / "ingress.db",
        runtime_deliver=deliver,
        now_fn=lambda: clock[0],
    )
    first = b'{"message":"one"}'
    second = b'{"message":"two"}'
    for body in (first, second):
        ts = str(int(clock[0]))
        router.handle_webhook(
            body, phone, timestamp=ts, signature=relay_hmac(key, body, ts)
        )
        clock[0] += 1
    assert _ingress_count(tmp_path / "ingress.db") == 2

    worker = GatewayRedeliverWorker(router)
    failing = False
    clock[0] += GatewayRedeliverWorker.BASE_BACKOFF_SECONDS + 1
    report = worker.run_once()
    assert delivered == [first], "the brake let the rest of the batch through"
    assert report.public_dict()["held"] == 1
    assert _ingress_count(tmp_path / "ingress.db") == 1, "the held row was dropped"

    # And it goes out once the switch is back on.
    monkeypatch.setenv("ABROLIA_WHATSAPP_SHARED_ENABLED", "1")
    clock[0] += GatewayRedeliverWorker.BASE_BACKOFF_SECONDS + 1
    assert worker.run_once().public_dict()["delivered"] == 1
    assert delivered == [first, second]


def test_the_brake_does_not_spend_the_attempts_of_a_keyless_row(
    tmp_path: Path, monkeypatch
) -> None:
    """The hold has to come before the retryable branch, not just before delivery.

    A `relay_key_absent` row reached `_defer` above the brake check, so every
    run while the switch was off spent one of its attempts. Long enough into an
    incident and `MAX_ATTEMPTS` deleted it — so when C5c finally installed the
    key and traffic was re-enabled, the message the brake was protecting had
    already been destroyed by the brake.
    """
    monkeypatch.setenv("ABROLIA_WHATSAPP_SHARED_ENABLED", "1")
    gateway_key = b"synthetic-gateway-hmac-key-1234567890abcdef"
    db, hid, phone = _bound_household(tmp_path, gateway_key=gateway_key)
    key = b"household-relay-key-1234567890abcdef"

    delivered: list[bytes] = []
    clock = [1_000_000.0]
    router = WhatsAppGatewayRouter(
        db,
        relay_keys={},  # C5c has not run
        gateway_hmac_key=gateway_key,
        ingress_path=tmp_path / "ingress.db",
        runtime_deliver=lambda h, p, t, s: delivered.append(p),
        now_fn=lambda: clock[0],
    )
    body = b'{"message":"queued with no key, during an incident"}'
    ts = str(int(clock[0]))
    assert router.handle_webhook(
        body, phone, timestamp=ts, signature=relay_hmac(key, body, ts)
    ).code == "relay_key_absent"

    def _attempts() -> int:
        return sqlite3.connect(tmp_path / "ingress.db").execute(
            "SELECT attempts FROM gateway_ingress"
        ).fetchone()[0]

    # One attempt was genuinely made at ingress: the key was looked for and was
    # not there. What must not grow is this number, once the switch is off.
    at_ingress = _attempts()
    assert at_ingress == 1

    worker = GatewayRedeliverWorker(router)
    monkeypatch.setenv("ABROLIA_WHATSAPP_SHARED_ENABLED", "0")
    for _ in range(GatewayRedeliverWorker.MAX_ATTEMPTS + 2):
        clock[0] += GatewayRedeliverWorker.BASE_BACKOFF_SECONDS * 2
        report = worker.run_once()
        assert report.public_dict()["dropped_exhausted"] == 0, (
            "the brake exhausted the row it was meant to protect"
        )
    assert _ingress_count(tmp_path / "ingress.db") == 1
    assert _attempts() == at_ingress, "a hold spent an attempt"

    # The incident ends and C5c installs the key.
    router.relay_keys[hid] = key
    monkeypatch.setenv("ABROLIA_WHATSAPP_SHARED_ENABLED", "1")
    clock[0] += GatewayRedeliverWorker.BASE_BACKOFF_SECONDS * 2
    assert worker.run_once().public_dict()["delivered"] == 1
    assert delivered == [body]


def test_a_slow_delivery_does_not_expire_the_rows_behind_it(
    tmp_path: Path, monkeypatch
) -> None:
    """I2: one clock per row, read when that row is processed.

    A batch is up to `limit` rows with a blocking network call in each, so a
    stamp taken at batch start goes stale INSIDE the batch. Once the first
    delivery has blocked longer than `REPLAY_WINDOW_SECONDS`, the next row was
    routed with that stale stamp, answered `timestamp_replay`, and was deleted
    as undeliverable — a queued household message lost because the one before
    it was slow.
    """
    monkeypatch.setenv("ABROLIA_WHATSAPP_SHARED_ENABLED", "1")
    gateway_key = b"synthetic-gateway-hmac-key-1234567890abcdef"
    db, hid, phone = _bound_household(tmp_path, gateway_key=gateway_key)
    key = b"household-relay-key-1234567890abcdef"

    clock = [1_000_000.0]
    delivered: list[bytes] = []
    failing = True

    def deliver(household_id, payload, timestamp, signature):
        if failing:
            raise RuntimeError("not yet")
        # This delivery blocks for longer than the replay window.
        clock[0] += WhatsAppGatewayRouter.REPLAY_WINDOW_SECONDS + 100
        delivered.append(payload)

    router = WhatsAppGatewayRouter(
        db,
        relay_keys={hid: key},
        gateway_hmac_key=gateway_key,
        ingress_path=tmp_path / "ingress.db",
        runtime_deliver=deliver,
        now_fn=lambda: clock[0],
    )
    first, second = b'{"message":"one"}', b'{"message":"two"}'
    for body in (first, second):
        ts = str(int(clock[0]))
        router.handle_webhook(
            body, phone, timestamp=ts, signature=relay_hmac(key, body, ts)
        )
        clock[0] += 1
    assert _ingress_count(tmp_path / "ingress.db") == 2

    failing = False
    clock[0] += GatewayRedeliverWorker.BASE_BACKOFF_SECONDS + 1
    report = GatewayRedeliverWorker(router).run_once()
    assert report.public_dict()["delivered"] == 2, (
        "the row behind a slow delivery was dropped as a replay"
    )
    assert report.public_dict()["dropped_undeliverable"] == 0
    assert delivered == [first, second]


def test_a_backoff_is_measured_from_after_the_attempt_that_failed(
    tmp_path: Path, monkeypatch
) -> None:
    """I2, the other half: an attempt consumes real time.

    A delivery that blocked for a minute before failing has already spent that
    minute. Scheduling the retry from a stamp taken BEFORE it would make the
    row due again immediately and retry hot through an outage — which is the
    thing the backoff exists to prevent.
    """
    monkeypatch.setenv("ABROLIA_WHATSAPP_SHARED_ENABLED", "1")
    gateway_key = b"synthetic-gateway-hmac-key-1234567890abcdef"
    db, hid, phone = _bound_household(tmp_path, gateway_key=gateway_key)
    key = b"household-relay-key-1234567890abcdef"

    clock = [1_000_000.0]
    blocked_for = 600.0

    def slow_then_fail(household_id, payload, timestamp, signature):
        clock[0] += blocked_for
        raise RuntimeError("the runtime took a long time to say no")

    router = WhatsAppGatewayRouter(
        db,
        relay_keys={hid: key},
        gateway_hmac_key=gateway_key,
        ingress_path=tmp_path / "ingress.db",
        runtime_deliver=slow_then_fail,
        now_fn=lambda: clock[0],
    )
    body = b'{"message":"slow refusal"}'
    ts = str(int(clock[0]))
    router.handle_webhook(body, phone, timestamp=ts, signature=relay_hmac(key, body, ts))

    # Past the backoff the ingress attempt already scheduled, so the row is due.
    clock[0] += GatewayRedeliverWorker.BASE_BACKOFF_SECONDS + 1
    started = clock[0]
    GatewayRedeliverWorker(router).run_once()
    next_attempt = sqlite3.connect(tmp_path / "ingress.db").execute(
        "SELECT next_attempt_at FROM gateway_ingress"
    ).fetchone()[0]
    assert next_attempt >= started + blocked_for, (
        "the retry was scheduled from before the attempt that consumed the time"
    )


def test_the_brake_stops_delivery_but_not_cleanup(tmp_path: Path, monkeypatch) -> None:
    """I1: the switch sits on the terminal/retryable seam.

    An incident suspends SENDING. It must not also suspend retention and
    security cleanup, or a forged payload and an expired message are kept for
    the length of the incident — message content held for no reason, and held
    precisely when the operator is trying to contain something.
    """
    monkeypatch.setenv("ABROLIA_WHATSAPP_SHARED_ENABLED", "1")
    gateway_key = b"synthetic-gateway-hmac-key-1234567890abcdef"
    db, hid, phone = _bound_household(tmp_path, gateway_key=gateway_key)
    key = b"household-relay-key-1234567890abcdef"

    clock = [1_000_000.0]
    path = tmp_path / "ingress.db"
    store = GatewayStore(path)
    # One deliverable, one already too old, one whose signature is a lie.
    good = b'{"message":"deliverable"}'
    good_ts = str(int(clock[0]))
    store.persist_before_ack(
        good, phone, channel="whatsapp", household_id=hid,
        timestamp=good_ts, signature=relay_hmac(key, good, good_ts), now=clock[0],
    )
    store.persist_before_ack(
        b'{"message":"far too old"}', phone, channel="whatsapp", household_id=hid,
        timestamp=good_ts, signature=relay_hmac(key, b'{"message":"far too old"}', good_ts),
        now=clock[0] - GatewayRedeliverWorker.MAX_AGE_SECONDS * 2,
    )
    forged = b'{"message":"never signed"}'
    store.persist_before_ack(
        forged, phone, channel="whatsapp", household_id=hid,
        timestamp=good_ts, signature="sha256=" + "0" * 64, now=clock[0],
    )

    router = WhatsAppGatewayRouter(
        db,
        relay_keys={hid: key},
        gateway_hmac_key=gateway_key,
        ingress_path=path,
        runtime_deliver=lambda h, p, t, s: (_ for _ in ()).throw(
            AssertionError("nothing may be sent while the switch is off")
        ),
        now_fn=lambda: clock[0],
    )
    monkeypatch.setenv("ABROLIA_WHATSAPP_SHARED_ENABLED", "0")
    report = GatewayRedeliverWorker(router).run_once().public_dict()

    assert report["dropped_expired"] == 1, "the brake suspended retention cleanup"
    assert report["dropped_unverifiable"] == 1, "the brake kept a forged payload"
    assert report["held"] == 1, "the deliverable row was not held"
    assert report["delivered"] == 0
    assert _ingress_count(path) == 1, "only the deliverable row should remain"


def test_one_unprocessable_row_does_not_take_the_batch_with_it(
    tmp_path: Path, monkeypatch
) -> None:
    """Structural: a row that throws is deferred, not allowed to abandon the run.

    Without this the first row to raise anywhere outside the delivery call
    aborts `run_once` before every row behind it, and because nothing was
    deferred the next run selects the same row and does it again — a backlog
    blocked forever by one bad row, with no backoff between attempts.
    """
    monkeypatch.setenv("ABROLIA_WHATSAPP_SHARED_ENABLED", "1")
    gateway_key = b"synthetic-gateway-hmac-key-1234567890abcdef"
    db, hid, phone = _bound_household(tmp_path, gateway_key=gateway_key)
    key = b"household-relay-key-1234567890abcdef"

    clock = [1_000_000.0]
    delivered: list[bytes] = []
    router = WhatsAppGatewayRouter(
        db,
        relay_keys={hid: key},
        gateway_hmac_key=gateway_key,
        ingress_path=tmp_path / "ingress.db",
        runtime_deliver=lambda h, p, t, s: delivered.append(p),
        now_fn=lambda: clock[0],
    )
    poison, healthy = b'{"message":"poison"}', b'{"message":"healthy"}'
    for body in (poison, healthy):
        ts = str(int(clock[0]))
        # Persisted directly so both rows are due and neither was delivered.
        router.store.persist_before_ack(
            body, phone, channel="whatsapp", household_id=hid,
            timestamp=ts, signature=relay_hmac(key, body, ts), now=clock[0],
        )

    real_route = router.route
    calls: list[int] = []

    def route_that_throws_once(sender, channel="whatsapp", *, timestamp=None):
        calls.append(1)
        if len(calls) == 1:  # the first row processed
            raise RuntimeError("the binding lookup blew up")
        return real_route(sender, channel, timestamp=timestamp)

    router.route = route_that_throws_once
    report = GatewayRedeliverWorker(router).run_once().public_dict()

    assert delivered == [healthy], "the batch stopped at the row that threw"
    assert report["deferred"] == 1, "the row that threw was not deferred"
    next_attempt = sqlite3.connect(tmp_path / "ingress.db").execute(
        "SELECT next_attempt_at FROM gateway_ingress"
    ).fetchone()[0]
    assert next_attempt > clock[0], "the failed row would be retried hot"

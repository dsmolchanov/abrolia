"""Gateway narrow multi-tenant routing + durable before ACK + HMAC."""

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

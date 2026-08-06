"""Gateway narrow multi-tenant routing + durable before ACK + HMAC."""

from pathlib import Path

from control_plane.db import open_control_plane_database
from gateway.whatsapp_router import (
    GatewayResult,
    WhatsAppGatewayRouter,
    relay_hmac,
    sender_hmac,
    verify_relay_hmac,
)


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
        "INSERT INTO channel_bindings (id, household_id, channel, external_id, actor_id, role, verified_at, verified_by_actor_id) "
        "VALUES (?, ?, 'whatsapp', ?, 'owner1', 'owner', 1, 'owner1')",
        ("b1", hid, phone),
    )
    db.connection.commit()
    router = WhatsAppGatewayRouter(db, ingress_path=tmp_path / "ingress.db")
    assert router.route("unknown") == GatewayResult(status="denied", code="unknown_sender", household_id=None)
    # add second household with same sender -> ambiguous
    db.connection.execute(
        "INSERT INTO channel_bindings (id, household_id, channel, external_id, actor_id, role, verified_at, verified_by_actor_id) "
        "VALUES (?, ?, 'whatsapp', ?, 'owner2', 'owner', 1, 'owner2')",
        ("b2", hid2, phone),
    )
    db.connection.commit()
    assert router.route(phone).code == "ambiguous_sender"
    # exact single mapping -> delivered
    db.connection.execute("DELETE FROM channel_bindings WHERE id='b2'")
    db.connection.commit()
    assert router.route(phone).household_id == hid


def test_per_household_relay_hmac_and_durable_before_ack(tmp_path: Path) -> None:
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
        "INSERT INTO channel_bindings (id, household_id, channel, external_id, external_id_hmac, actor_id, role, verified_at, verified_by_actor_id) "
        "VALUES (?, ?, 'whatsapp', ?, ?, 'owner1', 'owner', 1, 'owner1')",
        ("b1", hid, phone, phone_hmac),
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
    # wrong HMAC should be rejected
    result_bad = router2.handle_webhook(body, phone, timestamp=ts2, signature="bad")
    assert result_bad.code == "hmac_rejected"
    # Missing signature — fail-closed
    result_nosig = router2.handle_webhook(body, phone, timestamp=ts2, signature="")  # type: ignore[arg-type]
    assert result_nosig.code == "hmac_rejected"
    # Missing key — not delivered, WAL kept for reconcile
    router3 = WhatsAppGatewayRouter(
        db, relay_keys={}, gateway_hmac_key=gateway_key, ingress_path=tmp_path / "ingress3.db"
    )
    ts3 = str(int(router3.now_fn()))
    sig3 = relay_hmac(key, body, ts3)
    result3 = router3.handle_webhook(body, phone, timestamp=ts3, signature=sig3)
    assert result3.code == "hmac_rejected"

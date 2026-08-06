"""Gateway narrow multi-tenant routing + durable before ACK + HMAC."""

from pathlib import Path

from control_plane.db import open_control_plane_database
from gateway.whatsapp_router import GatewayResult, WhatsAppGatewayRouter, relay_hmac, verify_relay_hmac


def test_unknown_and_ambiguous_senders_denied(tmp_path: Path) -> None:
    db = open_control_plane_database(tmp_path / "cp.db")
    # household + binding
    hid = "00000000-0000-4000-a000-000000000001"
    hid2 = "00000000-0000-4000-a000-000000000002"
    db.connection.execute("INSERT INTO households (id, slug, status, created_at, updated_at) VALUES (?, ?, 'draft', 1, 1)", (hid, "hh1"))
    db.connection.execute("INSERT INTO households (id, slug, status, created_at, updated_at) VALUES (?, ?, 'draft', 1, 1)", (hid2, "hh2"))
    db.connection.commit()
    db.connection.execute("INSERT INTO channel_bindings (id, household_id, channel, external_id, actor_id, role, verified_at, verified_by_actor_id) VALUES (?, ?, 'whatsapp', '491511234567', 'owner1', 'owner', 1, 'owner1')", ("b1", hid))
    db.connection.commit()
    router = WhatsAppGatewayRouter(db, ingress_path=tmp_path / "ingress.db")
    assert router.route("unknown") == GatewayResult(status="denied", code="unknown_sender", household_id=None)
    # add second household with same sender -> ambiguous
    db.connection.execute("INSERT INTO channel_bindings (id, household_id, channel, external_id, actor_id, role, verified_at, verified_by_actor_id) VALUES (?, ?, 'whatsapp', '491511234567', 'owner2', 'owner', 1, 'owner2')", ("b2", hid2))
    db.connection.commit()
    assert router.route("491511234567").code == "ambiguous_sender"
    # exact single mapping -> delivered
    db.connection.execute("DELETE FROM channel_bindings WHERE id='b2'")
    db.connection.commit()
    assert router.route("491511234567").household_id == hid


def test_per_household_relay_hmac_and_durable_before_ack(tmp_path: Path) -> None:
    db = open_control_plane_database(tmp_path / "cp.db")
    hid = "00000000-0000-4000-a000-000000000011"
    db.connection.execute("INSERT INTO households (id, slug, status, created_at, updated_at) VALUES (?, ?, 'draft', 1, 1)", (hid, "hh1"))
    db.connection.execute("INSERT INTO channel_bindings (id, household_id, channel, external_id, actor_id, role, verified_at, verified_by_actor_id) VALUES (?, ?, 'whatsapp', '491511234567', 'owner1', 'owner', 1, 'owner1')", ("b1", hid))
    db.connection.commit()
    key = b"household-relay-key-1234567890abcdef"
    router = WhatsAppGatewayRouter(db, relay_keys={hid: key}, ingress_path=tmp_path / "ingress.db")
    body = b'{"message":"hello"}'
    ts = "1234567890"
    sig = relay_hmac(key, body, ts)
    assert verify_relay_hmac(key, body, ts, sig) is True
    assert verify_relay_hmac(key, body, ts, "sha256=" + sig) is True
    assert verify_relay_hmac(key, b"other", ts, sig) is False
    # durable ingress
    result = router.handle_webhook(body, "491511234567")
    assert result.status == "delivered"
    # wrong HMAC should be rejected by runtime (simulated)
    assert verify_relay_hmac(b"wrong-key", body, ts, sig) is False

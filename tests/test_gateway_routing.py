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

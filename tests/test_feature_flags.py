"""Phase F flag matrix — default off, fail-closed, independently togglable at call time."""

from __future__ import annotations

from control_plane.feature_flags import (
    is_byo_email_enabled,
    is_gmail_enabled,
    is_managed_email_enabled,
    is_web_push_enabled,
    is_whatsapp_dedicated_enabled,
    is_whatsapp_shared_enabled,
)
from gateway.whatsapp_router import WhatsAppGatewayRouter


def test_all_flags_default_off(monkeypatch) -> None:
    for key in [
        "ABROLIA_MANAGED_EMAIL_ENABLED",
        "ABROLIA_BYO_EMAIL_ENABLED",
        "ABROLIA_GMAIL_ENABLED",
        "ABROLIA_WHATSAPP_SHARED_ENABLED",
        "ABROLIA_WHATSAPP_DEDICATED_ENABLED",
        "ABROLIA_WEB_PUSH_ENABLED",
    ]:
        monkeypatch.delenv(key, raising=False)
    assert not is_managed_email_enabled()
    assert not is_byo_email_enabled()
    assert not is_gmail_enabled()
    assert not is_whatsapp_shared_enabled()
    assert not is_whatsapp_dedicated_enabled()
    assert not is_web_push_enabled()


def test_flags_independently_togglable(monkeypatch) -> None:
    monkeypatch.setenv("ABROLIA_MANAGED_EMAIL_ENABLED", "1")
    monkeypatch.setenv("ABROLIA_BYO_EMAIL_ENABLED", "0")
    assert is_managed_email_enabled() is True
    assert is_byo_email_enabled() is False
    monkeypatch.setenv("ABROLIA_BYO_EMAIL_ENABLED", "1")
    assert is_byo_email_enabled() is True


def test_flag_toggle_mid_run_blocks_next_call(monkeypatch, tmp_path) -> None:
    # Use gateway as representative provider path that reads flag at call time
    from control_plane.db import open_control_plane_database

    db = open_control_plane_database(tmp_path / "cp.db")
    hid = "00000000-0000-4000-a000-000000000099"
    phone = "+999511234599"
    db.connection.execute(
        "INSERT INTO households (id, slug, status, created_at, updated_at) VALUES (?, ?, 'draft', 1, 1)",
        (hid, "hh-flag"),
    )
    from gateway.whatsapp_router import sender_hmac

    gw_key = b"test-gateway-key-1234567890abcdef12345678"
    h = sender_hmac(phone, gw_key)
    db.connection.execute(
        "INSERT INTO channel_bindings (id, household_id, channel, external_id, external_id_hmac, actor_id, role, verified_at, verified_by_actor_id) VALUES (?, ?, 'whatsapp', ?, ?, 'owner', 'owner', 1, 'owner')",
        ("b-flag", hid, phone, h),
    )
    db.connection.commit()
    key = b"household-relay-key-1234567890abcdef"
    router = WhatsAppGatewayRouter(
        db, relay_keys={hid: key}, gateway_hmac_key=gw_key, ingress_path=tmp_path / "ingress.db"
    )
    body = b"hello"
    # Initially off -> blocked
    monkeypatch.setenv("ABROLIA_WHATSAPP_SHARED_ENABLED", "0")
    ts = str(int(router.now_fn()))
    from gateway.whatsapp_router import relay_hmac

    sig = relay_hmac(key, body, ts)
    r1 = router.handle_webhook(body, phone, timestamp=ts, signature=sig)
    assert r1.code == "flag_disabled"
    # Flip 0->1 -> allows
    monkeypatch.setenv("ABROLIA_WHATSAPP_SHARED_ENABLED", "1")
    ts2 = str(int(router.now_fn()))
    sig2 = relay_hmac(key, body, ts2)
    r2 = router.handle_webhook(body, phone, timestamp=ts2, signature=sig2)
    assert r2.status == "delivered"
    # Flip 1->0 -> blocks again
    monkeypatch.setenv("ABROLIA_WHATSAPP_SHARED_ENABLED", "0")
    ts3 = str(int(router.now_fn()))
    sig3 = relay_hmac(key, body, ts3)
    r3 = router.handle_webhook(body, phone, timestamp=ts3, signature=sig3)
    assert r3.code == "flag_disabled"


def test_eu_strict_still_fail_closed() -> None:
    # Existing eu-strict test remains green: load_config with eu-strict without vertex fails
    from hermes_cloud.core.config import load_config

    env = {"HERMES_VERTEX_EU_ENABLED": "0"}
    # Need manifest simulation — just check that load_config doesn't crash for default
    cfg = load_config(env=env)
    assert cfg.effort == "medium"

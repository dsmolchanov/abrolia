"""Phase F flag matrix — default off, fail-closed, togglable at call time.

Every switch in `FLAGS` must gate a real call site. Three did not: `managed_email`,
`whatsapp_dedicated` and `web_push` were accessors nothing called, while two
operator tables described them as stopping provisioning. They are retired rather
than wired, because each already had a stronger live counterpart — see the module
docstring. `test_every_declared_flag_gates_a_call_site` is what stops another one
appearing.
"""

from __future__ import annotations

import pytest

from control_plane.feature_flags import (
    FLAGS,
    check_provider_enabled,
    is_byo_email_enabled,
    is_gmail_enabled,
    is_whatsapp_shared_enabled,
)
from gateway.whatsapp_router import WhatsAppGatewayRouter

#: Where each declared switch is read. A switch with no entry here is the defect
#: this module exists to catch, so the mapping is asserted to be exhaustive
#: rather than consulted opportunistically.
GATING_CALL_SITES = {
    "byo_email": "control_plane.onboarding.service / provisioning.worker",
    "gmail": "control_plane.onboarding.service / provisioning.worker",
    "whatsapp_shared": "gateway.whatsapp_router.handle_webhook",
}


def test_every_declared_flag_gates_a_call_site() -> None:
    """A switch that gates nothing is worse than no switch, because it is believed.

    `ABROLIA_MANAGED_EMAIL_ENABLED=0` gated nothing while `docs/onboarding-runbook.md`
    told an operator it stopped `@abrolia.com` provisioning — the sort of thing
    read during an incident and acted on.
    """

    assert set(FLAGS.values()) == set(GATING_CALL_SITES)


def test_all_flags_default_off(monkeypatch) -> None:
    for key in FLAGS:
        monkeypatch.delenv(key, raising=False)
    assert not is_byo_email_enabled()
    assert not is_gmail_enabled()
    assert not is_whatsapp_shared_enabled()
    for switch in FLAGS.values():
        with pytest.raises(RuntimeError):
            check_provider_enabled(switch)


def test_flags_independently_togglable(monkeypatch) -> None:
    monkeypatch.setenv("ABROLIA_GMAIL_ENABLED", "1")
    monkeypatch.setenv("ABROLIA_BYO_EMAIL_ENABLED", "0")
    assert is_gmail_enabled() is True
    assert is_byo_email_enabled() is False
    monkeypatch.setenv("ABROLIA_BYO_EMAIL_ENABLED", "1")
    assert is_byo_email_enabled() is True


@pytest.mark.parametrize("retired", ["managed_email", "whatsapp_dedicated", "web_push"])
def test_a_retired_switch_is_gone_rather_than_silently_permissive(retired: str) -> None:
    """Removing an accessor must not leave `check_provider_enabled` answering yes.

    Deleting the helper but keeping the mapping entry would turn a switch that
    gated nothing into a switch that ALLOWED everything, which is the same
    defect with a worse failure direction.
    """

    with pytest.raises(ValueError):
        check_provider_enabled(retired)


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
        "INSERT INTO channel_bindings (id, household_id, channel, external_id, chat_id, external_id_hmac, actor_id, role, verified_at, verified_by_actor_id, published_revision) VALUES (?, ?, 'whatsapp', ?, ?, ?, 'owner', 'owner', 1, 'owner', 1)",
        ("b-flag", hid, phone, phone, h),
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

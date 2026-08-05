from __future__ import annotations

import base64

import pytest

from control_plane.config import ConfigurationError, ControlPlaneConfig

LOGIN_FROM = "login@" + "abrolia.com"


def _encoded(byte: int) -> str:
    return base64.urlsafe_b64encode(bytes([byte]) * 32).decode().rstrip("=")


def _production_env(**changes: str) -> dict[str, str]:
    values = {
        "ABROLIA_ENCRYPTION_KEY": _encoded(1),
        "ABROLIA_LOOKUP_HMAC_KEY": _encoded(2),
        "ABROLIA_TOKEN_HMAC_KEY": _encoded(3),
        "ABROLIA_CONTROL_PLANE_BACKUP_KEY": _encoded(4),
    }
    values.update(changes)
    return values


def test_production_defaults_are_visibly_synthetic_and_real_providers_off() -> None:
    config = ControlPlaneConfig.from_env(_production_env())
    assert config.synthetic_only
    assert not config.real_family_data_enabled
    assert not config.real_email_enabled
    assert not config.real_whatsapp_enabled
    assert not config.real_channel_enabled
    assert config.runtime_provider == "dry-run-runtime"
    assert not config.magic_link_delivery_enabled
    assert config.resend_api_key is None
    assert config.magic_link_from is None


def test_production_magic_link_mailer_requires_complete_secret_configuration() -> None:
    with pytest.raises(ConfigurationError, match="mailer configuration is incomplete"):
        ControlPlaneConfig.from_env(
            _production_env(
                ABROLIA_MAGIC_LINK_DELIVERY_ENABLED="1",
                ABROLIA_RESEND_API_KEY="re_secret-canary",
            )
        )
    with pytest.raises(ConfigurationError, match="mailer configuration is incomplete"):
        ControlPlaneConfig.from_env(
            _production_env(
                ABROLIA_MAGIC_LINK_DELIVERY_ENABLED="1",
                ABROLIA_MAGIC_LINK_FROM=LOGIN_FROM,
            )
        )

    config = ControlPlaneConfig.from_env(
        _production_env(
            ABROLIA_RESEND_API_KEY="re_secret-canary",
            ABROLIA_MAGIC_LINK_FROM=f"Abrolia <{LOGIN_FROM}>",
            ABROLIA_MAGIC_LINK_DELIVERY_ENABLED="1",
        )
    )

    assert config.magic_link_from == f"Abrolia <{LOGIN_FROM}>"
    assert config.magic_link_delivery_enabled
    assert "re_secret-canary" not in repr(config)


def test_production_magic_link_sender_rejects_header_injection() -> None:
    with pytest.raises(ConfigurationError, match="invalid characters"):
        ControlPlaneConfig.from_env(
            _production_env(
                ABROLIA_RESEND_API_KEY="re_secret-canary",
                ABROLIA_MAGIC_LINK_FROM=LOGIN_FROM + "\nBcc: attacker@example.com",
            )
        )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ABROLIA_SYNTHETIC_ONLY", "0"),
        ("REAL_FAMILY_DATA_ENABLED", "1"),
        ("ABROLIA_REAL_WHATSAPP_ENABLED", "1"),
        ("ABROLIA_REAL_CHANNEL_ENABLED", "1"),
    ],
)
def test_phase_one_real_data_and_provider_flags_fail_closed(
    name: str, value: str
) -> None:
    with pytest.raises(ConfigurationError, match="Phase 1|real provider"):
        ControlPlaneConfig.from_env(_production_env(**{name: value}))


def test_real_email_requires_complete_https_nerve_config_and_allowlist() -> None:
    with pytest.raises(ConfigurationError, match="complete Nerve"):
        ControlPlaneConfig.from_env(
            _production_env(ABROLIA_REAL_EMAIL_ENABLED="1")
        )
    with pytest.raises(ConfigurationError, match="HTTPS"):
        ControlPlaneConfig.from_env(
            _production_env(
                ABROLIA_REAL_EMAIL_ENABLED="1",
                ABROLIA_REAL_EMAIL_HOUSEHOLD_ALLOWLIST=(
                    "10000000-0000-4000-8000-000000000001"
                ),
                ABROLIA_NERVE_BASE_URL="http://nerve.example.test",
                ABROLIA_NERVE_ADMIN_KEY="synthetic-admin-key",
                ABROLIA_NERVE_PLATFORM_ORG_ID="20000000-0000-4000-8000-000000000001",
                ABROLIA_NERVE_PLATFORM_DOMAIN_ID="30000000-0000-4000-8000-000000000001",
            )
        )
    with pytest.raises(ConfigurationError, match="allowlist"):
        ControlPlaneConfig.from_env(
            _production_env(
                ABROLIA_REAL_EMAIL_ENABLED="1",
                ABROLIA_NERVE_BASE_URL="https://nerve.example.test",
                ABROLIA_NERVE_ADMIN_KEY="synthetic-admin-key",
                ABROLIA_NERVE_PLATFORM_ORG_ID="20000000-0000-4000-8000-000000000001",
                ABROLIA_NERVE_PLATFORM_DOMAIN_ID="30000000-0000-4000-8000-000000000001",
            )
        )


def test_real_email_config_is_independent_from_real_family_data_gate() -> None:
    household_id = "10000000-0000-4000-8000-000000000001"
    config = ControlPlaneConfig.from_env(
        _production_env(
            ABROLIA_REAL_EMAIL_ENABLED="1",
            ABROLIA_REAL_EMAIL_HOUSEHOLD_ALLOWLIST=household_id,
            ABROLIA_NERVE_BASE_URL="https://nerve.example.test",
            ABROLIA_NERVE_ADMIN_KEY="synthetic-admin-key",
            ABROLIA_NERVE_PLATFORM_ORG_ID="20000000-0000-4000-8000-000000000001",
            ABROLIA_NERVE_PLATFORM_DOMAIN_ID="30000000-0000-4000-8000-000000000001",
        )
    )

    assert config.synthetic_only
    assert not config.real_family_data_enabled
    assert config.real_email_enabled
    assert config.real_email_household_allowlist == frozenset({household_id})
    assert "synthetic-admin-key" not in repr(config)


def test_real_email_allowlist_rejects_non_uuid_entries() -> None:
    with pytest.raises(ConfigurationError, match="comma-separated UUIDs"):
        ControlPlaneConfig.from_env(
            _production_env(
                ABROLIA_REAL_EMAIL_HOUSEHOLD_ALLOWLIST="not-a-household-id"
            )
        )


def test_fly_runtime_requires_all_private_pinned_inputs() -> None:
    with pytest.raises(ConfigurationError, match="Fly synthetic runtime requires"):
        ControlPlaneConfig.from_env(
            _production_env(ABROLIA_RUNTIME_PROVIDER="fly-runtime")
        )
    config = ControlPlaneConfig.from_env(
        _production_env(
            ABROLIA_RUNTIME_PROVIDER="fly-runtime",
            FLY_API_TOKEN="synthetic-fly-token",
            ABROLIA_FLY_ORG="synthetic-org",
            ABROLIA_RUNTIME_IMAGE="registry.example.test/runtime@sha256:" + "a" * 64,
            ABROLIA_INTERNAL_BOOTSTRAP_HOST="control-plane.internal",
        )
    )
    assert config.runtime_provider == "fly-runtime"
    assert config.runtime_region == "ams"
    assert config.runtime_volume_mount == "/data"


def test_configuration_rejects_shared_keys_and_insecure_origin() -> None:
    shared = _encoded(9)
    with pytest.raises(ConfigurationError, match="independent"):
        ControlPlaneConfig.from_env(
            _production_env(
                ABROLIA_ENCRYPTION_KEY=shared,
                ABROLIA_LOOKUP_HMAC_KEY=shared,
            )
        )
    with pytest.raises(ConfigurationError, match="HTTPS"):
        ControlPlaneConfig.from_env(
            _production_env(ABROLIA_PUBLIC_ORIGIN="http://app.example.test")
        )

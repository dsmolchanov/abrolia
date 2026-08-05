from __future__ import annotations

import base64

import pytest

from control_plane.config import ConfigurationError, ControlPlaneConfig


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


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ABROLIA_SYNTHETIC_ONLY", "0"),
        ("REAL_FAMILY_DATA_ENABLED", "1"),
        ("ABROLIA_REAL_EMAIL_ENABLED", "1"),
        ("ABROLIA_REAL_WHATSAPP_ENABLED", "1"),
        ("ABROLIA_REAL_CHANNEL_ENABLED", "1"),
    ],
)
def test_phase_one_real_data_and_provider_flags_fail_closed(
    name: str, value: str
) -> None:
    with pytest.raises(ConfigurationError, match="Phase 1|real provider"):
        ControlPlaneConfig.from_env(_production_env(**{name: value}))


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

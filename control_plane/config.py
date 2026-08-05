"""Fail-closed configuration for the metadata-only onboarding control plane."""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field
from pathlib import Path


class ConfigurationError(RuntimeError):
    """Configuration would weaken a locked control-plane invariant."""


def _decode_key(value: str, *, name: str) -> bytes:
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as error:
        raise ConfigurationError(f"{name} must be urlsafe base64") from error
    if len(decoded) != 32:
        raise ConfigurationError(f"{name} must decode to exactly 32 bytes")
    return decoded


@dataclass(frozen=True)
class ControlPlaneConfig:
    database_path: Path = Path("data/control-plane.db")
    public_origin: str = "https://app.abrolia.com"
    encryption_keys: dict[str, bytes] = field(default_factory=dict, repr=False)
    active_encryption_key_version: str = "v1"
    lookup_hmac_key: bytes = field(default=b"", repr=False)
    token_hmac_key: bytes = field(default=b"", repr=False)
    backup_key: bytes = field(default=b"", repr=False)
    synthetic_only: bool = True
    real_family_data_enabled: bool = False
    real_email_enabled: bool = False
    real_whatsapp_enabled: bool = False
    real_channel_enabled: bool = False
    fly_api_token: str | None = field(default=None, repr=False)
    fly_org_slug: str | None = None
    runtime_image_digest: str | None = None
    runtime_region: str = "ams"
    runtime_volume_mount: str = "/data"
    runtime_provider: str = "dry-run-runtime"
    internal_bootstrap_host: str | None = None
    session_cookie_name: str = "__Host-abrolia_session"
    csrf_cookie_name: str = "__Host-abrolia_csrf"
    bootstrap_ttl_seconds: int = 3600

    def validate(self) -> ControlPlaneConfig:
        if self.public_origin != self.public_origin.rstrip("/"):
            raise ConfigurationError("public origin must not have a trailing slash")
        if not self.public_origin.startswith("https://"):
            raise ConfigurationError("control-plane public origin must use HTTPS")
        if self.runtime_region != "ams":
            raise ConfigurationError("Phase 1 runtime region is locked to ams")
        if not self.synthetic_only or self.real_family_data_enabled:
            raise ConfigurationError("Phase 1 is synthetic-only; real family data is blocked")
        if self.real_email_enabled or self.real_whatsapp_enabled or self.real_channel_enabled:
            raise ConfigurationError("real provider adapters are disabled in Phase 1")
        active = self.encryption_keys.get(self.active_encryption_key_version)
        if active is None or len(active) != 32:
            raise ConfigurationError("active AES-256-GCM key is missing or invalid")
        if len(self.lookup_hmac_key) != 32:
            raise ConfigurationError("lookup HMAC key must be 32 bytes")
        if len(self.token_hmac_key) != 32:
            raise ConfigurationError("token HMAC key must be 32 bytes")
        if self.backup_key and len(self.backup_key) != 32:
            raise ConfigurationError("backup key must be 32 bytes")
        field_keys = set(self.encryption_keys.values())
        if self.lookup_hmac_key in field_keys or self.token_hmac_key in field_keys:
            raise ConfigurationError("field encryption and HMAC keys must be independent")
        if self.lookup_hmac_key == self.token_hmac_key:
            raise ConfigurationError("lookup and token HMAC keys must be independent")
        if self.backup_key and self.backup_key in {
            *field_keys,
            self.lookup_hmac_key,
            self.token_hmac_key,
        }:
            raise ConfigurationError("backup key must be independent from application keys")
        if self.runtime_provider not in {"dry-run-runtime", "fly-runtime"}:
            raise ConfigurationError("runtime provider is not enabled for Phase 1")
        if self.runtime_provider == "fly-runtime" and (
            not self.fly_api_token
            or not self.fly_org_slug
            or not self.runtime_image_digest
            or not self.internal_bootstrap_host
        ):
            raise ConfigurationError(
                "Fly synthetic runtime requires token, org, image digest and private bootstrap host"
            )
        if self.internal_bootstrap_host and any(
            marker in self.internal_bootstrap_host for marker in ("/", ":", " ")
        ):
            raise ConfigurationError("internal bootstrap host must be a bare DNS name")
        if self.runtime_image_digest and "@sha256:" not in self.runtime_image_digest:
            raise ConfigurationError("runtime image must be pinned by immutable sha256 digest")
        return self

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> ControlPlaneConfig:
        source = dict(os.environ if env is None else env)
        version = source.get("ABROLIA_ENCRYPTION_KEY_VERSION", "v1")
        encoded_encryption = source.get("ABROLIA_ENCRYPTION_KEY", "")
        encoded_lookup = source.get("ABROLIA_LOOKUP_HMAC_KEY", "")
        encoded_token = source.get("ABROLIA_TOKEN_HMAC_KEY", "")
        encoded_backup = source.get("ABROLIA_CONTROL_PLANE_BACKUP_KEY", "")
        config = cls(
            database_path=Path(source.get("ABROLIA_CONTROL_PLANE_DB", "data/control-plane.db")),
            public_origin=source.get("ABROLIA_PUBLIC_ORIGIN", "https://app.abrolia.com"),
            encryption_keys=(
                {version: _decode_key(encoded_encryption, name="ABROLIA_ENCRYPTION_KEY")}
                if encoded_encryption
                else {}
            ),
            active_encryption_key_version=version,
            lookup_hmac_key=(
                _decode_key(encoded_lookup, name="ABROLIA_LOOKUP_HMAC_KEY")
                if encoded_lookup
                else b""
            ),
            token_hmac_key=(
                _decode_key(encoded_token, name="ABROLIA_TOKEN_HMAC_KEY")
                if encoded_token
                else b""
            ),
            backup_key=(
                _decode_key(
                    encoded_backup, name="ABROLIA_CONTROL_PLANE_BACKUP_KEY"
                )
                if encoded_backup
                else b""
            ),
            synthetic_only=source.get("ABROLIA_SYNTHETIC_ONLY", "1") == "1",
            real_family_data_enabled=source.get("REAL_FAMILY_DATA_ENABLED", "0") == "1",
            real_email_enabled=source.get("ABROLIA_REAL_EMAIL_ENABLED", "0") == "1",
            real_whatsapp_enabled=source.get("ABROLIA_REAL_WHATSAPP_ENABLED", "0") == "1",
            real_channel_enabled=source.get("ABROLIA_REAL_CHANNEL_ENABLED", "0") == "1",
            fly_api_token=source.get("FLY_API_TOKEN") or None,
            fly_org_slug=source.get("ABROLIA_FLY_ORG") or None,
            runtime_image_digest=source.get("ABROLIA_RUNTIME_IMAGE") or None,
            runtime_provider=source.get("ABROLIA_RUNTIME_PROVIDER", "dry-run-runtime"),
            internal_bootstrap_host=source.get("ABROLIA_INTERNAL_BOOTSTRAP_HOST") or None,
        )
        return config.validate()

    @classmethod
    def for_test(cls, root: Path, *, key_byte: int = 7) -> ControlPlaneConfig:
        key = bytes([key_byte]) * 32
        return cls(
            database_path=root / "control-plane.db",
            public_origin="https://app.example.test",
            encryption_keys={"test-v1": key},
            active_encryption_key_version="test-v1",
            lookup_hmac_key=bytes([key_byte + 1]) * 32,
            token_hmac_key=bytes([key_byte + 2]) * 32,
            internal_bootstrap_host="app.example.test",
        ).validate()

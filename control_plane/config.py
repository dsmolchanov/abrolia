"""Fail-closed configuration for the metadata-only onboarding control plane."""

from __future__ import annotations

import base64
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path


class ConfigurationError(RuntimeError):
    """Configuration would weaken a locked control-plane invariant."""


def decode_key_material(value: str) -> bytes:
    """Decode urlsafe base64 key material, tolerating absent padding.

    One spelling of the padding rule, because there was briefly more than one:
    the startup migration step decoded the SAME environment variable without
    restoring padding, so a valid unpadded key that the application accepted
    turned into `b""` there — the pre-migrate backup then failed closed and the
    container never reached `serve`. A key is either good for both readers or
    neither.
    """
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _decode_key(value: str, *, name: str) -> bytes:
    try:
        decoded = decode_key_material(value)
    except (ValueError, TypeError) as error:
        raise ConfigurationError(f"{name} must be urlsafe base64") from error
    if len(decoded) != 32:
        raise ConfigurationError(f"{name} must decode to exactly 32 bytes")
    return decoded


def backup_key_from_env(env: dict[str, str] | None = None) -> bytes:
    """The backup key alone, for commands that must work when nothing else does.

    `ControlPlaneConfig.from_env` validates the WHOLE application: field
    encryption, both HMAC keys and, under the checked-in `fly-runtime`
    configuration, the Fly token, organization, image digest and bootstrap host.
    Reading the backup key through it made `restore` — and the rollback install
    that follows it — refuse to run when any unrelated secret was missing or
    invalid, even with a perfectly good archive and a perfectly good key.

    That is exactly backwards. These two commands exist for the situation where
    the deployment is broken; making them depend on the deployment being intact
    withdraws the recovery path at the moment it is needed. They read the
    dedicated key, and nothing else.
    """
    source = dict(os.environ if env is None else env)
    encoded = source.get("ABROLIA_CONTROL_PLANE_BACKUP_KEY", "").strip()
    if not encoded:
        raise ConfigurationError(
            "ABROLIA_CONTROL_PLANE_BACKUP_KEY is required to read a backup archive"
        )
    return _decode_key(encoded, name="ABROLIA_CONTROL_PLANE_BACKUP_KEY")


def _uuid_set(value: str, *, name: str) -> frozenset[str]:
    parsed: set[str] = set()
    for item in value.split(","):
        candidate = item.strip()
        if not candidate:
            continue
        try:
            parsed.add(str(uuid.UUID(candidate)))
        except ValueError as error:
            raise ConfigurationError(f"{name} must contain comma-separated UUIDs") from error
    return frozenset(parsed)


def _canonical_uuid(value: str | None, *, name: str) -> str:
    try:
        parsed = str(uuid.UUID(value or ""))
    except ValueError as error:
        raise ConfigurationError(f"{name} must be a canonical UUID") from error
    if value != parsed:
        raise ConfigurationError(f"{name} must be a canonical UUID")
    return parsed


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
    real_email_household_allowlist: frozenset[str] = field(default_factory=frozenset)
    real_whatsapp_enabled: bool = False
    real_channel_enabled: bool = False
    # The Phase F per-provider kill switches are NOT here. They were: six
    # fields, parsed from six env vars, read by nothing. `feature_flags.py`
    # reads those same variables directly, because a kill switch has to answer
    # at call time and this object is built once and frozen — so a config copy
    # could only ever be a second, staler spelling of the same value. See
    # `AGENTS.repo-invariants.md`, "A precondition is enforced where the
    # provider is CALLED".
    nerve_base_url: str | None = None
    nerve_admin_key: str | None = field(default=None, repr=False)
    nerve_platform_org_id: str | None = None
    nerve_platform_domain_id: str | None = None
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
    magic_link_delivery_enabled: bool = False
    resend_api_key: str | None = field(default=None, repr=False)
    magic_link_from: str | None = None
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: str | None = field(default=None, repr=False)
    google_oauth_test_users: tuple[str, ...] = ()
    gmail_real_enabled: bool = False
    google_oauth_app_verified: bool = False
    google_gmail_scope_approved: bool = False
    google_casa_current: bool = False
    google_limited_use_disclosed: bool = False

    def validate(self) -> ControlPlaneConfig:
        if self.public_origin != self.public_origin.rstrip("/"):
            raise ConfigurationError("public origin must not have a trailing slash")
        if not self.public_origin.startswith("https://"):
            raise ConfigurationError("control-plane public origin must use HTTPS")
        if self.runtime_region != "ams":
            raise ConfigurationError("Phase 1 runtime region is locked to ams")
        if self.synthetic_only and (
            self.real_family_data_enabled
            or self.real_whatsapp_enabled
            or self.real_channel_enabled
            or self.gmail_real_enabled
        ):
            raise ConfigurationError("synthetic-only mode blocks real provider data")
        if not self.synthetic_only and not self.real_family_data_enabled:
            raise ConfigurationError(
                "real provider mode requires the family-data launch gate"
            )
        if self.real_whatsapp_enabled or self.real_channel_enabled:
            raise ConfigurationError("real WhatsApp/channel adapters are not enabled yet")
        if self.magic_link_delivery_enabled and not (
            self.resend_api_key and self.magic_link_from
        ):
            raise ConfigurationError("production magic-link mailer configuration is incomplete")
        if self.magic_link_from and any(
            character in self.magic_link_from for character in "\r\n"
        ):
            raise ConfigurationError("magic-link sender contains invalid characters")
        if self.real_email_enabled:
            if not all((
                self.nerve_base_url,
                self.nerve_admin_key,
                self.nerve_platform_org_id,
                self.nerve_platform_domain_id,
            )):
                raise ConfigurationError(
                    "real email requires complete Nerve admin and platform configuration"
                )
            if not self.nerve_base_url.startswith("https://"):
                raise ConfigurationError("Nerve admin origin must use HTTPS")
            if not self.real_email_household_allowlist:
                raise ConfigurationError(
                    "real email requires an explicit household allowlist"
                )
            for household_id in self.real_email_household_allowlist:
                _canonical_uuid(
                    household_id,
                    name="ABROLIA_REAL_EMAIL_HOUSEHOLD_ALLOWLIST entry",
                )
            _canonical_uuid(
                self.nerve_platform_org_id,
                name="ABROLIA_NERVE_PLATFORM_ORG_ID",
            )
            _canonical_uuid(
                self.nerve_platform_domain_id,
                name="ABROLIA_NERVE_PLATFORM_DOMAIN_ID",
            )
        google_configured = bool(
            self.google_oauth_client_id and self.google_oauth_client_secret
        )
        if bool(self.google_oauth_client_id) != bool(self.google_oauth_client_secret):
            raise ConfigurationError("Google OAuth client configuration is incomplete")
        if self.gmail_real_enabled and (
            not self.real_email_enabled
            or not google_configured
            or not all((
                self.google_oauth_app_verified,
                self.google_gmail_scope_approved,
                self.google_casa_current,
                self.google_limited_use_disclosed,
            ))
        ):
            raise ConfigurationError(
                "real Gmail requires verified OAuth, scope, CASA and Limited Use evidence"
            )
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
            real_email_household_allowlist=_uuid_set(
                source.get("ABROLIA_REAL_EMAIL_HOUSEHOLD_ALLOWLIST", ""),
                name="ABROLIA_REAL_EMAIL_HOUSEHOLD_ALLOWLIST",
            ),
            real_whatsapp_enabled=source.get("ABROLIA_REAL_WHATSAPP_ENABLED", "0") == "1",
            real_channel_enabled=source.get("ABROLIA_REAL_CHANNEL_ENABLED", "0") == "1",
            nerve_base_url=source.get("ABROLIA_NERVE_BASE_URL") or None,
            nerve_admin_key=source.get("ABROLIA_NERVE_ADMIN_KEY") or None,
            nerve_platform_org_id=source.get("ABROLIA_NERVE_PLATFORM_ORG_ID") or None,
            nerve_platform_domain_id=(
                source.get("ABROLIA_NERVE_PLATFORM_DOMAIN_ID") or None
            ),
            fly_api_token=source.get("FLY_API_TOKEN") or None,
            fly_org_slug=source.get("ABROLIA_FLY_ORG") or None,
            runtime_image_digest=source.get("ABROLIA_RUNTIME_IMAGE") or None,
            runtime_provider=source.get("ABROLIA_RUNTIME_PROVIDER", "dry-run-runtime"),
            internal_bootstrap_host=source.get("ABROLIA_INTERNAL_BOOTSTRAP_HOST") or None,
            magic_link_delivery_enabled=(
                source.get("ABROLIA_MAGIC_LINK_DELIVERY_ENABLED", "0") == "1"
            ),
            resend_api_key=source.get("ABROLIA_RESEND_API_KEY") or None,
            magic_link_from=source.get("ABROLIA_MAGIC_LINK_FROM") or None,
            google_oauth_client_id=source.get("ABROLIA_GOOGLE_OAUTH_CLIENT_ID") or None,
            google_oauth_client_secret=(
                source.get("ABROLIA_GOOGLE_OAUTH_CLIENT_SECRET") or None
            ),
            google_oauth_test_users=tuple(
                sorted({
                    item.strip().casefold()
                    for item in source.get("ABROLIA_GOOGLE_OAUTH_TEST_USERS", "").split(",")
                    if item.strip()
                })
            ),
            gmail_real_enabled=source.get("ABROLIA_GMAIL_REAL_ENABLED", "0") == "1",
            google_oauth_app_verified=(
                source.get("ABROLIA_GOOGLE_OAUTH_APP_VERIFIED", "0") == "1"
            ),
            google_gmail_scope_approved=(
                source.get("ABROLIA_GOOGLE_GMAIL_SCOPE_APPROVED", "0") == "1"
            ),
            google_casa_current=source.get("ABROLIA_GOOGLE_CASA_CURRENT", "0") == "1",
            google_limited_use_disclosed=(
                source.get("ABROLIA_GOOGLE_LIMITED_USE_DISCLOSED", "0") == "1"
            ),
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

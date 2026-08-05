"""Pure, versioned, non-secret configuration contract for a household runtime."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import tomllib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = frozenset({SCHEMA_VERSION})
ENV_HOUSEHOLD_FILE = "HERMES_HOUSEHOLD"
ENV_HOUSEHOLD_ID = "HERMES_HOUSEHOLD_ID"
ENV_CONFIG_REVISION = "HERMES_CONFIG_REVISION"
ENV_CONFIG_SHA256 = "HERMES_CONFIG_SHA256"
ENV_REQUIRE_MANIFEST = "HERMES_REQUIRE_MANIFEST"
DEFAULT_HOUSEHOLD_FILE = "household.toml"

_SHA256 = re.compile(r"[0-9a-fA-F]{64}")
_COUNTRY = re.compile(r"[A-Za-z]{2}")
_CHANNEL = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_VERSION_MARKER = re.compile(r"(?m)^\s*schema_version\s*=")
_FORBIDDEN_KEYS = {"token", "secret", "password", "api_key", "refresh_token", "account_id"}


class ManifestError(ValueError):
    """The manifest cannot be trusted or interpreted by this runtime."""


class UnsupportedManifestVersion(ManifestError):
    pass


class ManifestHashMismatch(ManifestError):
    pass


class ManifestEnvironmentMismatch(ManifestError):
    pass


@dataclass(frozen=True)
class ActorDirectory:
    owner: str
    family: frozenset[str]
    guests: frozenset[str]

    @property
    def all_ids(self) -> frozenset[str]:
        return frozenset({self.owner, *self.family, *self.guests})


@dataclass(frozen=True)
class ChannelBinding:
    channel: str
    actor_id: str
    chat_id: str
    verified: bool
    external_ref: str | None = None


@dataclass(frozen=True)
class EmailRouting:
    agent_inbox: str
    fallback: str


@dataclass(frozen=True)
class ConsentReceipt:
    receipt_id: str
    purpose: str
    text_version: str
    text_sha256: str


@dataclass(frozen=True)
class ConsentAuthority:
    authority: str
    enforcement: str
    required_purposes: tuple[str, ...]
    receipts: tuple[ConsentReceipt, ...]


@dataclass(frozen=True)
class RuntimeManifest:
    schema_version: int
    household_id: str
    config_revision: int
    config_sha256: str
    family_language: str
    timezone: str
    country_code: str
    residency_mode: str
    actors: ActorDirectory
    primary_channel: str
    channel_bindings: tuple[ChannelBinding, ...]
    email: EmailRouting
    consent: ConsentAuthority | None
    provider_refs: Mapping[str, Any]
    source: str = "<memory>"

    @property
    def verified_bindings(self) -> tuple[ChannelBinding, ...]:
        return tuple(item for item in self.channel_bindings if item.verified)

    @property
    def allowed_chats(self) -> frozenset[str]:
        return frozenset(item.chat_id for item in self.verified_bindings)

    @property
    def verified_actor_chat_pairs(self) -> frozenset[tuple[str, str]]:
        return frozenset((item.actor_id, item.chat_id) for item in self.verified_bindings)

    @property
    def primary_chat_id(self) -> str:
        chats = {
            item.chat_id
            for item in self.verified_bindings
            if item.channel == self.primary_channel
        }
        return next(iter(chats))


def _truthy(value: str | None) -> bool:
    return (value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _walk_public(value: Any, path: str = "manifest") -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key).strip().casefold().replace("-", "_")
            if key in _FORBIDDEN_KEYS:
                raise ManifestError(f"{path}.{raw_key}: forbidden field")
            _walk_public(child, f"{path}.{raw_key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_public(child, f"{path}[{index}]")


def _json_value(value: Any, path: str = "manifest") -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(child, f"{path}.{key}") for key, child in value.items()}
    if isinstance(value, list):
        return [_json_value(child, f"{path}[]") for child in value]
    if isinstance(value, (str, bool, int)) or value is None:
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ManifestError(f"{path}: unsupported canonical value {type(value).__name__}")


def canonical_manifest_bytes(document: Mapping[str, Any]) -> bytes:
    payload = dict(document)
    payload.pop("config_sha256", None)
    return json.dumps(
        _json_value(payload), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode()


def compute_config_sha256(document: Mapping[str, Any] | str | bytes) -> str:
    if isinstance(document, bytes):
        document = document.decode()
    if isinstance(document, str):
        try:
            document = tomllib.loads(document)
        except tomllib.TOMLDecodeError as error:
            raise ManifestError(f"invalid TOML: {error}") from error
    return hashlib.sha256(canonical_manifest_bytes(document)).hexdigest()


def _table(document: Mapping[str, Any], key: str, *, required: bool = True) -> Mapping[str, Any]:
    value = document.get(key)
    if value is None and not required:
        return {}
    if not isinstance(value, Mapping):
        raise ManifestError(f"{key}: expected a table")
    return value


def _text(document: Mapping[str, Any], key: str, where: str = "manifest") -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{where}.{key}: expected a non-empty string")
    return value.strip()


def _optional_text(document: Mapping[str, Any], key: str, where: str) -> str | None:
    value = document.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{where}.{key}: expected a non-empty string")
    return value.strip()


def _ids(document: Mapping[str, Any], key: str) -> frozenset[str]:
    value = document.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ManifestError(f"actors.{key}: expected non-empty string array")
    stripped = [item.strip() for item in value]
    if len(stripped) != len(set(stripped)):
        raise ManifestError(f"actors.{key}: duplicate actor")
    return frozenset(stripped)


def _email(value: str, field: str) -> str:
    if value.count("@") != 1 or any(char.isspace() for char in value):
        raise ManifestError(f"email.{field}: invalid address")
    local, domain = value.rsplit("@", 1)
    if not local or "." not in domain:
        raise ManifestError(f"email.{field}: invalid address")
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(child) for child in value)
    return value


def _check_environment(manifest: RuntimeManifest, env: Mapping[str, str]) -> None:
    expected_id = (env.get(ENV_HOUSEHOLD_ID) or "").strip()
    if expected_id and expected_id != manifest.household_id:
        raise ManifestEnvironmentMismatch(f"{ENV_HOUSEHOLD_ID} does not match manifest")
    expected_revision = (env.get(ENV_CONFIG_REVISION) or "").strip()
    if expected_revision:
        try:
            revision = int(expected_revision)
        except ValueError as error:
            raise ManifestEnvironmentMismatch(f"{ENV_CONFIG_REVISION} must be an integer") from error
        if revision != manifest.config_revision:
            raise ManifestEnvironmentMismatch(f"{ENV_CONFIG_REVISION} does not match manifest")
    expected_sha = (env.get(ENV_CONFIG_SHA256) or "").strip().casefold()
    if expected_sha and not hmac.compare_digest(expected_sha, manifest.config_sha256):
        raise ManifestEnvironmentMismatch(f"{ENV_CONFIG_SHA256} does not match manifest")


def parse_runtime_manifest(
    content: str | bytes, *, env: Mapping[str, str] | None = None, source: str = "<memory>"
) -> RuntimeManifest:
    """Parse and validate the agreed v1 ``household.toml`` wire contract."""
    if isinstance(content, bytes):
        try:
            content = content.decode()
        except UnicodeDecodeError as error:
            raise ManifestError(f"{source}: manifest is not UTF-8") from error
    try:
        document = tomllib.loads(content)
    except tomllib.TOMLDecodeError as error:
        raise ManifestError(f"{source}: invalid TOML: {error}") from error
    _walk_public(document)

    version = document.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ManifestError(f"{source}: schema_version must be an integer")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise UnsupportedManifestVersion(
            f"{source}: unsupported schema_version {version}; runtime supports 1"
        )
    household_id = _text(document, "household_id")
    try:
        uuid.UUID(household_id)
    except ValueError as error:
        raise ManifestError("manifest.household_id: expected a UUID") from error
    revision = document.get("config_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ManifestError("manifest.config_revision: expected a positive integer")

    declared_sha = _text(document, "config_sha256").casefold()
    if not _SHA256.fullmatch(declared_sha):
        raise ManifestError("manifest.config_sha256: expected a SHA-256")
    actual_sha = compute_config_sha256(document)
    if not hmac.compare_digest(declared_sha, actual_sha):
        raise ManifestHashMismatch(f"{source}: config_sha256 mismatch")

    language = _text(document, "family_language")
    timezone = _text(document, "timezone")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as error:
        raise ManifestError(f"manifest.timezone: unknown IANA timezone {timezone!r}") from error
    country = _text(document, "country_code").upper()
    if not _COUNTRY.fullmatch(country):
        raise ManifestError("manifest.country_code: expected two letters")
    residency = _text(document, "residency_mode")
    if residency not in {"eu-app", "eu-strict"}:
        raise ManifestError("manifest.residency_mode: expected eu-app or eu-strict")

    raw_actors = _table(document, "actors")
    actors = ActorDirectory(
        owner=_text(raw_actors, "owner", "actors"),
        family=_ids(raw_actors, "family"),
        guests=_ids(raw_actors, "guests"),
    )
    if actors.owner in actors.guests or actors.family & actors.guests:
        raise ManifestError("actors: conflicting roles")
    raw_channels = _table(document, "channels")
    primary = _text(raw_channels, "primary", "channels").casefold()
    if not _CHANNEL.fullmatch(primary):
        raise ManifestError("channels.primary: invalid channel")

    raw_bindings = document.get("channel_bindings")
    if not isinstance(raw_bindings, list) or not raw_bindings:
        raise ManifestError("channel_bindings: expected at least one binding")
    bindings: list[ChannelBinding] = []
    for index, raw in enumerate(raw_bindings):
        where = f"channel_bindings[{index}]"
        if not isinstance(raw, Mapping):
            raise ManifestError(f"{where}: expected a table")
        channel = _text(raw, "channel", where).casefold()
        if not _CHANNEL.fullmatch(channel):
            raise ManifestError(f"{where}.channel: invalid channel")
        actor_id = _text(raw, "actor_id", where)
        if actor_id not in actors.all_ids:
            raise ManifestError(f"{where}.actor_id: actor is not declared")
        verified = raw.get("verified")
        if not isinstance(verified, bool):
            raise ManifestError(f"{where}.verified: expected a boolean")
        bindings.append(ChannelBinding(
            channel=channel,
            actor_id=actor_id,
            chat_id=_text(raw, "chat_id", where),
            verified=verified,
            external_ref=_optional_text(raw, "external_ref", where),
        ))
    primary_chats = {
        item.chat_id for item in bindings if item.verified and item.channel == primary
    }
    if len(primary_chats) != 1:
        detail = "no verified binding" if not primary_chats else "multiple chats"
        raise ManifestError(f"channels.primary: {detail}")

    raw_email = _table(document, "email")
    email = EmailRouting(
        agent_inbox=_email(_text(raw_email, "agent_inbox", "email"), "agent_inbox"),
        fallback=_email(_text(raw_email, "fallback", "email"), "fallback"),
    )
    if email.agent_inbox.casefold() == email.fallback.casefold():
        raise ManifestError("email.agent_inbox must not equal email.fallback")
    raw_provider_refs = _table(document, "provider_refs", required=False)
    raw_consent = document.get("consent")
    consent: ConsentAuthority | None = None
    if raw_consent is not None:
        if not isinstance(raw_consent, Mapping):
            raise ManifestError("consent: expected a table")
        authority = _text(raw_consent, "authority", "consent")
        enforcement = _text(raw_consent, "enforcement", "consent")
        if authority != "control_plane" or enforcement != "required":
            raise ManifestError("consent: control-plane required enforcement expected")
        raw_required = raw_consent.get("required_purposes")
        if not isinstance(raw_required, list) or not raw_required or any(
            not isinstance(item, str) or not item for item in raw_required
        ):
            raise ManifestError("consent.required_purposes: expected non-empty string array")
        raw_receipts = raw_consent.get("receipts")
        if not isinstance(raw_receipts, list) or not raw_receipts:
            raise ManifestError("consent.receipts: expected at least one receipt")
        receipts: list[ConsentReceipt] = []
        for index, raw_receipt in enumerate(raw_receipts):
            where = f"consent.receipts[{index}]"
            if not isinstance(raw_receipt, Mapping):
                raise ManifestError(f"{where}: expected a table")
            digest = _text(raw_receipt, "text_sha256", where).casefold()
            if not _SHA256.fullmatch(digest):
                raise ManifestError(f"{where}.text_sha256: expected a SHA-256")
            receipts.append(
                ConsentReceipt(
                    receipt_id=_text(raw_receipt, "receipt_id", where),
                    purpose=_text(raw_receipt, "purpose", where),
                    text_version=_text(raw_receipt, "text_version", where),
                    text_sha256=digest,
                )
            )
        purposes = {receipt.purpose for receipt in receipts}
        if not set(raw_required) <= purposes:
            raise ManifestError("consent: required purpose has no receipt")
        if len({receipt.receipt_id for receipt in receipts}) != len(receipts):
            raise ManifestError("consent: duplicate receipt ID")
        consent = ConsentAuthority(
            authority=authority,
            enforcement=enforcement,
            required_purposes=tuple(raw_required),
            receipts=tuple(receipts),
        )
    elif raw_provider_refs.get("consent_authority") == "control_plane":
        raise ManifestError("consent: authoritative mirror is required")
    manifest = RuntimeManifest(
        schema_version=version,
        household_id=household_id,
        config_revision=revision,
        config_sha256=declared_sha,
        family_language=language,
        timezone=timezone,
        country_code=country,
        residency_mode=residency,
        actors=actors,
        primary_channel=primary,
        channel_bindings=tuple(bindings),
        email=email,
        consent=consent,
        provider_refs=_freeze(raw_provider_refs),
        source=source,
    )
    _check_environment(manifest, os.environ if env is None else env)
    return manifest


def load_runtime_manifest(
    path: Path | str | None = None, *, env: Mapping[str, str] | None = None
) -> RuntimeManifest:
    source_env = os.environ if env is None else env
    file = Path(path or source_env.get(ENV_HOUSEHOLD_FILE) or DEFAULT_HOUSEHOLD_FILE)
    try:
        content = file.read_bytes()
    except OSError as error:
        raise ManifestError(f"{file}: cannot read runtime manifest") from error
    return parse_runtime_manifest(content, env=source_env, source=str(file))


def maybe_load_runtime_manifest(
    path: Path | str | None = None, *, env: Mapping[str, str] | None = None
) -> RuntimeManifest | None:
    """Return ``None`` for a legacy/absent file unless provisioned mode requires v1."""
    source_env = os.environ if env is None else env
    file = Path(path or source_env.get(ENV_HOUSEHOLD_FILE) or DEFAULT_HOUSEHOLD_FILE)
    required = _truthy(source_env.get(ENV_REQUIRE_MANIFEST)) or bool(
        (source_env.get(ENV_CONFIG_REVISION) or "").strip()
        or (source_env.get(ENV_CONFIG_SHA256) or "").strip()
    )
    if not file.is_file():
        if required:
            raise ManifestError(f"{file}: versioned runtime manifest is required")
        return None
    try:
        content = file.read_text(encoding="utf-8")
        document = tomllib.loads(content)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        marker = bool(_VERSION_MARKER.search(locals().get("content", "")))
        if required or marker:
            raise ManifestError(f"{file}: invalid versioned runtime manifest") from error
        return None
    if "schema_version" not in document:
        if required:
            raise ManifestError(f"{file}: legacy household.toml is not allowed")
        return None
    return parse_runtime_manifest(content, env=source_env, source=str(file))

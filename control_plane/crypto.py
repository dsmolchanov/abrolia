"""Field encryption, deterministic lookup and secret-boundary validation."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, NoReturn

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class CryptoError(ValueError):
    """Encrypted data cannot be authenticated or decoded."""


class UnknownKeyVersion(CryptoError):
    pass


class SecretFieldError(ValueError):
    """A durable non-secret contract attempted to carry secret material."""


SECRET_FIELD_NAMES = frozenset({
    "api_key",
    "apikey",
    "authorization",
    "access_token",
    "bootstrap_token",
    "client_secret",
    "cookie",
    "credential",
    "credentials",
    "nerve_bootstrap_key",
    "nerve_runtime_key",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "secret_material",
    "signing_key",
    "token",
})

_SECRET_VALUE_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"),
    re.compile(r"\bnrv_(?:[a-z]+_)?[A-Za-z0-9]{24,}\b"),
    re.compile(r"\bGOCSPX-[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"\b1//[A-Za-z0-9._~-]{20,}\b"),
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35,}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
)
_UNTAGGED_HIGH_ENTROPY = re.compile(r"[A-Za-z0-9_-]{48,}")
_OPEN_VALUE_CHANNELS = (
    ".error_code",
    ".external_ref",
    ".provider_refs",
    ".provider_binding_ref",
    ".provider_subject",
    ".granted_scopes",
)
_SECRET_BINDING_REF = re.compile(r"[A-Z][A-Z0-9_]{0,127}")


def _normalise_key(value: object) -> str:
    return str(value).strip().lower().replace("-", "_")


def reject_secret_fields(value: Any, *, path: str = "$") -> None:
    """Reject secret-shaped keys and credential values before serialization."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalised = _normalise_key(key)
            if normalised in SECRET_FIELD_NAMES or normalised.endswith("_password"):
                raise SecretFieldError(f"secret-like field is forbidden at {path}.{key}")
            if normalised.endswith("_token") or normalised.endswith("_secret"):
                raise SecretFieldError(f"secret-like field is forbidden at {path}.{key}")
            reject_secret_fields(item, path=f"{path}.{key}")
        return
    if isinstance(value, str):
        open_value_channel = any(channel in path for channel in _OPEN_VALUE_CHANNELS)
        if (
            path.endswith(".secret_binding_ref")
            and value
            and not _SECRET_BINDING_REF.fullmatch(value)
        ):
            raise SecretFieldError(f"invalid secret binding reference at {path}")
        if any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS) or (
            open_value_channel and _UNTAGGED_HIGH_ENTROPY.fullmatch(value)
        ):
            raise SecretFieldError(f"credential-like value is forbidden at {path}")
        if open_value_channel and value[:1] in {"{", "["}:
            try:
                nested = json.loads(value)
            except (TypeError, ValueError):
                nested = None
            if isinstance(nested, (Mapping, list)):
                # Provider references are often encoded as canonical JSON strings.
                # Keep the parent channel in the path so nested opaque values receive
                # the same credential-shape checks as a normal structured result.
                reject_secret_fields(nested, path=f"{path}.$json")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            reject_secret_fields(item, path=f"{path}[{index}]")


def canonical_json(value: Any) -> bytes:
    reject_secret_fields(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def normalize_email(value: str) -> str:
    """NFKC-normalize an address and case-fold its domain for lookup."""
    normalized = unicodedata.normalize("NFKC", value).strip()
    local, separator, domain = normalized.rpartition("@")
    if not separator or not local or not domain or "@" in local:
        raise ValueError("invalid email address")
    domain = unicodedata.normalize("NFKC", domain).casefold().rstrip(".")
    return f"{local}@{domain}"


@dataclass(frozen=True)
class EncryptedField:
    ciphertext: bytes
    key_version: str


class FieldCipher:
    """AES-256-GCM with random nonces and caller-supplied stable AAD."""

    def __init__(self, keys: Mapping[str, bytes], active_version: str) -> None:
        self._keys = dict(keys)
        self.active_version = active_version
        if active_version not in self._keys:
            raise UnknownKeyVersion(active_version)
        if any(len(key) != 32 for key in self._keys.values()):
            raise CryptoError("every field encryption key must contain 32 bytes")

    def encrypt_bytes(
        self, plaintext: bytes, *, aad: str, key_version: str | None = None
    ) -> EncryptedField:
        nonce = os.urandom(12)
        version = key_version or self.active_version
        key = self._keys.get(version)
        if key is None:
            raise UnknownKeyVersion(version)
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad.encode("utf-8"))
        return EncryptedField(nonce + ciphertext, version)

    def decrypt_bytes(self, field: EncryptedField, *, aad: str) -> bytes:
        key = self._keys.get(field.key_version)
        if key is None:
            raise UnknownKeyVersion(field.key_version)
        if len(field.ciphertext) < 29:
            raise CryptoError("ciphertext is truncated")
        nonce, ciphertext = field.ciphertext[:12], field.ciphertext[12:]
        try:
            return AESGCM(key).decrypt(nonce, ciphertext, aad.encode("utf-8"))
        except InvalidTag as error:
            raise CryptoError("ciphertext authentication failed") from error

    def encrypt_json(
        self, value: Any, *, aad: str, key_version: str | None = None
    ) -> EncryptedField:
        return self.encrypt_bytes(canonical_json(value), aad=aad, key_version=key_version)

    def decrypt_json(self, field: EncryptedField, *, aad: str) -> Any:
        try:
            return json.loads(self.decrypt_bytes(field, aad=aad))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CryptoError("decrypted value is not valid JSON") from error


#: Labelled so a later scheme can coexist with this one through a rotation,
#: and so a digest computed for one purpose can never collide with another
#: derived from the same root.
RELAY_KEY_LABEL = "abrolia-relay-key-v1:"


def sender_hmac(sender: str, gateway_key: bytes) -> str:
    """What a strict-mode gateway looks a sender up by.

    Here rather than in `gateway/` because BOTH sides compute it — the gateway
    to resolve an incoming sender, the control plane to write
    `channel_bindings.external_id_hmac` — and two implementations of one
    lookup is how the two ends of a keyed comparison drift apart. C5a was
    exactly that failure for the relay signature.

    The gateway already imports from `control_plane`, so this is the direction
    that keeps: shared primitives here, the gateway importing them.
    """
    return hmac.new(gateway_key, sender.encode(), hashlib.sha256).hexdigest()


def derive_relay_secret(relay_root: bytes, household_id: str) -> str:
    """The household's relay secret, derived rather than stored.

    Returns the SECRET AS INSTALLED — a hex string — and not raw key bytes,
    because the representation is the contract and leaving it implicit is how
    the two ends drift. The runtime reads this out of an environment variable
    and signs with `secret.encode()` (`hermes_cloud/ingest/whatsapp_webhook.py`),
    so the HMAC key material is the ASCII of this string at BOTH ends. A caller
    holding raw bytes and a caller holding the hex of those bytes compute
    different signatures while each looks correct on its own — which is exactly
    the C5a failure this slice cites, and which an earlier draft of this
    function reproduced by returning `bytes`.

    Both ends need the same value: the control plane installs it into the
    runtime as `HERMES_WHATSAPP_RELAY_SECRET`, and the gateway signs each
    delivery with it. Deriving both from one root is not a preference, it is
    what the two surrounding facts leave.

    `FlySecretSink` can install, test for presence and delete, and cannot read
    — Fly secrets are write-only, which is most of what makes them safe. And
    the gateway is constructed with the database and its keys and nothing
    else; it holds no field cipher by design, so a key stored encrypted beside
    the binding would need a read path that does not exist and should not be
    built for the layer that resolves senders.

    So neither end can fetch the other's copy, and both can derive it. Storage
    disappears, and rotation becomes one root change rather than a
    per-household migration.

    Per HOUSEHOLD and not per revision: a re-provisioning installs the same
    value, so a rollout does not invalidate a delivery the gateway is part-way
    through signing.
    """
    return hmac.new(
        relay_root, (RELAY_KEY_LABEL + household_id).encode(), hashlib.sha256
    ).hexdigest()


class LookupHasher:
    """Separate keyed HMAC for equality lookup; never reuse the AES key."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise CryptoError("lookup HMAC key must contain 32 bytes")
        self._key = key

    def digest(self, value: str | bytes) -> str:
        raw = value.encode("utf-8") if isinstance(value, str) else value
        return hmac.new(self._key, raw, hashlib.sha256).hexdigest()

    def email(self, address: str) -> str:
        return self.digest(normalize_email(address).encode("utf-8"))


@dataclass
class SecretMaterial:
    """Short-lived mutable secret bytes that cannot be represented or serialized."""

    _values: dict[str, bytearray] = field(default_factory=dict, repr=False)

    @classmethod
    def from_mapping(cls, values: Mapping[str, str | bytes | bytearray]) -> SecretMaterial:
        return cls({
            str(name): bytearray(value.encode() if isinstance(value, str) else value)
            for name, value in values.items()
        })

    def __repr__(self) -> str:
        return "SecretMaterial(<redacted>)"

    def __str__(self) -> str:
        return "<redacted secret material>"

    def __getstate__(self) -> NoReturn:
        raise TypeError("SecretMaterial cannot be serialized")

    def items(self) -> list[tuple[str, memoryview]]:
        return [(name, memoryview(value)) for name, value in self._values.items()]

    def stage_companion(self, name: str, value: bytes) -> None:
        """Add a NON-secret entry that must be installed with this material.

        The generation marker for an email secret handoff is the only user: it
        has to reach the sink in the SAME installation as the credential it
        describes, so that no crash window can leave a marker attesting a
        credential that is not there, or a credential no later proof can
        recognise.

        Named for what it is. Everything else in here is secret bytes, and a
        caller adding a real secret through this door would be defeating the
        one-name validation the email path performs before installing.
        """
        self._values[str(name)] = bytearray(value)

    @property
    def is_empty(self) -> bool:
        return not self._values

    def clear(self) -> None:
        for value in self._values.values():
            value[:] = b"\x00" * len(value)
        self._values.clear()

    def __enter__(self) -> SecretMaterial:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.clear()

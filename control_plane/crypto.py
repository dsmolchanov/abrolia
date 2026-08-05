"""Field encryption, deterministic lookup and secret-boundary validation."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
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
    "access_token",
    "bootstrap_token",
    "client_secret",
    "nerve_bootstrap_key",
    "nerve_runtime_key",
    "password",
    "refresh_token",
    "secret",
    "secret_material",
    "token",
})


def _normalise_key(value: object) -> str:
    return str(value).strip().lower().replace("-", "_")


def reject_secret_fields(value: Any, *, path: str = "$") -> None:
    """Reject secret-shaped keys recursively before any durable serialization."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalised = _normalise_key(key)
            if normalised in SECRET_FIELD_NAMES or normalised.endswith("_password"):
                raise SecretFieldError(f"secret-like field is forbidden at {path}.{key}")
            if normalised.endswith("_token") or normalised.endswith("_secret"):
                raise SecretFieldError(f"secret-like field is forbidden at {path}.{key}")
            reject_secret_fields(item, path=f"{path}.{key}")
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

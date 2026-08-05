from __future__ import annotations

import json
import pickle

import pytest

from control_plane.crypto import (
    CryptoError,
    EncryptedField,
    FieldCipher,
    LookupHasher,
    SecretFieldError,
    SecretMaterial,
    UnknownKeyVersion,
    canonical_json,
    normalize_email,
    reject_secret_fields,
)


def test_equal_emails_have_one_lookup_digest_but_random_ciphertext() -> None:
    cipher = FieldCipher({"v1": b"e" * 32}, "v1")
    lookup = LookupHasher(b"l" * 32)
    first_email = normalize_email(" Owner@FAMILY.TEST. ")
    second_email = normalize_email("Owner@family.test")

    assert first_email == second_email
    assert lookup.email(first_email) == lookup.email(second_email)

    first = cipher.encrypt_json(first_email, aad="accounts:first:recovery_email")
    second = cipher.encrypt_json(first_email, aad="accounts:first:recovery_email")
    assert first.ciphertext != second.ciphertext
    assert cipher.decrypt_json(first, aad="accounts:first:recovery_email") == first_email
    assert cipher.decrypt_json(second, aad="accounts:first:recovery_email") == first_email


def test_tamper_wrong_aad_and_wrong_key_fail_closed() -> None:
    cipher = FieldCipher({"v1": b"a" * 32}, "v1")
    encrypted = cipher.encrypt_bytes(b"synthetic-pii", aad="table:row:field")
    tampered = bytearray(encrypted.ciphertext)
    tampered[-1] ^= 1

    with pytest.raises(CryptoError, match="authentication failed"):
        cipher.decrypt_bytes(
            EncryptedField(bytes(tampered), encrypted.key_version),
            aad="table:row:field",
        )
    with pytest.raises(CryptoError, match="authentication failed"):
        cipher.decrypt_bytes(encrypted, aad="table:other-row:field")
    with pytest.raises(UnknownKeyVersion):
        cipher.decrypt_bytes(
            EncryptedField(encrypted.ciphertext, "retired-v0"),
            aad="table:row:field",
        )
    wrong_key = FieldCipher({"v1": b"b" * 32}, "v1")
    with pytest.raises(CryptoError, match="authentication failed"):
        wrong_key.decrypt_bytes(encrypted, aad="table:row:field")


@pytest.mark.parametrize(
    "payload",
    [
        {"token": "secret-canary"},
        {"nested": [{"refresh-token": "secret-canary"}]},
        {"credentials": {"mail_password": "secret-canary"}},
        {"provider": {"client_secret": "secret-canary"}},
    ],
)
def test_recursive_secret_field_validator_rejects_every_depth(payload: object) -> None:
    with pytest.raises(SecretFieldError):
        reject_secret_fields(payload)
    with pytest.raises(SecretFieldError):
        canonical_json(payload)


def test_canonical_json_is_stable_and_strict() -> None:
    first = canonical_json({"z": [2, 1], "a": {"b": "✓"}})
    second = canonical_json({"a": {"b": "✓"}, "z": [2, 1]})
    assert first == second == '{"a":{"b":"✓"},"z":[2,1]}'.encode()
    assert json.loads(first) == {"a": {"b": "✓"}, "z": [2, 1]}
    with pytest.raises(ValueError):
        canonical_json({"not_a_number": float("nan")})


def test_secret_material_is_redacted_nonserializable_and_zeroized() -> None:
    material = SecretMaterial.from_mapping({"PROVIDER_API_KEY": "secret-canary"})
    view = dict(material.items())["PROVIDER_API_KEY"]

    assert "secret-canary" not in repr(material)
    assert "secret-canary" not in str(material)
    with pytest.raises(TypeError, match="cannot be serialized"):
        pickle.dumps(material)

    material.clear()
    assert bytes(view) == b"\x00" * len("secret-canary")
    assert material.is_empty

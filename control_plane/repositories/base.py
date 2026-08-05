"""Shared repository helpers; repositories never expose encryption keys."""

from __future__ import annotations

import json
from typing import Any

from control_plane.crypto import EncryptedField, FieldCipher, LookupHasher, canonical_json
from control_plane.db import ControlPlaneDatabase


class Repository:
    def __init__(
        self,
        database: ControlPlaneDatabase,
        cipher: FieldCipher,
        lookup: LookupHasher,
    ) -> None:
        self.db = database
        self.cipher = cipher
        self.lookup = lookup

    @staticmethod
    def aad(table: str, row_id: str, field: str) -> str:
        return f"{table}:{row_id}:{field}"

    def encrypt_json(
        self,
        table: str,
        row_id: str,
        field: str,
        value: Any,
        *,
        key_version: str | None = None,
    ) -> EncryptedField:
        return self.cipher.encrypt_json(
            value,
            aad=self.aad(table, row_id, field),
            key_version=key_version,
        )

    def decrypt_json(
        self,
        table: str,
        row_id: str,
        field: str,
        ciphertext: bytes,
        key_version: str,
    ) -> Any:
        return self.cipher.decrypt_json(
            EncryptedField(bytes(ciphertext), key_version),
            aad=self.aad(table, row_id, field),
        )

    @staticmethod
    def public_json(value: Any) -> str:
        return canonical_json(value).decode("utf-8")

    @staticmethod
    def parse_public_json(value: str | bytes | None) -> Any:
        return json.loads(value or "{}")

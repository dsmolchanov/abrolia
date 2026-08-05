"""Deterministic TOML wire encoding for the versioned runtime manifest."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from control_plane.provisioning.manifest import (
    DesiredHouseholdSpecV1,
    manifest_sha256,
)


def _string(value: str) -> str:
    """Encode a TOML basic string using JSON's compatible escaping rules."""
    return json.dumps(value, ensure_ascii=False)


def _array(values: list[str] | tuple[str, ...]) -> str:
    return "[" + ", ".join(_string(value) for value in values) + "]"


def manifest_to_toml(value: DesiredHouseholdSpecV1 | Mapping[str, Any]) -> str:
    """Return the byte-stable v1 ``household.toml`` representation.

    The control plane authenticates canonical JSON, while the runtime consumes
    TOML. Validating first and writing fields in a fixed order ensures that
    parsing this representation yields the exact document that was hashed.
    """
    spec = (
        value
        if isinstance(value, DesiredHouseholdSpecV1)
        else DesiredHouseholdSpecV1.model_validate(dict(value))
    )
    document = spec.model_dump(mode="json", exclude_none=True)
    digest = manifest_sha256(document)
    if document["config_sha256"] != digest:
        raise ValueError("manifest hash does not match its canonical content")

    lines = [
        f'schema_version = {document["schema_version"]}',
        f'household_id = {_string(document["household_id"])}',
        f'config_revision = {document["config_revision"]}',
        f'config_sha256 = {_string(document["config_sha256"])}',
        f'family_language = {_string(document["family_language"])}',
        f'timezone = {_string(document["timezone"])}',
        f'country_code = {_string(document["country_code"])}',
        f'residency_mode = {_string(document["residency_mode"])}',
        "",
        "[actors]",
        f'owner = {_string(document["actors"]["owner"])}',
        f'family = {_array(document["actors"]["family"])}',
        f'guests = {_array(document["actors"]["guests"])}',
        "",
        "[channels]",
        f'primary = {_string(document["channels"]["primary"])}',
    ]
    for binding in document["channel_bindings"]:
        lines.extend(
            [
                "",
                "[[channel_bindings]]",
                f'channel = {_string(binding["channel"])}',
                f'actor_id = {_string(binding["actor_id"])}',
                f'chat_id = {_string(binding["chat_id"])}',
                "verified = true",
            ]
        )
        if binding.get("external_ref") is not None:
            lines.append(f'external_ref = {_string(binding["external_ref"])}')
    lines.extend(
        [
            "",
            "[email]",
            f'agent_inbox = {_string(document["email"]["agent_inbox"])}',
            f'fallback = {_string(document["email"]["fallback"])}',
            f'provider_kind = {_string(document["email"]["provider_kind"])}',
        ]
    )
    for field in ("provider_binding_ref", "secret_binding_ref"):
        if document["email"].get(field) is not None:
            lines.append(f'{field} = {_string(document["email"][field])}')
    lines.extend(
        [
            "",
            "[consent]",
            f'authority = {_string(document["consent"]["authority"])}',
            f'enforcement = {_string(document["consent"]["enforcement"])}',
            f'required_purposes = {_array(document["consent"]["required_purposes"])}',
        ]
    )
    for receipt in document["consent"]["receipts"]:
        lines.extend(
            [
                "",
                "[[consent.receipts]]",
                f'receipt_id = {_string(receipt["receipt_id"])}',
                f'purpose = {_string(receipt["purpose"])}',
                f'text_version = {_string(receipt["text_version"])}',
                f'text_sha256 = {_string(receipt["text_sha256"])}',
            ]
        )
    lines.extend(
        [
            "",
            "[provider_refs]",
        ]
    )
    lines.extend(
        f"{_string(key)} = {_string(document['provider_refs'][key])}"
        for key in sorted(document["provider_refs"])
    )
    return "\n".join(lines) + "\n"

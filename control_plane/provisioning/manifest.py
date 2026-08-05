from __future__ import annotations

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.crypto import canonical_json, normalize_email, reject_secret_fields


class ActorsV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    owner: str
    family: tuple[str, ...] = ()
    guests: tuple[str, ...] = ()


class ChannelsV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    primary: Literal["telegram", "whatsapp", "web"]


class ChannelBindingV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    channel: Literal["telegram", "whatsapp", "web"]
    actor_id: str
    chat_id: str
    verified: Literal[True] = True
    external_ref: str | None = None


class EmailV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    agent_inbox: str
    fallback: str


class ConsentReceiptV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    receipt_id: str = Field(min_length=1, max_length=128)
    purpose: Literal[
        "whatsapp_channel_privacy", "whatsapp_linked_device_risk"
    ]
    text_version: str = Field(min_length=1, max_length=64)
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ConsentAuthorityV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    authority: Literal["control_plane"] = "control_plane"
    enforcement: Literal["required"] = "required"
    required_purposes: tuple[
        Literal["whatsapp_channel_privacy", "whatsapp_linked_device_risk"], ...
    ]
    receipts: tuple[ConsentReceiptV1, ...]

    @model_validator(mode="after")
    def validate_receipts(self) -> ConsentAuthorityV1:
        purposes = {receipt.purpose for receipt in self.receipts}
        if not self.required_purposes or not set(self.required_purposes) <= purposes:
            raise ValueError("every required consent purpose needs a receipt")
        if len({receipt.receipt_id for receipt in self.receipts}) != len(self.receipts):
            raise ValueError("consent receipt IDs must be unique")
        return self


class DesiredHouseholdSpecV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    household_id: str
    config_revision: int = Field(gt=0)
    config_sha256: str = ""
    family_language: str
    timezone: str
    country_code: str
    residency_mode: Literal["eu-app", "eu-strict"]
    actors: ActorsV1
    channels: ChannelsV1
    channel_bindings: tuple[ChannelBindingV1, ...]
    email: EmailV1
    consent: ConsentAuthorityV1
    provider_refs: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_contract(self) -> DesiredHouseholdSpecV1:
        dumped = self.model_dump(mode="json")
        reject_secret_fields(dumped)
        matches = [
            binding
            for binding in self.channel_bindings
            if binding.channel == self.channels.primary and binding.verified
        ]
        if not matches:
            raise ValueError("primary channel needs a verified binding")
        if all(binding.actor_id != self.actors.owner for binding in matches):
            raise ValueError("runtime owner must come from a verified channel binding")
        if normalize_email(self.email.agent_inbox) == normalize_email(self.email.fallback):
            raise ValueError("agent inbox cannot be the recovery-email fallback")
        return self

    def with_hash(self) -> DesiredHouseholdSpecV1:
        payload = self.model_dump(mode="json")
        payload.pop("config_sha256", None)
        digest = hashlib.sha256(canonical_json(payload)).hexdigest()
        return self.model_copy(update={"config_sha256": digest})

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.model_dump(mode="json"))


def manifest_sha256(value: DesiredHouseholdSpecV1 | dict[str, Any]) -> str:
    payload = (
        value.model_dump(mode="json")
        if isinstance(value, DesiredHouseholdSpecV1)
        else dict(value)
    )
    claimed = payload.pop("config_sha256", "")
    digest = hashlib.sha256(canonical_json(payload)).hexdigest()
    if claimed and claimed != digest:
        raise ValueError("manifest hash does not match its canonical content")
    return digest

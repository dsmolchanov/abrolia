from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from control_plane.crypto import reject_secret_fields


class EmailOption(StrEnum):
    MANAGED_ABROLIA = "managed_abrolia"
    GMAIL = "gmail"
    OWN_DOMAIN = "own_domain"

    @classmethod
    def from_selection(cls, kind: str) -> EmailOption:
        return cls({
            "abrolia_managed": cls.MANAGED_ABROLIA,
            "gmail_agent": cls.GMAIL,
            "family_domain": cls.OWN_DOMAIN,
        }[kind])


class EmailIdentityStatus(StrEnum):
    SELECTED = "selected"
    PROVISIONING = "provisioning"
    WAITING_USER = "waiting_user"
    VERIFIED = "verified"
    ACTIVATING = "activating"
    ACTIVE = "active"
    NEEDS_ATTENTION = "needs_attention"
    DISCONNECTING = "disconnecting"
    DELETED = "deleted"
    OUTCOME_UNKNOWN = "outcome_unknown"


class EmailProvisionIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    identity_id: str
    household_id: str
    option: EmailOption
    selection: dict[str, Any]
    secret_namespace_ref: str

    def model_post_init(self, _context: Any) -> None:
        reject_secret_fields(self.model_dump(mode="json"))


class EmailPublicBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    address: str
    provider: str
    provider_refs: dict[str, str] = Field(default_factory=dict)
    secret_binding_ref: str | None = None
    granted_scopes: tuple[str, ...] = ()

    def model_post_init(self, _context: Any) -> None:
        reject_secret_fields(self.model_dump(mode="json"))


@dataclass(frozen=True)
class EmailIdentityRecord:
    id: str
    household_id: str
    option: EmailOption
    status: EmailIdentityStatus
    address: str | None
    address_masked: str | None
    provider_subject: str | None
    provider_resource_refs: dict[str, str]
    secret_binding_ref: str | None
    granted_scopes: tuple[str, ...]
    version: int
    verified_at: float | None
    activated_at: float | None
    disconnected_at: float | None

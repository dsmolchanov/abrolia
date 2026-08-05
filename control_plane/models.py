"""Typed control-plane contracts and fail-closed schema classification."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from control_plane.crypto import reject_secret_fields

_SYNTHETIC_ACTOR_OR_CHAT = re.compile(
    r"^synthetic-[a-z0-9](?:[a-z0-9._-]{0,126})$"
)
_SYNTHETIC_PROVIDER_REF = re.compile(
    r"^(?:synthetic(?::|-)[a-z0-9][a-z0-9._:-]*|abrolia-hh-[a-z2-7]{26})$"
)


def _require_synthetic_actor_or_chat(value: str) -> str:
    if not _SYNTHETIC_ACTOR_OR_CHAT.fullmatch(value):
        raise ValueError("Phase 1 actor and chat IDs must use the synthetic- namespace")
    return value


def _require_synthetic_provider_ref(value: str) -> str:
    if not _SYNTHETIC_PROVIDER_REF.fullmatch(value):
        raise ValueError("Phase 1 provider references must use an allowed synthetic namespace")
    return value


class AccountStatus(StrEnum):
    ACTIVE = "active"
    LOCKED = "locked"
    DELETING = "deleting"
    DELETED = "deleted"


class HouseholdStatus(StrEnum):
    DRAFT = "draft"
    ONBOARDING = "onboarding"
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    DELETING = "deleting"
    DELETED = "deleted"


class WorkflowState(StrEnum):
    PROFILE_REQUIRED = "profile_required"
    IN_PROGRESS = "in_progress"
    RUNTIME_PROVISIONING = "runtime_provisioning"
    ACTIVATING = "activating"
    COMPLETE = "complete"
    CANCELLED = "cancelled"


class StepKind(StrEnum):
    PROFILE = "profile"
    EMAIL = "email_identity"
    WHATSAPP = "whatsapp_identity"
    PRIMARY_CHANNEL = "primary_channel"
    RUNTIME = "runtime"


USER_STEPS = (StepKind.EMAIL, StepKind.WHATSAPP, StepKind.PRIMARY_CHANNEL)


class StepStatus(StrEnum):
    LOCKED = "locked"
    AVAILABLE = "available"
    SELECTED = "selected"
    PROVISIONING = "provisioning"
    WAITING_USER = "waiting_user"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    CANCELLED = "cancelled"


class ConfigRevisionStatus(StrEnum):
    PLANNED = "planned"
    ISSUED = "issued"
    CLAIMED = "claimed"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"


class ResidencyMode(StrEnum):
    EU_APP = "eu-app"
    EU_STRICT = "eu-strict"


class DurableContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def _contains_no_secret_fields(self) -> DurableContract:
        reject_secret_fields(self.model_dump(mode="json"))
        return self


class ProfileInput(DurableContract):
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    family_language: str = Field(min_length=2, max_length=35)
    timezone: str = Field(min_length=1, max_length=64)
    country_code: str = Field(pattern=r"^[A-Z]{2}$")
    residency_mode: ResidencyMode = ResidencyMode.EU_APP

    @field_validator("first_name", "last_name", "family_language")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return value.strip()


class ManagedEmailSelection(DurableContract):
    kind: Literal["abrolia_managed"] = "abrolia_managed"
    local_part: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")


class GmailAgentSelection(DurableContract):
    kind: Literal["gmail_agent"] = "gmail_agent"
    separate_agent_account_acknowledged: Literal[True]


class FamilyDomainSelection(DurableContract):
    kind: Literal["family_domain"] = "family_domain"
    domain: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])$")

    @field_validator("domain")
    @classmethod
    def _reserved_test_domain_only(cls, value: str) -> str:
        if not value.endswith(".test"):
            raise ValueError("Phase 1 custom email domains must use reserved .test")
        return value


EmailSelection = Annotated[
    ManagedEmailSelection | GmailAgentSelection | FamilyDomainSelection,
    Field(discriminator="kind"),
]


class SharedWhatsAppSelection(DurableContract):
    kind: Literal["shared_abrolia"] = "shared_abrolia"
    member_phone_test_ref: str = Field(pattern=r"^synthetic-phone:[a-z0-9-]+$")
    privacy_notice_receipt_id: str


class DedicatedWhatsAppSelection(DurableContract):
    kind: Literal["dedicated_number"] = "dedicated_number"
    phone_test_ref: str = Field(pattern=r"^synthetic-phone:[a-z0-9-]+$")
    privacy_notice_receipt_id: str
    linked_device_risk_receipt_id: str


WhatsAppSelection = Annotated[
    SharedWhatsAppSelection | DedicatedWhatsAppSelection,
    Field(discriminator="kind"),
]


class PrimaryChannelSelection(DurableContract):
    kind: Literal["telegram", "whatsapp", "web"]
    actor_id: str = Field(min_length=1, max_length=128)
    chat_id: str = Field(min_length=1, max_length=128)

    @field_validator("actor_id", "chat_id")
    @classmethod
    def _synthetic_id_only(cls, value: str) -> str:
        return _require_synthetic_actor_or_chat(value)


class ProviderPublicResult(DurableContract):
    external_ref: str = Field(min_length=1, max_length=256)
    public_result: dict[str, Any]
    verified: bool = True

    @field_validator("external_ref")
    @classmethod
    def _synthetic_external_ref_only(cls, value: str) -> str:
        return _require_synthetic_provider_ref(value)


class StepSnapshot(DurableContract):
    kind: StepKind
    ordinal: int
    status: StepStatus
    selection_kind: str | None = None
    public_status: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None


class OnboardingSnapshot(DurableContract):
    household_id: str
    workflow_id: str
    version: int
    state: WorkflowState
    current_step: StepKind
    synthetic_only: Literal[True] = True
    steps: tuple[StepSnapshot, ...]


@dataclass(frozen=True)
class TableClassification:
    export: bool
    delete: bool
    retention: str
    reason: str = ""


# Every table in 0001_control_plane.sql must be listed here. Tests compare this
# registry with sqlite_master so adding a table without a privacy decision fails.
TABLE_CLASSIFICATION: dict[str, TableClassification] = {
    "schema_migrations": TableClassification(False, False, "service", "schema versions"),
    "accounts": TableClassification(True, True, "account+30d"),
    "households": TableClassification(True, True, "account+30d"),
    "household_profiles": TableClassification(True, True, "account+30d"),
    "household_memberships": TableClassification(True, True, "account+30d"),
    "auth_tokens": TableClassification(False, True, "used-or-expired+24h", "credential hash"),
    "sessions": TableClassification(False, True, "revoked-or-expired+30d", "credential hash"),
    "rate_limit_buckets": TableClassification(False, True, "24h", "coarse security metadata"),
    "onboarding_workflows": TableClassification(True, True, "account+30d"),
    "onboarding_steps": TableClassification(True, True, "account+30d"),
    "onboarding_transitions": TableClassification(True, True, "account+30d"),
    "idempotency_requests": TableClassification(False, True, "24h", "request replay metadata"),
    "provisioning_jobs": TableClassification(True, True, "payload-30d;metadata-90d"),
    "external_resources": TableClassification(True, True, "account+30d"),
    "config_revisions": TableClassification(True, True, "account+30d"),
    "bootstrap_tokens": TableClassification(False, True, "payload-30d;metadata-90d", "token hash"),
    "consent_receipts": TableClassification(True, False, "withdrawal+3y", "accountability"),
    "deletion_tombstones": TableClassification(True, False, "3y", "anti-resurrection"),
}

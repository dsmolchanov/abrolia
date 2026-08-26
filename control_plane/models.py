"""Typed control-plane contracts and fail-closed schema classification."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from control_plane.crypto import reject_secret_fields
from control_plane.email.domain_policy import canonicalize_domain, domain_guidance
from control_plane.email.local_part import normalize_local_part

_SYNTHETIC_ACTOR_OR_CHAT = re.compile(
    r"^synthetic-[a-z0-9](?:[a-z0-9._-]{0,126})$"
)
_SYNTHETIC_PROVIDER_REF = re.compile(
    r"^(?:synthetic(?::|-)[a-z0-9][a-z0-9._:-]*|abrolia-hh-[a-z2-7]{26})$"
)
_UUID_TEXT_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
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


class SpecialCategoryConsent(DurableContract):
    """Receipts every email selection may carry.

    The restriction bounds what may be sent at all; the household consent is the
    Art 9(2)(a) condition and is required only in a real-email rollout
    (docs/privacy/lawful-bases.md section 3).
    """

    special_category_restriction_acknowledged: Literal[True] | None = None
    special_category_restriction_receipt_id: str | None = Field(
        default=None, min_length=36, max_length=36, pattern=_UUID_TEXT_PATTERN
    )
    special_category_restriction_text_version: str | None = None
    special_category_restriction_text_sha256: str | None = None
    special_category_household_consent: Literal[True] | None = None
    special_category_household_receipt_id: str | None = Field(
        default=None, min_length=36, max_length=36, pattern=_UUID_TEXT_PATTERN
    )
    special_category_household_text_version: str | None = None
    special_category_household_text_sha256: str | None = None


class ManagedEmailSelection(SpecialCategoryConsent):
    kind: Literal["abrolia_managed"] = "abrolia_managed"
    local_part: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")


class GmailAgentSelection(SpecialCategoryConsent):
    kind: Literal["gmail_agent"] = "gmail_agent"
    separate_agent_account_acknowledged: Literal[True]


class FamilyDomainSelection(SpecialCategoryConsent):
    kind: Literal["family_domain"] = "family_domain"
    domain: str = Field(min_length=3, max_length=253)
    local_part: str = "assistant"
    mx_change_acknowledged: bool = False

    @field_validator("domain")
    @classmethod
    def _canonical_domain(cls, value: str, info: ValidationInfo) -> str:
        allow_real = bool(
            isinstance(info.context, dict)
            and info.context.get("allow_real_email_domains")
        )
        canonical = canonicalize_domain(value, allow_test=not allow_real)
        if not allow_real and not canonical.endswith(".test"):
            raise ValueError("real email domains are disabled by the rollout gate")
        return canonical

    @field_validator("local_part")
    @classmethod
    def _canonical_local_part(cls, value: str) -> str:
        return normalize_local_part(value)

    @model_validator(mode="after")
    def _acknowledge_apex_mx_risk(self) -> FamilyDomainSelection:
        if domain_guidance(self.domain).apex_mx_risk and not self.mx_change_acknowledged:
            raise ValueError("apex MX changes require explicit acknowledgement")
        return self


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
    "email_identities": TableClassification(True, True, "account+30d"),
    "email_address_reservations": TableClassification(
        True, True, "released-or-expired+24h"
    ),
    "oauth_transactions": TableClassification(
        False, True, "consumed-or-expired+24h", "ephemeral OAuth security metadata"
    ),
    "email_activation_receipts": TableClassification(True, True, "account+30d"),
    "email_secret_installs": TableClassification(
        False, True, "account+30d", "non-secret install receipt (name + job id only)"
    ),
    "channel_preferences": TableClassification(True, True, "account+30d"),
    "channel_bindings": TableClassification(True, True, "account+30d"),
    "channel_binding_challenges": TableClassification(
        False,
        True,
        "consumed-or-expired+24h",
        "ephemeral join credential; its durable outcome is channel_bindings",
    ),
}

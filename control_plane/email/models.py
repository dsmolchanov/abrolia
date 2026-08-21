from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from control_plane.crypto import normalize_email, reject_secret_fields
from control_plane.models import EmailSelection

_EMAIL_SELECTION_ADAPTER = TypeAdapter(EmailSelection)
_UUID_TEXT_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_SYNTHETIC_ID_PATTERN = (
    r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}|"
    r"synthetic:identity:[A-Za-z0-9_-]{1,64})$"
)
SYNTHETIC_EMAIL_SECRET_BINDING = "ABROLIA_EMAIL_PROVIDER_KEY"
NERVE_EMAIL_SECRET_BINDING = "ABROLIA_NERVE_EMAIL_CREDENTIALS"
NERVE_EMAIL_SCOPES = ("nerve:email.read", "nerve:email.send")
GMAIL_EMAIL_SECRET_BINDING = "ABROLIA_GMAIL_OAUTH_GRANT"
GMAIL_EMAIL_SCOPES = tuple(sorted((
    "openid",
    "email",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
)))


class EmptyEmailProviderRefs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SyntheticEmailProviderRefs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    identity_id: str = Field(pattern=_SYNTHETIC_ID_PATTERN)


class NerveManagedEmailProviderRefs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    org_id: str = Field(pattern=_UUID_TEXT_PATTERN)
    grant_id: str = Field(pattern=_UUID_TEXT_PATTERN)
    inbox_id: str = Field(pattern=_UUID_TEXT_PATTERN)
    key_id: str = Field(pattern=_UUID_TEXT_PATTERN)
    webhook_id: str = Field(pattern=_UUID_TEXT_PATTERN)


class NerveByoEmailProviderRefs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    org_id: str = Field(pattern=_UUID_TEXT_PATTERN)
    domain_id: str = Field(pattern=_UUID_TEXT_PATTERN)
    inbox_id: str = Field(pattern=_UUID_TEXT_PATTERN)
    key_id: str = Field(pattern=_UUID_TEXT_PATTERN)
    webhook_id: str = Field(pattern=_UUID_TEXT_PATTERN)


class GmailEmailProviderRefs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    google_subject: str = Field(min_length=1, max_length=256)


EmailProviderRefs = (
    EmptyEmailProviderRefs
    | SyntheticEmailProviderRefs
    | NerveManagedEmailProviderRefs
    | NerveByoEmailProviderRefs
    | GmailEmailProviderRefs
)


class EmailOption(StrEnum):
    MANAGED_ABROLIA = "managed_abrolia"
    GMAIL = "gmail"
    OWN_DOMAIN = "own_domain"

    @classmethod
    def from_selection(cls, kind: str) -> EmailOption:
        return cls(_SELECTION_KINDS[kind])


#: Selection kind -> option, in the order the onboarding page offers them.
#:
#: Ordered and module-level so the page can enumerate the options instead of
#: hard-coding them a fourth time. `from_selection` reads the same mapping: the
#: page must not be able to offer a kind the server cannot resolve.
_SELECTION_KINDS = {
    "abrolia_managed": EmailOption.MANAGED_ABROLIA,
    "gmail_agent": EmailOption.GMAIL,
    "family_domain": EmailOption.OWN_DOMAIN,
}

EMAIL_SELECTION_KINDS = tuple(_SELECTION_KINDS)


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
        domain = self.selection.get("domain")
        typed_selection = _EMAIL_SELECTION_ADAPTER.validate_python(
            self.selection,
            context={
                "allow_real_email_domains": not (
                    isinstance(domain, str) and domain.casefold().rstrip(".").endswith(".test")
                )
            },
        )
        if EmailOption.from_selection(typed_selection.kind) is not self.option:
            raise ValueError("email option does not match its typed selection")
        object.__setattr__(
            self,
            "selection",
            typed_selection.model_dump(mode="json"),
        )
        reject_secret_fields(self.model_dump(mode="json"))


class EmailPublicBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_inbox: str
    provider: Literal["synthetic", "nerve", "gmail"] = "synthetic"
    provider_subject: str | None = Field(default=None, max_length=256)
    provider_refs: EmailProviderRefs = Field(default_factory=EmptyEmailProviderRefs)
    secret_binding_ref: str | None = None
    granted_scopes: tuple[str, ...] = Field(default=(), max_length=16)
    masked_external_ref: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9_-]{1,16}$"
    )
    mode: Literal["abrolia_managed", "gmail_agent", "family_domain"] | None = None

    @field_validator("agent_inbox")
    @classmethod
    def _canonical_address(cls, value: str) -> str:
        return normalize_email(value)

    @field_validator("granted_scopes")
    @classmethod
    def _bounded_scopes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not value
            or len(value) > 128
            or not all(character.isalnum() or character in ".:/_-" for character in value)
            for value in values
        ):
            raise ValueError("invalid provider scope")
        return values

    @model_validator(mode="after")
    def _provider_contract(self) -> EmailPublicBinding:
        refs = self.provider_refs
        if self.provider == "synthetic":
            if not isinstance(refs, (EmptyEmailProviderRefs, SyntheticEmailProviderRefs)):
                raise ValueError("synthetic provider returned non-synthetic references")
            if self.provider_subject is not None or self.granted_scopes:
                raise ValueError("synthetic provider returned unsupported authority fields")
            if self.secret_binding_ref not in {
                None,
                SYNTHETIC_EMAIL_SECRET_BINDING,
            }:
                raise ValueError("synthetic provider returned an invalid secret binding")
        elif self.provider == "nerve":
            if not isinstance(
                refs, (NerveManagedEmailProviderRefs, NerveByoEmailProviderRefs)
            ):
                raise ValueError("Nerve provider returned an invalid reference set")
            if self.provider_subject != refs.org_id:
                raise ValueError("Nerve provider subject does not match its organization")
            if self.secret_binding_ref != NERVE_EMAIL_SECRET_BINDING:
                raise ValueError("Nerve provider returned an invalid secret binding")
            if self.granted_scopes != NERVE_EMAIL_SCOPES:
                raise ValueError("Nerve provider returned an invalid scope set")
        elif self.provider == "gmail":
            if not isinstance(refs, GmailEmailProviderRefs):
                raise ValueError("Gmail provider returned an invalid reference set")
            if self.provider_subject != refs.google_subject:
                raise ValueError("Gmail subject does not match its reference")
            if self.secret_binding_ref != GMAIL_EMAIL_SECRET_BINDING:
                raise ValueError("Gmail provider returned an invalid secret binding")
            if self.granted_scopes != GMAIL_EMAIL_SCOPES:
                raise ValueError("Gmail provider returned an invalid scope set")
        return self

    def model_post_init(self, _context: Any) -> None:
        reject_secret_fields(self.model_dump(mode="json"))


class EmailGoogleOAuthPublicStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: Literal["oauth_required", "dedicated_account_confirmation"]
    disclosure: Literal[
        "Abrolia reads and sends mail only for this dedicated agent mailbox; "
        "Google data is not used to train a general model."
    ]
    connected_address_masked: str | None = Field(default=None, max_length=320)

    def model_post_init(self, _context: Any) -> None:
        reject_secret_fields(self.model_dump(mode="json"))


class EmailDnsRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: str = Field(pattern=r"^[A-Z][A-Z0-9]{0,15}$")
    host: str = Field(min_length=1, max_length=253)
    value: str = Field(min_length=1, max_length=4096)
    priority: int | None = Field(default=None, ge=0, le=65535)
    purpose: str | None = Field(default=None, max_length=512)
    required: bool = True

    def model_post_init(self, _context: Any) -> None:
        reject_secret_fields(self.model_dump(mode="json"))


class EmailDnsPublicStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: Literal["dns_required"] = "dns_required"
    domain: str = Field(min_length=3, max_length=253)
    dns_records: tuple[EmailDnsRecord, ...] = Field(min_length=1)
    record_status: dict[
        Literal["ownership", "mx", "spf", "dkim", "dmarc"], bool
    ] = Field(default_factory=dict)
    mx_change_warning: str | None = Field(default=None, max_length=256)

    def model_post_init(self, _context: Any) -> None:
        reject_secret_fields(self.model_dump(mode="json"))


class NerveAttachmentOperatorAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: Literal["nerve-flags"] = "nerve-flags"
    arguments: tuple[str, ...] = Field(min_length=5, max_length=5)
    audit_actor_required: Literal[True] = True

    def model_post_init(self, _context: Any) -> None:
        reject_secret_fields(self.model_dump(mode="json"))


class EmailNerveAttachmentPublicStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    readiness: Literal["attachments_flag_pending"] = "attachments_flag_pending"
    nerve_org_id: str = Field(pattern=_UUID_TEXT_PATTERN)
    operator_action: NerveAttachmentOperatorAction
    next_action: Literal[
        "enable the flag, wait for convergence, then check again"
    ] = "enable the flag, wait for convergence, then check again"

    @model_validator(mode="after")
    def _exact_operator_command(self) -> EmailNerveAttachmentPublicStatus:
        if self.operator_action.arguments != (
            "set",
            "attachments",
            "--org",
            self.nerve_org_id,
            "--enabled=true",
        ):
            raise ValueError("invalid Nerve attachment activation command")
        return self

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

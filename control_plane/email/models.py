from __future__ import annotations

import hashlib
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


# --- Generation-scoped secret handoff (B-02) -------------------------------

# The suffix that turns a binding name into its generation marker. The Fly
# secret sink can attest that a NAME exists and nothing whatever about the
# value behind it, so a generation that must be provable to that sink has to
# live in a name.
EMAIL_SECRET_GENERATION_MARKER_INFIX = "_GEN_"

# `SECRET_NAME` allows 128 characters. The longest binding is 31
# (`ABROLIA_NERVE_EMAIL_CREDENTIALS`), the infix is 5, so the digest is bounded
# well inside the limit while staying long enough that two generations do not
# collide.
EMAIL_SECRET_GENERATION_DIGEST_CHARS = 32


def email_secret_generation(job_id: str) -> str:
    """The generation a receipt and a marker must agree on.

    The provisioning job that installed the material IS the generation. It is
    stable across that job's own retries and reconciliations — which is what
    lets a crash after the sink write converge without an operator — and it is
    necessarily different for a later re-provisioning of the same identity,
    which is what stops generation N-1's surviving secret from answering for
    generation N.

    Deriving it from anything the provider chooses would let the party whose
    freshness is in question certify its own freshness. The control plane
    assigns it.
    """
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("a secret generation needs the job that installed it")
    return job_id


def email_secret_generation_marker(binding_ref: str, generation: str) -> str:
    """The secret NAME whose presence proves this generation was installed.

    Hashed rather than embedded: a job id is not constrained to the uppercase
    alphabet `SECRET_NAME` requires, and a name is the one part of a secret
    that leaks into `fly secrets list` output, operator terminals and support
    transcripts. The digest is not a confidentiality measure — a job id is not
    a secret — it is what makes the name well-formed and fixed-length.
    """
    if not isinstance(binding_ref, str) or not binding_ref:
        raise ValueError("a generation marker needs its binding")
    digest = hashlib.sha256(
        f"{binding_ref}\x00{email_secret_generation(generation)}".encode()
    ).hexdigest()[:EMAIL_SECRET_GENERATION_DIGEST_CHARS]
    return (
        f"{binding_ref}{EMAIL_SECRET_GENERATION_MARKER_INFIX}{digest.upper()}"
    )


def email_secret_sink_digest(namespace_ref: str, marker_name: str) -> str:
    """What the receipt claims the sink was holding, over NON-secret inputs.

    Explicitly not a digest of the secret value. No value reaches this process
    after installation, and a sink that cannot attest values cannot be asked to
    prove one. This digests what was actually attested — the namespace and the
    marker that carries the generation — so a receipt can be checked against
    the sink it describes rather than merely believed.
    """
    return hashlib.sha256(
        f"{namespace_ref}\x00{marker_name}".encode()
    ).hexdigest()


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

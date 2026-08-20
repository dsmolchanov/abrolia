"""Versioned consent text identifiers owned by the control plane."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

CONSENT_TEXTS = {
    # v2, 2026-08-19. v1 forbade special-category data "about any person",
    # which included the owner and their own minor children — precisely the
    # subjects the Art 9(2)(a) consent below authorises. A family could not obey
    # the restriction and use the consented feature at the same time, and the
    # contradiction also misstated the S5 boundary, which puts only THIRD-PARTY
    # special categories out of scope (docs/privacy/lawful-bases.md section 3).
    # The version bump invalidates v1 receipts by design: every enforcement
    # boundary compares the exact version and digest, so a household holding v1
    # must accept v2 before real content flows again.
    "special_category_content_restriction": (
        "special-category-content-restriction-v2",
        "Do not send or forward medical certificates, health or allergy data, "
        "religious beliefs, or other special-category personal data about "
        "anyone other than yourself and your own minor children, to the agent "
        "inbox or channel. Data about other people — other children, teachers, "
        "other parents — is outside the scope of this service and Abrolia has "
        "no lawful condition to process it. If such content is sent by "
        "mistake, stop using it and request deletion at help@abrolia.com. This "
        "acknowledgement does not transfer Abrolia's legal obligations.",
    ),
    # Art 9(2)(a) condition selected 2026-08-12; see docs/privacy/lawful-bases.md
    # section 3. Required only where real family content can actually arrive.
    "special_category_household_content": (
        "special-category-household-content-v1",
        "You give explicit consent under Art. 9(2)(a) GDPR for Abrolia to "
        "process special-category personal data — such as health information "
        "or religious observance — contained in the content you choose to send "
        "to the assistant inbox or channel, about yourself and about your own "
        "minor children, for whom you give this consent as a holder of "
        "parental responsibility. Identifying religion, beliefs, ideology or "
        "ethnic origin is never a purpose of the processing. This consent can "
        "be withdrawn at any time in one step at help@abrolia.com; withdrawal "
        "stops further processing and does not affect processing already "
        "carried out. It does not cover special-category data about anyone "
        "else.",
    ),
    "whatsapp_channel_privacy": (
        "whatsapp-channel-privacy-v1",
        "Shared or dedicated WhatsApp participants may expose channel metadata "
        "to the household runtime; Phase 1 uses synthetic identities only.",
    ),
    "whatsapp_linked_device_risk": (
        "whatsapp-linked-device-risk-v1",
        "Linked-device automation can expire, disconnect, or contribute to a "
        "number restriction; Phase 1 uses a synthetic number only.",
    ),
}


def consent_version_and_sha(purpose: str) -> tuple[str, str]:
    version, text = CONSENT_TEXTS[purpose]
    return version, hashlib.sha256(text.encode("utf-8")).hexdigest()


def consent_version_and_text(purpose: str) -> tuple[str, str]:
    """Return the exact versioned copy presented before a receipt is accepted."""
    return CONSENT_TEXTS[purpose]


#: What withdrawing each consent must actually terminate.
#:
#: `withdraw()` ran ONE teardown for every purpose: disconnect the email inbox,
#: revoke every active revision, stop the runtime. So withdrawing
#: `whatsapp_channel_privacy` deprovisioned the household's Gmail or Nerve inbox
#: and the mail stored in it, while never touching the WhatsApp resource the
#: consent was actually about. Art. 7(3) requires that withdrawing a consent
#: stop the processing it authorised — not processing it did not.
#:
#: `resource_types` names the `external_resources` rows to tear down.
#: `stops_runtime` says whether the household's runtime is told to stop, which
#: only the authoritative content consent warrants: the runtime processes
#: household content, and a channel-specific withdrawal does not end that.
#:
#: A purpose absent from this map is refused rather than defaulted, because
#: defaulting is what caused the damage — the safe default for "I do not know
#: what this terminates" is to do nothing and say so.
WITHDRAWAL_SCOPES: dict[str, dict[str, object]] = {
    # The Art 9(2)(a) condition is the lawful basis for processing household
    # content at all. Withdrawing it ends the processing, so everything goes.
    "special_category_household_content": {
        "resource_types": frozenset({"email_identity"}),
        "stops_runtime": True,
    },
    # The restriction acknowledgement is a condition ON content processing, and
    # the planner refuses to issue a revision without a current one. Withdrawing
    # it therefore stops content processing by the same route.
    "special_category_content_restriction": {
        "resource_types": frozenset({"email_identity"}),
        "stops_runtime": True,
    },
    # Channel-scoped. These consent to WhatsApp's metadata exposure and its
    # linked-device risk; neither authorises the inbox, and neither withdrawal
    # is a reason to stop the runtime serving the household's other channels.
    "whatsapp_channel_privacy": {
        "resource_types": frozenset({"whatsapp_identity", "channel_binding"}),
        "stops_runtime": False,
    },
    "whatsapp_linked_device_risk": {
        "resource_types": frozenset({"whatsapp_identity", "channel_binding"}),
        "stops_runtime": False,
    },
}


CONTENT_RESTRICTION_PURPOSE = "special_category_content_restriction"
HOUSEHOLD_CONTENT_PURPOSE = "special_category_household_content"

# Provider kinds that carry no personal data. The set is an ALLOWLIST so that an
# unrecognised provider counts as real and therefore requires the Art. 9(2)(a)
# consent — a new provider must be added here deliberately, and forgetting to
# blocks provisioning rather than opening it.
SYNTHETIC_EMAIL_PROVIDER_KINDS = frozenset({"fake-email", "synthetic"})

# One spelling of "the household holds this consent right now". Enforcement runs
# at four boundaries — planner, provisioning worker, bootstrap, runtime
# readiness — and three of them had grown their own copy of this predicate.
# Copies drift: the planner's omitted both `revoked_at IS NULL` and the version
# columns, so a revoked or superseded receipt still satisfied it.
CURRENT_RECEIPT_SQL = (
    "SELECT 1 FROM consent_receipts WHERE household_id = ? AND purpose = ?"
    " AND text_version = ? AND text_sha256 = ? AND revoked_at IS NULL LIMIT 1"
)


def current_receipt_params(household_id: str, purpose: str) -> tuple[str, str, str, str]:
    """Bind parameters for `CURRENT_RECEIPT_SQL` at the purpose's current text."""
    version, sha256 = consent_version_and_sha(purpose)
    return (household_id, purpose, version, sha256)


def processes_real_household_content(provider_kind: str | None) -> bool:
    """Whether this email provider can deliver real family content.

    `lawful-bases.md` section 3 scopes the Art. 9(2)(a) requirement to exactly
    this case: in the synthetic circuit there is no personal data and therefore
    no question of a condition.
    """
    if not provider_kind:
        return True
    return provider_kind not in SYNTHETIC_EMAIL_PROVIDER_KINDS


def required_consent_purposes(
    *, provider_kind: str | None, whatsapp_dedicated_number: bool
) -> list[str]:
    """The authoritative purpose set a household must hold, in manifest order.

    The single place that answers "which consents does this household owe?", so
    the planner's manifest and the boundaries that re-check it cannot disagree.
    """
    purposes = [CONTENT_RESTRICTION_PURPOSE, "whatsapp_channel_privacy"]
    if whatsapp_dedicated_number:
        purposes.append("whatsapp_linked_device_risk")
    if processes_real_household_content(provider_kind):
        purposes.append(HOUSEHOLD_CONTENT_PURPOSE)
    return purposes


def manifest_required_purposes(manifest: Mapping[str, Any] | None) -> list[str]:
    """Authoritative purposes declared by a stored manifest document.

    The manifest is what the runtime enforces, so every control-plane boundary
    that re-checks consent before that runtime can receive content reads the
    requirement from the same place. Unknown purposes are dropped — they cannot
    be checked against a text we do not hold — and the S5 restriction is added
    back if absent, because it is owed by every household and a manifest that
    omits it is malformed rather than permissive.
    """
    declared = ((manifest or {}).get("consent") or {}).get("required_purposes") or ()
    purposes = [purpose for purpose in declared if purpose in CONSENT_TEXTS]
    if CONTENT_RESTRICTION_PURPOSE not in purposes:
        purposes.append(CONTENT_RESTRICTION_PURPOSE)
    return purposes

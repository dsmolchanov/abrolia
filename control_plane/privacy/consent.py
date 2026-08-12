"""Versioned consent text identifiers owned by the control plane."""

from __future__ import annotations

import hashlib

CONSENT_TEXTS = {
    "special_category_content_restriction": (
        "special-category-content-restriction-v1",
        "Do not send or forward medical certificates, health or allergy data, "
        "religious beliefs, or other special-category personal data about any "
        "person to the agent inbox or channel. If such content is sent by "
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

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

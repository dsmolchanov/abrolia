"""Versioned consent text identifiers owned by the control plane."""

from __future__ import annotations

import hashlib

CONSENT_TEXTS = {
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

"""Phase F per-provider kill switches — default off, fail-closed, read at call time."""

from __future__ import annotations

import os

FLAGS = {
    "ABROLIA_MANAGED_EMAIL_ENABLED": "managed_email",
    "ABROLIA_BYO_EMAIL_ENABLED": "byo_email",
    "ABROLIA_GMAIL_ENABLED": "gmail",
    "ABROLIA_WHATSAPP_SHARED_ENABLED": "whatsapp_shared",
    "ABROLIA_WHATSAPP_DEDICATED_ENABLED": "whatsapp_dedicated",
    "ABROLIA_WEB_PUSH_ENABLED": "web_push",
}


def _is_enabled(env_name: str) -> bool:
    return os.environ.get(env_name, "0").strip() == "1"


def is_managed_email_enabled() -> bool:
    return _is_enabled("ABROLIA_MANAGED_EMAIL_ENABLED")


def is_byo_email_enabled() -> bool:
    return _is_enabled("ABROLIA_BYO_EMAIL_ENABLED")


def is_gmail_enabled() -> bool:
    return _is_enabled("ABROLIA_GMAIL_ENABLED")


def is_whatsapp_shared_enabled() -> bool:
    return _is_enabled("ABROLIA_WHATSAPP_SHARED_ENABLED")


def is_whatsapp_dedicated_enabled() -> bool:
    return _is_enabled("ABROLIA_WHATSAPP_DEDICATED_ENABLED")


def is_web_push_enabled() -> bool:
    return _is_enabled("ABROLIA_WEB_PUSH_ENABLED")


def check_provider_enabled(provider: str) -> None:
    """Raise if provider is disabled — fail-closed at call time."""
    mapping = {
        "managed_email": is_managed_email_enabled,
        "byo_email": is_byo_email_enabled,
        "gmail": is_gmail_enabled,
        "whatsapp_shared": is_whatsapp_shared_enabled,
        "whatsapp_dedicated": is_whatsapp_dedicated_enabled,
        "web_push": is_web_push_enabled,
    }
    checker = mapping.get(provider)
    if checker is None:
        raise ValueError(f"unknown provider {provider!r}")
    if not checker():
        raise RuntimeError(f"provider {provider} disabled by flag (fail-closed)")

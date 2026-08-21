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


#: The email options cut from MVP: selection kind, provisioning provider name,
#: and the flag both answer to.
#:
#: ONE table with two views, because the flag has to be asked in two places and
#: two independent maps would drift. `_assert_email_rollout` asks by SELECTION
#: KIND, which is what a user picks; `ProvisioningWorker` asks by PROVIDER NAME,
#: which is what the queued job carries. A row here reaches both.
#:
#: Asking at selection alone is not a kill switch. The provider call happens
#: later, from a queued job, so `1 -> 0` during an incident has to stop work
#: that is ALREADY queued — the case the switch exists for. Asking at the
#: provider call alone would let a cut option stay on the onboarding screen and
#: fail only after the user picked it. Both, or it is decorative.
#:
#: `abrolia_managed` / `nerve-managed` is deliberately absent. Canon has all six
#: flags fail-closed, but wiring the managed one today would take email away
#: from every deployment that has not set `ABROLIA_MANAGED_EMAIL_ENABLED` —
#: including the synthetic app, which sets none of them. That is a separate step
#: with a `fly.toml` change behind it.
CUT_EMAIL_OPTIONS = (
    ("gmail_agent", "google-oauth", "gmail"),
    ("family_domain", "nerve-byo-domain", "byo_email"),
)

#: View for the selection layer: what the user picked -> the flag.
GATED_EMAIL_OPTIONS = {kind: flag for kind, _provider, flag in CUT_EMAIL_OPTIONS}

#: View for the provisioning layer: what the job will call -> the flag.
GATED_EMAIL_PROVIDERS = {
    provider: flag for _kind, provider, flag in CUT_EMAIL_OPTIONS
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

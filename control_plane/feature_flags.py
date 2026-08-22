"""Per-provider kill switches — default off, fail-closed, read at call time.

Only the switches that actually gate something live here. Three others used to:
`managed_email`, `whatsapp_dedicated` and `web_push` were declared, given
accessors, listed in two operator runbook tables — and reachable from no call
site. `ABROLIA_MANAGED_EMAIL_ENABLED=0` gated nothing at all while an operator
reading the runbook mid-incident was told it stopped `@abrolia.com`
provisioning.

They were not replaced, because each already had a stronger live counterpart:

- managed email — `ABROLIA_REAL_EMAIL_ENABLED=0` routes the managed option to
  `fake-email` (`container.py`), so no Nerve call happens, and real provisioning
  additionally requires a non-empty `ABROLIA_REAL_EMAIL_HOUSEHOLD_ALLOWLIST`.
  A brake that falls back to a working synthetic path beats one that errors, and
  the allowlist bounds it per household rather than per deployment.
- dedicated WhatsApp and web push — `ControlPlaneConfig.validate` REFUSES TO
  BOOT with `real_whatsapp_enabled` or `real_channel_enabled` set. There is no
  adapter to gate yet, and a flag cannot be more fail-closed than a config that
  will not start.

`phase-DE-pilot.md` anticipated exactly this: "Alternative short form if the
codebase consolidates: `ABROLIA_REAL_EMAIL_ENABLED` covers managed+BYO ...
Either form is acceptable if the matrix below is covered and default-off."

What remains are the two options cut from MVP, where the question is not "can
real content arrive" but "is this on offer at all" — a question no `REAL_*` flag
answers, because both options are refused even where they would route to a fake
provider.
"""

from __future__ import annotations

import os

#: Env var -> switch name, for the switches that gate a call site.
FLAGS = {
    "ABROLIA_BYO_EMAIL_ENABLED": "byo_email",
    "ABROLIA_GMAIL_ENABLED": "gmail",
    "ABROLIA_WHATSAPP_SHARED_ENABLED": "whatsapp_shared",
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


def is_byo_email_enabled() -> bool:
    return _is_enabled("ABROLIA_BYO_EMAIL_ENABLED")


def is_gmail_enabled() -> bool:
    return _is_enabled("ABROLIA_GMAIL_ENABLED")


def is_whatsapp_shared_enabled() -> bool:
    """The shared WhatsApp relay brake, enforced in `gateway.whatsapp_router`.

    Unlike the email switches this one gates an inbound relay rather than a
    provisioning call, so it has no entry in `CUT_EMAIL_OPTIONS` and is asked
    directly at the webhook.
    """
    return _is_enabled("ABROLIA_WHATSAPP_SHARED_ENABLED")


def is_real_email_enabled() -> bool:
    """The managed/BYO incident brake, read at call time.

    Deliberately a SECOND reader of a variable `ControlPlaneConfig` also parses,
    which is normally the defect this module exists to remove. The exception is
    forced: the adapters must stay registered so teardown can resolve them, so
    registration can no longer carry the brake, and the worker holds no
    `ControlPlaneConfig` to ask. Asking the environment here keeps the brake
    where the provider is actually called — which is also what makes `1 -> 0`
    stop work already queued.
    """
    return _is_enabled("ABROLIA_REAL_EMAIL_ENABLED")


def check_provider_enabled(provider: str) -> None:
    """Raise if provider is disabled — fail-closed at call time."""
    mapping = {
        "byo_email": is_byo_email_enabled,
        "gmail": is_gmail_enabled,
        "whatsapp_shared": is_whatsapp_shared_enabled,
    }
    checker = mapping.get(provider)
    if checker is None:
        raise ValueError(f"unknown provider {provider!r}")
    if not checker():
        raise RuntimeError(f"provider {provider} disabled by flag (fail-closed)")

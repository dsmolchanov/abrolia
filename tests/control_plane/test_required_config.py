"""Every name the deploy preflight demands must actually be required at boot.

The list in `deploy/control-plane/required-runtime-config.txt` is what the
production deploy checks against the app's Fly secrets and `fly.toml [env]`
before it mutates anything. A list like that rots in two directions, and both
are silent:

* a name the code started requiring never gets added, and the deploy sails past
  a machine that cannot boot. That is exactly what
  `ABROLIA_RUNTIME_MODEL_API_KEY` did — required from #71, named nowhere,
  noticed nine days later when production went down;
* a name the code stopped requiring stays on the list, and deploys demand a
  secret nobody needs, which teaches operators to add junk to satisfy a gate.

So this does not compare the file against a second copy of the rules. It
removes each name from an otherwise-valid production environment and asserts
that the boot REFUSES — the same call `abrolia-control-plane serve` makes.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from control_plane.config import (
    ConfigurationError,
    ControlPlaneConfig,
    backup_key_from_env,
)

REPOSITORY = Path(__file__).resolve().parents[2]
MANIFEST = REPOSITORY / "deploy" / "control-plane" / "required-runtime-config.txt"
FLY_TOML = REPOSITORY / "deploy" / "control-plane" / "fly.toml"


def _required_names() -> list[str]:
    return [
        line.strip()
        for line in MANIFEST.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _encoded(seed: int) -> str:
    return base64.b64encode(bytes([seed]) * 32).decode()


def _production_env() -> dict[str, str]:
    """A complete production environment: the shape `fly.toml` plus secrets make."""
    return {
        "ABROLIA_ENCRYPTION_KEY": _encoded(1),
        "ABROLIA_LOOKUP_HMAC_KEY": _encoded(2),
        "ABROLIA_TOKEN_HMAC_KEY": _encoded(3),
        "ABROLIA_CONTROL_PLANE_BACKUP_KEY": _encoded(4),
        "ABROLIA_RUNTIME_PROVIDER": "fly-runtime",
        "FLY_API_TOKEN": "fly-token",
        "ABROLIA_FLY_ORG": "abrolia-synthetic",
        "ABROLIA_RUNTIME_IMAGE": "registry.fly.io/runtime@sha256:" + "a" * 64,
        "ABROLIA_RUNTIME_MODEL_API_KEY": "sk-ant-synthetic",
        "ABROLIA_INTERNAL_BOOTSTRAP_HOST": "abrolia-control-plane-synthetic.flycast",
        # Self-signup for testers: fly.toml turns production delivery on, which
        # makes the sending key and the sender required at boot.
        "ABROLIA_MAGIC_LINK_DELIVERY_ENABLED": "1",
        "ABROLIA_MAGIC_LINK_FROM": "Abrolia <login@example.test>",
        "ABROLIA_RESEND_API_KEY": "re_synthetic",
        "ABROLIA_SELF_SIGNUP_ENABLED": "1",
        # fly.toml offers the agent Gmail, and an offered card has to be one a
        # family can finish: the OAuth client and, while real Gmail is off, the
        # test-user list become required at boot.
        "ABROLIA_GMAIL_ENABLED": "1",
        "ABROLIA_GOOGLE_OAUTH_CLIENT_ID": "synthetic-client.apps.example.test",
        "ABROLIA_GOOGLE_OAUTH_CLIENT_SECRET": "synthetic-client-secret",
        "ABROLIA_GOOGLE_OAUTH_TEST_USERS": "tester@example.test",
    }


def test_the_reference_environment_boots() -> None:
    """The baseline has to be valid, or every case below passes vacuously."""
    config = ControlPlaneConfig.from_env(_production_env())
    assert config.runtime_provider == "fly-runtime"


#: Names whose absence refuses `ControlPlaneConfig.from_env` — the call
#: `abrolia-control-plane serve` makes before it binds a port.
BOOT_CONFIG = {
    "ABROLIA_ENCRYPTION_KEY",
    "ABROLIA_LOOKUP_HMAC_KEY",
    "ABROLIA_TOKEN_HMAC_KEY",
    "FLY_API_TOKEN",
    "ABROLIA_FLY_ORG",
    "ABROLIA_RUNTIME_IMAGE",
    "ABROLIA_RUNTIME_MODEL_API_KEY",
    "ABROLIA_INTERNAL_BOOTSTRAP_HOST",
    "ABROLIA_RESEND_API_KEY",
    "ABROLIA_MAGIC_LINK_FROM",
    "ABROLIA_GOOGLE_OAUTH_CLIENT_ID",
    "ABROLIA_GOOGLE_OAUTH_CLIENT_SECRET",
    "ABROLIA_GOOGLE_OAUTH_TEST_USERS",
}

#: Proven by a different call, and CONDITIONALLY fatal, which is worse than
#: unconditionally: `ControlPlaneConfig` does not read the backup key at all —
#: deliberately, so `restore` still works when the deployment is broken — so a
#: machine without it starts perfectly well until the day a deploy carries a
#: migration. Then `migrate --backup-first` fails closed ("never migrate a
#: database we could not snapshot", `db.py:318`) and the machine will not boot
#: at all, on exactly the deploy that changes the schema.
BACKUP_KEY = {"ABROLIA_CONTROL_PLANE_BACKUP_KEY"}


def test_every_declared_name_has_a_proof() -> None:
    """No name enters the manifest without a case below that pins it.

    The two sets are spelled out rather than derived so that adding a name to
    the manifest forces a decision about HOW it is required. A name in neither
    set is one the preflight would demand for no reason anybody recorded.
    """
    assert set(_required_names()) == BOOT_CONFIG | BACKUP_KEY


@pytest.mark.parametrize("name", sorted(BOOT_CONFIG))
def test_removing_a_required_name_refuses_the_boot(name: str) -> None:
    """Each declared name, proven required by taking it away."""
    env = _production_env()
    assert name in env, (
        f"{name} is declared required but the reference environment does not "
        "set it — the case below would prove nothing"
    )
    del env[name]
    with pytest.raises(ConfigurationError):
        ControlPlaneConfig.from_env(env)


@pytest.mark.parametrize("name", sorted(BACKUP_KEY))
def test_removing_the_backup_key_refuses_the_pre_migrate_snapshot(name: str) -> None:
    """It does not stop `serve` — and that is precisely the hazard.

    Absent, the application boots and serves happily. The failure waits for a
    deploy that carries a migration, which is the deploy you least want to
    discover it on.
    """
    env = _production_env()
    del env[name]
    ControlPlaneConfig.from_env(env)  # serve is unaffected: the trap
    with pytest.raises(ConfigurationError):
        backup_key_from_env(env)


def test_every_non_secret_name_is_actually_in_fly_toml() -> None:
    """The settings the list says live in `fly.toml` have to be there.

    The preflight resolves a name against secrets OR `fly.toml [env]`. A
    non-secret that is in neither would be reported missing on every deploy,
    and the fix would look like "add another secret" rather than "the file is
    wrong".
    """
    toml = FLY_TOML.read_text(encoding="utf-8")
    for name in ("ABROLIA_FLY_ORG", "ABROLIA_INTERNAL_BOOTSTRAP_HOST"):
        assert f"{name} =" in toml, f"{name} is not set in fly.toml [env]"


def test_secrets_are_never_carried_in_fly_toml() -> None:
    """`fly.toml` is committed, so a secret named there would be a leak.

    The check is on the NAME appearing as a setting, which is what would carry
    a value into git — the file is allowed to mention one in a comment.
    """
    toml = FLY_TOML.read_text(encoding="utf-8")
    for name in (
        "ABROLIA_ENCRYPTION_KEY",
        "ABROLIA_LOOKUP_HMAC_KEY",
        "ABROLIA_TOKEN_HMAC_KEY",
        "ABROLIA_CONTROL_PLANE_BACKUP_KEY",
        "FLY_API_TOKEN",
        "ABROLIA_RUNTIME_MODEL_API_KEY",
        "ABROLIA_RESEND_API_KEY",
        # Not a secret, but a real address: the fixtures gate refuses it in a
        # committed file, so it is carried in the secret store.
        "ABROLIA_MAGIC_LINK_FROM",
    ):
        assert f"{name} =" not in toml, f"{name} must be a secret, not a fly.toml value"


#: Real Gmail is gated on Google's evidence for the SEND-ONLY design: a
#: verified app, sensitive-scope approval for `gmail.send`, and the Limited
#: Use disclosure. There is no CASA flag — that assessment is owed for a
#: restricted scope, and `gmail.readonly` is no longer requested.
GMAIL_EVIDENCE = (
    "ABROLIA_GOOGLE_OAUTH_APP_VERIFIED",
    "ABROLIA_GOOGLE_GMAIL_SCOPE_APPROVED",
    "ABROLIA_GOOGLE_LIMITED_USE_DISCLOSED",
)


def _real_gmail_env() -> dict[str, str]:
    env = _production_env()
    env.update(
        {
            "ABROLIA_SYNTHETIC_ONLY": "0",
            "REAL_FAMILY_DATA_ENABLED": "1",
            "ABROLIA_REAL_EMAIL_ENABLED": "1",
            "ABROLIA_NERVE_BASE_URL": "https://nerve.example.test",
            "ABROLIA_NERVE_ADMIN_KEY": "synthetic-nerve-admin-key",
            "ABROLIA_NERVE_PLATFORM_ORG_ID": "10000000-0000-4000-8000-000000000001",
            "ABROLIA_NERVE_PLATFORM_DOMAIN_ID": "10000000-0000-4000-8000-000000000002",
            "ABROLIA_GMAIL_REAL_ENABLED": "1",
            **{name: "1" for name in GMAIL_EVIDENCE},
        }
    )
    return env


def test_real_gmail_boots_on_the_three_send_only_evidence_flags() -> None:
    config = ControlPlaneConfig.from_env(_real_gmail_env())
    assert config.gmail_real_enabled is True
    assert not hasattr(config, "google_casa_current")


@pytest.mark.parametrize("name", GMAIL_EVIDENCE)
def test_real_gmail_refuses_to_boot_without_each_evidence_flag(name: str) -> None:
    env = _real_gmail_env()
    env[name] = "0"
    with pytest.raises(ConfigurationError, match="sensitive-scope"):
        ControlPlaneConfig.from_env(env)


def test_a_casa_flag_is_neither_required_nor_read() -> None:
    """A deployment still carrying the old flag must not be blocked by it, and
    setting it must not stand in for the evidence that is required."""
    env = _real_gmail_env()
    env["ABROLIA_GOOGLE_CASA_CURRENT"] = "0"
    ControlPlaneConfig.from_env(env)
    env["ABROLIA_GOOGLE_CASA_CURRENT"] = "1"
    env["ABROLIA_GOOGLE_GMAIL_SCOPE_APPROVED"] = "0"
    with pytest.raises(ConfigurationError, match="sensitive-scope"):
        ControlPlaneConfig.from_env(env)


def test_an_unoffered_gmail_option_requires_no_google_configuration() -> None:
    """The requirement follows the switch, so a deployment that does not offer
    Gmail is not made to hold OAuth secrets it never uses."""
    env = _production_env()
    env["ABROLIA_GMAIL_ENABLED"] = "0"
    for name in (
        "ABROLIA_GOOGLE_OAUTH_CLIENT_ID",
        "ABROLIA_GOOGLE_OAUTH_CLIENT_SECRET",
        "ABROLIA_GOOGLE_OAUTH_TEST_USERS",
    ):
        del env[name]
    ControlPlaneConfig.from_env(env)

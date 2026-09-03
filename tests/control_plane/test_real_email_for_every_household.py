"""Owner decision 2026-09-03: real managed email for every household, no allowlist.

Testers register themselves, and each one used to wait for an operator to put
their household id on a secret. `ABROLIA_REAL_EMAIL_ALL_HOUSEHOLDS=1` makes
selection and dispatch authorize every household. Three things must stay
true: the list is ignored, not half-consulted; the live brake still stops
dispatch; and the flag is dormant while real email is off.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from control_plane.config import ControlPlaneConfig
from control_plane.models import StepKind
from control_plane.onboarding.contracts import InvalidTransition

NERVE = dict(
    ABROLIA_REAL_EMAIL_ENABLED="1",
    ABROLIA_NERVE_BASE_URL="https://nerve.example.test",
    ABROLIA_NERVE_ADMIN_KEY="synthetic-admin-key",
    ABROLIA_NERVE_PLATFORM_ORG_ID="20000000-0000-4000-8000-000000000001",
    ABROLIA_NERVE_PLATFORM_DOMAIN_ID="30000000-0000-4000-8000-000000000001",
)


def _env(**changes: str) -> dict[str, str]:
    from tests.control_plane.test_config import _production_env

    return _production_env(**changes)


def test_the_flag_is_read_and_silences_the_allowlist_blockers() -> None:
    open_config = ControlPlaneConfig.from_env(_env(
        **NERVE,
        ABROLIA_REAL_EMAIL_ALL_HOUSEHOLDS="1",
        ABROLIA_REAL_EMAIL_HOUSEHOLD_ALLOWLIST="whatever@example.test",
    ))
    assert open_config.real_email_all_households
    # The list is ignored, so its problems are not reported either.
    assert open_config.real_email_allowlist_blockers == ()

    listed = ControlPlaneConfig.from_env(_env(**NERVE, ABROLIA_REAL_EMAIL_HOUSEHOLD_ALLOWLIST=""))
    assert not listed.real_email_all_households
    assert listed.real_email_allowlist_blockers == ("real_email_allowlist_empty",)

    dormant = ControlPlaneConfig.from_env(_env(ABROLIA_REAL_EMAIL_ALL_HOUSEHOLDS="1"))
    assert dormant.real_email_all_households and not dormant.real_email_enabled
    assert dormant.real_email_allowlist_blockers == ()


def test_selection_no_longer_asks_the_list(cp_stack) -> None:
    """The household is NOT on the list, and passes the wall anyway.

    Proven by reaching the NEXT refusal — the content-restriction receipt —
    rather than by success, so the test does not depend on the rest of the
    real-email path.
    """
    cp_stack.complete_profile()
    cp_stack.service.real_email_enabled = True
    cp_stack.service.email_provider = "nerve-managed"
    cp_stack.service.real_email_household_allowlist = frozenset()

    cp_stack.service.real_email_all_households = False
    with pytest.raises(InvalidTransition, match="not enabled for this household"):
        cp_stack.service.select(
            cp_stack.household.id,
            StepKind.EMAIL,
            {"kind": "abrolia_managed", "local_part": "family-agent"},
            context=cp_stack.context(),
        )

    cp_stack.service.real_email_all_households = True
    with pytest.raises(InvalidTransition, match="content restriction receipt"):
        cp_stack.service.select(
            cp_stack.household.id,
            StepKind.EMAIL,
            {"kind": "abrolia_managed", "local_part": "family-agent"},
            context=cp_stack.context(),
        )


def _job(household_id: str = "40000000-0000-4000-8000-000000000001") -> SimpleNamespace:
    return SimpleNamespace(
        id="job-every-household",
        kind="email_identity",
        provider="nerve-managed",
        operation="ensure",
        household_id=household_id,
        request={},
    )


def test_dispatch_authorizes_every_household_but_still_obeys_the_brake(cp_stack, monkeypatch) -> None:
    worker = cp_stack.make_worker()
    worker.real_email_authorized_households = frozenset()

    monkeypatch.setenv("ABROLIA_REAL_EMAIL_ENABLED", "1")
    worker.real_email_all_households = False
    assert worker._blocked_by_email_kill_switch(_job()) == "real_email_disabled"

    worker.real_email_all_households = True
    assert worker._blocked_by_email_kill_switch(_job()) is None

    # The live brake is subtractive and wins over the flag: 1 -> 0 stops dispatch.
    monkeypatch.setenv("ABROLIA_REAL_EMAIL_ENABLED", "0")
    assert worker._blocked_by_email_kill_switch(_job()) == "real_email_disabled"

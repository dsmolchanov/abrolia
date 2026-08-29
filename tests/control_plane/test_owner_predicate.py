"""Who the fallback owner is, asked in one place — and kept that way.

`AGENTS.repo-invariants.md`, "A household's fallback owner is decided in one
place", records the rule. This is the check that would have caught each of the
four rounds that produced it: every one of them was a second spelling of the
same predicate, and every one passed its own tests.

The two assertions below answer different halves. The first is about WHERE the
question may be asked, and it is greppable because the question needs a join
that nothing else in the control plane performs. The second is about the
answer itself, and it is behavioural: a membership can be active while the
account behind it is locked, which is the distinction the fourth round found
missing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from control_plane.channel_preferences import ChannelPreferenceError
from control_plane.email.service import MailboxRefused
from control_plane.models import StepKind
from tests.control_plane.conftest import BASE_TIME
from tests.control_plane.test_manifest import (
    CHANNEL_SELECTION,
    EMAIL_SELECTION,
    WHATSAPP_SELECTION,
)

CONTROL_PLANE = Path(__file__).resolve().parents[2] / "control_plane"
#: The join that answers "is this account an active owner of this household".
#: `control_plane/owners.py` is the only module allowed to perform it, and the
#: alias is part of the string because a query that needs both tables needs one.
JOIN = "JOIN household_memberships AS m ON m.account_id = a.id"


def test_only_one_module_decides_who_an_active_owner_is() -> None:
    """A second spelling of this predicate is the defect, not a style question.

    Four review rounds found four of them: a membership-only check in the
    planner, an initiating-account comparison in the OAuth callback, and two
    copies of the mailbox rule that could drift apart. Any module that needs
    the answer imports it; a module that writes the join again is the next
    round waiting to happen.
    """
    offenders = sorted(
        path.relative_to(CONTROL_PLANE).as_posix()
        for path in CONTROL_PLANE.rglob("*.py")
        if JOIN in path.read_text(encoding="utf-8")
        and path.name != "owners.py"
    )
    assert offenders == [], (
        "these modules answer the fallback-owner question themselves; import"
        f" `control_plane.owners` instead: {', '.join(offenders)}"
    )


def _provisioned(cp_stack):
    cp_stack.complete_profile()
    worker = cp_stack.make_worker(now=BASE_TIME + 50)
    for offset, (kind, selection) in enumerate(
        (
            (StepKind.EMAIL, EMAIL_SELECTION),
            (StepKind.WHATSAPP, WHATSAPP_SELECTION),
            (StepKind.PRIMARY_CHANNEL, CHANNEL_SELECTION),
        ),
        start=2,
    ):
        cp_stack.service.select(
            cp_stack.household.id,
            kind,
            selection,
            context=cp_stack.context(),
            now=BASE_TIME + offset,
        )
        assert worker.run_once().status == "succeeded"


def test_a_locked_owner_is_neither_chosen_nor_refused_on(cp_stack) -> None:
    """Active means the membership AND the account, in both directions.

    A locked account cannot receive a fallback, so it must not be chosen as
    one — the planner picked by membership status alone, and the refusal then
    arrived from inside a provisioning job. The same fact points the other
    way too: a mailbox equal to a locked owner's address is refused on behalf
    of an account that could never have been the fallback, which blocks an
    onboarding for no reason.
    """
    locked = cp_stack.accounts.create_verified("locked-owner@family.test")
    with cp_stack.database.write() as connection:
        connection.execute(
            "INSERT INTO household_memberships (account_id, household_id, role,"
            " status, created_at, accepted_at) VALUES (?, ?, 'owner', 'active',"
            " ?, ?)",
            (locked.id, cp_stack.household.id, BASE_TIME - 10, BASE_TIME - 10),
        )
        connection.execute(
            "UPDATE accounts SET status = 'locked' WHERE id = ?", (locked.id,)
        )

    # The oldest membership is the locked one, so a predicate that reads
    # membership alone would choose it here.
    _provisioned(cp_stack)
    pref = cp_stack.channel_prefs.get_household(cp_stack.household.id)
    assert pref is not None and pref.fallback_account_id == cp_stack.account.id

    with cp_stack.database.write() as connection:
        # Recording the locked account as the fallback is refused…
        with pytest.raises(ChannelPreferenceError, match="active owner"):
            cp_stack.channel_prefs.set_household(
                connection,
                household_id=cp_stack.household.id,
                primary_channel="telegram",
                fallback_account_id=locked.id,
            )
        # … and its address is not a collision, because it is not reachable.
        # Called for its refusal and not for a return value: a locked owner's
        # address must pass, where an active owner's must not.
        cp_stack.service.email_identities._reject_owner_contact(
            connection, cp_stack.household.id, locked.recovery_email
        )
        with pytest.raises(MailboxRefused, match="self-ingestion"):
            cp_stack.service.email_identities._reject_owner_contact(
                connection, cp_stack.household.id, cp_stack.account.recovery_email
            )

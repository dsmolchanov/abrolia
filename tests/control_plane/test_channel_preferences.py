"""C4a: the table that had a schema, constraints, and no writer.

The four cases this file used to hold asserted the repository against values
the test handed it — `set_household(..., fallback_email=X, agent_inbox=Y)` —
so the self-ingestion case proved that two arguments can be compared and
nothing about whether the system knows its own inbox. They are replaced rather
than extended: what is worth pinning is that provisioning writes the row, and
that the refusals are answered from the database.
"""

from __future__ import annotations

import pytest

from control_plane.channel_preferences import ChannelPreferenceError
from control_plane.models import StepKind
from control_plane.provisioning.manifest import DesiredHouseholdSpecV1
from tests.control_plane.conftest import BASE_TIME
from tests.control_plane.test_manifest import (
    CHANNEL_SELECTION,
    EMAIL_SELECTION,
    WHATSAPP_SELECTION,
)


def _provisioned(cp_stack):
    """Drive a household to its first revision, the way onboarding does."""
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
    return DesiredHouseholdSpecV1.model_validate(
        cp_stack.configs.manifest(cp_stack.household.id, 1)
    )


def _agent_inbox(cp_stack) -> str:
    """The address this household's own assistant answers on."""
    row = cp_stack.database.query_one(
        "SELECT id FROM email_identities WHERE household_id = ?",
        (cp_stack.household.id,),
    )
    assert row is not None
    identity = cp_stack.email_identities.get(row["id"])
    assert identity is not None and identity.address is not None
    return identity.address


def test_provisioning_writes_the_row_the_schema_has_had_since_0006(cp_stack) -> None:
    """The preference is seeded where the owner binding is, from one result.

    `channel_preferences` has been readable and constrained since 0006 and
    empty in every deployment, because nothing built the repository, let alone
    called it. The planner now seeds it from the same `primary_channel` result
    it builds `channels.primary` from — a projection rather than a second
    choice, so the two cannot disagree.
    """
    spec = _provisioned(cp_stack)

    pref = cp_stack.channel_prefs.get_household(cp_stack.household.id)
    assert pref is not None
    assert pref.primary_channel == CHANNEL_SELECTION["kind"] == spec.channels.primary
    assert pref.fallback_channel == "email"
    # A reference to the account, never a copy of its address.
    assert pref.fallback_account_id == cp_stack.account.id
    # And the moment that address became usable, not the moment this ran.
    assert pref.verified_at == cp_stack.account.email_verified_at


def test_re_planning_updates_the_one_row_rather_than_adding_another(cp_stack) -> None:
    """The planner runs on every revision, so seeding has to be idempotent."""
    _provisioned(cp_stack)
    planner = cp_stack.make_worker().planner
    with cp_stack.database.write() as connection:
        planner.issue(connection, household_id=cp_stack.household.id)

    rows = cp_stack.database.query(
        "SELECT subject_id FROM channel_preferences WHERE subject_id = ?",
        (cp_stack.household.id,),
    )
    assert len(rows) == 1


def test_a_fallback_that_is_the_households_own_inbox_is_refused(cp_stack) -> None:
    """The loop 0006 named, refused with the system's own knowledge.

    Nothing is passed in to be compared. The colliding account is created
    through the accounts repository over the REAL inbox address, so the digests
    match the way two rows naming one address always do — `accounts` and
    `email_identities` hash with the same `LookupHasher` — and the check reads
    them without a key and without a caller's assurance.
    """
    _provisioned(cp_stack)
    looping = cp_stack.accounts.create_verified(_agent_inbox(cp_stack), now=BASE_TIME)
    with cp_stack.database.write() as connection:
        # An owner in good standing, whose verified contact happens to be the
        # address their own assistant answers on.
        connection.execute(
            "INSERT INTO household_memberships (account_id, household_id, role,"
            " status, created_at, accepted_at) VALUES (?, ?, 'owner', 'active',"
            " ?, ?)",
            (looping.id, cp_stack.household.id, BASE_TIME, BASE_TIME),
        )
        with pytest.raises(ChannelPreferenceError, match="self-ingestion"):
            cp_stack.channel_prefs.set_household(
                connection,
                household_id=cp_stack.household.id,
                primary_channel="telegram",
                fallback_account_id=looping.id,
            )


def test_a_fallback_that_is_not_an_active_owner_here_is_refused(cp_stack) -> None:
    """A fallback that reaches somebody else is a disclosure, not a delivery."""
    _provisioned(cp_stack)
    stranger = cp_stack.accounts.create_verified("stranger@family.test", now=BASE_TIME)
    with (
        cp_stack.database.write() as connection,
        pytest.raises(ChannelPreferenceError, match="active owner"),
    ):
            cp_stack.channel_prefs.set_household(
                connection,
                household_id=cp_stack.household.id,
                primary_channel="telegram",
                fallback_account_id=stranger.id,
            )


def test_the_database_requires_a_fallback_account_too(cp_stack) -> None:
    """Required by a trigger, because 0006's rows predate the column.

    The repository refuses first, and this is the floor under it: a row that
    names an email fallback and no account to send it to would be a preference
    that cannot be acted on, which is exactly what the C4 audit found the whole
    table to be.
    """
    _provisioned(cp_stack)
    with (
        pytest.raises(Exception, match="fallback_account_id is required"),
        cp_stack.database.write() as connection,
    ):
            connection.execute(
                "INSERT INTO channel_preferences (subject_type, subject_id,"
                " primary_channel, fallback_channel, updated_at)"
                " VALUES ('household', ?, 'telegram', 'email', ?)",
                (cp_stack.household.id, BASE_TIME),
            )


@pytest.mark.parametrize(
    ("field", "value"),
    (("primary_channel", "sms"), ("fallback_channel", "sms")),
)
def test_a_channel_this_system_does_not_speak_is_rejected(
    cp_stack, field, value
) -> None:
    _provisioned(cp_stack)
    arguments = {
        "household_id": cp_stack.household.id,
        "primary_channel": "telegram",
        "fallback_account_id": cp_stack.account.id,
        field: value,
    }
    with (
        cp_stack.database.write() as connection,
        pytest.raises(ChannelPreferenceError, match=f"unknown {field}"),
    ):
            cp_stack.channel_prefs.set_household(connection, **arguments)

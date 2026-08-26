"""C3: the challenge → verification → row write, and what it refuses.

`channel_bindings` existed from 0007 with `verified_at`/`verified_by_actor_id`
and no production writer: the gateway read the table, and only tests ever put
a row in it for the gateway to find. These cases cover the flow that finally
writes one, and — mostly — the things it must not write.
"""

from __future__ import annotations

import pytest

from control_plane.repositories.bindings import BindingError
from tests.control_plane.conftest import BASE_TIME

OWNER = "synthetic-owner"
ADULT = "synthetic-second-adult"
PHONE = "+999511234567"


@pytest.fixture
def second_household(cp_stack) -> str:
    """A household this flow has nothing to do with, and must not disturb."""
    account = cp_stack.accounts.create_verified("other@family.test", now=BASE_TIME)
    return cp_stack.households.create_for_owner(account.id, now=BASE_TIME).id


def _issue(cp_stack, connection, **overrides):
    kwargs = dict(
        household_id=cp_stack.household.id,
        channel="whatsapp",
        external_id=PHONE,
        actor_id=ADULT,
        role="adult",
        issued_by_actor_id=OWNER,
        now=BASE_TIME,
    )
    kwargs.update(overrides)
    return cp_stack.bindings.issue_challenge(connection, **kwargs)


def test_a_verified_challenge_is_what_writes_the_binding_row(cp_stack) -> None:
    with cp_stack.database.write() as connection:
        issued = _issue(cp_stack, connection)
        assert cp_stack.bindings.verified(
            connection, household_id=cp_stack.household.id
        ) == ()

        binding = cp_stack.bindings.verify_challenge(
            connection,
                code=issued.code,
                household_id=cp_stack.household.id,
                owner_actor_id=OWNER, now=BASE_TIME + 60
        )

    assert binding.actor_id == ADULT
    assert binding.role == "adult"
    # The two columns 0007 declared and nothing ever filled.
    assert binding.verified_at == BASE_TIME + 60
    assert binding.verified_by_actor_id == OWNER


def test_the_raw_code_is_never_stored(cp_stack) -> None:
    """A challenge code is a bearer credential for joining a household."""
    with cp_stack.database.write() as connection:
        issued = _issue(cp_stack, connection)

    rows = cp_stack.database.query("SELECT * FROM channel_binding_challenges")
    assert len(rows) == 1
    stored = " ".join(str(value) for value in dict(rows[0]).values())
    assert issued.code not in stored
    assert rows[0]["code_hash"] != issued.code


def test_an_expired_or_reused_challenge_is_refused(cp_stack) -> None:
    with cp_stack.database.write() as connection:
        expired = _issue(cp_stack, connection, external_id="+999511111111")
        with pytest.raises(BindingError, match="invalid or expired"):
            cp_stack.bindings.verify_challenge(
                connection,
                code=expired.code,
                household_id=cp_stack.household.id,
                owner_actor_id=OWNER,
                now=BASE_TIME + 10_000,
            )

        once = _issue(cp_stack, connection)
        cp_stack.bindings.verify_challenge(
            connection,
            code=once.code,
            household_id=cp_stack.household.id,
            owner_actor_id=OWNER, now=BASE_TIME + 60
        )
        # Single use: the same code must not bind a second time, even though
        # the binding it produced is perfectly valid.
        with pytest.raises(BindingError, match="invalid or expired"):
            cp_stack.bindings.verify_challenge(
                connection,
            code=once.code,
            household_id=cp_stack.household.id,
            owner_actor_id=OWNER, now=BASE_TIME + 61
            )


def test_a_code_cannot_be_redeemed_under_a_different_owner(cp_stack) -> None:
    with cp_stack.database.write() as connection:
        issued = _issue(cp_stack, connection)
        with pytest.raises(BindingError, match="invalid or expired"):
            cp_stack.bindings.verify_challenge(
                connection,
                code=issued.code,
                household_id=cp_stack.household.id,
                owner_actor_id="synthetic-someone-else",
                now=BASE_TIME + 60,
            )
        assert cp_stack.bindings.verified(
            connection, household_id=cp_stack.household.id
        ) == ()


def test_a_challenge_cannot_mint_an_owner(cp_stack) -> None:
    """Ownership follows the account, and no code delivered over a channel
    can confer it. The schema says the same thing in a CHECK constraint."""
    with (
        cp_stack.database.write() as connection,
        pytest.raises(BindingError, match="cannot confer this role"),
    ):
        _issue(cp_stack, connection, role="owner")


def test_an_external_id_bound_elsewhere_is_refused_at_issue_and_at_verify(
    cp_stack, second_household
) -> None:
    """The invariant that protects a household this flow never touches.

    `gateway/whatsapp_router.py` resolves a sender across ALL households and
    denies `ambiguous_sender` when more than one row matches. So binding a
    number another household already holds does not share a channel — it
    breaks delivery for both, including the household that was there first and
    did nothing wrong. Refusing is the only outcome that cannot do that.
    """
    with cp_stack.database.write() as connection:
        cp_stack.bindings.ensure_owner_binding(
            connection,
            household_id=second_household,
            channel="whatsapp",
            external_id=PHONE,
            actor_id="synthetic-other-owner",
            now=BASE_TIME,
        )

        with pytest.raises(BindingError, match="another household"):
            _issue(cp_stack, connection)

    # And again at verification, for a challenge issued BEFORE the clash
    # existed — the check at issue time cannot see a binding made after it.
    with cp_stack.database.write() as connection:
        connection.execute("DELETE FROM channel_bindings")
        issued = _issue(cp_stack, connection)
        cp_stack.bindings.ensure_owner_binding(
            connection,
            household_id=second_household,
            channel="whatsapp",
            external_id=PHONE,
            actor_id="synthetic-other-owner",
            now=BASE_TIME,
        )
        with pytest.raises(BindingError, match="another household"):
            cp_stack.bindings.verify_challenge(
                connection,
                code=issued.code,
                household_id=cp_stack.household.id,
                owner_actor_id=OWNER, now=BASE_TIME + 60
            )


def test_rebinding_the_same_id_to_a_different_member_is_refused(cp_stack) -> None:
    """Re-running the flow is fine; silently moving a channel to someone else
    is not — neither member would be told."""
    with cp_stack.database.write() as connection:
        first = _issue(cp_stack, connection)
        cp_stack.bindings.verify_challenge(
            connection,
            code=first.code,
            household_id=cp_stack.household.id,
            owner_actor_id=OWNER, now=BASE_TIME + 60
        )

        again = _issue(cp_stack, connection)
        repeat = cp_stack.bindings.verify_challenge(
            connection,
            code=again.code,
            household_id=cp_stack.household.id,
            owner_actor_id=OWNER, now=BASE_TIME + 120
        )
        assert repeat.actor_id == ADULT
        assert repeat.verified_at == BASE_TIME + 60  # the original row, not a new one

        stolen = _issue(cp_stack, connection, actor_id="synthetic-third-adult")
        with pytest.raises(BindingError, match="already bound to another member"):
            cp_stack.bindings.verify_challenge(
                connection,
                code=stolen.code,
                household_id=cp_stack.household.id,
                owner_actor_id=OWNER, now=BASE_TIME + 180
            )


def test_hmac_column_stays_null_until_c5_provisions_the_key(cp_stack) -> None:
    """A known gap, pinned so it fails a test rather than a delivery.

    The gateway's strict mode looks bindings up by `external_id_hmac`, keyed
    with the relay key. The control plane does not hold that key and has no
    path to one — `provisioning/secrets.py` is C5's work and does not exist.
    So every binding written here is invisible to a strict-mode gateway. When
    C5 lands the key, this test is the one that should start failing.
    """
    with cp_stack.database.write() as connection:
        issued = _issue(cp_stack, connection)
        cp_stack.bindings.verify_challenge(
            connection,
                code=issued.code,
                household_id=cp_stack.household.id,
                owner_actor_id=OWNER, now=BASE_TIME + 60
        )

    rows = cp_stack.database.query("SELECT external_id_hmac FROM channel_bindings")
    assert [row["external_id_hmac"] for row in rows] == [None]


def test_the_retention_sweep_retires_a_challenge_nobody_answered(cp_stack) -> None:
    from control_plane.privacy.retention import RetentionService

    with cp_stack.database.write() as connection:
        _issue(cp_stack, connection)

    service = RetentionService(cp_stack.bindings)
    result = service.run(now=BASE_TIME + 3 * 24 * 60 * 60)

    assert result.deleted["channel_binding_challenges"] == 1
    assert cp_stack.database.query("SELECT id FROM channel_binding_challenges") == []


# --- the second adult, all the way into the manifest -----------------------

from control_plane.models import StepKind  # noqa: E402
from control_plane.provisioning.manifest import DesiredHouseholdSpecV1  # noqa: E402
from control_plane.provisioning.manifest_toml import manifest_to_toml  # noqa: E402
from hermes_cloud.core.runtime_manifest import parse_runtime_manifest  # noqa: E402
from tests.control_plane.test_manifest import (  # noqa: E402
    CHANNEL_SELECTION,
    EMAIL_SELECTION,
    WHATSAPP_SELECTION,
)


def _shape(spec: DesiredHouseholdSpecV1) -> dict:
    """The manifest minus the two fields that move on every revision.

    `config_sha256` covers `config_revision`, so two revisions of identical
    content never share a hash. Comparing what is left is what actually says
    whether the projection changed.
    """
    payload = spec.model_dump(mode="json")
    payload.pop("config_revision", None)
    payload.pop("config_sha256", None)
    return payload


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


def test_the_owners_binding_is_seeded_into_the_table_planning_writes_it(
    cp_stack,
) -> None:
    """The defect this slice closes, stated as a test.

    Before C3 the planner built the owner's binding inline and the table stayed
    empty — so `gateway/whatsapp_router.py`, whose only source is that table,
    could not route to a household the manifest considered fully bound.
    """
    spec = _provisioned(cp_stack)

    rows = cp_stack.database.query("SELECT * FROM channel_bindings")
    assert len(rows) == 1
    assert rows[0]["role"] == "owner"
    assert rows[0]["actor_id"] == CHANNEL_SELECTION["actor_id"]
    assert rows[0]["external_id"] == CHANNEL_SELECTION["chat_id"]
    # And the manifest is the same fact, not a second one.
    assert spec.actors.owner == CHANNEL_SELECTION["actor_id"]
    assert spec.actors.family == (CHANNEL_SELECTION["actor_id"],)
    assert [binding.chat_id for binding in spec.channel_bindings] == [
        CHANNEL_SELECTION["chat_id"]
    ]


def test_an_adult_on_the_primary_channel_is_refused_because_the_store_cannot_hold_one(
    cp_stack,
) -> None:
    """The limit C3 stops at, and the reason, both pinned.

    `channel_bindings.external_id` answers two questions at once: the gateway
    matches it against a SENDER, the planner projects it as the manifest's
    `chat_id`. For the owner they coincide. For a second adult on that channel
    they cannot — the household's chat collides with the owner's row under
    `UNIQUE (household_id, channel, external_id)`, and an identity of their own
    makes the projection emit two verified chats for the primary channel, which
    the runtime refuses outright.

    So it is refused where someone asks for it, rather than written and left to
    fail at a deployment nobody is watching.
    """
    _provisioned(cp_stack)
    for external_id in (CHANNEL_SELECTION["chat_id"], "synthetic-a-chat-of-their-own"):
        with cp_stack.database.write() as connection:
            issued = cp_stack.bindings.issue_challenge(
                connection,
                household_id=cp_stack.household.id,
                channel=CHANNEL_SELECTION["kind"],
                external_id=external_id,
                actor_id=ADULT,
                role="adult",
                issued_by_actor_id=CHANNEL_SELECTION["actor_id"],
                now=BASE_TIME + 100,
            )
            with pytest.raises(BindingError, match="primary"):
                cp_stack.bindings.verify_challenge(
                    connection,
                    code=issued.code,
                    household_id=cp_stack.household.id,
                    owner_actor_id=CHANNEL_SELECTION["actor_id"],
                    now=BASE_TIME + 200,
                )
            assert cp_stack.database.query(
                "SELECT id FROM channel_bindings WHERE actor_id = ?", (ADULT,)
            ) == []


def test_an_adult_on_another_channel_is_bound_and_the_runtime_can_start_it(
    cp_stack,
) -> None:
    """What the lifecycle does deliver.

    A different channel takes no part in the primary-chat constraint and cannot
    collide with the owner's row, so the adult is representable there — and the
    projection is checked through the REAL runtime parser, not merely against
    the control plane's own contract. A manifest has two contracts and the
    earlier version of this test exercised only one, which is how a revision
    the runtime refuses looked green.
    """
    _provisioned(cp_stack)
    with cp_stack.database.write() as connection:
        issued = cp_stack.bindings.issue_challenge(
            connection,
            household_id=cp_stack.household.id,
            channel="whatsapp",
            external_id="+999511234567",
            actor_id=ADULT,
            role="adult",
            issued_by_actor_id=CHANNEL_SELECTION["actor_id"],
            now=BASE_TIME + 100,
        )
        cp_stack.bindings.verify_challenge(
            connection,
            code=issued.code,
            household_id=cp_stack.household.id,
            owner_actor_id=CHANNEL_SELECTION["actor_id"],
            now=BASE_TIME + 200,
        )
        cp_stack.make_worker().planner.issue(
            connection, household_id=cp_stack.household.id
        )

    spec = DesiredHouseholdSpecV1.model_validate(
        cp_stack.configs.manifest(cp_stack.household.id, 2)
    )
    assert spec.actors.family == (CHANNEL_SELECTION["actor_id"], ADULT)

    runtime = parse_runtime_manifest(manifest_to_toml(spec))
    # One chat to speak into; the adult authorized from their own channel.
    assert runtime.primary_chat_id == CHANNEL_SELECTION["chat_id"]
    assert runtime.verified_actor_chat_pairs == frozenset({
        (CHANNEL_SELECTION["actor_id"], CHANNEL_SELECTION["chat_id"]),
        (ADULT, "+999511234567"),
    })


def test_replanning_without_a_new_binding_leaves_the_manifest_identical(
    cp_stack,
) -> None:
    """The projection must be stable, or every revision would look like a
    change and `config_sha256` would stop meaning anything."""
    first = _provisioned(cp_stack)
    with cp_stack.database.write() as connection:
        cp_stack.make_worker().planner.issue(
            connection, household_id=cp_stack.household.id
        )
    second = DesiredHouseholdSpecV1.model_validate(
        cp_stack.configs.manifest(cp_stack.household.id, 2)
    )
    assert _shape(second) == _shape(first)
    assert second.config_revision == first.config_revision + 1
    # Seeding is idempotent: replanning must not add a duplicate owner row.
    assert len(cp_stack.database.query("SELECT id FROM channel_bindings")) == 1

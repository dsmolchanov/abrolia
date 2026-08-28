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
#: A SENDER identity, not a chat. The distinction is the whole of C3a: on
#: Telegram a user ID and a chat ID are different values, and before the split
#: one column had to be both.
ADULT_SENDER = "synthetic-second-adult-sender"
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
            chat_id=PHONE,
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
            chat_id=PHONE,
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


def test_an_already_bound_channel_is_refused_before_a_code_is_sent(
    cp_stack,
) -> None:
    """The refusal moved to issue time, and that is the better place.

    It also bounds the durable collections: `verify_challenge` returns the
    existing binding on a repeat, so no challenge or binding row grew — but the
    endpoint replans afterwards and `create_revision` inserts another encrypted
    manifest each time. An owner looping issue-then-verify on an already-bound
    tuple grew `config_revisions` without limit while every cap reported room.

    Refusing here means a verification can only ever follow a binding that did
    not exist, so revisions are bounded by the binding cap — and nobody is sent
    a code that could not have done anything.
    """
    with cp_stack.database.write() as connection:
        first = _issue(cp_stack, connection)
        cp_stack.bindings.verify_challenge(
            connection,
            code=first.code,
            household_id=cp_stack.household.id,
            owner_actor_id=OWNER,
        )

        # Same member, same channel: already true, so nothing to invite to.
        with pytest.raises(BindingError, match="already bound to that member"):
            _issue(cp_stack, connection)

        # Somebody else's member on a channel this household has bound: the
        # refusal that stops one person's channel being handed to another.
        with pytest.raises(BindingError, match="already bound to another member"):
            _issue(cp_stack, connection, actor_id="synthetic-third-adult")

        rows = cp_stack.bindings.verified(
            connection, household_id=cp_stack.household.id
        )
    assert [(r.actor_id, r.external_id) for r in rows] == [(ADULT, PHONE)]
    # Nothing was persisted by either refusal.
    assert cp_stack.database.query(
        "SELECT id FROM channel_binding_challenges WHERE consumed_at IS NULL"
    ) == []


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


def test_two_members_share_one_chat_and_the_revision_still_starts(
    cp_stack,
) -> None:
    """C3a's whole point, as one case: the arrangement the store could not hold.

    This replaces `test_an_adult_on_the_primary_channel_is_refused_because_
    the_store_cannot_hold_one`, deleted rather than skipped. That test
    documented a limitation, and a test that documents a limitation must die
    with the limitation — leaving it skipped would keep asserting that the
    refusal is the correct behaviour.

    An owner and an adult with DISTINCT sender identities in the SAME Telegram
    chat. Before the split that was two impossible shapes and no third option:
    the household's chat collided with the owner's row under `UNIQUE
    (household_id, channel, external_id)`, and an identity of their own made
    the projection emit two verified chats for the primary channel, which the
    runtime refused with `channels.primary: multiple chats`.

    It is round-tripped through the REAL runtime parser and not asserted
    against `DesiredHouseholdSpecV1` alone. A manifest has two contracts, and
    checking only the control plane's is exactly how an unstartable revision
    looked green in C3.
    """
    _provisioned(cp_stack)
    household_chat = CHANNEL_SELECTION["chat_id"]

    with cp_stack.database.write() as connection:
        issued = cp_stack.bindings.issue_challenge(
            connection,
            household_id=cp_stack.household.id,
            channel=CHANNEL_SELECTION["kind"],
            # A sender of their own — a Telegram user ID is not a chat ID —
            # speaking in the chat the household already has.
            external_id=ADULT_SENDER,
            chat_id=household_chat,
            actor_id=ADULT,
            role="adult",
            issued_by_actor_id=CHANNEL_SELECTION["actor_id"],
            now=BASE_TIME + 100,
        )
        binding = cp_stack.bindings.verify_challenge(
            connection,
            code=issued.code,
            household_id=cp_stack.household.id,
            owner_actor_id=CHANNEL_SELECTION["actor_id"],
            now=BASE_TIME + 200,
        )
        cp_stack.make_worker().planner.issue(
            connection, household_id=cp_stack.household.id
        )

    # The row says both things, and says them separately.
    assert (binding.external_id, binding.chat_id) == (ADULT_SENDER, household_chat)

    spec = DesiredHouseholdSpecV1.model_validate(
        cp_stack.configs.manifest(cp_stack.household.id, 2)
    )
    assert spec.actors.family == (CHANNEL_SELECTION["actor_id"], ADULT)

    runtime = parse_runtime_manifest(manifest_to_toml(spec))
    # Two members, one conversation, one review surface — and the review
    # surface is the OWNER's chat because that is what designates it now,
    # rather than the manifest happening to carry exactly one chat.
    assert runtime.primary_chat_id == household_chat
    assert runtime.verified_actor_chat_pairs == frozenset({
        (CHANNEL_SELECTION["actor_id"], household_chat),
        (ADULT, household_chat),
    })


def test_relaxing_uniqueness_did_not_relax_the_pair_check(cp_stack) -> None:
    """Cross-pair denial, which is the property most likely to be lost here.

    `chat_id` stopped being part of any unique key, so the store no longer
    objects to a member in a second conversation. Authorization must, and it is
    a PAIR that authorizes: an actor verified in one chat is not verified in
    another, and the owner is not authorized in the adult's private thread just
    because they own the household.
    """
    _provisioned(cp_stack)
    household_chat = CHANNEL_SELECTION["chat_id"]
    owner = CHANNEL_SELECTION["actor_id"]

    with cp_stack.database.write() as connection:
        for external_id, chat_id in (
            (ADULT_SENDER, household_chat),
            # The same adult, a second binding, a private thread of their own.
            ("+999511234567", "+999511234567"),
        ):
            issued = cp_stack.bindings.issue_challenge(
                connection,
                household_id=cp_stack.household.id,
                channel=(
                    CHANNEL_SELECTION["kind"] if chat_id == household_chat
                    else "whatsapp"
                ),
                external_id=external_id,
                chat_id=chat_id,
                actor_id=ADULT,
                role="adult",
                issued_by_actor_id=owner,
                now=BASE_TIME + 100,
            )
            cp_stack.bindings.verify_challenge(
                connection,
                code=issued.code,
                household_id=cp_stack.household.id,
                owner_actor_id=owner,
                now=BASE_TIME + 200,
            )
        cp_stack.make_worker().planner.issue(
            connection, household_id=cp_stack.household.id
        )

    spec = DesiredHouseholdSpecV1.model_validate(
        cp_stack.configs.manifest(cp_stack.household.id, 2)
    )
    runtime = parse_runtime_manifest(manifest_to_toml(spec))

    # `Household.knows_binding` is a membership test on exactly this set, and
    # `tests/test_runcontext.py` exercises the denial through it. What belongs
    # HERE is that the projection still produces pairs and not a widened chat
    # allowlist — the pair is the only thing standing between "chat_id may now
    # repeat" and "any known actor is authorized in any known chat".
    assert runtime.verified_actor_chat_pairs == frozenset({
        (owner, household_chat),
        (ADULT, household_chat),
        (ADULT, "+999511234567"),
    })
    assert (owner, "+999511234567") not in runtime.verified_actor_chat_pairs
    # `allowed_chats` widened, exactly as the plan said it would. It is now the
    # weaker check, and nothing authorizes from it while pairs are present.
    assert runtime.allowed_chats == frozenset({household_chat, "+999511234567"})


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


def test_reprovisioning_the_primary_channel_retires_what_it_replaces(
    cp_stack,
) -> None:
    """`reset_from(PRIMARY_CHANNEL)` lets a household re-run the step onto a
    different chat, or a different channel. Seeding only ever inserted, so the
    previous owner row survived — and two of them are not merely untidy.

    Two owner rows on one channel make the projection emit two verified chats
    for the primary channel, which the runtime refuses; and a row on the
    channel the household LEFT keeps its sender routable, because
    `gateway/whatsapp_router.py` resolves senders across the whole table and
    has no notion of a binding having been superseded. An owner who moves the
    household off a channel has revoked it.
    """
    hid = cp_stack.household.id
    with cp_stack.database.write() as connection:
        cp_stack.bindings.ensure_owner_binding(
            connection, household_id=hid, channel="telegram",
            external_id="synthetic-chat-one", actor_id=OWNER, now=BASE_TIME,
            chat_id="synthetic-chat-one",
        )
        # Re-onboarded onto a different chat on the same channel.
        cp_stack.bindings.ensure_owner_binding(
            connection, household_id=hid, channel="telegram",
            external_id="synthetic-chat-two", actor_id=OWNER, now=BASE_TIME + 10,
            chat_id="synthetic-chat-two",
        )
        rows = cp_stack.bindings.verified(connection, household_id=hid)

    assert [(r.channel, r.external_id) for r in rows] == [
        ("telegram", "synthetic-chat-two")
    ]

    # And onto a different channel entirely: the old sender stops routing.
    with cp_stack.database.write() as connection:
        cp_stack.bindings.ensure_owner_binding(
            connection, household_id=hid, channel="whatsapp",
            external_id=PHONE, actor_id=OWNER, now=BASE_TIME + 20,
            chat_id=PHONE,
        )
    remaining = cp_stack.database.query(
        "SELECT channel, external_id FROM channel_bindings WHERE household_id = ?",
        (hid,),
    )
    assert [(r["channel"], r["external_id"]) for r in remaining] == [
        ("whatsapp", PHONE)
    ]


def test_reprovisioning_drops_outstanding_challenges_and_dependent_adults(
    cp_stack,
) -> None:
    """A code issued against the arrangement that just changed must not redeem
    into the one that replaced it — and an adult on the channel now becoming
    primary cannot be represented there, so it goes with it rather than
    rebuilding the unstartable manifest by another route."""
    hid = cp_stack.household.id
    with cp_stack.database.write() as connection:
        cp_stack.bindings.ensure_owner_binding(
            connection, household_id=hid, channel="telegram",
            external_id="synthetic-chat-one", actor_id=OWNER, now=BASE_TIME,
            chat_id="synthetic-chat-one",
        )
        issued = cp_stack.bindings.issue_challenge(
            connection, household_id=hid, channel="whatsapp", external_id=PHONE,
            actor_id=ADULT, role="adult", issued_by_actor_id=OWNER, now=BASE_TIME,
        )
        cp_stack.bindings.verify_challenge(
            connection, code=issued.code, household_id=hid,
            owner_actor_id=OWNER, now=BASE_TIME + 5,
        )
        pending = cp_stack.bindings.issue_challenge(
            connection, household_id=hid, channel="whatsapp",
            external_id="+999511119999", actor_id="synthetic-third",
            role="adult", issued_by_actor_id=OWNER, now=BASE_TIME + 6,
        )
        # The household moves its primary channel onto WhatsApp, where that
        # adult already sits.
        cp_stack.bindings.ensure_owner_binding(
            connection, household_id=hid, channel="whatsapp",
            external_id="+999511230000", actor_id=OWNER, now=BASE_TIME + 20,
            chat_id="+999511230000",
        )
        rows = cp_stack.bindings.verified(connection, household_id=hid)
        # The outstanding code no longer redeems.
        with pytest.raises(BindingError, match="invalid or expired"):
            cp_stack.bindings.verify_challenge(
                connection, code=pending.code, household_id=hid,
                owner_actor_id=OWNER, now=BASE_TIME + 25,
            )

    assert [(r.channel, r.external_id, r.role) for r in rows] == [
        ("whatsapp", "+999511230000", "owner")
    ]


# --- review follow-ups: four findings that arrived after the last fix ------


def test_one_actor_with_several_bindings_is_one_family_member(cp_stack) -> None:
    """`family` was built per BINDING, not per actor.

    `verify_challenge` lets one actor hold several bindings — an adult
    reachable on WhatsApp and on web is two rows and one person — so the
    projection emitted `family=(owner, adult, adult)`, which the runtime
    rejects as `actors.family: duplicate actor`. Another revision that cannot
    start, reached by a different route than the primary-channel one.
    """
    _provisioned(cp_stack)
    with cp_stack.database.write() as connection:
        for channel, external_id in (
            ("whatsapp", "+999511234567"),
            ("web", "synthetic-web-seat"),
        ):
            issued = cp_stack.bindings.issue_challenge(
                connection,
                household_id=cp_stack.household.id,
                channel=channel,
                external_id=external_id,
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
    # Two bindings, one member — and the runtime accepts it.
    assert len(spec.channel_bindings) == 3
    parse_runtime_manifest(manifest_to_toml(spec))


def test_seeding_reconciles_role_and_actor_not_just_the_tuple(cp_stack) -> None:
    """The early return matched `(channel, external_id)` whatever the row WAS.

    Reproduced twice: re-onboarding onto a tuple an ADULT holds wrote no owner
    binding at all while the stale owner row survived on the channel the
    household had just left; and an owner whose ACTOR changed during reset
    never became authoritative.
    """
    hid = cp_stack.household.id
    with cp_stack.database.write() as connection:
        cp_stack.bindings.ensure_owner_binding(
            connection, household_id=hid, channel="telegram",
            external_id="synthetic-chat-one", actor_id=OWNER, now=BASE_TIME,
            chat_id="synthetic-chat-one",
        )
        issued = cp_stack.bindings.issue_challenge(
            connection, household_id=hid, channel="whatsapp", external_id=PHONE,
            actor_id=ADULT, role="adult", issued_by_actor_id=OWNER, now=BASE_TIME,
        )
        cp_stack.bindings.verify_challenge(
            connection, code=issued.code, household_id=hid,
            owner_actor_id=OWNER, now=BASE_TIME + 1,
        )
        # 1. The owner re-onboards onto the tuple the adult holds.
        cp_stack.bindings.ensure_owner_binding(
            connection, household_id=hid, channel="whatsapp",
            external_id=PHONE, actor_id=OWNER, now=BASE_TIME + 10,
            chat_id=PHONE,
        )
        rows = cp_stack.bindings.verified(connection, household_id=hid)

    assert [(r.channel, r.external_id, r.actor_id, r.role) for r in rows] == [
        ("whatsapp", PHONE, OWNER, "owner")
    ]

    # 2. The actor changes while the chat stays the same.
    with cp_stack.database.write() as connection:
        cp_stack.bindings.ensure_owner_binding(
            connection, household_id=hid, channel="whatsapp",
            external_id=PHONE, actor_id="synthetic-owner-renamed", now=BASE_TIME + 20,
            chat_id=PHONE,
        )
        rows = cp_stack.bindings.verified(connection, household_id=hid)

    assert [(r.actor_id, r.role) for r in rows] == [
        ("synthetic-owner-renamed", "owner")
    ]


def test_a_household_cannot_grow_challenges_or_bindings_without_end(
    cp_stack,
) -> None:
    """Every issued challenge is a stored row and every verified binding
    becomes a manifest entry and a config revision, so an authenticated owner
    looping over unique IDs grows a shared volume and the manifest with it."""
    from control_plane.repositories.bindings import (
        MAX_BINDINGS,
        MAX_OPEN_CHALLENGES,
    )

    hid = cp_stack.household.id
    with cp_stack.database.write() as connection:
        cp_stack.bindings.ensure_owner_binding(
            connection, household_id=hid, channel="telegram",
            external_id="synthetic-chat", actor_id=OWNER, now=BASE_TIME,
            chat_id="synthetic-chat",
        )
        for index in range(MAX_OPEN_CHALLENGES):
            cp_stack.bindings.issue_challenge(
                connection, household_id=hid, channel="whatsapp",
                external_id=f"+99951100{index:04d}", actor_id=f"synthetic-a{index}",
                role="adult", issued_by_actor_id=OWNER, now=BASE_TIME,
            )
        with pytest.raises(BindingError, match="outstanding challenges"):
            cp_stack.bindings.issue_challenge(
                connection, household_id=hid, channel="whatsapp",
                external_id="+999511009999", actor_id="synthetic-one-too-many",
                role="adult", issued_by_actor_id=OWNER, now=BASE_TIME,
            )

        # Bindings are capped where rows are created, so the limit holds
        # however the row was reached.
        connection.execute("DELETE FROM channel_binding_challenges")
        for index in range(MAX_BINDINGS - 1):
            issued = cp_stack.bindings.issue_challenge(
                connection, household_id=hid, channel="whatsapp",
                external_id=f"+99951199{index:04d}", actor_id=f"synthetic-b{index}",
                role="adult", issued_by_actor_id=OWNER, now=BASE_TIME,
            )
            cp_stack.bindings.verify_challenge(
                connection, code=issued.code, household_id=hid,
                owner_actor_id=OWNER, now=BASE_TIME + 1,
            )
        over = cp_stack.bindings.issue_challenge(
            connection, household_id=hid, channel="whatsapp",
            external_id="+999511998888", actor_id="synthetic-last",
            role="adult", issued_by_actor_id=OWNER, now=BASE_TIME,
        )
        with pytest.raises(BindingError, match="as many bindings"):
            cp_stack.bindings.verify_challenge(
                connection, code=over.code, household_id=hid,
                owner_actor_id=OWNER, now=BASE_TIME + 1,
            )


def test_redeeming_one_invitation_leaves_the_others_standing(cp_stack) -> None:
    """Replanning is not a reset.

    The planner runs on every revision — including the one issued immediately
    after a verification — so invalidating outstanding challenges from its
    idempotent path meant redeeming one invitation silently cancelled every
    other. A household inviting two people could only ever seat the first.
    """
    hid = cp_stack.household.id
    with cp_stack.database.write() as connection:
        cp_stack.bindings.ensure_owner_binding(
            connection, household_id=hid, channel="telegram",
            external_id="synthetic-chat", actor_id=OWNER, now=BASE_TIME,
            chat_id="synthetic-chat",
        )
        first = cp_stack.bindings.issue_challenge(
            connection, household_id=hid, channel="whatsapp",
            external_id="+999511110001", actor_id="synthetic-adult-a",
            role="adult", issued_by_actor_id=OWNER, now=BASE_TIME,
        )
        second = cp_stack.bindings.issue_challenge(
            connection, household_id=hid, channel="whatsapp",
            external_id="+999511110002", actor_id="synthetic-adult-b",
            role="adult", issued_by_actor_id=OWNER, now=BASE_TIME,
        )
        cp_stack.bindings.verify_challenge(
            connection, code=first.code, household_id=hid,
            owner_actor_id=OWNER, now=BASE_TIME + 1,
        )
        # What the verify endpoint does next, and what onboarding and the
        # provisioning worker do routinely.
        cp_stack.bindings.ensure_owner_binding(
            connection, household_id=hid, channel="telegram",
            external_id="synthetic-chat", actor_id=OWNER, now=BASE_TIME + 2,
            chat_id="synthetic-chat",
        )
        # The second invitation is still redeemable.
        bound = cp_stack.bindings.verify_challenge(
            connection, code=second.code, household_id=hid,
            owner_actor_id=OWNER, now=BASE_TIME + 3,
        )

    assert bound.actor_id == "synthetic-adult-b"

    # A real owner-state change still invalidates them, which is the only case
    # where the generation actually moved.
    with cp_stack.database.write() as connection:
        pending = cp_stack.bindings.issue_challenge(
            connection, household_id=hid, channel="web",
            external_id="synthetic-web-seat", actor_id="synthetic-adult-c",
            role="adult", issued_by_actor_id=OWNER, now=BASE_TIME + 4,
        )
        cp_stack.bindings.ensure_owner_binding(
            connection, household_id=hid, channel="telegram",
            external_id="synthetic-a-different-chat", actor_id=OWNER,
            chat_id="synthetic-a-different-chat",
            now=BASE_TIME + 5,
        )
        with pytest.raises(BindingError, match="invalid or expired"):
            cp_stack.bindings.verify_challenge(
                connection, code=pending.code, household_id=hid,
                owner_actor_id=OWNER, now=BASE_TIME + 6,
            )


# --- C3b: the revision reaches the runtime ---------------------------------


def test_verifying_a_binding_schedules_the_rollout_and_leaves_onboarding_alone(
    cp_stack,
) -> None:
    """Planning a revision is not deploying one.

    The endpoint answered with revision N while the runtime kept serving N-1,
    so the gateway could route a new member to a runtime that had never heard
    of them. The rollout is scheduled now — and the onboarding workflow stays
    `complete`, because `workflow.state` is user-visible through
    `OnboardingSnapshot` and a household that finished setup months ago must
    not be shown as mid-setup to serve a rollout.
    """
    from control_plane.provisioning.rollout import schedule_runtime_rollout
    from control_plane.provisioning.worker import REPROVISION_RUNTIME_OPERATION

    _provisioned(cp_stack)
    # The precondition, not the subject: a household that finished setup.
    # `_provisioned` stops at `runtime_provisioning` because activation runs
    # through the runtime's bootstrap, which this harness does not host. These
    # two writes are exactly what activation performs
    # (`provisioning/bootstrap.py:427` and `:440`).
    with cp_stack.database.write() as connection:
        connection.execute(
            "UPDATE households SET status = 'active' WHERE id = ?",
            (cp_stack.household.id,),
        )
        connection.execute(
            "UPDATE onboarding_workflows SET state = 'complete' WHERE household_id = ?",
            (cp_stack.household.id,),
        )
    before = cp_stack.onboarding.snapshot(cp_stack.household.id)
    assert before.state.value == "complete"

    with cp_stack.database.write() as connection:
        issued = cp_stack.bindings.issue_challenge(
            connection,
            household_id=cp_stack.household.id,
            channel="whatsapp",
            external_id=PHONE,
            actor_id=ADULT,
            role="adult",
            issued_by_actor_id=CHANNEL_SELECTION["actor_id"],
            now=BASE_TIME + 100,
        )

    with cp_stack.database.write() as connection:
        cp_stack.bindings.verify_challenge(
            connection,
            code=issued.code,
            household_id=cp_stack.household.id,
            owner_actor_id=CHANNEL_SELECTION["actor_id"],
            now=BASE_TIME + 200,
        )
        planned = cp_stack.make_worker().planner.issue(
            connection, household_id=cp_stack.household.id
        )
        schedule_runtime_rollout(
            connection,
            jobs=cp_stack.jobs,
            onboarding=cp_stack.onboarding,
            household_id=cp_stack.household.id,
            planned=planned,
            runtime_provider="dry-run-runtime",
            now=BASE_TIME + 300,
        )
    assert planned.revision.revision == 2

    job = cp_stack.database.query_one(
        "SELECT kind, operation, desired_revision FROM provisioning_jobs"
        " WHERE household_id = ? AND desired_revision = 2",
        (cp_stack.household.id,),
    )
    assert job is not None
    assert (job["kind"], job["operation"]) == ("runtime", REPROVISION_RUNTIME_OPERATION)

    household = cp_stack.database.query_one(
        "SELECT status, current_config_revision FROM households WHERE id = ?",
        (cp_stack.household.id,),
    )
    assert household["current_config_revision"] == 2
    assert household["status"] == "provisioning"

    # The criterion that rejects reusing the onboarding workflow.
    after = cp_stack.onboarding.snapshot(cp_stack.household.id)
    assert after.state.value == "complete"
    assert after.current_step == before.current_step

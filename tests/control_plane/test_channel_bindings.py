"""C3: the challenge → verification → row write, and what it refuses.

`channel_bindings` existed from 0007 with `verified_at`/`verified_by_actor_id`
and no production writer: the gateway read the table, and only tests ever put
a row in it for the gateway to find. These cases cover the flow that finally
writes one, and — mostly — the things it must not write.
"""

from __future__ import annotations

import json

import pytest

from control_plane.repositories.bindings import BindingError
from tests.control_plane.conftest import BASE_TIME


def _jid(phone: str) -> str:
    """The conversation identifier WhatsApp ingest reports for a 1:1 thread.

    Deliberately NOT the sender. `parse_webhook` normalizes the actor to
    `+999…` and reads the chat from the provider's `remote_jid`, and
    `trusted_run_context` compares both by string — so a fixture that passes
    one value for both cannot notice a consumer reading the wrong column. The
    reserved `.invalid` TLD and the `99XXXXXXX` range are what the repository's
    synthetic-fixture checker requires; `tests/test_whatsapp.py` uses the same
    shape against the real parser.
    """
    return f"{phone.lstrip('+')}@s.whatsapp.invalid"


OWNER = "synthetic-owner"
#: The second adult's identity ON TELEGRAM, and it is one value, not two.
#: `actor_id` and `external_id` are two readings of the transport sender —
#: the runtime authorizes `message.from.id`, the gateway routes by the same
#: string — so a fixture that gave the adult an "internal" actor beside a
#: separate sender was encoding a binding the runtime silently ignores. That
#: was the second [BLOCKER] on #76.
ADULT = "990000002"
#: The family's Telegram group chat, as `parse_update` renders `chat.id`.
TELEGRAM_CHAT = "-100990000101"
PHONE = "+999511234"
#: What `hermes_cloud/ingest/whatsapp_webhook.py` actually reports as the CHAT
#: for a 1:1 thread with PHONE. It is not the sender: the actor is normalized
#: to `+999…` and the conversation is the provider's `remote_jid`. Copying one
#: field into the other writes a pair no inbound turn can ever match.
PHONE_CHAT = _jid("+999511234")


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
        chat_id=PHONE_CHAT,
        role="adult",
        issued_by_actor_id=OWNER,
        now=BASE_TIME,
    )
    kwargs.update(overrides)
    # The actor IS the sender, so it follows an overridden `external_id`
    # instead of being named separately. A helper that let them drift would
    # hand every caller the binding shape the runtime cannot honour.
    kwargs.setdefault("actor_id", kwargs["external_id"])
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

    # One identity, read twice: the actor the runtime authorizes IS the
    # sender the gateway routes by.
    assert binding.actor_id == binding.external_id == PHONE
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
        expired = _issue(cp_stack, connection, external_id="+999511111")
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
            chat_id=PHONE_CHAT,
            actor_id=PHONE,
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
            chat_id=PHONE_CHAT,
            actor_id=PHONE,
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

        # Handing one person's channel to another used to be expressible —
        # same sender, different actor — and was refused by name. It is no
        # longer CONSTRUCTIBLE: the actor is the sender, so "a different
        # member" is a different tuple. The refusal that fires now is the
        # identity invariant itself, one step earlier. The
        # "already bound to another member" guards are kept rather than
        # deleted: they are the fail-closed answer if a sender-to-actor
        # mapping is ever introduced, which is the design that would make
        # them reachable again.
        with pytest.raises(BindingError, match="actor must be the identity"):
            _issue(cp_stack, connection, actor_id="synthetic-third-adult")

        rows = cp_stack.bindings.verified(
            connection, household_id=cp_stack.household.id
        )
    assert [(r.actor_id, r.external_id) for r in rows] == [(PHONE, PHONE)]
    # Nothing was persisted by either refusal.
    assert cp_stack.database.query(
        "SELECT id FROM channel_binding_challenges WHERE consumed_at IS NULL"
    ) == []


def test_hmac_column_stays_null_until_c5_provisions_the_key(cp_stack) -> None:
    """A known gap, pinned so it fails a test rather than a delivery.

    The gateway's strict mode looks bindings up by `external_id_hmac`, keyed
    with the relay key. The control plane does not hold that key, so every
    binding written here is invisible to a strict-mode gateway. When C5c lands
    the key, this test is the one that should start failing.

    What is missing is the KEY, not a place to put one. This said
    `provisioning/secrets.py` "is C5's work and does not exist", and that file
    has been on disk throughout — `FlySecretSink` installs secret material and
    the email providers already use it. Nothing generates a per-household relay
    secret, installs it as `ABROLIA_WHATSAPP_RELAY_SECRET`, or computes the
    digest this column holds; `WhatsAppGatewayRouter.relay_keys` is populated
    only by tests. Naming the sink as the gap pointed the next reader at a file
    they would find already written.
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

from control_plane.models import StepKind, synthetic_channel_identity  # noqa: E402
from control_plane.provisioning.manifest import DesiredHouseholdSpecV1  # noqa: E402
from control_plane.provisioning.manifest_toml import manifest_to_toml  # noqa: E402
from control_plane.provisioning.rollout import schedule_runtime_rollout  # noqa: E402
from control_plane.provisioning.worker import (  # noqa: E402
    REPROVISION_RUNTIME_OPERATION,
)
from gateway.whatsapp_router import WhatsAppGatewayRouter  # noqa: E402
from hermes_cloud.core.runcontext import Household  # noqa: E402
from hermes_cloud.core.runtime_manifest import parse_runtime_manifest  # noqa: E402
from hermes_cloud.ingest.whatsapp_webhook import (  # noqa: E402
    WhatsAppInbound,
    as_eml,
    trusted_run_context,
)
from tests.control_plane.test_manifest import (  # noqa: E402
    CHANNEL_SELECTION,
    EMAIL_SELECTION,
    WHATSAPP_SELECTION,
    owner_actor,
    owner_chat,
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
    # Three names, each from the field that means it: the owner's SENDER is
    # onboarding's `actor_id` — which becomes `actors.owner` and is what
    # `message.from.id` is compared against — and the CHAT is its `chat_id`.
    # Seeding the sender column from the chat was the other half of the
    # conflation, and M1 of the plan wrongly called it uncorrectable.
    assert rows[0]["actor_id"] == owner_actor(cp_stack)
    assert rows[0]["external_id"] == owner_actor(cp_stack)
    assert rows[0]["chat_id"] == owner_chat(cp_stack)
    # And the manifest is the same fact, not a second one.
    assert spec.actors.owner == owner_actor(cp_stack)
    assert spec.actors.family == (owner_actor(cp_stack),)
    assert [binding.chat_id for binding in spec.channel_bindings] == [
        owner_chat(cp_stack)
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
    household_chat = owner_chat(cp_stack)

    with cp_stack.database.write() as connection:
        issued = cp_stack.bindings.issue_challenge(
            connection,
            household_id=cp_stack.household.id,
            channel=CHANNEL_SELECTION["kind"],
            # A sender of their own — a Telegram user ID is not a chat ID —
            # speaking in the chat the household already has.
            external_id=ADULT,
            chat_id=household_chat,
            actor_id=ADULT,
            role="adult",
            issued_by_actor_id=owner_actor(cp_stack),
            now=BASE_TIME + 100,
        )
        binding = cp_stack.bindings.verify_challenge(
            connection,
            code=issued.code,
            household_id=cp_stack.household.id,
            owner_actor_id=owner_actor(cp_stack),
            now=BASE_TIME + 200,
        )
        cp_stack.make_worker().planner.issue(
            connection, household_id=cp_stack.household.id
        )

    # The row says both things, and says them separately.
    assert (binding.external_id, binding.chat_id) == (ADULT, household_chat)

    spec = DesiredHouseholdSpecV1.model_validate(
        cp_stack.configs.manifest(cp_stack.household.id, 2)
    )
    assert spec.actors.family == (owner_actor(cp_stack), ADULT)

    runtime = parse_runtime_manifest(manifest_to_toml(spec))
    # Two members, one conversation, one review surface — and the review
    # surface is the OWNER's chat because that is what designates it now,
    # rather than the manifest happening to carry exactly one chat.
    assert runtime.primary_chat_id == household_chat
    assert runtime.verified_actor_chat_pairs == frozenset({
        (owner_actor(cp_stack), household_chat),
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
    household_chat = owner_chat(cp_stack)
    owner = owner_actor(cp_stack)

    with cp_stack.database.write() as connection:
        for external_id, chat_id in (
            (ADULT, household_chat),
            # The same adult, a second binding, a private thread of their own.
            # Sender and conversation are deliberately different strings.
            (PHONE, PHONE_CHAT),
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
                actor_id=external_id,
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
        (PHONE, PHONE_CHAT),
    })
    assert (owner, PHONE_CHAT) not in runtime.verified_actor_chat_pairs
    # `allowed_chats` widened, exactly as the plan said it would. It is now the
    # weaker check, and nothing authorizes from it while pairs are present.
    assert runtime.allowed_chats == frozenset({household_chat, PHONE_CHAT})


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
            external_id="+999511234",
            chat_id=_jid("+999511234"),
            actor_id="+999511234",
            role="adult",
            issued_by_actor_id=owner_actor(cp_stack),
            now=BASE_TIME + 100,
        )
        cp_stack.bindings.verify_challenge(
            connection,
            code=issued.code,
            household_id=cp_stack.household.id,
            owner_actor_id=owner_actor(cp_stack),
            now=BASE_TIME + 200,
        )
        cp_stack.make_worker().planner.issue(
            connection, household_id=cp_stack.household.id
        )

    spec = DesiredHouseholdSpecV1.model_validate(
        cp_stack.configs.manifest(cp_stack.household.id, 2)
    )
    assert spec.actors.family == (owner_actor(cp_stack), PHONE)

    runtime = parse_runtime_manifest(manifest_to_toml(spec))
    # One chat to speak into; the adult authorized from their own channel.
    assert runtime.primary_chat_id == owner_chat(cp_stack)
    assert runtime.verified_actor_chat_pairs == frozenset({
        (owner_actor(cp_stack), owner_chat(cp_stack)),
        # The pair is (sender, conversation) and the two are different strings
        # on WhatsApp. This assertion used to read `(ADULT, "+999511234")`,
        # which is the shape `trusted_run_context` can never see: it compares
        # the +-normalized actor against the provider's `remote_jid`.
        (PHONE, PHONE_CHAT),
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
            external_id="synthetic-chat-one", actor_id="synthetic-chat-one", now=BASE_TIME,
            chat_id="synthetic-chat-one",
        )
        # Re-onboarded onto a different chat on the same channel.
        cp_stack.bindings.ensure_owner_binding(
            connection, household_id=hid, channel="telegram",
            external_id="synthetic-chat-two", actor_id="synthetic-chat-two", now=BASE_TIME + 10,
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
            external_id=PHONE, actor_id=PHONE, now=BASE_TIME + 20,
            chat_id=PHONE_CHAT,
        )
    remaining = cp_stack.database.query(
        "SELECT channel, external_id FROM channel_bindings WHERE household_id = ?",
        (hid,),
    )
    assert [(r["channel"], r["external_id"]) for r in remaining] == [
        ("whatsapp", PHONE)
    ]


def test_reprovisioning_drops_outstanding_challenges_but_keeps_other_chats(
    cp_stack,
) -> None:
    """Retirement is scoped to the chat the owner leaves, not to the channel.

    This used to assert the opposite — that an adult on the channel becoming
    primary goes with the move — and that was right only while
    `_reject_unrepresentable_member` refused an adult there at all. C3a removed
    the limitation, so deleting a member because the owner ARRIVED on their
    channel revokes a binding nobody superseded.

    What is still dropped: outstanding challenges, whatever their chat, because
    a code issued against the arrangement that just changed must not redeem
    into the one that replaced it.
    """
    hid = cp_stack.household.id
    with cp_stack.database.write() as connection:
        cp_stack.bindings.ensure_owner_binding(
            connection, household_id=hid, channel="telegram",
            external_id="synthetic-chat-one", actor_id="synthetic-chat-one", now=BASE_TIME,
            chat_id="synthetic-chat-one",
        )
        issued = cp_stack.bindings.issue_challenge(
            connection, household_id=hid, channel="whatsapp", external_id=PHONE,
                                                                          chat_id=PHONE_CHAT,
            actor_id=PHONE, role="adult", issued_by_actor_id=OWNER, now=BASE_TIME,
        )
        cp_stack.bindings.verify_challenge(
            connection, code=issued.code, household_id=hid,
            owner_actor_id=OWNER, now=BASE_TIME + 5,
        )
        pending = cp_stack.bindings.issue_challenge(
            connection, household_id=hid, channel="whatsapp",
            external_id="+999511119", actor_id="+999511119",
            chat_id=_jid("+999511119"),
            role="adult", issued_by_actor_id=OWNER, now=BASE_TIME + 6,
        )
        # The household moves its primary channel onto WhatsApp, where that
        # adult already sits — in a thread of their own, which the owner's
        # move does not touch.
        cp_stack.bindings.ensure_owner_binding(
            connection, household_id=hid, channel="whatsapp",
            external_id="+999511230", actor_id="+999511230", now=BASE_TIME + 20,
            chat_id=_jid("+999511230"),
        )
        rows = cp_stack.bindings.verified(connection, household_id=hid)
        # The outstanding code no longer redeems.
        with pytest.raises(BindingError, match="invalid or expired"):
            cp_stack.bindings.verify_challenge(
                connection, code=pending.code, household_id=hid,
                owner_actor_id=OWNER, now=BASE_TIME + 25,
            )

    # The adult keeps their own thread. The owner arriving on WhatsApp is not
    # a revocation of somebody who was verified there independently.
    assert [(r.channel, r.external_id, r.role) for r in rows] == [
        ("whatsapp", PHONE, "adult"),
        ("whatsapp", "+999511230", "owner"),
    ]


# --- review follow-ups: four findings that arrived after the last fix ------


def test_the_owner_appears_in_family_once_though_two_things_name_them(
    cp_stack,
) -> None:
    """`family` was built per BINDING, not per actor.

    The projection is `(actors.owner, *every binding's actor)`, and the owner
    is BOTH — so without `dict.fromkeys` it emitted `family=(owner, owner, …)`,
    which the runtime rejects as `actors.family: duplicate actor`. A revision
    that cannot start.

    This case used to be written as one adult reachable on two channels, and
    that shape is no longer expressible — worth recording, because it is a
    real consequence of the identity invariant rather than a test being
    tidied. An actor IS a transport sender, so the same human on WhatsApp and
    on web has two sender identities and is two members of `family`. The
    system has no cross-channel notion of a person; inventing internal actor
    names in the control plane made it look as though it did, and that
    appearance was the defect. Giving one person one identity across channels
    is the sender-to-actor mapping design C3a deliberately does not build.
    """
    _provisioned(cp_stack)
    with cp_stack.database.write() as connection:
        # Distinct verification times on purpose. `verified()` orders by
        # `(verified_at, id)`, and `id` is a fresh UUID — so two bindings
        # verified in the same instant are ordered by a value nothing chose,
        # and asserting their order would be asserting something the
        # projection does not promise. It is stable for a given database,
        # which is what `config_sha256` needs; it is not predictable across
        # them, which is what a test would need.
        for offset, (channel, external_id, chat) in enumerate((
            ("whatsapp", PHONE, PHONE_CHAT),
            ("web", "synthetic-web-seat", "synthetic-web-seat"),
        )):
            issued = cp_stack.bindings.issue_challenge(
                connection,
                household_id=cp_stack.household.id,
                channel=channel,
                external_id=external_id,
                chat_id=chat,
                actor_id=external_id,
                role="adult",
                issued_by_actor_id=owner_actor(cp_stack),
                now=BASE_TIME + 100,
            )
            cp_stack.bindings.verify_challenge(
                connection,
                code=issued.code,
                household_id=cp_stack.household.id,
                owner_actor_id=owner_actor(cp_stack),
                now=BASE_TIME + 200 + offset,
            )
        cp_stack.make_worker().planner.issue(
            connection, household_id=cp_stack.household.id
        )

    spec = DesiredHouseholdSpecV1.model_validate(
        cp_stack.configs.manifest(cp_stack.household.id, 2)
    )
    # The owner once, then each adult sender in verification order — and the
    # owner is not repeated despite `actors.owner` and their own binding both
    # naming them.
    assert spec.actors.family == (
        owner_actor(cp_stack), PHONE, "synthetic-web-seat",
    )
    assert len(spec.channel_bindings) == 3
    # Two senders, two members: the honest projection of what the runtime can
    # actually tell apart.
    assert len(set(spec.actors.family)) == len(spec.actors.family)
    parse_runtime_manifest(manifest_to_toml(spec))


def test_seeding_reconciles_the_whole_row_not_just_the_tuple(cp_stack) -> None:
    """The early return matched `(channel, external_id)` whatever the row WAS.

    The original defect: re-onboarding onto a tuple an ADULT held wrote no
    owner binding at all, while the stale owner row survived on the channel the
    household had just left. Matching now means role, actor, tuple and chat
    together.

    Both halves of this case have been rewritten since, and the reasons are
    worth keeping because each was a design change rather than a tidy-up.

    The owner taking a member's identity is now REFUSED. C3 resolved it by
    deleting every adult on the arriving channel, which was right only while
    the store could not hold one there; C3a removed that limitation, so the
    deletion became a way to unbind a member by accident. It is unreachable
    through the endpoints — `issue_challenge` refuses an actor equal to the
    issuer — and it is refused rather than left to raise `IntegrityError` from
    the unique index.

    And the second half used to read "the ACTOR changed while the tuple stayed
    the same", which the identity invariant makes impossible: the actor is the
    sender, so an actor change IS a tuple change. The CHAT is the field that
    can now move underneath a matching tuple, and it is the one reconciliation
    has to notice.
    """
    hid = cp_stack.household.id
    with cp_stack.database.write() as connection:
        cp_stack.bindings.ensure_owner_binding(
            connection, household_id=hid, channel="telegram",
            external_id="synthetic-chat-one", actor_id="synthetic-chat-one",
            now=BASE_TIME, chat_id="synthetic-chat-one",
        )
        issued = cp_stack.bindings.issue_challenge(
            connection, household_id=hid, channel="whatsapp", external_id=PHONE,
            chat_id=PHONE_CHAT, actor_id=PHONE, role="adult",
            issued_by_actor_id=OWNER, now=BASE_TIME,
        )
        cp_stack.bindings.verify_challenge(
            connection, code=issued.code, household_id=hid,
            owner_actor_id=OWNER, now=BASE_TIME + 1,
        )

        # 1. The owner cannot re-onboard onto an identity a member holds.
        with pytest.raises(BindingError, match="another member of the household"):
            cp_stack.bindings.ensure_owner_binding(
                connection, household_id=hid, channel="whatsapp",
                external_id=PHONE, actor_id=PHONE, now=BASE_TIME + 10,
                chat_id=PHONE_CHAT,
            )
        # A refused reset changes nothing: the adult keeps their binding and
        # the owner keeps the one they had.
        rows = cp_stack.bindings.verified(connection, household_id=hid)
    assert [(r.channel, r.external_id, r.role) for r in rows] == [
        ("telegram", "synthetic-chat-one", "owner"),
        ("whatsapp", PHONE, "adult"),
    ]

    # 2. The chat moves while the sender stays put: the family keeps its
    #    Telegram user IDs and the assistant is pointed at a new group. The
    #    adult who was speaking in the OLD chat goes with it; the one with a
    #    thread of their own does not.
    with cp_stack.database.write() as connection:
        issued = cp_stack.bindings.issue_challenge(
            connection, household_id=hid, channel="telegram",
            external_id=ADULT, chat_id="synthetic-chat-one", actor_id=ADULT,
            role="adult", issued_by_actor_id=OWNER, now=BASE_TIME + 15,
        )
        cp_stack.bindings.verify_challenge(
            connection, code=issued.code, household_id=hid,
            owner_actor_id=OWNER, now=BASE_TIME + 16,
        )
        cp_stack.bindings.ensure_owner_binding(
            connection, household_id=hid, channel="telegram",
            external_id="synthetic-chat-one", actor_id="synthetic-chat-one",
            now=BASE_TIME + 20, chat_id="synthetic-chat-two",
        )
        rows = cp_stack.bindings.verified(connection, household_id=hid)

    assert [(r.channel, r.actor_id, r.chat_id, r.role) for r in rows] == [
        ("whatsapp", PHONE, PHONE_CHAT, "adult"),
        ("telegram", "synthetic-chat-one", "synthetic-chat-two", "owner"),
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
            external_id="synthetic-chat", actor_id="synthetic-chat", now=BASE_TIME,
            chat_id="synthetic-chat",
        )
        for index in range(MAX_OPEN_CHALLENGES):
            phone = f"+{999110000 + index}"
            cp_stack.bindings.issue_challenge(
                connection, household_id=hid, channel="whatsapp",
                external_id=phone, actor_id=phone,
                chat_id=_jid(phone),
                role="adult", issued_by_actor_id=OWNER, now=BASE_TIME,
            )
        with pytest.raises(BindingError, match="outstanding challenges"):
            cp_stack.bindings.issue_challenge(
                connection, household_id=hid, channel="whatsapp",
                external_id="+999110999", actor_id="+999110999",
                chat_id=_jid("+999110999"),
                role="adult", issued_by_actor_id=OWNER, now=BASE_TIME,
            )

        # Bindings are capped where rows are created, so the limit holds
        # however the row was reached.
        connection.execute("DELETE FROM channel_binding_challenges")
        for index in range(MAX_BINDINGS - 1):
            phone = f"+{999119000 + index}"
            issued = cp_stack.bindings.issue_challenge(
                connection, household_id=hid, channel="whatsapp",
                external_id=phone, actor_id=phone,
                chat_id=_jid(phone),
                role="adult", issued_by_actor_id=OWNER, now=BASE_TIME,
            )
            cp_stack.bindings.verify_challenge(
                connection, code=issued.code, household_id=hid,
                owner_actor_id=OWNER, now=BASE_TIME + 1,
            )
        over = cp_stack.bindings.issue_challenge(
            connection, household_id=hid, channel="whatsapp",
            external_id="+999119888", actor_id="+999119888",
            chat_id=_jid("+999119888"),
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
            external_id="synthetic-chat", actor_id="synthetic-chat", now=BASE_TIME,
            chat_id="synthetic-chat",
        )
        first = cp_stack.bindings.issue_challenge(
            connection, household_id=hid, channel="whatsapp",
            external_id="+999511101", actor_id="+999511101",
            chat_id=_jid("+999511101"),
            role="adult", issued_by_actor_id=OWNER, now=BASE_TIME,
        )
        second = cp_stack.bindings.issue_challenge(
            connection, household_id=hid, channel="whatsapp",
            external_id="+999511102", actor_id="+999511102",
            chat_id=_jid("+999511102"),
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
            external_id="synthetic-chat", actor_id="synthetic-chat", now=BASE_TIME + 2,
            chat_id="synthetic-chat",
        )
        # The second invitation is still redeemable.
        bound = cp_stack.bindings.verify_challenge(
            connection, code=second.code, household_id=hid,
            owner_actor_id=OWNER, now=BASE_TIME + 3,
        )

    assert bound.actor_id == bound.external_id == "+999511102"

    # A real owner-state change still invalidates them, which is the only case
    # where the generation actually moved.
    with cp_stack.database.write() as connection:
        pending = cp_stack.bindings.issue_challenge(
            connection, household_id=hid, channel="web",
            external_id="synthetic-web-seat", actor_id="synthetic-web-seat",
            chat_id="synthetic-web-seat",
            role="adult", issued_by_actor_id=OWNER, now=BASE_TIME + 4,
        )
        cp_stack.bindings.ensure_owner_binding(
            connection, household_id=hid, channel="telegram",
            external_id="synthetic-a-different-chat", actor_id="synthetic-a-different-chat",
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
    # `_provisioned` stops short because activation runs through the runtime's
    # bootstrap, which this harness does not host — `_activated` performs what
    # it would, including settling the runtime job, which since C3e is part of
    # what "finished" means.
    _activated(cp_stack)
    before = cp_stack.onboarding.snapshot(cp_stack.household.id)
    assert before.state.value == "complete"

    with cp_stack.database.write() as connection:
        issued = cp_stack.bindings.issue_challenge(
            connection,
            household_id=cp_stack.household.id,
            channel="whatsapp",
            external_id=PHONE,
            chat_id=PHONE_CHAT,
            actor_id=PHONE,
            role="adult",
            issued_by_actor_id=owner_actor(cp_stack),
            now=BASE_TIME + 100,
        )

    with cp_stack.database.write() as connection:
        cp_stack.bindings.verify_challenge(
            connection,
            code=issued.code,
            household_id=cp_stack.household.id,
            owner_actor_id=owner_actor(cp_stack),
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


def test_a_bound_whatsapp_member_is_authorized_by_a_real_inbound_turn(
    cp_stack,
) -> None:
    """A stored `chat_id` is only worth anything if ingest produces that string.

    The first cut of C3a let `chat_id` default to `external_id`, documented as
    "the truth for WhatsApp, where a 1:1 thread IS the number". It is not the
    truth in this system, and Codex blocked the PR for it:
    `hermes_cloud/ingest/whatsapp_webhook.py` normalizes the SENDER to `+999…`
    and reports the CHAT as the provider's `remote_jid`, `999…@s.whatsapp.invalid`.
    `trusted_run_context` then authorizes that exact pair by string. A binding
    written as `(+999…, +999…)` therefore succeeded, published a revision, and
    was unknown to every real message that followed.

    So the assertion is made through the ACTUAL ingest path — a webhook-shaped
    inbound, rendered by `as_eml`, read back by `trusted_run_context` — rather
    than against a pair this test made up. A control-plane test that invents
    both halves of the comparison cannot notice that neither matches reality.
    """
    _provisioned(cp_stack)
    owner = owner_actor(cp_stack)

    with cp_stack.database.write() as connection:
        issued = cp_stack.bindings.issue_challenge(
            connection,
            household_id=cp_stack.household.id,
            channel="whatsapp",
            external_id=PHONE,
            chat_id=PHONE_CHAT,
            actor_id=PHONE,
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
    manifest = parse_runtime_manifest(manifest_to_toml(spec))
    household = Household(
        household_id=manifest.household_id,
        owner=manifest.actors.owner,
        family=manifest.actors.family,
        guests=manifest.actors.guests,
        allowed_chats=manifest.allowed_chats,
        verified_bindings=manifest.verified_actor_chat_pairs,
    )

    raw = as_eml(
        WhatsAppInbound(
            message_id="synthetic-wa-1",
            chat_id=PHONE_CHAT,
            actor_id=PHONE,
            text="я заберу его в пятницу",
            display_name=None,
            instance="synthetic-instance",
        )
    )
    context = trusted_run_context(raw, household)

    # The CHAT the control plane stored is the string ingest produces, so the
    # chat half of the authorization pair matches a real turn...
    assert context.chat_id == PHONE_CHAT
    assert (PHONE, context.chat_id) in manifest.verified_actor_chat_pairs
    # ...and the string the removed default would have stored does not. This is
    # the assertion that fails against the first cut of this slice.
    assert context.chat_id != PHONE
    assert (PHONE, PHONE) not in manifest.verified_actor_chat_pairs
    assert PHONE not in household.allowed_chats

    # And the ACTOR half, which was the second [BLOCKER] and is now closed:
    # `trusted_run_context` reports the channel sender, `channel_bindings`
    # stores that same sender as the actor, so the pair matches end to end and
    # the member is a known family member rather than an unknown.
    assert context.actor_id == PHONE
    assert household.knows_binding(context.actor_id, context.chat_id), (
        "the binding the control plane wrote must authorize the turn the"
        " runtime actually receives — both halves of the pair, not one"
    )
    assert context.role != "unknown"


def test_a_challenge_cannot_borrow_the_sender_as_its_chat(cp_stack) -> None:
    """`chat_id` is required, and no longer defaulted from `external_id`.

    Requiring it is the fix rather than deriving it: the control plane does not
    own WhatsApp's JID format, and a derivation right for a 1:1 thread
    (`@s.whatsapp.invalid`) is wrong for a group (`@g.us`) — which is the shared
    conversation this whole slice exists to allow. A guess that answers the
    chat question from the sender is the conflation C3a removed, rebuilt one
    layer up.
    """
    _provisioned(cp_stack)
    with cp_stack.database.write() as connection:
        with pytest.raises(TypeError, match="chat_id"):
            cp_stack.bindings.issue_challenge(
                connection,
                household_id=cp_stack.household.id,
                channel="whatsapp",
                external_id=PHONE,
                actor_id=PHONE,
                role="adult",
                issued_by_actor_id=owner_actor(cp_stack),
                now=BASE_TIME + 100,
            )
        with pytest.raises(BindingError, match="chat ID is required"):
            cp_stack.bindings.issue_challenge(
                connection,
                household_id=cp_stack.household.id,
                channel="whatsapp",
                external_id=PHONE,
                chat_id="   ",
                actor_id=PHONE,
                role="adult",
                issued_by_actor_id=owner_actor(cp_stack),
                now=BASE_TIME + 100,
            )


def test_a_padded_identity_is_stored_as_the_string_ingest_produces(
    cp_stack,
) -> None:
    """Whitespace is not a variant of an identity; it is a different string.

    `Household.knows_binding` compares by string and every ingest path strips
    before it reports — `whatsapp_webhook._text` returns `value.strip()`. So a
    binding stored as `"999…@g.us "` issues, verifies, publishes a revision and
    schedules a rollout, and then matches nothing: another arrangement that
    succeeds at every layer the control plane can see and fails at the only one
    that matters.

    Canonicalizing happens BEFORE the uniqueness checks, which is the half that
    is easy to get wrong. `_reject_foreign_holder` and the already-bound lookup
    both compare by string, so a padded duplicate of a bound sender would slip
    past them and land as a second row for one identity.
    """
    _provisioned(cp_stack)
    owner = owner_actor(cp_stack)
    chat = owner_chat(cp_stack)

    with cp_stack.database.write() as connection:
        issued = cp_stack.bindings.issue_challenge(
            connection,
            household_id=cp_stack.household.id,
            channel=CHANNEL_SELECTION["kind"],
            external_id=f"  {ADULT}\n",
            chat_id=f"\t{chat} ",
            actor_id=f" {ADULT}  ",
            role="adult",
            issued_by_actor_id=owner,
            now=BASE_TIME + 100,
        )
        binding = cp_stack.bindings.verify_challenge(
            connection,
            code=issued.code,
            household_id=cp_stack.household.id,
            owner_actor_id=owner,
            now=BASE_TIME + 200,
        )
        # Stored canonical, so the row says what ingest will say.
        assert (binding.external_id, binding.chat_id, binding.actor_id) == (
            ADULT, chat, ADULT,
        )
        # And the padded form is not a second identity: re-inviting the same
        # sender with different padding is refused as already bound, not
        # written beside it.
        with pytest.raises(BindingError, match="already bound"):
            cp_stack.bindings.issue_challenge(
                connection,
                household_id=cp_stack.household.id,
                channel=CHANNEL_SELECTION["kind"],
                external_id=f"{ADULT} ",
                chat_id=chat,
                actor_id=f"{ADULT} ",
                role="adult",
                issued_by_actor_id=owner,
                now=BASE_TIME + 300,
            )
        # An identity that is only whitespace is nothing at all.
        for field, kwargs in (
            ("external ID", {"external_id": "   ", "chat_id": chat}),
            ("chat ID", {"external_id": "990000007", "chat_id": "  "}),
        ):
            with pytest.raises(BindingError, match=f"{field} is required"):
                cp_stack.bindings.issue_challenge(
                    connection,
                    household_id=cp_stack.household.id,
                    channel=CHANNEL_SELECTION["kind"],
                    actor_id=kwargs["external_id"],
                    role="adult",
                    issued_by_actor_id=owner,
                    now=BASE_TIME + 400,
                    **kwargs,
                )


def _provision_second_household(cp_stack, email: str):
    """Drive a SECOND household to its first revision.

    The suite has never provisioned two, which is exactly how D0 survived: with
    one household the shared constants look like working code. Everything below
    is what `_provisioned` does for the fixture's own household, with the
    account, session and workflow version of another one.
    """
    account = cp_stack.accounts.create_verified(email, now=BASE_TIME)
    household = cp_stack.households.create_for_owner(account.id, now=BASE_TIME)
    session = cp_stack.sessions.issue(account.id, now=BASE_TIME)
    worker = cp_stack.make_worker(now=BASE_TIME + 50)

    def context():
        return cp_stack.context(
            account_id=account.id,
            session_id=session.id,
            expected_version=cp_stack.onboarding.workflow_for_household(
                household.id
            ).version,
        )

    def drain():
        while (result := worker.run_once()) is not None:
            # Since C3e a runtime job stays open until its revision activates.
            assert result.status in {"succeeded", "pending"}, result

    cp_stack.service.save_profile(
        household.id,
        cp_stack.valid_profile(),
        context=context(),
        now=BASE_TIME + 1,
    )
    drain()
    # Consent receipts are per COMMAND, and the shared fixtures carry fixed
    # IDs — a second household reusing one is refused as belonging to another
    # command. Production derives them per household in `web._receipt_id`; only
    # these fixtures are constant, and the same shared-constant habit is what
    # D0 itself turned out to be.
    email = {
        **EMAIL_SELECTION,
        # A second household cannot hold the same inbox, which is a real
        # constraint rather than a fixture artefact: `email_identities`
        # is unique on the address.
        "local_part": "family-agent-two",
        "special_category_restriction_receipt_id": (
            "10000000-0000-4000-8000-000000000020"
        ),
    }
    whatsapp = {**WHATSAPP_SELECTION, "privacy_notice_receipt_id": "synthetic-wa-two"}
    for offset, (kind, selection) in enumerate(
        (
            (StepKind.EMAIL, email),
            (StepKind.WHATSAPP, whatsapp),
            (StepKind.PRIMARY_CHANNEL, CHANNEL_SELECTION),
        ),
        start=2,
    ):
        cp_stack.service.select(
            household.id, kind, selection, context=context(), now=BASE_TIME + offset
        )
        drain()
    return household


def test_a_second_household_can_provision_and_routes_to_itself(cp_stack) -> None:
    """D0: onboarding gave every household the same identity, so only one existed.

    `control_plane/api/web.py` returned `actor_id: "synthetic-owner"` and
    `chat_id: "synthetic-chat"` as constants for EVERY household, and the
    browser posted the same pair. `DesiredSpecPlanner.issue` seeds the owner
    binding from them and `_reject_foreign_holder` refuses an identity another
    household already holds — correctly, because the gateway resolves senders
    across the whole table and would answer `ambiguous_sender` for both. So the
    second family to finish onboarding could not provision at all, and the
    refusal was right while the constants were wrong.

    It predates C3a: before the split the collision happened on `chat_id`,
    which was equally shared. C3a moved which column collides and fixed
    neither.

    The assertion that matters is the last one. Two households provisioning is
    necessary but not sufficient — if both ended up bound under identities that
    happened not to collide, the gateway could still answer `ambiguous_sender`,
    which breaks delivery for the innocent household too.
    """
    _provisioned(cp_stack)
    second = _provision_second_household(cp_stack, "second@family.test")

    first_actor, first_chat = synthetic_channel_identity(cp_stack.household.id)
    second_actor, second_chat = synthetic_channel_identity(second.id)
    assert first_actor != second_actor and first_chat != second_chat

    rows = {
        row["household_id"]: row
        for row in cp_stack.database.query(
            "SELECT household_id, external_id, chat_id FROM channel_bindings"
        )
    }
    assert set(rows) == {cp_stack.household.id, second.id}
    assert (
        rows[cp_stack.household.id]["external_id"],
        rows[cp_stack.household.id]["chat_id"],
    ) == (first_actor, first_chat)
    assert (rows[second.id]["external_id"], rows[second.id]["chat_id"]) == (
        second_actor, second_chat,
    )

    # Publishing what activation would publish. This case is about D0 —
    # each household holding a DISTINCT identity — and since C3c a binding
    # only routes once its revision has activated, which neither household
    # here has done. Staging is asserted in its own case
    # (`tests/test_gateway_routing.py`); simulating it would be the subject.
    with cp_stack.database.write() as connection:
        connection.execute(
            "UPDATE channel_bindings SET published_revision = 1"
            " WHERE published_revision IS NULL"
        )

    # And each sender reaches its OWN household, with no `ambiguous_sender`.
    router = WhatsAppGatewayRouter(
        cp_stack.database, ingress_path=cp_stack.config.database_path.parent / "ing.db"
    )
    assert router.route(first_actor, "telegram").household_id == cp_stack.household.id
    assert router.route(second_actor, "telegram").household_id == second.id


# --- D1: the runtime kept authorizing what the table had revoked -----------

from control_plane.provisioning.rollout import (  # noqa: E402
    find_stale_bindings,
    reconcile_stale_bindings,
)


def _drain(worker) -> None:
    """Run the queue out. `cancelled` is a terminal state, not a failure.

    Scheduling a revision supersedes an older runtime job for the same
    household, and the worker cancels it deliberately. Asserting `succeeded`
    for every job would make this helper fail on the very behaviour a
    re-planning sweep depends on.
    """
    while (result := worker.run_once()) is not None:
        assert result.status in {"succeeded", "cancelled", "pending"}, result


def _activated(cp_stack) -> None:
    """The precondition, not the subject: a household that finished setup.

    `_provisioned` stops at `runtime_provisioning` because activation runs
    through the runtime's bootstrap, which this harness does not host. These
    two writes are exactly what activation performs
    (`provisioning/bootstrap.py:427` and `:440`).
    """
    with cp_stack.database.write() as connection:
        connection.execute(
            "UPDATE households SET status = 'active' WHERE id = ?",
            (cp_stack.household.id,),
        )
        connection.execute(
            "UPDATE onboarding_workflows SET state = 'complete' WHERE household_id = ?",
            (cp_stack.household.id,),
        )
        # Activation also settles the runtime job and publishes the bindings
        # that revision carries. Since C3e the job stays open until it does, so
        # a helper that skipped this left the household looking `active` with a
        # rollout still in flight — and `schedule_runtime_rollout` refuses
        # that, correctly.
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'succeeded', error_code = NULL,"
            " settled_at = 1 WHERE household_id = ? AND kind = 'runtime'"
            " AND settled_at IS NULL",
            (cp_stack.household.id,),
        )
        connection.execute(
            "UPDATE channel_bindings SET published_revision ="
            " (SELECT current_config_revision FROM households WHERE id = ?)"
            " WHERE household_id = ? AND published_revision IS NULL",
            (cp_stack.household.id, cp_stack.household.id),
        )
        # And the revision itself goes live. `config_revisions.status` is the
        # only fact that answers "which revision is serving" — the household
        # column answers "which is being rolled out" — so a simulation that
        # skipped this left every consumer of the real answer seeing nothing
        # activated at all.
        connection.execute(
            "UPDATE config_revisions SET status = 'superseded'"
            " WHERE household_id = ? AND status = 'active'"
            " AND revision != (SELECT current_config_revision FROM households"
            " WHERE id = ?)",
            (cp_stack.household.id, cp_stack.household.id),
        )
        connection.execute(
            "UPDATE config_revisions SET status = 'active', activated_at = 1"
            " WHERE household_id = ? AND revision ="
            " (SELECT current_config_revision FROM households WHERE id = ?)",
            (cp_stack.household.id, cp_stack.household.id),
        )



def _served_pairs(cp_stack, household_id: str):
    """The (actor, chat) pairs the RUNTIME is authorizing right now.

    Read through the real parser from the revision the household is serving,
    not from the newest one planned — those differ exactly when a rollout is
    in flight, which is the state this whole area is about.
    """
    served = cp_stack.households.get(household_id).current_config_revision
    spec = DesiredHouseholdSpecV1.model_validate(
        cp_stack.configs.manifest(household_id, served)
    )
    return parse_runtime_manifest(manifest_to_toml(spec)).verified_actor_chat_pairs


def test_a_revoked_binding_still_authorizes_until_the_sweep_replans(
    cp_stack,
) -> None:
    """D1: revoking a binding does not reach a runtime that is already deployed.

    `Household.knows_binding` answers from the manifest the runtime booted
    with, and the runtime does not read `channel_bindings`. So anything that
    changes the table WITHOUT issuing a revision leaves the two disagreeing —
    and the disagreement is invisible, because every layer the control plane
    can see reports success while the runtime goes on authorizing somebody it
    has been told not to.

    Migration 0010 is the instance: it retires bindings whose identities were
    never provenanced, and nothing re-plans. `ControlPlaneDatabase.migrate`
    does not call the planner and neither does startup.

    The revocation is done here by deleting the row, which is what the
    migration does — not through a lifecycle method — because the point is that
    ANY path which changes the table without a revision leaves this gap.
    """
    _provisioned(cp_stack)
    _activated(cp_stack)
    owner = owner_actor(cp_stack)
    household_chat = owner_chat(cp_stack)

    with cp_stack.database.write() as connection:
        issued = cp_stack.bindings.issue_challenge(
            connection,
            household_id=cp_stack.household.id,
            channel=CHANNEL_SELECTION["kind"],
            external_id=ADULT,
            chat_id=household_chat,
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
        planned = cp_stack.make_worker().planner.issue(
            connection, household_id=cp_stack.household.id
        )
        schedule_runtime_rollout(
            connection,
            jobs=cp_stack.jobs,
            onboarding=cp_stack.onboarding,
            household_id=cp_stack.household.id,
            planned=planned,
            runtime_provider=cp_stack.config.runtime_provider,
            now=BASE_TIME + 201,
        )
    _drain(cp_stack.make_worker(now=BASE_TIME + 202))
    # A scheduled rollout leaves the household `provisioning` until the
    # runtime's bootstrap activates it, which this harness does not host.
    _activated(cp_stack)

    assert (ADULT, household_chat) in _served_pairs(cp_stack, cp_stack.household.id)

    # Now revoke it the way 0010 does: the row goes, and nothing re-plans.
    with cp_stack.database.write() as connection:
        connection.execute(
            "DELETE FROM channel_bindings WHERE actor_id = ?", (ADULT,)
        )

    # The defect, stated: the table has revoked the member and the runtime has
    # not heard. Nothing raised, nothing logged, and the deployed manifest goes
    # on authorizing them.
    assert (ADULT, household_chat) in _served_pairs(cp_stack, cp_stack.household.id)

    with cp_stack.database.write() as connection:
        stale = find_stale_bindings(
            connection,
            configs=cp_stack.configs,
            bindings=cp_stack.bindings,
            onboarding=cp_stack.onboarding,
        )
    assert [item.household_id for item in stale] == [cp_stack.household.id]
    assert stale[0].blocked_by is None
    # The report names counts and identifiers, never the identities themselves.
    report = stale[0].public_dict()
    assert report["revoked"] == 1 and report["added"] == 0
    assert ADULT not in json.dumps(report)

    # A dry run reports and writes nothing — the default, because this
    # schedules real deployments for households nobody asked about.
    with cp_stack.database.write() as connection:
        dry = reconcile_stale_bindings(
            connection,
            planner=cp_stack.make_worker().planner,
            jobs=cp_stack.jobs,
            onboarding=cp_stack.onboarding,
            configs=cp_stack.configs,
            bindings=cp_stack.bindings,
            runtime_provider=cp_stack.config.runtime_provider,
        )
    assert [item["action"] for item in dry] == ["would_reconcile"]
    assert (ADULT, household_chat) in _served_pairs(cp_stack, cp_stack.household.id)

    with cp_stack.database.write() as connection:
        applied = reconcile_stale_bindings(
            connection,
            planner=cp_stack.make_worker().planner,
            jobs=cp_stack.jobs,
            onboarding=cp_stack.onboarding,
            configs=cp_stack.configs,
            bindings=cp_stack.bindings,
            runtime_provider=cp_stack.config.runtime_provider,
            apply=True,
            now=BASE_TIME + 300,
        )
    assert [item["action"] for item in applied] == ["reconciled"]
    _drain(cp_stack.make_worker(now=BASE_TIME + 301))
    _activated(cp_stack)

    # The runtime now authorizes what the table says, and only that.
    pairs = _served_pairs(cp_stack, cp_stack.household.id)
    assert (ADULT, household_chat) not in pairs
    assert (owner, household_chat) in pairs

    # And a second sweep finds nothing: the fix converges rather than
    # re-planning the same household on every run.
    with cp_stack.database.write() as connection:
        assert find_stale_bindings(
            connection,
            configs=cp_stack.configs,
            bindings=cp_stack.bindings,
            onboarding=cp_stack.onboarding,
        ) == ()


def test_a_household_still_settling_is_reported_and_never_forced(cp_stack) -> None:
    """A blocked household is skipped, not pushed through.

    `schedule_runtime_rollout` refuses a household that is not `active` with a
    `complete` workflow, and those are the states where forcing strands jobs: a
    second rollout while the first is in flight overwrites the single
    `current_config_revision`, and then neither job can match it.

    The sweep asks the same two facts that scheduler asks, so the report cannot
    promise a rollout the scheduler then declines.

    Staleness is created by VERIFYING a member the served revision does not
    carry, which is the case a settling household actually reaches — an
    invitation redeemed while a rollout is in flight. An earlier version of
    this test deleted every binding the household had, including the owner's,
    and so ran into `owner_binding_retired` instead of the state it names.
    """
    _provisioned(cp_stack)
    _activated(cp_stack)
    owner = owner_actor(cp_stack)
    with cp_stack.database.write() as connection:
        issued = cp_stack.bindings.issue_challenge(
            connection,
            household_id=cp_stack.household.id,
            channel=CHANNEL_SELECTION["kind"],
            external_id=ADULT,
            chat_id=owner_chat(cp_stack),
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
        # A rollout genuinely in flight. Writing the status alone is no longer
        # enough to express that, and that is the point of this phase: a
        # household at `provisioning` with no job left is STRANDED, and the
        # sweep must be able to repair it. Busy is the open job.
        connection.execute(
            "UPDATE households SET status = 'provisioning' WHERE id = ?",
            (cp_stack.household.id,),
        )
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'pending', settled_at = NULL,"
            " error_code = 'awaiting_activation' WHERE household_id = ?"
            " AND kind = 'runtime'",
            (cp_stack.household.id,),
        )
        stale = find_stale_bindings(
            connection,
            configs=cp_stack.configs,
            bindings=cp_stack.bindings,
            onboarding=cp_stack.onboarding,
        )
        # The reason names the rollout, not the row: `provisioning` alone
        # cannot tell a household that is busy from one that is stranded, and
        # telling them apart is what lets the sweep repair the second.
        assert [item.blocked_by for item in stale] == ["rollout_in_flight"]

        applied = reconcile_stale_bindings(
            connection,
            planner=cp_stack.make_worker().planner,
            jobs=cp_stack.jobs,
            onboarding=cp_stack.onboarding,
            configs=cp_stack.configs,
            bindings=cp_stack.bindings,
            runtime_provider=cp_stack.config.runtime_provider,
            apply=True,
        )
        assert [item["action"] for item in applied] == ["skipped"]
        # Nothing was queued for a household that could not take one.
        assert connection.execute(
            "SELECT COUNT(*) AS total FROM provisioning_jobs WHERE operation = ?",
            (REPROVISION_RUNTIME_OPERATION,),
        ).fetchone()["total"] == 0


# --- D3: canonical means each channel's rule, not strip() ------------------

from control_plane.channels import (  # noqa: E402
    ChannelIdentityError,
    canonical_chat,
    canonical_sender,
)
from hermes_cloud.channels.telegram import parse_update  # noqa: E402
from hermes_cloud.ingest.whatsapp_webhook import (  # noqa: E402
    parse_webhook,
)


def _whatsapp_ingest(sender: str, chat: str) -> tuple[str, str]:
    """The (actor, chat) a REAL WhatsApp turn arrives as.

    Driven all the way through the provider's own parser, `as_eml`, and
    `trusted_run_context` rather than asserted against a string this test made
    up. A canonicalizer checked against an invented expectation proves the test
    and the code agree, which is not the property that matters.
    """
    payload = json.dumps({
        "instance": "synthetic-instance",
        "message": {
            "id": "synthetic-wa-canon",
            "from": sender,
            "remote_jid": chat,
            "text": "привет",
        },
    }).encode()
    inbound = parse_webhook(payload, expected_instance="synthetic-instance")
    raw = as_eml(inbound)
    household = Household(
        household_id="h", family=frozenset(), allowed_chats=frozenset()
    )
    context = trusted_run_context(raw, household)
    return context.actor_id, context.chat_id


def _telegram_ingest(sender: str, chat: str) -> tuple[str, str]:
    """The same, for Telegram, through `parse_update`."""
    household = Household(
        household_id="h", family=frozenset(), allowed_chats=frozenset()
    )
    # As NUMBERS, which is the whole point. Telegram's Bot API sends `id` as a
    # JSON integer and `parse_update` renders it with `str()`, so no spelling
    # with a leading zero can survive the round trip. Passing strings here made
    # the helper able to "produce" `00123`, and a canonicalizer checked against
    # that agrees with a payload the provider cannot send.
    message = parse_update(
        {"message": {"message_id": 1, "text": "привет",
                     "from": {"id": int(sender)}, "chat": {"id": int(chat)}}},
        household,
    )
    return message.context.actor_id, message.context.chat_id


@pytest.mark.parametrize(
    ("channel", "spellings", "sender", "chat"),
    [
        pytest.param(
            "whatsapp",
            # Bare and `+`-prefixed are the same person; `as_eml` adds the `+`,
            # so that is the form authorization sees. A JID's domain is
            # case-insensitive where its local part is not.
            ("  +999511234\n", "999511234", "+999511234 "),
            "+999511234",
            "999511234@s.whatsapp.invalid",
            id="whatsapp",
        ),
        pytest.param(
            "telegram",
            (ADULT, f" {ADULT} ", f"{ADULT}\t"),
            ADULT,
            TELEGRAM_CHAT,
            id="telegram",
        ),
    ],
)
def test_canonical_identity_is_what_that_channels_ingest_produces(
    channel, spellings, sender, chat
) -> None:
    """Every spelling normalizes to the ONE string a real turn carries.

    `strip()` closed the reported case and only that. Whitespace is the easiest
    way to spell an identity wrongly, not the only one — and two spellings of
    one identity are two rows, of which the second authorizes nobody.

    The expected value is not written down here. It is whatever the channel's
    own parser emits for the same turn, so this fails if the canonicalizer and
    the ingest path ever drift, which is the agreement the debt plan wanted a
    per-channel owner for and that the dependency direction forbade.
    """
    ingest = _whatsapp_ingest if channel == "whatsapp" else _telegram_ingest
    actual_sender, actual_chat = ingest(sender, chat)

    for spelling in spellings:
        assert canonical_sender(channel, spelling) == actual_sender
    for spelling in (chat, f"  {chat} ", chat.upper() if channel == "whatsapp" else chat):
        assert canonical_chat(channel, spelling) == actual_chat


@pytest.mark.parametrize(
    ("field", "channel", "value", "reason"),
    [
        # The conflation C3a removed, refused at the door: a WhatsApp
        # conversation is a JID, and a phone number is not one.
        ("chat", "whatsapp", "+999511234", "JID"),
        ("sender", "whatsapp", "not-a-number", "phone number"),
        ("chat", "telegram", "not-an-id", "numeric chat ID"),
        ("sender", "telegram", "user-two", "numeric user ID"),
        ("sender", "whatsapp", "   ", "external ID is required"),
        ("chat", "telegram", "  ", "chat ID is required"),
        # A Telegram ID is a JSON number, so `str()` can never render a
        # leading zero. A padded ID is not another spelling of `ADULT` the
        # way a bare phone number is another spelling of `+999…` — it is a
        # value no inbound turn carries, and storing it would issue, verify,
        # publish and roll out a binding that then matches nothing. Refused
        # at the door instead. The spellings are BUILT rather than written:
        # a padded digit run is outside the reserved ranges the fixture
        # sanitizer allows, which is a rule about literals in this repo.
        ("sender", "telegram", f"0{ADULT}", "numeric user ID"),
        ("chat", "telegram", f"-0{TELEGRAM_CHAT.lstrip('-')}", "numeric chat ID"),
    ],
)
def test_an_identity_that_channel_could_never_carry_is_refused(
    field, channel, value, reason
) -> None:
    """One field per case, so a passing assertion names which rule fired.

    An earlier version called both canonicalizers inside one `raises` block,
    where the first refusal hides whether the second would have refused at all.
    """
    canonicalize = canonical_sender if field == "sender" else canonical_chat
    with pytest.raises(ChannelIdentityError, match=reason):
        canonicalize(channel, value)


def test_the_synthetic_namespace_is_canonical_as_written(cp_stack) -> None:
    """B-07's identities have no transport form to normalize into.

    `synthetic-owner.<household>` is not a phone number waiting to be
    reshaped, and applying WhatsApp's rule to it would refuse the only
    identities this deployment is allowed to hold. It is an opaque token, so
    the rule for it is the rule for any opaque token: strip and store.
    """
    actor, chat = synthetic_channel_identity(cp_stack.household.id)
    for channel in ("telegram", "whatsapp", "web"):
        assert canonical_sender(channel, f"  {actor} ") == actor
        assert canonical_chat(channel, f"\t{chat}") == chat


# --- D4 and D5: the revocation guards, asserted directly -------------------


def test_two_households_cannot_hold_one_actor_on_one_channel(
    cp_stack, second_household
) -> None:
    """D4: the guard covers both readings of the identity, not just one.

    `_reject_foreign_holder` asked about `external_id` alone. `actor_id` is the
    same transport identity read by the runtime instead of the gateway, and
    C3a enforces the two equal on every new row — so today the actor predicate
    can never fire on its own.

    It is asserted anyway, and directly rather than as a consequence of that
    equality, because "they happen to be equal" is not the rule. Migration
    `0010` has already had to delete both halves of a shared-actor pair that
    legacy rows could hold, and a future write path that relaxes the equality
    somewhere else must not quietly reintroduce it.
    """
    with cp_stack.database.write() as connection:
        cp_stack.bindings.ensure_owner_binding(
            connection,
            household_id=second_household,
            channel="telegram",
            external_id=ADULT,
            chat_id="synthetic-other-chat",
            actor_id=ADULT,
            now=BASE_TIME,
        )
        # The identity is held by another household under BOTH names, so both
        # halves of the guard have something to find.
        row = cp_stack.database.query_one(
            "SELECT external_id, actor_id FROM channel_bindings WHERE household_id = ?",
            (second_household,),
        )
        assert row["external_id"] == row["actor_id"] == ADULT

        with pytest.raises(BindingError, match="another household"):
            cp_stack.bindings.ensure_owner_binding(
                connection,
                household_id=cp_stack.household.id,
                channel="telegram",
                external_id=ADULT,
                chat_id="synthetic-our-chat",
                actor_id=ADULT,
                now=BASE_TIME + 1,
            )

        # And the actor predicate specifically: a row whose sender differs but
        # whose actor collides is still refused. Only a direct write can build
        # that shape now, which is the point of checking it.
        connection.execute(
            "UPDATE channel_bindings SET external_id = ? WHERE household_id = ?",
            ("990000777", second_household),
        )
        with pytest.raises(BindingError, match="another household"):
            cp_stack.bindings.ensure_owner_binding(
                connection,
                household_id=cp_stack.household.id,
                channel="telegram",
                external_id=ADULT,
                chat_id="synthetic-our-chat",
                actor_id=ADULT,
                now=BASE_TIME + 2,
            )


def test_moving_the_household_chat_retires_only_that_conversation(cp_stack) -> None:
    """D5, corrected: retirement is scoped to the chat, not to the channel.

    The debt plan asked that nothing of any role remain on the channel the
    household LEFT. That is too broad. An adult verified in a thread of their
    own was never re-attested by the owner moving house, and a non-primary
    channel is a supported arrangement — the owner's departure from it does not
    revoke everybody else's presence.

    What IS stale is a binding into the conversation the owner has just left:
    nobody speaks there for the household any more, so an actor authorized in
    it is authorized in an abandoned room.

    Three members, three fates, one move.
    """
    hid = cp_stack.household.id
    with cp_stack.database.write() as connection:
        cp_stack.bindings.ensure_owner_binding(
            connection, household_id=hid, channel="telegram",
            external_id="synthetic-owner-one", chat_id="synthetic-family-chat",
            actor_id="synthetic-owner-one", now=BASE_TIME,
        )
        for external_id, channel, chat in (
            # Shares the household chat the owner is about to leave.
            (ADULT, "telegram", "synthetic-family-chat"),
            # Same channel, a thread of their own.
            ("990000003", "telegram", "-100990000303"),
            # A different channel entirely.
            (PHONE, "whatsapp", PHONE_CHAT),
        ):
            issued = cp_stack.bindings.issue_challenge(
                connection, household_id=hid, channel=channel,
                external_id=external_id, chat_id=chat, actor_id=external_id,
                role="adult", issued_by_actor_id="synthetic-owner-one",
                now=BASE_TIME + 1,
            )
            cp_stack.bindings.verify_challenge(
                connection, code=issued.code, household_id=hid,
                owner_actor_id="synthetic-owner-one", now=BASE_TIME + 2,
            )

        # The family moves to a new group, keeping their Telegram user IDs.
        cp_stack.bindings.ensure_owner_binding(
            connection, household_id=hid, channel="telegram",
            external_id="synthetic-owner-one", chat_id="synthetic-new-chat",
            actor_id="synthetic-owner-one", now=BASE_TIME + 10,
        )
        rows = cp_stack.bindings.verified(connection, household_id=hid)

    survivors = {(r.channel, r.actor_id, r.chat_id, r.role) for r in rows}
    assert survivors == {
        # The member who was only ever in the abandoned chat is retired.
        ("telegram", "990000003", "-100990000303", "adult"),
        ("whatsapp", PHONE, PHONE_CHAT, "adult"),
        ("telegram", "synthetic-owner-one", "synthetic-new-chat", "owner"),
    }
    assert ADULT not in {r.actor_id for r in rows}
# --- D6: comparing identities, and a sweep that isolates its households ----


def test_a_legacy_spelling_still_holds_the_identity_against_another_household(
    cp_stack,
) -> None:
    """The guard compares identities, not the spellings they were stored in.

    `_canonical_pair` puts the CANDIDATE into canonical form, and every row
    written since D3 is canonical too — but rows written before it are stored
    as they were typed. A household holding the bare `999511234` therefore did
    not, by string comparison, hold `+999511234`, so the next household could
    claim the same physical sender and `_reject_foreign_holder` would allow it:
    one transport identity, two households, which is the single thing that
    guard exists to prevent.

    The legacy row is inserted through SQL on purpose. Going through the
    repository would canonicalize it and the case under test would not exist —
    the row has to be what a pre-D3 write actually left behind.
    """
    _provisioned(cp_stack)
    second = _provision_second_household(cp_stack, "legacy@family.test")
    second_owner, _ = synthetic_channel_identity(second.id)

    with cp_stack.database.write() as connection:
        connection.execute(
            "INSERT INTO channel_bindings (id, household_id, channel,"
            " external_id, chat_id, actor_id, role, verified_at,"
            " verified_by_actor_id) VALUES ('legacy', ?, 'whatsapp',"
            " '999511234', ?, '999511234', 'adult', ?, 'o')",
            (cp_stack.household.id, PHONE_CHAT, BASE_TIME),
        )
        with pytest.raises(BindingError, match="another household"):
            cp_stack.bindings.issue_challenge(
                connection,
                household_id=second.id,
                channel="whatsapp",
                external_id=PHONE,
                chat_id=PHONE_CHAT,
                actor_id=PHONE,
                role="adult",
                issued_by_actor_id=second_owner,
                now=BASE_TIME + 100,
            )


def test_the_sweep_never_re_creates_an_owner_binding_a_migration_retired(
    cp_stack,
) -> None:
    """D1 must propagate a revocation, not undo it.

    `DesiredSpecPlanner.issue` seeds the owner row from the onboarding result
    every time it runs, which is right while onboarding is the thing that
    proved the channel. Re-planning a household whose owner binding migration
    0010 RETIRED would write that identity straight back from the step it came
    from — and 0010 retires an owner binding exactly when its identity cannot
    be trusted: two households recorded under one owner actor, where reseeding
    also re-creates the collision the migration removed.

    So the sweep reports such a household and leaves it alone. It cannot be
    repaired without inventing the identity that was taken away; what it needs
    is re-onboarding, which captures one.
    """
    _provisioned(cp_stack)
    _activated(cp_stack)
    with cp_stack.database.write() as connection:
        # Exactly what 0010 does to an owner row it cannot trust.
        connection.execute(
            "DELETE FROM channel_bindings WHERE household_id = ? AND role = 'owner'",
            (cp_stack.household.id,),
        )
        stale = find_stale_bindings(
            connection,
            configs=cp_stack.configs,
            bindings=cp_stack.bindings,
            onboarding=cp_stack.onboarding,
        )
        assert [item.blocked_by for item in stale] == ["owner_binding_retired"]

        applied = reconcile_stale_bindings(
            connection,
            planner=cp_stack.make_worker().planner,
            jobs=cp_stack.jobs,
            onboarding=cp_stack.onboarding,
            configs=cp_stack.configs,
            bindings=cp_stack.bindings,
            runtime_provider=cp_stack.config.runtime_provider,
            apply=True,
        )
        assert [item["action"] for item in applied] == ["skipped"]
        # The identity stays retired, and nothing was queued to deploy it.
        assert not connection.execute(
            "SELECT id FROM channel_bindings WHERE household_id = ?",
            (cp_stack.household.id,),
        ).fetchall()
        assert connection.execute(
            "SELECT COUNT(*) AS total FROM provisioning_jobs WHERE operation = ?",
            (REPROVISION_RUNTIME_OPERATION,),
        ).fetchone()["total"] == 0


def test_one_household_the_planner_refuses_leaves_the_others_reconciled(
    cp_stack,
) -> None:
    """A sweep visits households that have nothing to do with each other.

    `cli.main` holds ONE write transaction around the whole report, so a
    planner refusal used to roll back the revisions and jobs already created
    for earlier households and abandon every later one — one unplannable
    household leaving every other stale runtime authorizing bindings the table
    has revoked. That is the opposite of what this command exists to do.

    The refusal cannot be predicted by `_blocked_by`: `issue` rejects an
    incomplete profile, an unverified provider result, a missing account owner
    and a consent receipt that is absent, revoked or superseded, and asking
    those questions here would be a second copy of the planner's preconditions
    that drifts from the first. So the planner decides, per household, inside
    its own savepoint, and what it said is what the report carries.
    """
    _provisioned(cp_stack)
    # Before the first household is activated: `_provision_second_household`
    # drains the queue, and it asserts every job it drains succeeded.
    refused = _provision_second_household(cp_stack, "refused@family.test")
    _activated(cp_stack)
    owner = owner_actor(cp_stack)

    # The refused household must reach the PLANNER, which means it cannot be
    # blocked earlier for an unrelated reason. Since C3e its runtime job is
    # still open awaiting activation, and `rollout_in_flight` would answer
    # before the planner was ever asked — so this settles it the way
    # activation does, leaving the planner's own refusal as the only thing in
    # the way. That is what the case is about.
    with cp_stack.database.write() as connection:
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'succeeded', error_code = NULL,"
            " settled_at = 1 WHERE household_id = ? AND kind = 'runtime'"
            " AND settled_at IS NULL",
            (refused.id,),
        )

    with cp_stack.database.write() as connection:
        # The plannable household is stale because a member was verified after
        # its revision was deployed — the ordinary case this sweep repairs.
        issued = cp_stack.bindings.issue_challenge(
            connection,
            household_id=cp_stack.household.id,
            channel=CHANNEL_SELECTION["kind"],
            external_id=ADULT,
            chat_id=owner_chat(cp_stack),
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
        # The second household finished setup and is stale the same way, so it
        # passes every check `_blocked_by` can make…
        connection.execute(
            "UPDATE households SET status = 'active' WHERE id = ?", (refused.id,)
        )
        connection.execute(
            "UPDATE onboarding_workflows SET state = 'complete' WHERE household_id = ?",
            (refused.id,),
        )
        # …including having a revision that actually SERVES something. Without
        # one the sweep does not consider it at all, and rightly: a household
        # that has never activated anything has no runtime authorizing a stale
        # set. It has to be a live household for the planner's refusal to be
        # the thing under test.
        connection.execute(
            "UPDATE config_revisions SET status = 'active', activated_at = 1"
            " WHERE household_id = ? AND revision ="
            " (SELECT current_config_revision FROM households WHERE id = ?)",
            (refused.id, refused.id),
        )
        connection.execute(
            "INSERT INTO channel_bindings (id, household_id, channel,"
            " external_id, chat_id, actor_id, role, verified_at,"
            " verified_by_actor_id) VALUES ('extra', ?, 'whatsapp', ?, ?, ?,"
            " 'adult', ?, 'o')",
            (refused.id, PHONE, PHONE_CHAT, PHONE, BASE_TIME),
        )
        # … and the planner still refuses it, on a precondition of its own.
        connection.execute(
            "UPDATE onboarding_steps SET status = 'waiting_user' WHERE kind ="
            " 'primary_channel' AND workflow_id = (SELECT id FROM"
            " onboarding_workflows WHERE household_id = ?)",
            (refused.id,),
        )

        revisions = connection.execute(
            "SELECT COUNT(*) AS total FROM config_revisions"
        ).fetchone()["total"]
        # The dry run classifies both households the way the apply run will —
        # including the one only the planner can refuse — and writes nothing.
        dry = {
            item["household_id"]: item
            for item in reconcile_stale_bindings(
                connection,
                planner=cp_stack.make_worker().planner,
                jobs=cp_stack.jobs,
                onboarding=cp_stack.onboarding,
                configs=cp_stack.configs,
                bindings=cp_stack.bindings,
                runtime_provider=cp_stack.config.runtime_provider,
            )
        }
        assert dry[refused.id]["action"] == "skipped"
        assert str(dry[refused.id]["blocked_by"]).startswith("planner_refused")
        assert dry[cp_stack.household.id]["action"] == "would_reconcile"
        assert connection.execute(
            "SELECT COUNT(*) AS total FROM config_revisions"
        ).fetchone()["total"] == revisions
        assert not connection.execute(
            "SELECT id FROM provisioning_jobs WHERE operation = ?",
            (REPROVISION_RUNTIME_OPERATION,),
        ).fetchall()

        applied = {
            item["household_id"]: item
            for item in reconcile_stale_bindings(
                connection,
                planner=cp_stack.make_worker().planner,
                jobs=cp_stack.jobs,
                onboarding=cp_stack.onboarding,
                configs=cp_stack.configs,
                bindings=cp_stack.bindings,
                runtime_provider=cp_stack.config.runtime_provider,
                apply=True,
            )
        }

    assert applied[refused.id]["action"] == "skipped"
    assert str(applied[refused.id]["blocked_by"]).startswith("planner_refused")
    # The point of the savepoint: the other household was reconciled anyway,
    # and its rollout survived the refusal.
    assert applied[cp_stack.household.id]["action"] == "reconciled"
    assert cp_stack.database.query(
        "SELECT id FROM provisioning_jobs WHERE operation = ? AND household_id = ?",
        (REPROVISION_RUNTIME_OPERATION, cp_stack.household.id),
    )
    assert not cp_stack.database.query(
        "SELECT id FROM provisioning_jobs WHERE operation = ? AND household_id = ?",
        (REPROVISION_RUNTIME_OPERATION, refused.id),
    )


@pytest.mark.parametrize(
    "writer", ("issue_challenge", "verify_challenge", "ensure_owner_binding")
)
def test_a_legacy_spelling_is_one_identity_to_every_writer(cp_stack, writer) -> None:
    """The household's own rows are matched canonically too, not just others'.

    `_reject_foreign_holder` asks about OTHER households. The three writers
    below each asked their own household the same question with an exact-string
    `WHERE external_id = ?`, so a household holding the bare `999511234` did
    not, to any of them, hold `+999511234` — and the challenge that should have
    been refused, or the row that should have been reconciled, produced a
    SECOND row for one transport sender instead: a duplicate actor in the
    manifest, and binding and revision capacity spent on it.

    One lookup answers all four now. The parameterisation is per writer so a
    passing assertion names which one was covered.
    """
    _provisioned(cp_stack)
    owner = owner_actor(cp_stack)
    bare = PHONE.lstrip("+")

    with cp_stack.database.write() as connection:
        if writer == "verify_challenge":
            # Issued while the table held no such sender, which is the only
            # order in which a challenge for this identity can exist. The
            # legacy row is written underneath it below: what is under test is
            # the STATE the two make together, not the sequence.
            issued = cp_stack.bindings.issue_challenge(
                connection,
                household_id=cp_stack.household.id,
                channel="whatsapp",
                external_id=PHONE,
                chat_id=PHONE_CHAT,
                actor_id=PHONE,
                role="adult",
                issued_by_actor_id=owner,
                now=BASE_TIME + 100,
            )
        # The row as a pre-D3 write left it: the same person, spelled bare.
        connection.execute(
            "INSERT INTO channel_bindings (id, household_id, channel,"
            " external_id, chat_id, actor_id, role, verified_at,"
            " verified_by_actor_id) VALUES ('legacy', ?, 'whatsapp', ?, ?, ?,"
            " 'adult', ?, 'o')",
            (cp_stack.household.id, bare, PHONE_CHAT, bare, BASE_TIME),
        )

        if writer == "issue_challenge":
            with pytest.raises(BindingError, match="already bound to that member"):
                cp_stack.bindings.issue_challenge(
                    connection,
                    household_id=cp_stack.household.id,
                    channel="whatsapp",
                    external_id=PHONE,
                    chat_id=PHONE_CHAT,
                    actor_id=PHONE,
                    role="adult",
                    issued_by_actor_id=owner,
                    now=BASE_TIME + 200,
                )
        elif writer == "verify_challenge":
            cp_stack.bindings.verify_challenge(
                connection,
                code=issued.code,
                household_id=cp_stack.household.id,
                owner_actor_id=owner,
                now=BASE_TIME + 200,
            )
        else:
            # The owner moving onto an identity a MEMBER holds. This one is not
            # about a duplicate row: `_retire_superseded` would have cleared
            # the household's rows and inserted over them, so the legacy adult
            # was silently unbound to make room — which the refusal below
            # exists to prevent and the exact-string lookup could not see.
            with pytest.raises(
                BindingError, match="another member of the household"
            ):
                cp_stack.bindings.ensure_owner_binding(
                    connection,
                    household_id=cp_stack.household.id,
                    channel="whatsapp",
                    external_id=PHONE,
                    chat_id=PHONE_CHAT,
                    actor_id=PHONE,
                    now=BASE_TIME + 200,
                )

        # One transport sender, one row — whichever spelling reached the table
        # first.
        held = connection.execute(
            "SELECT external_id FROM channel_bindings WHERE household_id = ?"
            " AND channel = 'whatsapp'",
            (cp_stack.household.id,),
        ).fetchall()
        assert [row["external_id"] for row in held] in ([bare], [PHONE])


# --- C3c Phase 3: a terminal failure retires what it staged ----------------


def test_a_terminal_failure_retires_the_members_it_staged(cp_stack) -> None:
    """A staged binding whose revision dies is a dead end, not a delay.

    It never routes, and it holds its identity against every future attempt:
    `issue_challenge` refuses a tuple this household already holds and
    `_reject_foreign_holder` refuses it to every other. So the owner cannot
    re-invite that member and nobody else can claim the sender either — the
    failure of a rollout would permanently consume an identity.

    The owner's row is left alone even though it is staged too. It is re-seeded
    from the durable onboarding result on every plan, so the next successful
    activation publishes the same row; deleting it would leave `owner_actor`
    returning None and the household unable to invite anyone at all.
    """
    _provisioned(cp_stack)
    _activated(cp_stack)
    owner = owner_actor(cp_stack)
    household_chat = owner_chat(cp_stack)

    with cp_stack.database.write() as connection:
        issued = cp_stack.bindings.issue_challenge(
            connection,
            household_id=cp_stack.household.id,
            channel=CHANNEL_SELECTION["kind"],
            external_id=ADULT,
            chat_id=household_chat,
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
        rows = cp_stack.bindings.verified(
            connection, household_id=cp_stack.household.id
        )
    # The owner is published — its revision activated — and the new member is
    # staged behind a rollout that has not.
    published = {r.actor_id: r.published_revision for r in rows}
    assert published[ADULT] is None
    assert published[owner] is not None

    with cp_stack.database.write() as connection:
        retired = cp_stack.bindings.retire_staged_members(
            connection, household_id=cp_stack.household.id
        )
        rows = cp_stack.bindings.verified(
            connection, household_id=cp_stack.household.id
        )
    assert retired == 1
    assert [r.actor_id for r in rows] == [owner]

    # And the identity is free again, which is the whole point.
    with cp_stack.database.write() as connection:
        cp_stack.bindings.issue_challenge(
            connection,
            household_id=cp_stack.household.id,
            channel=CHANNEL_SELECTION["kind"],
            external_id=ADULT,
            chat_id=household_chat,
            actor_id=ADULT,
            role="adult",
            issued_by_actor_id=owner,
            now=BASE_TIME + 300,
        )


def test_retirement_never_touches_a_published_binding(cp_stack) -> None:
    """Published means routing, and routing members are not this to remove."""
    _provisioned(cp_stack)
    _activated(cp_stack)
    owner = owner_actor(cp_stack)

    with cp_stack.database.write() as connection:
        issued = cp_stack.bindings.issue_challenge(
            connection,
            household_id=cp_stack.household.id,
            channel="whatsapp",
            external_id=PHONE,
            chat_id=PHONE_CHAT,
            actor_id=PHONE,
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
        # What activation would have done for a revision that succeeded.
        connection.execute(
            "UPDATE channel_bindings SET published_revision = 1"
            " WHERE household_id = ? AND actor_id = ?",
            (cp_stack.household.id, PHONE),
        )
        retired = cp_stack.bindings.retire_staged_members(
            connection, household_id=cp_stack.household.id
        )
        rows = cp_stack.bindings.verified(
            connection, household_id=cp_stack.household.id
        )

    assert retired == 0
    assert PHONE in {r.actor_id for r in rows}


def test_the_sweep_repairs_a_household_no_job_will_ever_return_to(cp_stack) -> None:
    """C3c Phase 4: the D1 unblock.

    `reconcile_stale_bindings` exists to find households whose runtime is
    serving a binding set the table no longer has, and it could not reach the
    ones that need it most. `_blocked_by` refused anything not `active`, and a
    household stranded by a failed rollout sits at `provisioning` — reported
    `household_status_provisioning` and skipped, for good, because
    `JobsRepository.lease` never returns a settled job and `reconcile` refuses
    anything that is not `outcome_unknown`.

    The household row cannot tell busy from stranded; the QUEUE can. Busy means
    a rollout is still in flight and a second would collide with it. Stranded
    means nothing is coming.

    Repair is a roll forward. Planning the next revision converges the runtime
    whatever it currently holds, which is a claim this side can honestly make
    where "you are back on N-1" is not — that is why
    `_restore_settled_household` declines once the provider has been touched.
    """
    _provisioned(cp_stack)
    _activated(cp_stack)
    owner = owner_actor(cp_stack)

    with cp_stack.database.write() as connection:
        issued = cp_stack.bindings.issue_challenge(
            connection,
            household_id=cp_stack.household.id,
            channel=CHANNEL_SELECTION["kind"],
            external_id=ADULT,
            chat_id=owner_chat(cp_stack),
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
        # A rollout that died without handing the household back: the job is
        # terminal, and the household was left mid-flight.
        planned = cp_stack.make_worker().planner.issue(
            connection, household_id=cp_stack.household.id
        )
        schedule_runtime_rollout(
            connection,
            jobs=cp_stack.jobs,
            onboarding=cp_stack.onboarding,
            household_id=cp_stack.household.id,
            planned=planned,
            runtime_provider=cp_stack.config.runtime_provider,
            now=BASE_TIME + 201,
        )
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'failed', settled_at = 1,"
            " error_code = 'activation_deadline_passed' WHERE household_id = ?"
            " AND kind = 'runtime' AND settled_at IS NULL",
            (cp_stack.household.id,),
        )

    household = cp_stack.database.query_one(
        "SELECT status FROM households WHERE id = ?", (cp_stack.household.id,)
    )
    assert household["status"] == "provisioning", "the precondition, not the subject"

    with cp_stack.database.write() as connection:
        stale = find_stale_bindings(
            connection,
            configs=cp_stack.configs,
            bindings=cp_stack.bindings,
            onboarding=cp_stack.onboarding,
        )
        # Reachable. This is the assertion the whole phase is for: before it,
        # every one of these answered `household_status_provisioning`.
        assert [item.blocked_by for item in stale] == [None]

        applied = reconcile_stale_bindings(
            connection,
            planner=cp_stack.make_worker().planner,
            jobs=cp_stack.jobs,
            onboarding=cp_stack.onboarding,
            configs=cp_stack.configs,
            bindings=cp_stack.bindings,
            runtime_provider=cp_stack.config.runtime_provider,
            apply=True,
            now=BASE_TIME + 300,
        )
    assert [item["action"] for item in applied] == ["reconciled"]

    # And a fresh rollout is queued, which is what rolling forward means.
    assert cp_stack.database.query_one(
        "SELECT COUNT(*) AS total FROM provisioning_jobs WHERE household_id = ?"
        " AND kind = 'runtime' AND settled_at IS NULL",
        (cp_stack.household.id,),
    )["total"] == 1


def test_the_sweep_reaches_a_stranded_household_whose_drift_was_retired(
    cp_stack,
) -> None:
    """The same strandedness, reached the way the worker actually reaches it.

    The test above marks the job failed by hand, which leaves the staged member
    in the table — so the binding sets disagree and `find_stale_bindings` finds
    the household through drift. The real path does one more thing:
    `_mark_step_problem` calls `retire_staged_members` before
    `_restore_settled_household` declines to move a household whose provider was
    touched. Deleting that member puts the table back to EXACTLY the manifest
    the runtime is still serving.

    So the household is stranded at `provisioning` with no drift at all, and the
    equality check skipped it before reading the queue — the mechanism built to
    reach stranded households was blind to the one path that actually strands
    them. Drift is how this sweep usually finds work; it is not the only way a
    household needs converging.
    """
    _provisioned(cp_stack)
    _activated(cp_stack)
    owner = owner_actor(cp_stack)

    with cp_stack.database.write() as connection:
        issued = cp_stack.bindings.issue_challenge(
            connection,
            household_id=cp_stack.household.id,
            channel=CHANNEL_SELECTION["kind"],
            external_id=ADULT,
            chat_id=owner_chat(cp_stack),
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
        planned = cp_stack.make_worker().planner.issue(
            connection, household_id=cp_stack.household.id
        )
        schedule_runtime_rollout(
            connection,
            jobs=cp_stack.jobs,
            onboarding=cp_stack.onboarding,
            household_id=cp_stack.household.id,
            planned=planned,
            runtime_provider=cp_stack.config.runtime_provider,
            now=BASE_TIME + 201,
        )
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'failed', settled_at = 1,"
            " error_code = 'activation_deadline_passed' WHERE household_id = ?"
            " AND kind = 'runtime' AND settled_at IS NULL",
            (cp_stack.household.id,),
        )
        # The step the other test omits, and the whole point of this one.
        cp_stack.bindings.retire_staged_members(
            connection, household_id=cp_stack.household.id
        )

    household = cp_stack.database.query_one(
        "SELECT status FROM households WHERE id = ?", (cp_stack.household.id,)
    )
    assert household["status"] == "provisioning", "the precondition, not the subject"

    with cp_stack.database.write() as connection:
        stale = find_stale_bindings(
            connection,
            configs=cp_stack.configs,
            bindings=cp_stack.bindings,
            onboarding=cp_stack.onboarding,
        )
        # There is no drift left to find — that is the premise, asserted so a
        # future change that stops retiring cannot make this test pass for the
        # wrong reason.
        assert [item.bindings_now for item in stale] == [
            item.bindings_served for item in stale
        ]
        assert [item.blocked_by for item in stale] == [None]

        applied = reconcile_stale_bindings(
            connection,
            planner=cp_stack.make_worker().planner,
            jobs=cp_stack.jobs,
            onboarding=cp_stack.onboarding,
            configs=cp_stack.configs,
            bindings=cp_stack.bindings,
            runtime_provider=cp_stack.config.runtime_provider,
            apply=True,
            now=BASE_TIME + 300,
        )
    assert [item["action"] for item in applied] == ["reconciled"]
    assert cp_stack.database.query_one(
        "SELECT COUNT(*) AS total FROM provisioning_jobs WHERE household_id = ?"
        " AND kind = 'runtime' AND settled_at IS NULL",
        (cp_stack.household.id,),
    )["total"] == 1

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
            chat_id=PHONE,
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
            chat_id=PHONE,
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

from control_plane.models import StepKind, synthetic_channel_identity  # noqa: E402
from control_plane.provisioning.manifest import DesiredHouseholdSpecV1  # noqa: E402
from control_plane.provisioning.manifest_toml import manifest_to_toml  # noqa: E402
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
        # adult already sits.
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

    assert [(r.channel, r.external_id, r.role) for r in rows] == [
        ("whatsapp", "+999511230", "owner")
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
        for offset, (channel, external_id) in enumerate((
            ("whatsapp", "+999511234"),
            ("web", "synthetic-web-seat"),
        )):
            issued = cp_stack.bindings.issue_challenge(
                connection,
                household_id=cp_stack.household.id,
                channel=channel,
                external_id=external_id,
                chat_id=external_id,
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

    Two shapes slipped past reconciliation. Re-onboarding onto a tuple an
    ADULT holds wrote no owner binding at all, while the stale owner row
    survived on the channel the household had just left. And the owner's CHAT
    could change while their sender stayed the same — a family moving to a new
    Telegram group keeps its user IDs — which left the household's review
    surface pointing at the conversation it had just left.

    The second case used to be written as "the ACTOR changed while the tuple
    stayed the same". Under the identity invariant that cannot happen: the
    actor is the sender, so an actor change IS a tuple change and takes the
    ordinary reset path. The chat is now the field that can move underneath a
    matching tuple, and it is the one that must be reconciled.
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
            owner_actor_id=OWNER, now=BASE_TIME + 1,
        )
        # 1. The owner re-onboards onto the tuple the adult holds.
        cp_stack.bindings.ensure_owner_binding(
            connection, household_id=hid, channel="whatsapp",
            external_id=PHONE, actor_id=PHONE, now=BASE_TIME + 10,
            chat_id=PHONE,
        )
        rows = cp_stack.bindings.verified(connection, household_id=hid)

    # The adult's row is gone and the owner holds the tuple, as the owner —
    # not merged into whatever the previous row said.
    assert [(r.channel, r.external_id, r.actor_id, r.role) for r in rows] == [
        ("whatsapp", PHONE, PHONE, "owner")
    ]

    # 2. The chat moves while the sender stays put: the family keeps its
    #    WhatsApp number and the assistant is pointed at a new conversation.
    with cp_stack.database.write() as connection:
        cp_stack.bindings.ensure_owner_binding(
            connection, household_id=hid, channel="whatsapp",
            external_id=PHONE, actor_id=PHONE, now=BASE_TIME + 20,
            chat_id="999511230@g.whatsapp.invalid",
        )
        rows = cp_stack.bindings.verified(connection, household_id=hid)

    assert [(r.actor_id, r.chat_id, r.role) for r in rows] == [
        (PHONE, "999511230@g.whatsapp.invalid", "owner")
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
            assert result.status == "succeeded", result

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

    # And each sender reaches its OWN household, with no `ambiguous_sender`.
    router = WhatsAppGatewayRouter(
        cp_stack.database, ingress_path=cp_stack.config.database_path.parent / "ing.db"
    )
    assert router.route(first_actor, "telegram").household_id == cp_stack.household.id
    assert router.route(second_actor, "telegram").household_id == second.id

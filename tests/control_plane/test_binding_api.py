"""C3 HTTP surface: the only caller of the binding lifecycle.

`channel_bindings` spent two phases as a table with no writer. A lifecycle with
no caller would be the same defect one layer up, so these cases exercise the
endpoints rather than the repository — the repository's own rules are proven in
`test_channel_bindings.py`.
"""

from __future__ import annotations

CHALLENGES = "/api/v1/household/bindings/challenges"
VERIFY = "/api/v1/household/bindings/verify"

ADULT = "synthetic-second-adult"
#: The member's SENDER identity, and the CONVERSATION they speak in. Two
#: values since C3a, and deliberately different strings here: a test that
#: passes one value for both cannot notice a consumer reading the wrong one.
SENDER = "synthetic-second-sender"
CHAT = "synthetic-household-chat"
#: The same distinction on WhatsApp, where the two are NOT interchangeable:
#: ingest normalizes the sender to `+999…` and reports the conversation as the
#: provider's `remote_jid`.
WA_SENDER = "+999511234567"
WA_CHAT = "999511234567@s.whatsapp.invalid"


def _seed_owner(
    harness,
    household_id: str,
    actor_id: str = "synthetic-owner",
    external_id: str = "synthetic-owner-chat",
) -> None:
    """Give the household the owner binding provisioning would have written.

    `external_id` is per-household on purpose: an ID held by one household is
    refused to every other, which is the rule that keeps the gateway from
    answering `ambiguous_sender` for both.
    """
    with harness.container.database.write() as connection:
        harness.container.bindings.ensure_owner_binding(
            connection,
            household_id=household_id,
            channel="telegram",
            external_id=external_id,
            chat_id=external_id,
            actor_id=actor_id,
        )


def test_both_routes_gate_like_every_other_private_mutation(api_harness) -> None:
    """Origin, session and CSRF are enforced before anything else runs."""
    world = api_harness.create_principal()
    api_harness.authenticate(world)
    _seed_owner(api_harness, world.household.id)
    origin_only = {"Origin": api_harness.config.public_origin}
    body = {"channel": "telegram", "external_id": SENDER, "chat_id": CHAT, "actor_id": ADULT}

    for path, payload in ((CHALLENGES, body), (VERIFY, {"code": "whatever"})):
        assert api_harness.client.post(path, json=payload).status_code == 403
        assert (
            api_harness.client.post(path, json=payload, headers=origin_only).status_code
            == 403
        )
        assert (
            api_harness.client.post(
                path,
                json=payload,
                headers={**origin_only, "X-CSRF-Token": "wrong-csrf"},
            ).status_code
            == 403
        )


def test_a_household_without_an_owner_binding_cannot_acquire_a_second_member(
    api_harness,
) -> None:
    world = api_harness.create_principal()
    api_harness.authenticate(world)
    response = api_harness.client.post(
        CHALLENGES,
        json={"channel": "telegram", "external_id": SENDER, "chat_id": CHAT, "actor_id": ADULT},
        headers=api_harness.mutation_headers,
    )
    assert response.status_code == 409
    assert "owner binding" in response.json()["detail"]


def test_the_code_is_returned_once_and_never_written_down(api_harness, caplog) -> None:
    world = api_harness.create_principal()
    api_harness.authenticate(world)
    _seed_owner(api_harness, world.household.id)

    with caplog.at_level("DEBUG"):
        response = api_harness.client.post(
            CHALLENGES,
            json={"channel": "telegram", "external_id": SENDER, "chat_id": CHAT, "actor_id": ADULT},
            headers=api_harness.mutation_headers,
        )
    assert response.status_code == 200
    code = response.json()["code"]
    assert code

    assert code not in caplog.text
    rows = api_harness.container.database.query(
        "SELECT * FROM channel_binding_challenges"
    )
    assert code not in " ".join(str(value) for value in dict(rows[0]).values())


def test_a_wrong_code_is_refused_and_writes_nothing(api_harness) -> None:
    world = api_harness.create_principal()
    api_harness.authenticate(world)
    _seed_owner(api_harness, world.household.id)
    api_harness.client.post(
        CHALLENGES,
        json={"channel": "telegram", "external_id": SENDER, "chat_id": CHAT, "actor_id": ADULT},
        headers=api_harness.mutation_headers,
    )

    response = api_harness.client.post(
        VERIFY, json={"code": "not-the-code"}, headers=api_harness.mutation_headers
    )
    assert response.status_code == 403
    bound = api_harness.container.database.query(
        "SELECT actor_id FROM channel_bindings WHERE actor_id = ?", (ADULT,)
    )
    assert bound == []


def test_an_owner_cannot_bind_an_id_another_household_holds(api_harness) -> None:
    """The refusal that protects a household this request never names."""
    world = api_harness.create_principal()
    other = api_harness.create_principal(email="other-owner@family.test")
    api_harness.authenticate(world)
    _seed_owner(api_harness, world.household.id)
    with api_harness.container.database.write() as connection:
        api_harness.container.bindings.ensure_owner_binding(
            connection,
            household_id=other.household.id,
            channel="telegram",
            external_id=SENDER,
            chat_id=CHAT,
            actor_id="synthetic-other-owner",
        )

    response = api_harness.client.post(
        CHALLENGES,
        json={"channel": "telegram", "external_id": SENDER, "chat_id": CHAT, "actor_id": ADULT},
        headers=api_harness.mutation_headers,
    )
    assert response.status_code == 409
    assert "another household" in response.json()["detail"]


def test_an_unprovisionable_household_is_declined_not_broken(api_harness) -> None:
    """The planner refuses a household whose onboarding is incomplete. That is
    a state, and the binding must not survive it."""
    world = api_harness.create_principal()
    api_harness.authenticate(world)
    _seed_owner(api_harness, world.household.id)
    # Any channel reaches the planner now — C3a removed the primary-channel
    # refusal this comment used to route around — and the planner is what is
    # under test.
    issued = api_harness.client.post(
        CHALLENGES,
        json={
            "channel": "whatsapp",
            "external_id": WA_SENDER,
            "chat_id": WA_CHAT,
            "actor_id": ADULT,
        },
        headers=api_harness.mutation_headers,
    )

    response = api_harness.client.post(
        VERIFY,
        json={"code": issued.json()["code"]},
        headers=api_harness.mutation_headers,
    )
    assert response.status_code == 409
    # Rolled back with the refusal: no half-joined member is left behind.
    assert (
        api_harness.container.database.query(
            "SELECT id FROM channel_bindings WHERE actor_id = ?", (ADULT,)
        )
        == []
    )
    assert api_harness.container.database.query(
        "SELECT consumed_at FROM channel_binding_challenges"
    )[0]["consumed_at"] is None


def test_a_challenge_cannot_be_redeemed_from_another_household(api_harness) -> None:
    """Actor IDs are unique within a household and nowhere else.

    `channel_bindings.actor_id` is plain TEXT, so two households may both call
    their owner `synthetic-owner`. The lookup was global and compared only
    `issued_by_actor_id`, so the colliding owner could redeem the other
    household's challenge — writing the binding into the ISSUING household
    while their own got the revision.
    """
    victim = api_harness.create_principal(email="victim@family.test")
    attacker = api_harness.create_principal(email="attacker@family.test")
    # The collision the schema permits: the same owner actor ID in both.
    _seed_owner(
        api_harness,
        victim.household.id,
        actor_id="synthetic-owner",
        external_id="synthetic-victim-chat",
    )
    _seed_owner(
        api_harness,
        attacker.household.id,
        actor_id="synthetic-owner",
        external_id="synthetic-attacker-chat",
    )

    with api_harness.container.database.write() as connection:
        issued = api_harness.container.bindings.issue_challenge(
            connection,
            household_id=victim.household.id,
            channel="whatsapp",
            external_id="+999511234567",
            chat_id="+999511234567",
            actor_id=ADULT,
            role="adult",
            issued_by_actor_id="synthetic-owner",
        )

    api_harness.authenticate(attacker)
    response = api_harness.client.post(
        VERIFY, json={"code": issued.code}, headers=api_harness.mutation_headers
    )
    assert response.status_code == 403
    assert api_harness.container.database.query(
        "SELECT id FROM channel_bindings WHERE household_id = ? AND actor_id = ?",
        (victim.household.id, ADULT),
    ) == []


def test_both_binding_routes_bound_the_body_before_parsing_it(api_harness) -> None:
    """The invariant fixed for `/api/web/message` and then not applied to the
    two endpoints written in the same session — the instance, not the rule."""
    world = api_harness.create_principal()
    api_harness.authenticate(world)
    _seed_owner(api_harness, world.household.id)

    huge = "я" * (200 * 1024)
    for path, payload in (
        (
            CHALLENGES,
            {
                "channel": "whatsapp",
                "external_id": huge,
                "chat_id": WA_CHAT,
                "actor_id": ADULT,
            },
        ),
        (VERIFY, {"code": huge}),
    ):
        response = api_harness.client.post(
            path, json=payload, headers=api_harness.mutation_headers
        )
        assert response.status_code == 413, path


def test_only_the_owner_may_attest_a_binding(api_harness) -> None:
    """`current_household_mutation` accepts any ACTIVE membership regardless of
    role, and nothing above these routes ever read `household_memberships.role`.

    An adult could therefore issue a code, redeem it, and have `owner_actor()`
    record the binding against the OWNER's actor — adding a member to the
    household's durable routing state without the owner being asked.
    """
    owner = api_harness.create_principal(email="the-owner@family.test")
    adult = api_harness.create_principal(email="the-adult@family.test")
    _seed_owner(api_harness, owner.household.id)

    # An active adult membership in the OWNER's household — the shape the
    # schema permits and the route did not distinguish.
    with api_harness.container.database.write() as connection:
        connection.execute(
            "DELETE FROM household_memberships WHERE account_id = ?",
            (adult.account.id,),
        )
        connection.execute(
            "INSERT INTO household_memberships (account_id, household_id, role,"
            " status, created_at, accepted_at) VALUES (?, ?, 'adult', 'active', 1, 1)",
            (adult.account.id, owner.household.id),
        )

    api_harness.authenticate(adult)
    for path, payload in (
        (
            CHALLENGES,
            {
                "channel": "whatsapp",
                "external_id": WA_SENDER,
                "chat_id": WA_CHAT,
                "actor_id": ADULT,
            },
        ),
        (VERIFY, {"code": "anything"}),
    ):
        response = api_harness.client.post(
            path, json=payload, headers=api_harness.mutation_headers
        )
        assert response.status_code == 403, path
        assert "owner" in response.json()["detail"]

    # The owner completes the same flow the adult was refused.
    api_harness.authenticate(owner)
    issued = api_harness.client.post(
        CHALLENGES,
        json={
            "channel": "whatsapp",
            "external_id": WA_SENDER,
            "chat_id": WA_CHAT,
            "actor_id": ADULT,
        },
        headers=api_harness.mutation_headers,
    )
    assert issued.status_code == 200


def test_repeated_verification_cannot_grow_the_revision_history(api_harness) -> None:
    """The cap that wasn't.

    `MAX_OPEN_CHALLENGES` counts only UNCONSUMED challenges, so issue-then-
    verify loops freely; `verify_challenge` returns the existing binding, so
    `_reject_when_full` never fires either. Both caps reported room while the
    endpoint replanned on every pass and `create_revision` inserted another
    encrypted manifest — `config_revisions` growing without limit on a shared
    volume.
    """
    world = api_harness.create_principal()
    api_harness.authenticate(world)
    _seed_owner(api_harness, world.household.id)
    body = {
        "channel": "whatsapp",
        "external_id": WA_SENDER,
        "chat_id": WA_CHAT,
        "actor_id": ADULT,
    }

    # Bind the tuple through the repository, which does not replan — the point
    # under test is what the ENDPOINT does once a binding exists.
    with api_harness.container.database.write() as connection:
        issued = api_harness.container.bindings.issue_challenge(
            connection,
            household_id=world.household.id,
            channel="whatsapp",
            external_id="+999511234567",
            chat_id="+999511234567",
            actor_id=ADULT,
            role="adult",
            issued_by_actor_id="synthetic-owner",
        )
        api_harness.container.bindings.verify_challenge(
            connection,
            code=issued.code,
            household_id=world.household.id,
            owner_actor_id="synthetic-owner",
        )

    revisions_before = len(
        api_harness.container.database.query(
            "SELECT id FROM config_revisions WHERE household_id = ?",
            (world.household.id,),
        )
    )

    # The loop: the same tuple, over and over.
    for _ in range(5):
        repeat = api_harness.client.post(
            CHALLENGES, json=body, headers=api_harness.mutation_headers
        )
        assert repeat.status_code == 409
        assert "already bound" in repeat.json()["detail"]

    challenges = api_harness.container.database.query(
        "SELECT id FROM channel_binding_challenges WHERE household_id = ?",
        (world.household.id,),
    )
    revisions_after = api_harness.container.database.query(
        "SELECT id FROM config_revisions WHERE household_id = ?",
        (world.household.id,),
    )
    # The one consumed challenge that made the binding, and not one more —
    # nor a single extra revision.
    assert len(challenges) == 1
    assert len(revisions_after) == revisions_before


def test_the_endpoint_carries_a_chat_alongside_the_sender(api_harness) -> None:
    """C3a's HTTP half: an owner can put a member into an existing conversation.

    `external_id` is the SENDER the gateway will match; `chat_id` is where that
    member speaks. Sending both is how a second adult joins the family's own
    Telegram group, which the pre-C3a store could not represent at all — the
    chat was the only identity a binding had, so it collided with the owner's.
    """
    world = api_harness.create_principal()
    api_harness.authenticate(world)
    _seed_owner(api_harness, world.household.id)

    issued = api_harness.client.post(
        CHALLENGES,
        json={
            "channel": "telegram",
            "external_id": "synthetic-adult-sender",
            "chat_id": "synthetic-owner-chat",
            "actor_id": ADULT,
        },
        headers=api_harness.mutation_headers,
    )
    assert issued.status_code == 200
    challenge = api_harness.container.database.query(
        "SELECT external_id, chat_id FROM channel_binding_challenges"
    )[0]
    assert (challenge["external_id"], challenge["chat_id"]) == (
        "synthetic-adult-sender",
        "synthetic-owner-chat",
    )


def test_a_challenge_without_a_chat_is_refused_rather_than_guessed(
    api_harness,
) -> None:
    """`chat_id` is required at the endpoint, and writes nothing when absent.

    The first cut made it optional and defaulted it to `external_id`, on the
    reasoning that a WhatsApp 1:1 thread is the number. It is not the string
    this system authorizes against: `whatsapp_webhook.parse_webhook` reports
    the conversation as the provider's `remote_jid`, so the default published a
    binding no inbound turn could match — a success the owner had no way to
    tell from a working one.

    Deriving the JID instead of requiring it was the other option and is worse:
    the control plane does not own that format, and a rule right for
    `@s.whatsapp.invalid` is wrong for the `@g.us` group that is the very
    arrangement this slice exists to allow.
    """
    world = api_harness.create_principal()
    api_harness.authenticate(world)
    _seed_owner(api_harness, world.household.id)

    response = api_harness.client.post(
        CHALLENGES,
        json={
            "channel": "whatsapp",
            "external_id": "+999511234567",
            "actor_id": ADULT,
        },
        headers=api_harness.mutation_headers,
    )
    assert response.status_code == 422
    assert api_harness.container.database.query(
        "SELECT id FROM channel_binding_challenges"
    ) == []

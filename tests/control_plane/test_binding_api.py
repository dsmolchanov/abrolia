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
CHAT = "synthetic-second-chat"


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
            actor_id=actor_id,
        )


def test_both_routes_gate_like_every_other_private_mutation(api_harness) -> None:
    """Origin, session and CSRF are enforced before anything else runs."""
    world = api_harness.create_principal()
    api_harness.authenticate(world)
    _seed_owner(api_harness, world.household.id)
    origin_only = {"Origin": api_harness.config.public_origin}
    body = {"channel": "telegram", "external_id": CHAT, "actor_id": ADULT}

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
        json={"channel": "telegram", "external_id": CHAT, "actor_id": ADULT},
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
            json={"channel": "telegram", "external_id": CHAT, "actor_id": ADULT},
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
        json={"channel": "telegram", "external_id": CHAT, "actor_id": ADULT},
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
            external_id=CHAT,
            actor_id="synthetic-other-owner",
        )

    response = api_harness.client.post(
        CHALLENGES,
        json={"channel": "telegram", "external_id": CHAT, "actor_id": ADULT},
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
    # A NON-primary channel, so the request gets past the primary-channel
    # refusal and actually reaches the planner, which is what is under test.
    issued = api_harness.client.post(
        CHALLENGES,
        json={"channel": "whatsapp", "external_id": "+999511234567", "actor_id": ADULT},
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
        (CHALLENGES, {"channel": "whatsapp", "external_id": huge, "actor_id": ADULT}),
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
        (CHALLENGES, {"channel": "whatsapp", "external_id": "+999511234567", "actor_id": ADULT}),
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
        json={"channel": "whatsapp", "external_id": "+999511234567", "actor_id": ADULT},
        headers=api_harness.mutation_headers,
    )
    assert issued.status_code == 200

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


def _seed_owner(harness, household_id: str, actor_id: str = "synthetic-owner") -> None:
    """Give the household the owner binding provisioning would have written."""
    with harness.container.database.write() as connection:
        harness.container.bindings.ensure_owner_binding(
            connection,
            household_id=household_id,
            channel="telegram",
            external_id="synthetic-owner-chat",
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
    issued = api_harness.client.post(
        CHALLENGES,
        json={"channel": "telegram", "external_id": CHAT, "actor_id": ADULT},
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

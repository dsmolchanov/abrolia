"""C3 HTTP surface: the only caller of the binding lifecycle.

`channel_bindings` spent two phases as a table with no writer. A lifecycle with
no caller would be the same defect one layer up, so these cases exercise the
endpoints rather than the repository — the repository's own rules are proven in
`test_channel_bindings.py`.
"""

from __future__ import annotations

from control_plane.models import ProfileInput

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
            actor_id=external_id,
        )


def test_both_routes_gate_like_every_other_private_mutation(api_harness) -> None:
    """Origin, session and CSRF are enforced before anything else runs."""
    world = api_harness.create_principal()
    api_harness.authenticate(world)
    _seed_owner(api_harness, world.household.id)
    origin_only = {"Origin": api_harness.config.public_origin}
    body = {"channel": "telegram", "external_id": SENDER, "chat_id": CHAT, "actor_id": SENDER}

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
        json={"channel": "telegram", "external_id": SENDER, "chat_id": CHAT, "actor_id": SENDER},
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
            json={"channel": "telegram", "external_id": SENDER, "chat_id": CHAT, "actor_id": SENDER},
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
        json={"channel": "telegram", "external_id": SENDER, "chat_id": CHAT, "actor_id": SENDER},
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
            actor_id=SENDER,
        )

    response = api_harness.client.post(
        CHALLENGES,
        json={"channel": "telegram", "external_id": SENDER, "chat_id": CHAT, "actor_id": SENDER},
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
            "actor_id": WA_SENDER,
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
            external_id=WA_SENDER,
            chat_id=WA_CHAT,
            actor_id="+999511234567",
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
                "actor_id": huge,
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
                "actor_id": WA_SENDER,
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
            "actor_id": WA_SENDER,
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
        "actor_id": WA_SENDER,
    }

    # Bind the tuple through the repository, which does not replan — the point
    # under test is what the ENDPOINT does once a binding exists.
    with api_harness.container.database.write() as connection:
        issued = api_harness.container.bindings.issue_challenge(
            connection,
            household_id=world.household.id,
            channel="whatsapp",
            external_id=WA_SENDER,
            chat_id=WA_CHAT,
            actor_id="+999511234567",
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
            "actor_id": "synthetic-adult-sender",
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
            "external_id": WA_SENDER,
            "actor_id": WA_SENDER,
        },
        headers=api_harness.mutation_headers,
    )
    assert response.status_code == 422
    assert api_harness.container.database.query(
        "SELECT id FROM channel_binding_challenges"
    ) == []


# --- C3d: a regression that reaches the path it is named for ---------------

from control_plane.models import StepKind, synthetic_channel_identity  # noqa: E402
from control_plane.onboarding.contracts import CommandContext  # noqa: E402
from control_plane.provisioning.worker import (  # noqa: E402
    REPROVISION_RUNTIME_OPERATION,
)
from tests.control_plane.conftest import BASE_TIME  # noqa: E402
from tests.control_plane.test_manifest import (  # noqa: E402
    CHANNEL_SELECTION,
    EMAIL_SELECTION,
    WHATSAPP_SELECTION,
)


def _finish_onboarding(api_harness, world) -> None:
    """Drive the household to a settled, active state.

    The PRECONDITION, not the subject. It goes through the service rather than
    HTTP because what this file has to exercise over HTTP is the binding
    endpoint; putting onboarding through the client too would make the test
    about onboarding's routes instead.
    """
    container = api_harness.container
    sequence = iter(range(1, 500))

    def context() -> CommandContext:
        index = next(sequence)
        return CommandContext(
            account_id=world.account.id,
            session_id=world.session.id,
            request_id=f"synthetic-request-{index}",
            idempotency_key=f"synthetic-key-{index}",
            expected_version=container.onboarding_repository.workflow_for_household(
                world.household.id
            ).version,
        )

    def drain() -> None:
        while (result := container.worker.run_once()) is not None:
            # Since C3e a runtime job stays open until its revision activates,
            # so `pending` here is a launched runtime, not an unfinished drain.
            assert result.status in {"succeeded", "cancelled", "pending"}, result

    container.onboarding.save_profile(
        world.household.id,
        ProfileInput.model_validate({
            "first_name": "Test", "last_name": "Family", "family_language": "en",
            "timezone": "Europe/Prague", "country_code": "DE",
            "residency_mode": "eu-app",
        }),
        context=context(),
        now=BASE_TIME + 1,
    )
    drain()
    for offset, (kind, selection) in enumerate((
        (StepKind.EMAIL, EMAIL_SELECTION),
        (StepKind.WHATSAPP, WHATSAPP_SELECTION),
        (StepKind.PRIMARY_CHANNEL, CHANNEL_SELECTION),
    ), start=2):
        container.onboarding.select(
            world.household.id, kind, selection,
            context=context(), now=BASE_TIME + offset,
        )
        drain()
    # Activation runs through the runtime's bootstrap, which this harness does
    # not host. These two writes are what it performs.
    with container.database.write() as connection:
        connection.execute(
            "UPDATE households SET status = 'active' WHERE id = ?",
            (world.household.id,),
        )
        connection.execute(
            "UPDATE onboarding_workflows SET state = 'complete' WHERE household_id = ?",
            (world.household.id,),
        )
        # Activation also settles the runtime job and publishes the bindings
        # that revision carries. Since C3e the job stays open until it does,
        # and a household whose row says `active` while a rollout is still in
        # flight is exactly what `schedule_runtime_rollout` refuses.
        connection.execute(
            "UPDATE provisioning_jobs SET status = 'succeeded', error_code = NULL,"
            " settled_at = 1 WHERE household_id = ? AND kind = 'runtime'"
            " AND settled_at IS NULL",
            (world.household.id,),
        )
        connection.execute(
            "UPDATE channel_bindings SET published_revision ="
            " (SELECT current_config_revision FROM households WHERE id = ?)"
            " WHERE household_id = ? AND published_revision IS NULL",
            (world.household.id, world.household.id),
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
            (world.household.id, world.household.id),
        )
        connection.execute(
            "UPDATE config_revisions SET status = 'active', activated_at = 1"
            " WHERE household_id = ? AND revision ="
            " (SELECT current_config_revision FROM households WHERE id = ?)",
            (world.household.id, world.household.id),
        )



def test_verifying_over_http_deploys_the_revision_it_reports(api_harness) -> None:
    """C3d: the rollout regression that existed did not reach this path.

    `test_verifying_a_binding_schedules_the_rollout_and_leaves_onboarding_alone`
    asserts the right things by calling the repository, the planner and
    `schedule_runtime_rollout` directly. Deleting the scheduling call from
    `verify_binding_challenge` leaves the entire suite green — verified — so
    the endpoint's own duty to deploy what it reports was covered by nothing.

    This goes through the authenticated endpoint and then runs the worker, so
    the assertion is about the seam that was untested: the endpoint answers
    revision N, and revision N is what the household is actually serving.
    """
    world = api_harness.create_principal("c3d-owner@family.test")
    api_harness.authenticate(world)
    _finish_onboarding(api_harness, world)

    before = api_harness.container.households.get(
        world.household.id
    ).current_config_revision

    issued = api_harness.client.post(
        CHALLENGES,
        json={
            "channel": "whatsapp",
            "external_id": WA_SENDER,
            "chat_id": WA_CHAT,
            "actor_id": WA_SENDER,
        },
        headers=api_harness.mutation_headers,
    )
    assert issued.status_code == 200

    verified = api_harness.client.post(
        VERIFY,
        json={"code": issued.json()["code"]},
        headers=api_harness.mutation_headers,
    )
    assert verified.status_code == 200
    reported = verified.json()["config_revision"]
    assert reported == before + 1

    # The endpoint scheduled the deployment, not merely the plan.
    job = api_harness.container.database.query_one(
        "SELECT desired_revision FROM provisioning_jobs"
        " WHERE household_id = ? AND operation = ?",
        (world.household.id, REPROVISION_RUNTIME_OPERATION),
    )
    assert job is not None, "the endpoint reported a revision it never deployed"
    assert job["desired_revision"] == reported

    while (result := api_harness.container.worker.run_once()) is not None:
        # Since C3e a runtime job stays open until its revision activates, so
        # `pending` here is a launched runtime rather than an unfinished drain.
        assert result.status in {"succeeded", "cancelled", "pending"}, result

    # The runtime then CLAIMS its token and ACTIVATES the revision, which is
    # the half that makes the endpoint's answer true.
    #
    # Stopping at the worker is not enough, and the reason is easy to miss:
    # `schedule_runtime_rollout` writes `current_config_revision` when it
    # queues the job, and `_settle_runtime_ready` marks the job succeeded
    # right after launch. So the revision and manifest assertions below hold
    # even if claiming or activation is broken or never happens — the
    # household would sit at `provisioning` forever, serving N-1, while every
    # assertion passed.
    active = api_harness.container
    household = active.households.get(world.household.id)
    assert household.status == "provisioning"
    raw_token = active.secret_sink.get(
        household.runtime_ref, "HERMES_BOOTSTRAP_TOKEN"
    )
    assert raw_token is not None
    binding = {
        "household_id": world.household.id,
        "runtime_ref": household.runtime_ref,
        "config_revision": reported,
    }
    active.bootstrap.claim(raw_token, **binding)
    active.bootstrap.activate(
        raw_token,
        **binding,
        activated_sha256=active.configs.manifest(
            world.household.id, reported
        )["config_sha256"],
    )

    # And the household is serving what the endpoint said it would.
    household = active.households.get(world.household.id)
    assert household.status == "active", "activation never completed"
    assert household.current_config_revision == reported
    # The onboarding page is untouched: a family that finished setup months ago
    # must not be shown as mid-setup because somebody added an adult.
    assert (
        api_harness.container.onboarding_repository.workflow_for_household(
            world.household.id
        ).state
        == "complete"
    )
    # The member the whole flow existed for is in the served manifest.
    manifest = api_harness.container.configs.manifest(
        world.household.id, household.current_config_revision
    )
    owner_actor, _ = synthetic_channel_identity(world.household.id)
    actors = {b["actor_id"] for b in manifest["channel_bindings"]}
    assert actors == {owner_actor, WA_SENDER}


def test_an_adults_web_seat_is_reachable_through_the_api(api_harness) -> None:
    """C3f round two: the flow the feature exists for, over HTTP.

    `issue_challenge` began refusing a web challenge without `account_id`, and
    the only product caller never accepted or passed the field — so every
    attempt to create a second adult's web seat answered 409 before writing a
    challenge, and C3f's headline capability was unreachable in production.
    Found in review on #106, and the reason this test goes through the endpoint
    rather than the repository: the repository half was already green.
    """
    owner = api_harness.create_principal("seat-owner@family.test")
    adult = api_harness.create_principal("seat-adult@family.test")
    api_harness.authenticate(owner)
    # Fully onboarded: verification schedules a rollout, which a household that
    # cannot yet be issued refuses — so a half-set-up fixture would fail here
    # for a reason that has nothing to do with seats.
    _finish_onboarding(api_harness, owner)
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

    api_harness.authenticate(owner)
    seat = {
        "channel": "web",
        "external_id": "synthetic-adult-seat",
        "chat_id": "web:synthetic-adult-seat",
        "actor_id": "synthetic-adult-seat",
    }

    # Without the account there is no member to attribute the seat to, and the
    # refusal says so rather than writing a seat nothing can reach.
    missing = api_harness.client.post(
        CHALLENGES, json=seat, headers=api_harness.mutation_headers
    )
    assert missing.status_code == 409
    assert "account" in missing.json()["detail"]

    # An account that is not a member of this household cannot be given one:
    # the seat is what authorizes a turn.
    stranger = api_harness.create_principal(email="stranger@family.test")
    foreign = api_harness.client.post(
        CHALLENGES,
        json={**seat, "account_id": stranger.account.id},
        headers=api_harness.mutation_headers,
    )
    assert foreign.status_code == 409
    assert "active member" in foreign.json()["detail"]

    issued = api_harness.client.post(
        CHALLENGES,
        json={**seat, "account_id": adult.account.id},
        headers=api_harness.mutation_headers,
    )
    assert issued.status_code == 200

    verified = api_harness.client.post(
        VERIFY,
        json={"code": issued.json()["code"]},
        headers=api_harness.mutation_headers,
    )
    assert verified.status_code == 200, verified.json()

    row = api_harness.container.database.query_one(
        "SELECT account_id, actor_id, chat_id FROM channel_bindings"
        " WHERE household_id = ? AND channel = 'web' AND role = 'adult'",
        (owner.household.id,),
    )
    # The seat carries the ADULT's account, not the owner's — the owner
    # redeems the code on their behalf, so the principal at redemption is the
    # wrong person by construction and the challenge is what remembers.
    assert row["account_id"] == adult.account.id
    assert row["actor_id"] == "synthetic-adult-seat"

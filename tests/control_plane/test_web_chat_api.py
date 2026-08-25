"""C2 control-plane half: /api/web/message hardening and private-runtime proxy."""

from __future__ import annotations

from typing import Any

import pytest

from control_plane.privacy.runtime import RuntimeBoundaryError

VALID_REF = "abrolia-hh-" + "a" * 26


class StubChatClient:
    """Stands in for PrivateRuntimeWebChatClient; records every turn sent."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.reply = "готово"
        self.error: Exception | None = None

    def send(self, runtime_ref: str, *, actor_id: str, role: str, text: str) -> dict[str, Any]:
        self.calls.append(
            {"runtime_ref": runtime_ref, "actor_id": actor_id, "role": role, "text": text}
        )
        if self.error is not None:
            raise self.error
        return {"reply": self.reply}


def _attach_runtime(harness, household_id: str) -> None:
    with harness.container.database.write() as connection:
        connection.execute(
            "UPDATE households SET runtime_ref = ? WHERE id = ?",
            (VALID_REF, household_id),
        )


def test_web_message_gates_like_every_mutation(api_harness) -> None:
    """Origin, session and CSRF are enforced before anything else runs."""
    world = api_harness.create_principal()
    api_harness.authenticate(world)
    origin_only = {"Origin": api_harness.config.public_origin}

    # Cross-origin/no-origin is refused before authentication even runs.
    no_origin = api_harness.client.post("/api/web/message", json={"text": "привет"})
    assert no_origin.status_code == 403

    # A bare session cookie is never enough to spend a model call.
    origin_no_csrf = api_harness.client.post(
        "/api/web/message", json={"text": "привет"}, headers=origin_only
    )
    assert origin_no_csrf.status_code == 403

    wrong = api_harness.client.post(
        "/api/web/message",
        json={"text": "привет"},
        headers={**origin_only, "X-CSRF-Token": "wrong-csrf"},
    )
    assert wrong.status_code == 403


def test_web_message_refuses_without_provisioned_runtime(api_harness) -> None:
    world = api_harness.create_principal()
    api_harness.authenticate(world)
    response = api_harness.client.post(
        "/api/web/message", json={"text": "привет"}, headers=api_harness.mutation_headers
    )
    assert response.status_code == 503
    assert "not provisioned" in response.json()["detail"]


def test_web_message_proxies_to_household_runtime(api_harness) -> None:
    world = api_harness.create_principal()
    api_harness.authenticate(world)
    stub = StubChatClient()
    api_harness.container.web_chat = stub
    _attach_runtime(api_harness, world.household.id)

    response = api_harness.client.post(
        "/api/web/message",
        json={"text": "собери повестку"},
        headers=api_harness.mutation_headers,
    )

    assert response.status_code == 200
    assert response.json() == {"reply": "готово", "status": "staged"}
    assert stub.calls == [
        {
            "runtime_ref": VALID_REF,
            "actor_id": world.account.id,
            "role": "owner",
            "text": "собери повестку",
        }
    ]


def test_web_message_maps_boundary_errors_to_honest_unavailable(api_harness) -> None:
    world = api_harness.create_principal()
    api_harness.authenticate(world)
    stub = StubChatClient()
    stub.error = RuntimeBoundaryError("stub failure")
    api_harness.container.web_chat = stub
    _attach_runtime(api_harness, world.household.id)

    response = api_harness.client.post(
        "/api/web/message", json={"text": "привет"}, headers=api_harness.mutation_headers
    )

    assert response.status_code == 503
    assert "unavailable" in response.json()["detail"]


# --- review round 1: what the endpoint must refuse and must not block ------


def test_the_role_fails_closed_when_membership_cannot_be_established(
    api_harness, monkeypatch
) -> None:
    """This defaulted to `owner`.

    A database error, or a membership row removed between the household lookup
    and the role lookup, granted the caller the owner role — which the runtime
    maps to `manifest.actors.owner`, whose tools include data export and
    deletion. The most power was handed out at exactly the moment
    authorization could not be established.
    """
    world = api_harness.create_principal()
    api_harness.authenticate(world)
    _attach_runtime(api_harness, world.household.id)
    stub = StubChatClient()
    api_harness.container.web_chat = stub

    # 1. The membership is gone by the time the role is read. The household
    #    lookup and the role lookup are two separate queries, so a membership
    #    removed between them lands exactly here — and used to return `owner`.
    real_query = api_harness.container.database.query

    def _membership_vanished(sql, params=()):
        # Only the ROLE lookup; the household lookup reads the same table and
        # must still succeed, which is what makes this the race rather than a
        # missing household.
        if "SELECT account_id, role" in sql:
            return []
        return real_query(sql, params)

    monkeypatch.setattr(
        api_harness.container.database, "query", _membership_vanished
    )
    raced = api_harness.client.post(
        "/api/web/message", json={"text": "привет"}, headers=api_harness.mutation_headers
    )
    assert raced.status_code == 403
    assert stub.calls == []
    monkeypatch.undo()
    api_harness.container.web_chat = stub

    # 2. The query itself fails. Unavailable is not affirmative.
    def _explode(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(api_harness.container.database, "query", _explode)
    # The failure PROPAGATES — the previous code caught it and fell through to
    # the owner default, which is the whole defect. Becoming a 500 is correct;
    # becoming an authorization is not.
    with pytest.raises(RuntimeError, match="database unavailable"):
        api_harness.client.post(
            "/api/web/message",
            json={"text": "привет"},
            headers=api_harness.mutation_headers,
        )
    assert stub.calls == []


def test_an_oversized_body_is_refused_before_it_is_parsed(api_harness) -> None:
    """The 2 000-character check ran after FastAPI had already materialised the
    whole document, so it bounded what was accepted and not what was read."""
    world = api_harness.create_principal()
    api_harness.authenticate(world)
    _attach_runtime(api_harness, world.household.id)
    api_harness.container.web_chat = StubChatClient()

    huge = "я" * (200 * 1024)
    response = api_harness.client.post(
        "/api/web/message", json={"text": huge}, headers=api_harness.mutation_headers
    )
    assert response.status_code == 413

    # The accepted boundary still works: a normal turn is nowhere near it.
    ok = api_harness.client.post(
        "/api/web/message", json={"text": "привет"}, headers=api_harness.mutation_headers
    )
    assert ok.status_code == 200


def test_a_slow_runtime_does_not_stall_unrelated_requests(api_harness) -> None:
    """`web_chat.send` is synchronous and waits out a whole model turn behind a
    120-second timeout, while production runs Uvicorn with a single worker."""
    import threading

    world = api_harness.create_principal()
    api_harness.authenticate(world)
    _attach_runtime(api_harness, world.household.id)

    release = threading.Event()
    entered = threading.Event()

    class BlockingChatClient(StubChatClient):
        def send(self, runtime_ref, *, actor_id, role, text):
            entered.set()
            assert release.wait(timeout=10), "the blocked call was never released"
            return super().send(runtime_ref, actor_id=actor_id, role=role, text=text)

    api_harness.container.web_chat = BlockingChatClient()

    chat_result: list[int] = []

    def _chat() -> None:
        response = api_harness.client.post(
            "/api/web/message",
            json={"text": "привет"},
            headers=api_harness.mutation_headers,
        )
        chat_result.append(response.status_code)

    caller = threading.Thread(target=_chat)
    caller.start()
    try:
        assert entered.wait(timeout=10), "the chat call never reached the client"
        # The event loop is still serving while the chat turn is held open.
        health = api_harness.client.get("/healthz")
        assert health.status_code == 200
    finally:
        release.set()
        caller.join(timeout=15)

    assert chat_result == [200]

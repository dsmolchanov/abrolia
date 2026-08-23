"""C2 control-plane half: /api/web/message hardening and private-runtime proxy."""

from __future__ import annotations

from typing import Any

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

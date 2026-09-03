"""The unauthenticated auth bodies are bounded before they are parsed.

`request-link` (JSON and the no-JS form) and `consume` run before any rate
limiter, and a Pydantic body parameter lets FastAPI hold the whole document
before a field limit applies. With self-signup on, the JSON route is the
public front door, so an oversized body — declared or chunked — has to be
refused at the byte level on every one of the three consumers, and the refusal
must not consume a rate-limit attempt or reach the mailer.
"""

from __future__ import annotations

import pytest

from control_plane.api.auth import MAX_AUTH_BODY_BYTES

ROUTES = (
    ("json", "/api/v1/auth/request-link"),
    ("json", "/api/v1/auth/consume"),
    ("form", "/start/request-link"),
)


def _oversized(kind: str) -> tuple[bytes, dict[str, str]]:
    filler = "x" * (MAX_AUTH_BODY_BYTES + 1)
    if kind == "json":
        return (
            f'{{"email": "a@pilot.test", "token": "{filler}"}}'.encode(),
            {"Content-Type": "application/json"},
        )
    return (
        f"email=a%40pilot.test&filler={filler}".encode(),
        {"Content-Type": "application/x-www-form-urlencoded"},
    )


@pytest.mark.parametrize(("kind", "path"), ROUTES)
@pytest.mark.parametrize("framing", ("declared", "chunked"))
def test_an_oversized_body_is_refused_before_parsing(api_harness, kind, path, framing) -> None:
    body, headers = _oversized(kind)
    headers["Origin"] = api_harness.config.public_origin
    # A chunked request declares no length, so only the streamed bound can
    # catch it; a declared one is refused on the header before any byte.
    content = iter([body[: len(body) // 2], body[len(body) // 2 :]]) if framing == "chunked" else body

    response = api_harness.client.post(path, content=content, headers=headers)

    assert response.status_code == 413, (path, framing)
    assert api_harness.mailer.sent == []
    rows = api_harness.container.database.query(
        "SELECT COUNT(*) AS n FROM rate_limit_buckets"
    )
    assert rows[0]["n"] == 0, "a refused body must not spend a rate-limit attempt"


def test_a_normal_form_still_reaches_the_handler(api_harness) -> None:
    existing = api_harness.create_principal("bounded@pilot.test")
    response = api_harness.client.post(
        "/start/request-link",
        headers={"Origin": api_harness.config.public_origin},
        data={"email": existing.account.recovery_email},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert [m.purpose for m in api_harness.mailer.sent] == ["login"]

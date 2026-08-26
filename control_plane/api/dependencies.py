from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, NamedTuple

from fastapi import Header, HTTPException, Request, status
from pydantic import ValidationError

from control_plane.container import ControlPlaneContainer
from control_plane.db import new_id
from control_plane.repositories.auth import InvalidCredential, SessionRecord
from control_plane.repositories.households import HouseholdNotFound, HouseholdRecord

SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


@dataclass(frozen=True)
class Principal:
    session: SessionRecord

    @property
    def account_id(self) -> str:
        return self.session.account_id


@dataclass(frozen=True)
class CurrentHousehold:
    principal: Principal
    household: HouseholdRecord


@dataclass(frozen=True)
class CommandHeaders:
    idempotency_key: str
    expected_version: int


def container(request: Request) -> ControlPlaneContainer:
    return request.app.state.container


def require_origin(request: Request) -> None:
    expected = container(request).config.public_origin
    if request.headers.get("origin") != expected:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "same-origin request required")


def authenticate(request: Request) -> Principal:
    active = container(request)
    raw_token = request.cookies.get(active.config.session_cookie_name)
    if not raw_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")
    try:
        session = active.sessions.authenticate(raw_token)
    except InvalidCredential as error:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required") from error
    return Principal(session)


def require_private_mutation(
    request: Request,
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> Principal:
    require_origin(request)
    principal = authenticate(request)
    if not x_csrf_token or not container(request).auth.verify_csrf(
        principal.session.id, x_csrf_token
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF validation failed")
    return principal


def require_fresh_mutation(
    request: Request,
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> Principal:
    principal = require_private_mutation(request, x_csrf_token)
    try:
        container(request).sessions.require_fresh(principal.session)
    except PermissionError as error:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "fresh re-authentication required") from error
    return principal


def current_household(request: Request) -> CurrentHousehold:
    principal = authenticate(request)
    try:
        household = container(request).households.current_for_account(principal.account_id)
    except HouseholdNotFound as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "household not found") from error
    return CurrentHousehold(principal, household)


def current_household_mutation(
    request: Request,
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> CurrentHousehold:
    principal = require_private_mutation(request, x_csrf_token)
    try:
        household = container(request).households.current_for_account(principal.account_id)
    except HouseholdNotFound as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "household not found") from error
    return CurrentHousehold(principal, household)


def current_household_fresh_mutation(
    request: Request,
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> CurrentHousehold:
    current = current_household_mutation(request, x_csrf_token)
    try:
        container(request).sessions.require_fresh(current.principal.session)
    except PermissionError as error:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "fresh re-authentication required"
        ) from error
    return current


def command_headers(
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> CommandHeaders:
    if not idempotency_key or not 8 <= len(idempotency_key) <= 128:
        raise HTTPException(status.HTTP_428_PRECONDITION_REQUIRED, "Idempotency-Key required")
    if if_match is None:
        raise HTTPException(status.HTTP_428_PRECONDITION_REQUIRED, "If-Match required")
    raw = if_match.strip()
    if raw.startswith("W/"):
        raw = raw[2:]
    raw = raw.strip('"')
    try:
        expected_version = int(raw)
    except ValueError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "If-Match must be a version") from error
    if expected_version < 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "If-Match must be non-negative")
    return CommandHeaders(idempotency_key, expected_version)


def request_id(request: Request) -> str:
    supplied = request.headers.get("X-Request-ID", "")
    return supplied if SAFE_REQUEST_ID.fullmatch(supplied) else new_id()


def network_bucket(request: Request) -> str:
    # Raw address is used transiently as HMAC input and is never persisted.
    return request.client.host if request.client else "unknown"


class BrowserSession(NamedTuple):
    """What a rendered page needs about its caller."""

    household: Any
    account: Any


class SeeOther(HTTPException):
    """A 303 to a page, raised from a dependency.

    Dependencies signal by raising, and a page must send the browser somewhere
    rather than render a 401 as an error page. `create_app` turns this back into
    the `RedirectResponse` the inline check used to return.
    """

    def __init__(self, location: str) -> None:
        super().__init__(
            status_code=status.HTTP_303_SEE_OTHER, headers={"Location": location}
        )


def browser_session(request: Request) -> BrowserSession:
    """Authenticate a page request, or send the browser to `/start`.

    A DEPENDENCY rather than a `try` inside the handler. The inline version
    behaved correctly, and the rule it broke is a checkable one: an auth
    dependency is visible in the route's signature and survives an early return
    added above it later, where an inline check is one edit away from being
    stepped over.

    Module level, beside the other dependencies, for a mechanical reason as well
    as a tidy one: `from __future__ import annotations` makes every annotation a
    string that FastAPI resolves against MODULE globals, so a dependency defined
    inside `create_app` cannot be named in an `Annotated[...]` at all.
    """
    active = container(request)
    try:
        # The sessions repository directly, NOT the `authenticate` dependency
        # above: that one raises `HTTPException(401)`, and a browser renders a
        # 401 as an error page. This route has always redirected an
        # unauthenticated visitor to `/start`, and moving the check into a
        # dependency must not change what the family sees.
        principal = active.sessions.authenticate(
            request.cookies.get(active.config.session_cookie_name, "")
        )
        household = active.households.current_for_account(principal.account_id)
        account = active.accounts.get(principal.account_id)
    except (PermissionError, HouseholdNotFound) as error:
        raise SeeOther("/start") from error
    return BrowserSession(household, account)


#: Bodies are bounded before they are PARSED, not after. A Pydantic body
#: parameter lets FastAPI materialise the whole document first, so the field
#: limits on a model bound what is accepted and never what is read — an
#: authenticated caller could spend the process's memory without reaching any
#: handler. `Content-Length` is checked when offered, and the stream is bounded
#: regardless, because a chunked request offers none.
MAX_JSON_BODY_BYTES = 64 * 1024


async def read_bounded_json(
    request: Request, model: type[Any], *, limit: int = MAX_JSON_BODY_BYTES
) -> Any:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise HTTPException(
                    status.HTTP_413_CONTENT_TOO_LARGE, "body too large"
                )
        except ValueError as error:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "invalid content-length"
            ) from error
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        # Catches a request that declares nothing, or lies about it.
        if len(body) > limit:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "body too large")
    try:
        return model.model_validate_json(bytes(body))
    except ValidationError as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid request body"
        ) from error

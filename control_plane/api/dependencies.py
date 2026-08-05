from __future__ import annotations

import re
from dataclasses import dataclass

from fastapi import Header, HTTPException, Request, status

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

from __future__ import annotations

from contextlib import suppress
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from control_plane.api.dependencies import (
    Principal,
    authenticate,
    container,
    network_bucket,
    require_origin,
    require_private_mutation,
)
from control_plane.auth.rate_limit import RateLimitExceeded
from control_plane.auth.sessions import SESSION_ABSOLUTE_TTL_SECONDS
from control_plane.repositories.auth import InvalidCredential

router = APIRouter()


class LinkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=320)


class ConsumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=32, max_length=512)


def issue_requested_link(request: Request, email: str) -> None:
    """Issue login/reauth only; new-account invites stay operator-only."""

    active = container(request)
    authenticated_account_id = None
    existing_session = request.cookies.get(active.config.session_cookie_name)
    if existing_session:
        with suppress(InvalidCredential):
            authenticated_account_id = active.sessions.authenticate(
                existing_session
            ).account_id
    target = active.account_service.requested_link_target(
        email,
        authenticated_account_id=authenticated_account_id,
    )
    if target is not None:
        active.magic_links.issue(
            target.email,
            purpose=target.purpose,
            account_id=target.account_id,
        )


@router.post("/api/v1/auth/request-link", status_code=status.HTTP_202_ACCEPTED)
def request_link(payload: LinkRequest, request: Request) -> dict[str, str]:
    require_origin(request)
    active = container(request)
    generic = {"status": "accepted"}
    try:
        active.rate_limiter.check(
            "request-link-network", network_bucket(request), limit=10, window_seconds=3600
        )
        active.rate_limiter.check(
            "request-link-address", payload.email, limit=5, window_seconds=3600
        )
        issue_requested_link(request, payload.email)
    except (RateLimitExceeded, ValueError):
        # The public response deliberately does not reveal address eligibility,
        # account existence, or whether a synthetic message was emitted.
        pass
    return generic


@router.post("/api/v1/auth/consume")
def consume(payload: ConsumeRequest, request: Request, response: Response) -> dict:
    require_origin(request)
    active = container(request)
    try:
        active.rate_limiter.check(
            "consume-network", network_bucket(request), limit=30, window_seconds=3600
        )
        active.rate_limiter.check(
            "consume-token", payload.token, limit=5, window_seconds=900
        )
    except RateLimitExceeded as error:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "try again later") from error
    previous_session_id = None
    previous = request.cookies.get(active.config.session_cookie_name)
    if previous:
        with suppress(InvalidCredential):
            previous_session_id = active.sessions.authenticate(previous).id
    try:
        result = active.account_service.consume_magic_link(
            payload.token, previous_session_id=previous_session_id
        )
    except (InvalidCredential, PermissionError) as error:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired link") from error
    response.set_cookie(
        active.config.session_cookie_name,
        result.session.token,
        secure=True,
        httponly=True,
        samesite="strict",
        path="/",
        max_age=SESSION_ABSOLUTE_TTL_SECONDS,
    )
    response.set_cookie(
        active.config.csrf_cookie_name,
        result.session.csrf_token,
        secure=True,
        httponly=False,
        samesite="strict",
        path="/",
        max_age=SESSION_ABSOLUTE_TTL_SECONDS,
    )
    response.headers["Cache-Control"] = "no-store"
    return {
        "account": {"id": result.account.id, "recovery_email": result.account.masked_email},
        "household": {"id": result.household.id, "status": result.household.status},
        "csrf_token": result.session.csrf_token,
        "next": "/onboarding",
    }


@router.post("/api/v1/auth/logout", response_class=Response)
def logout(
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(require_private_mutation)],
) -> Response:
    active = container(request)
    active.auth.revoke_session(principal.session.id)
    response.delete_cookie(active.config.session_cookie_name, path="/")
    response.delete_cookie(active.config.csrf_cookie_name, path="/")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/api/v1/me")
def me(
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
) -> dict:
    active = container(request)
    account = active.accounts.get(principal.account_id)
    if account is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")
    households = active.household_service.list_for_account(account.id)
    return {
        "account": {"id": account.id, "recovery_email": account.masked_email},
        "households": [
            {
                "id": household.id,
                "slug": household.slug,
                "status": household.status,
                "config_revision": household.current_config_revision,
            }
            for household in households
        ],
    }

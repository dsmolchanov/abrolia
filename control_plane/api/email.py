from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from control_plane.api.dependencies import (
    CommandHeaders,
    CurrentHousehold,
    command_headers,
    container,
    current_household,
    current_household_fresh_mutation,
    request_id,
)
from control_plane.auth.rate_limit import RateLimitExceeded
from control_plane.email.domain_policy import domain_guidance
from control_plane.models import StepKind
from control_plane.onboarding.contracts import CommandContext
from control_plane.providers.email.google_oauth import (
    GoogleOAuthDisabled,
    GoogleOAuthError,
    GoogleOAuthInvalidState,
)
from control_plane.provisioning.contracts import OutcomeUnknown, ProviderRejected

router = APIRouter()


class GoogleConfirmRequest(BaseModel):
    dedicated_mailbox: bool


@router.get("/api/v1/email/local-part/suggestion")
def local_part_suggestion(
    request: Request,
    current: Annotated[CurrentHousehold, Depends(current_household)],
) -> dict[str, str]:
    try:
        suggestion = container(request).email_identity_service.suggest(
            current.household.id
        )
    except ValueError as error:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "household profile is incomplete"
        ) from error
    return {"local_part": suggestion}


@router.get("/api/v1/email/local-part/availability")
def local_part_availability(
    request: Request,
    current: Annotated[CurrentHousehold, Depends(current_household)],
    local_part: Annotated[str, Query(min_length=1, max_length=64)],
) -> dict[str, bool]:
    active = container(request)
    try:
        active.rate_limiter.check(
            "email-local-part-availability",
            current.principal.account_id,
            limit=30,
            window_seconds=60,
        )
        available = active.email_identity_service.available(local_part)
    except RateLimitExceeded as error:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "request limit reached") from error
    except ValueError as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid local part"
        ) from error
    return {"available": available}


@router.get("/api/v1/email/domain/guidance")
def family_domain_guidance(
    request: Request,
    current: Annotated[CurrentHousehold, Depends(current_household)],
    domain: Annotated[str, Query(min_length=1, max_length=253)],
) -> dict[str, str | bool]:
    active = container(request)
    try:
        active.rate_limiter.check(
            "email-domain-guidance",
            current.principal.account_id,
            limit=20,
            window_seconds=60,
        )
        guidance = domain_guidance(domain)
    except RateLimitExceeded as error:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "request limit reached") from error
    except ValueError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid domain") from error
    return {
        "domain": guidance.domain,
        "registrable_domain": guidance.registrable_domain,
        "recommended_domain": guidance.recommended_domain,
        "apex_mx_risk": guidance.apex_mx_risk,
    }


@router.post("/api/v1/email/google/start")
def google_oauth_start(
    request: Request,
    current: Annotated[
        CurrentHousehold, Depends(current_household_fresh_mutation)
    ],
) -> dict[str, str | float]:
    active = container(request)
    workflow = active.onboarding_repository.workflow_for_household(
        current.household.id
    )
    try:
        started = active.google_oauth.start(
            household_id=current.household.id,
            account_id=current.principal.account_id,
            session_id=current.principal.session.id,
            workflow_version=workflow.version,
        )
    except (GoogleOAuthDisabled, GoogleOAuthInvalidState) as error:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Google OAuth is unavailable"
        ) from error
    return {
        "authorization_url": started.authorization_url,
        "expires_at": started.expires_at,
    }


@router.get("/api/v1/email/google/callback", include_in_schema=False)
def google_oauth_callback(
    request: Request,
    state: Annotated[str, Query(min_length=32, max_length=256)],
    code: Annotated[str, Query(min_length=1, max_length=4096)],
    current: Annotated[CurrentHousehold, Depends(current_household)],
) -> RedirectResponse:
    try:
        container(request).google_oauth.callback(
            state=state,
            code=code,
            household_id=current.household.id,
            account_id=current.principal.account_id,
            session_id=current.principal.session.id,
        )
    except (GoogleOAuthError, OutcomeUnknown, ProviderRejected):
        return RedirectResponse(
            "/onboarding?error=google_oauth", status_code=status.HTTP_303_SEE_OTHER
        )
    return RedirectResponse(
        "/onboarding?google=confirm", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/api/v1/email/google/confirm")
def google_oauth_confirm(
    body: GoogleConfirmRequest,
    request: Request,
    current: Annotated[
        CurrentHousehold, Depends(current_household_fresh_mutation)
    ],
    headers: Annotated[CommandHeaders, Depends(command_headers)],
) -> JSONResponse:
    active = container(request)
    try:
        active.google_oauth.confirm(
            household_id=current.household.id,
            account_id=current.principal.account_id,
            dedicated_mailbox=body.dedicated_mailbox,
        )
        result = active.onboarding.check(
            current.household.id,
            StepKind.EMAIL,
            context=CommandContext(
                account_id=current.principal.account_id,
                session_id=current.principal.session.id,
                request_id=request_id(request),
                idempotency_key=headers.idempotency_key,
                expected_version=headers.expected_version,
            ),
        )
    except GoogleOAuthInvalidState as error:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Google account confirmation is invalid"
        ) from error
    response = JSONResponse(result.snapshot.model_dump(mode="json"))
    response.headers["ETag"] = f'"{result.snapshot.version}"'
    return response

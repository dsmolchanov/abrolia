from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from control_plane.api.dependencies import CurrentHousehold, container, current_household
from control_plane.auth.rate_limit import RateLimitExceeded
from control_plane.email.domain_policy import domain_guidance

router = APIRouter()


@router.get("/api/v1/email/local-part/suggestion")
def local_part_suggestion(
    request: Request,
    current: Annotated[CurrentHousehold, Depends(current_household)],
) -> dict[str, str]:
    return {
        "local_part": container(request).email_identity_service.suggest(
            current.household.id
        )
    }


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

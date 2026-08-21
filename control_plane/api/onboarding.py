from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from control_plane.api.dependencies import (
    CommandHeaders,
    CurrentHousehold,
    command_headers,
    container,
    current_household,
    current_household_fresh_mutation,
    current_household_mutation,
    request_id,
)
from control_plane.models import ProfileInput, StepKind
from control_plane.onboarding.contracts import CommandContext, CommandResult
from control_plane.privacy.consent import (
    consent_version_and_sha,
    consent_version_and_text,
)

router = APIRouter()


def _require_email_content_restriction(
    kind: StepKind, selection: dict[str, Any]
) -> None:
    """Fail closed at the user-facing boundary in every rollout mode."""
    if kind is not StepKind.EMAIL:
        return
    expected_version, expected_sha = consent_version_and_sha(
        "special_category_content_restriction"
    )
    if (
        selection.get("special_category_restriction_acknowledged") is not True
        or not selection.get("special_category_restriction_receipt_id")
        or selection.get("special_category_restriction_text_version")
        != expected_version
        or selection.get("special_category_restriction_text_sha256") != expected_sha
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "special-category content restriction acknowledgement required",
        )


@router.get("/api/v1/onboarding/consent/special-category-content-restriction")
def email_content_restriction_contract(
    current: Annotated[CurrentHousehold, Depends(current_household)],
) -> JSONResponse:
    """Expose the exact copy and digest API clients must acknowledge.

    Authenticated like every other route on this router. The copy is not a
    secret — it is shown to the family before they accept it — but the
    repository rule is that every route carries an auth dependency, and an
    unauthenticated endpoint on the onboarding API is a surface whether or not
    today's response happens to be public. `current` is unused deliberately: the
    contract is the same for every household, and the dependency is here to
    authenticate the caller, not to select the copy.
    """
    del current
    purpose = "special_category_content_restriction"
    version, copy = consent_version_and_text(purpose)
    _, sha256 = consent_version_and_sha(purpose)
    return JSONResponse({
        "purpose": purpose,
        "text_version": version,
        "text": copy,
        "text_sha256": sha256,
    })


@router.get("/api/v1/onboarding/consent/special-category-household-content")
def household_content_consent_contract(
    current: Annotated[CurrentHousehold, Depends(current_household)],
) -> JSONResponse:
    """Art 9(2)(a) copy and digest; required only in a real-email rollout.

    Authenticated, for the same reason as its sibling above.
    """
    del current
    purpose = "special_category_household_content"
    version, copy = consent_version_and_text(purpose)
    _, sha256 = consent_version_and_sha(purpose)
    return JSONResponse({
        "purpose": purpose,
        "text_version": version,
        "text": copy,
        "text_sha256": sha256,
    })


def _response(result: CommandResult) -> JSONResponse:
    response = JSONResponse(result.snapshot.model_dump(mode="json"))
    response.headers["ETag"] = f'"{result.snapshot.version}"'
    if result.replayed:
        response.headers["X-Idempotent-Replay"] = "true"
    return response


def _context(
    request: Request, current: CurrentHousehold, headers: CommandHeaders
) -> CommandContext:
    return CommandContext(
        account_id=current.principal.account_id,
        session_id=current.principal.session.id,
        request_id=request_id(request),
        idempotency_key=headers.idempotency_key,
        expected_version=headers.expected_version,
    )


@router.get("/api/v1/onboarding/current")
def get_current(
    request: Request,
    current: Annotated[CurrentHousehold, Depends(current_household)],
) -> JSONResponse:
    snapshot = container(request).onboarding_repository.snapshot(current.household.id)
    return _response(CommandResult(snapshot))


@router.put("/api/v1/onboarding/profile")
def save_profile(
    profile: ProfileInput,
    request: Request,
    current: Annotated[CurrentHousehold, Depends(current_household_mutation)],
    headers: Annotated[CommandHeaders, Depends(command_headers)],
) -> JSONResponse:
    result = container(request).onboarding.save_profile(
        current.household.id, profile, context=_context(request, current, headers)
    )
    return _response(result)


@router.post("/api/v1/onboarding/steps/{kind}/select")
def select_step(
    kind: StepKind,
    selection: dict[str, Any],
    request: Request,
    current: Annotated[CurrentHousehold, Depends(current_household_mutation)],
    headers: Annotated[CommandHeaders, Depends(command_headers)],
) -> JSONResponse:
    _require_email_content_restriction(kind, selection)
    try:
        result = container(request).onboarding.select(
            current.household.id,
            kind,
            selection,
            context=_context(request, current, headers),
        )
    except ValidationError as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid synthetic selection",
        ) from error
    return _response(result)


@router.post("/api/v1/onboarding/steps/{kind}/retry")
def retry_step(
    kind: StepKind,
    request: Request,
    current: Annotated[CurrentHousehold, Depends(current_household_mutation)],
    headers: Annotated[CommandHeaders, Depends(command_headers)],
) -> JSONResponse:
    result = container(request).onboarding.retry(
        current.household.id, kind, context=_context(request, current, headers)
    )
    return _response(result)


@router.post("/api/v1/onboarding/steps/{kind}/check")
def check_step(
    kind: StepKind,
    request: Request,
    current: Annotated[CurrentHousehold, Depends(current_household_mutation)],
    headers: Annotated[CommandHeaders, Depends(command_headers)],
) -> JSONResponse:
    result = container(request).onboarding.check(
        current.household.id, kind, context=_context(request, current, headers)
    )
    return _response(result)


@router.post("/api/v1/onboarding/reset/{kind}")
def reset_step(
    kind: StepKind,
    request: Request,
    current: Annotated[CurrentHousehold, Depends(current_household_mutation)],
    headers: Annotated[CommandHeaders, Depends(command_headers)],
) -> JSONResponse:
    result = container(request).onboarding.reset_from(
        current.household.id, kind, context=_context(request, current, headers)
    )
    return _response(result)


@router.post("/api/v1/onboarding/cancel")
def cancel(
    request: Request,
    current: Annotated[CurrentHousehold, Depends(current_household_fresh_mutation)],
    headers: Annotated[CommandHeaders, Depends(command_headers)],
) -> JSONResponse:
    result = container(request).onboarding.cancel(
        current.household.id, context=_context(request, current, headers)
    )
    return _response(result)

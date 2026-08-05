from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from control_plane.api.dependencies import (
    CommandHeaders,
    CurrentHousehold,
    Principal,
    authenticate,
    command_headers,
    container,
    current_household_fresh_mutation,
)

router = APIRouter()


@router.get("/api/v1/onboarding/export")
def export_household(
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
) -> JSONResponse:
    active = container(request)
    try:
        active.sessions.require_fresh(principal.session)
    except PermissionError as error:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "fresh re-authentication required"
        ) from error
    household = active.households.current_for_account(principal.account_id)
    payload = active.exporter.export(principal.account_id, household.id)
    response = JSONResponse(
        payload,
        status_code=(
            status.HTTP_200_OK
            if payload["completion_status"] == "complete"
            else status.HTTP_206_PARTIAL_CONTENT
        ),
    )
    response.headers["Content-Disposition"] = 'attachment; filename="abrolia-export.json"'
    return response


@router.post("/api/v1/onboarding/delete")
def delete_household(
    request: Request,
    current: Annotated[CurrentHousehold, Depends(current_household_fresh_mutation)],
    headers: Annotated[CommandHeaders, Depends(command_headers)],
) -> JSONResponse:
    active = container(request)
    result = active.deletion.delete(
        current.principal.account_id,
        current.household.id,
        idempotency_key=headers.idempotency_key,
        expected_version=headers.expected_version,
    )
    response_status = (
        status.HTTP_200_OK
        if result.completion_status == "complete"
        else status.HTTP_202_ACCEPTED
    )
    response = JSONResponse(
        result.public_dict(),
        status_code=response_status,
    )
    if result.replayed:
        response.headers["X-Idempotent-Replay"] = "true"
    return response

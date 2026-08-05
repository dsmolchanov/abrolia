from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from control_plane.api.dependencies import (
    CommandHeaders,
    Principal,
    command_headers,
    container,
    require_private_mutation,
)

router = APIRouter()


@router.post("/api/v1/households")
def create_household(
    request: Request,
    principal: Annotated[Principal, Depends(require_private_mutation)],
    headers: Annotated[CommandHeaders, Depends(command_headers)],
) -> JSONResponse:
    active = container(request)
    payload, replayed = active.household_service.create_idempotent(
        principal.account_id,
        idempotency_key=headers.idempotency_key,
        expected_version=headers.expected_version,
    )
    response = JSONResponse(payload)
    response.headers["ETag"] = f'"{payload["version"]}"'
    if replayed:
        response.headers["X-Idempotent-Replay"] = "true"
    return response

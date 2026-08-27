"""Owner-driven endpoints for adding a second adult to a household.

The lifecycle lives in `control_plane/repositories/bindings.py`; this is the
only thing that calls it. Both routes sit behind `require_private_mutation`
(origin → session → CSRF double-submit), the same guard every other private
mutation uses — C2 moved `/api/web/message` onto it for the same reason.

Verifying a binding ends by issuing a new configuration revision, in the SAME
transaction as the row write. That ordering is the point: the runtime derives
`RunContext` from the manifest, so a binding that existed without a revision
would be a member the household could see and the runtime would refuse, and a
revision without the row would be the reverse. Neither half can be observed
alone.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import status as http_status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from control_plane.api.dependencies import (
    CurrentHousehold,
    container,
    current_household_owner_mutation,
    read_bounded_json,
)
from control_plane.provisioning.rollout import (
    RolloutNotReady,
    schedule_runtime_rollout,
)
from control_plane.repositories.bindings import BindingError

router = APIRouter()


class ChallengeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: str = Field(min_length=1, max_length=32)
    external_id: str = Field(min_length=1, max_length=128)
    actor_id: str = Field(min_length=1, max_length=128)


class VerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=256)


async def _challenge_body(request: Request) -> ChallengeRequest:
    return await read_bounded_json(request, ChallengeRequest)


async def _verify_body(request: Request) -> VerifyRequest:
    return await read_bounded_json(request, VerifyRequest)


@router.post("/api/v1/household/bindings/challenges")
async def issue_binding_challenge(
    request: Request,
    payload: Annotated[ChallengeRequest, Depends(_challenge_body)],
    current: Annotated[CurrentHousehold, Depends(current_household_owner_mutation)],
) -> JSONResponse:
    active = container(request)
    with active.database.write() as connection:
        owner = active.bindings.owner_actor(
            connection, household_id=current.household.id
        )
        if owner is None:
            # A household with no first member cannot acquire a second. This
            # is the pre-provisioning state, not an error in the request.
            raise HTTPException(
                http_status.HTTP_409_CONFLICT,
                "household has no owner binding yet",
            )
        try:
            issued = active.bindings.issue_challenge(
                connection,
                household_id=current.household.id,
                channel=payload.channel,
                external_id=payload.external_id,
                actor_id=payload.actor_id,
                role="adult",
                issued_by_actor_id=owner,
            )
        except BindingError as error:
            raise HTTPException(http_status.HTTP_409_CONFLICT, str(error)) from error

    # The one and only time the code leaves this process. It is returned to the
    # owner to deliver, because the control plane has no sender for the channel
    # being bound — see the repository docstring for what that does and does
    # not establish. It is deliberately NOT logged.
    return JSONResponse(
        {
            "challenge_id": issued.record.id,
            "channel": issued.record.channel,
            "actor_id": issued.record.actor_id,
            "expires_at": issued.record.expires_at,
            "code": issued.code,
        }
    )


@router.post("/api/v1/household/bindings/verify")
async def verify_binding_challenge(
    request: Request,
    payload: Annotated[VerifyRequest, Depends(_verify_body)],
    current: Annotated[CurrentHousehold, Depends(current_household_owner_mutation)],
) -> JSONResponse:
    active = container(request)
    with active.database.write() as connection:
        owner = active.bindings.owner_actor(
            connection, household_id=current.household.id
        )
        if owner is None:
            raise HTTPException(
                http_status.HTTP_409_CONFLICT,
                "household has no owner binding yet",
            )
        try:
            binding = active.bindings.verify_challenge(
                connection,
                code=payload.code,
                household_id=current.household.id,
                owner_actor_id=owner,
            )
        except BindingError as error:
            raise HTTPException(http_status.HTTP_403_FORBIDDEN, str(error)) from error
        # Same transaction as the row write: see the module docstring. The
        # planner refuses a household whose onboarding is not fully verified,
        # and that refusal is a state, not a fault — answering 500 would tell
        # the owner the system broke when it in fact declined. The write above
        # rolls back with this exception, so the binding never outlives the
        # revision that authorizes it.
        try:
            planned = active.planner.issue(
                connection, household_id=current.household.id
            )
        except ValueError as error:
            raise HTTPException(
                http_status.HTTP_409_CONFLICT,
                "household configuration cannot be issued yet",
            ) from error

        # Planning a revision is not deploying one — see
        # `provisioning/rollout.py` for what that cost and why the onboarding
        # workflow is left alone. Scheduled in the SAME transaction as the row
        # and the revision, so a member is never durable without the work that
        # makes them real.
        try:
            schedule_runtime_rollout(
                connection,
                jobs=active.jobs,
                onboarding=active.onboarding_repository,
                household_id=current.household.id,
                planned=planned,
                runtime_provider=active.config.runtime_provider,
            )
        except RolloutNotReady as error:
            # The household is still settling — its first rollout has not
            # activated. A state, not a fault, and the transaction rolls back
            # with it: no binding, no revision, nothing half-applied.
            raise HTTPException(
                http_status.HTTP_409_CONFLICT,
                "household configuration cannot be issued yet",
            ) from error

    return JSONResponse(
        {
            "binding_id": binding.id,
            "channel": binding.channel,
            "actor_id": binding.actor_id,
            "role": binding.role,
            "verified_by_actor_id": binding.verified_by_actor_id,
            "config_revision": planned.revision.revision,
        }
    )

"""Progressive-enhancement form endpoints backed by the same durable commands."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from pydantic import ValidationError

from control_plane.api.auth import issue_requested_link
from control_plane.api.dependencies import container
from control_plane.auth.rate_limit import RateLimitExceeded
from control_plane.db import new_id
from control_plane.models import ProfileInput, StepKind
from control_plane.onboarding.contracts import CommandContext, WorkflowConflict
from control_plane.repositories.auth import InvalidCredential
from control_plane.repositories.households import HouseholdNotFound

router = APIRouter()

_CONSENT_RECEIPT_NAMESPACE = uuid.UUID("84b94abc-d057-48c4-a1ee-2fabf19f5139")


def _redirect(error: str | None = None) -> RedirectResponse:
    target = "/onboarding" if error is None else f"/onboarding?error={error}"
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/start/request-link")
async def request_link_form(request: Request) -> RedirectResponse:
    active = container(request)
    if request.headers.get("origin") != active.config.public_origin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "same-origin request required")
    form = await request.form()
    email = str(form.get("email", ""))
    network = request.client.host if request.client else "unknown"
    try:
        active.rate_limiter.check(
            "request-link-network", network, limit=10, window_seconds=3600
        )
        active.rate_limiter.check(
            "request-link-address", email, limit=5, window_seconds=3600
        )
        issue_requested_link(request, email)
    except (RateLimitExceeded, ValueError):
        pass
    return RedirectResponse("/start?sent=1", status_code=status.HTTP_303_SEE_OTHER)


async def _command(request: Request) -> tuple[Any, Any, CommandContext, dict[str, str]]:
    active = container(request)
    if request.headers.get("origin") != active.config.public_origin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "same-origin request required")
    form = {key: str(value) for key, value in (await request.form()).items()}
    raw_session = request.cookies.get(active.config.session_cookie_name, "")
    try:
        session = active.sessions.authenticate(raw_session)
        household = active.households.current_for_account(session.account_id)
    except (InvalidCredential, HouseholdNotFound) as error:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required") from error
    if not form.get("csrf_token") or not active.auth.verify_csrf(
        session.id, form["csrf_token"]
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF validation failed")
    try:
        expected = int(form["version"])
    except (KeyError, ValueError) as error:
        raise HTTPException(status.HTTP_428_PRECONDITION_REQUIRED, "version required") from error
    key = form.get("idempotency_key", "")
    if not 8 <= len(key) <= 128:
        raise HTTPException(status.HTTP_428_PRECONDITION_REQUIRED, "idempotency required")
    context = CommandContext(session.account_id, session.id, new_id(), key, expected)
    return active, household, context, form


@router.post("/onboarding/profile")
async def profile_form(request: Request) -> RedirectResponse:
    active, household, context, form = await _command(request)
    try:
        profile = ProfileInput.model_validate({
            "first_name": form.get("first_name"),
            "last_name": form.get("last_name"),
            "family_language": form.get("family_language"),
            "timezone": form.get("timezone"),
            "country_code": form.get("country_code"),
            "residency_mode": form.get("residency_mode", "eu-app"),
        })
        active.onboarding.save_profile(household.id, profile, context=context)
    except (ValidationError, WorkflowConflict, ValueError):
        return _redirect("profile")
    return _redirect()


def _receipt_id(
    context: CommandContext,
    household_id: str,
    purpose: str,
) -> str:
    stable_name = (
        f"{context.account_id}:{household_id}:{context.idempotency_key}:{purpose}"
    )
    return str(uuid.uuid5(_CONSENT_RECEIPT_NAMESPACE, stable_name))


def _selection(
    kind: StepKind,
    form: dict[str, str],
    *,
    context: CommandContext,
    household_id: str,
) -> dict[str, Any]:
    option = form.get("kind", "")
    if kind is StepKind.EMAIL:
        if option == "abrolia_managed":
            return {"kind": option, "local_part": form.get("local_part", "family.assistant")}
        if option == "gmail_agent":
            return {"kind": option, "separate_agent_account_acknowledged": True}
        return {
            "kind": option,
            "domain": form.get("domain", "family.example.test"),
            "local_part": form.get("local_part", "assistant"),
            "mx_change_acknowledged": form.get("mx_change_acknowledged") == "yes",
        }
    if kind is StepKind.WHATSAPP:
        if form.get("privacy_notice_accepted") != "yes":
            raise ValueError("privacy notice acceptance required")
        if option == "shared_abrolia":
            return {
                "kind": option,
                "member_phone_test_ref": "synthetic-phone:owner",
                "privacy_notice_receipt_id": _receipt_id(
                    context, household_id, "whatsapp_channel_privacy"
                ),
            }
        if form.get("linked_device_risk_accepted") != "yes":
            raise ValueError("linked-device risk acceptance required")
        return {
            "kind": option,
            "phone_test_ref": "synthetic-phone:owner",
            "privacy_notice_receipt_id": _receipt_id(
                context, household_id, "whatsapp_channel_privacy"
            ),
            "linked_device_risk_receipt_id": _receipt_id(
                context, household_id, "whatsapp_linked_device_risk"
            ),
        }
    return {
        "kind": option,
        "actor_id": "synthetic-owner",
        "chat_id": "synthetic-chat",
    }


@router.post("/onboarding/select/{kind}")
async def select_form(kind: StepKind, request: Request) -> RedirectResponse:
    active, household, context, form = await _command(request)
    try:
        active.onboarding.select(
            household.id,
            kind,
            _selection(
                kind,
                form,
                context=context,
                household_id=household.id,
            ),
            context=context,
        )
    except (ValidationError, WorkflowConflict, ValueError):
        return _redirect("selection")
    return _redirect()


@router.post("/onboarding/retry/{kind}")
async def retry_form(kind: StepKind, request: Request) -> RedirectResponse:
    active, household, context, _form = await _command(request)
    try:
        active.onboarding.retry(household.id, kind, context=context)
    except (WorkflowConflict, ValueError):
        return _redirect("retry")
    return _redirect()


@router.post("/onboarding/check/{kind}")
async def check_form(kind: StepKind, request: Request) -> RedirectResponse:
    active, household, context, _form = await _command(request)
    try:
        active.onboarding.check(household.id, kind, context=context)
    except (WorkflowConflict, ValueError):
        return _redirect("check")
    return _redirect()


@router.post("/onboarding/reset/{kind}")
async def reset_form(kind: StepKind, request: Request) -> RedirectResponse:
    active, household, context, _form = await _command(request)
    try:
        active.onboarding.reset_from(household.id, kind, context=context)
    except (WorkflowConflict, ValueError):
        return _redirect("reset")
    return _redirect()

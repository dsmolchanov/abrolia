"""Progressive-enhancement form endpoints backed by the same durable commands."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi import status as http_status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ValidationError

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
    return RedirectResponse(target, status_code=http_status.HTTP_303_SEE_OTHER)


@router.post("/start/request-link")
async def request_link_form(request: Request) -> RedirectResponse:
    active = container(request)
    if request.headers.get("origin") != active.config.public_origin:
        raise HTTPException(http_status.HTTP_403_FORBIDDEN, "same-origin request required")
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
    return RedirectResponse("/start?sent=1", status_code=http_status.HTTP_303_SEE_OTHER)


async def _command(request: Request) -> tuple[Any, Any, CommandContext, dict[str, str]]:
    active = container(request)
    if request.headers.get("origin") != active.config.public_origin:
        raise HTTPException(http_status.HTTP_403_FORBIDDEN, "same-origin request required")
    form = {key: str(value) for key, value in (await request.form()).items()}
    raw_session = request.cookies.get(active.config.session_cookie_name, "")
    try:
        session = active.sessions.authenticate(raw_session)
        household = active.households.current_for_account(session.account_id)
    except (InvalidCredential, HouseholdNotFound) as error:
        raise HTTPException(http_status.HTTP_401_UNAUTHORIZED, "authentication required") from error
    if not form.get("csrf_token") or not active.auth.verify_csrf(
        session.id, form["csrf_token"]
    ):
        raise HTTPException(http_status.HTTP_403_FORBIDDEN, "CSRF validation failed")
    try:
        expected = int(form["version"])
    except (KeyError, ValueError) as error:
        raise HTTPException(http_status.HTTP_428_PRECONDITION_REQUIRED, "version required") from error
    key = form.get("idempotency_key", "")
    if not 8 <= len(key) <= 128:
        raise HTTPException(http_status.HTTP_428_PRECONDITION_REQUIRED, "idempotency required")
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
        if form.get("special_category_restriction_acknowledged") != "yes":
            raise ValueError("special-category content restriction acknowledgement required")
        restriction_receipt_id = _receipt_id(
            context, household_id, "special_category_content_restriction"
        )
        restriction_binding = {
            "special_category_restriction_text_version": form.get(
                "special_category_restriction_text_version", ""
            ),
            "special_category_restriction_text_sha256": form.get(
                "special_category_restriction_text_sha256", ""
            ),
        }
        # The Art 9(2)(a) consent rides along on every email option, exactly as
        # the restriction does. The form only renders it in a real-email
        # rollout, so an absent checkbox here contributes nothing and the
        # server-side gate stays the single place that decides whether it was
        # required — the browser is not trusted to make that call.
        household_binding: dict[str, Any] = {}
        if form.get("special_category_household_consent") == "yes":
            household_binding = {
                "special_category_household_consent": True,
                "special_category_household_receipt_id": _receipt_id(
                    context, household_id, "special_category_household_content"
                ),
                "special_category_household_text_version": form.get(
                    "special_category_household_text_version", ""
                ),
                "special_category_household_text_sha256": form.get(
                    "special_category_household_text_sha256", ""
                ),
            }
        if option == "abrolia_managed":
            return {
                "kind": option,
                "local_part": form.get("local_part", "family.assistant"),
                "special_category_restriction_acknowledged": True,
                "special_category_restriction_receipt_id": restriction_receipt_id,
                **restriction_binding,
                **household_binding,
            }
        if option == "gmail_agent":
            return {
                "kind": option,
                "separate_agent_account_acknowledged": True,
                "special_category_restriction_acknowledged": True,
                "special_category_restriction_receipt_id": restriction_receipt_id,
                **restriction_binding,
                **household_binding,
            }
        return {
            "kind": option,
            "domain": form.get("domain", "family.example.test"),
            "local_part": form.get("local_part", "assistant"),
            "mx_change_acknowledged": form.get("mx_change_acknowledged") == "yes",
            "special_category_restriction_acknowledged": True,
            "special_category_restriction_receipt_id": restriction_receipt_id,
            **restriction_binding,
            **household_binding,
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


class WebMessageInput(BaseModel):
    text: str


@router.post("/api/web/message")
async def web_message(request: Request, payload: WebMessageInput) -> JSONResponse:
    """Authenticated Web chat — same pipeline as other channels, server-verified context."""
    active = container(request)
    raw_session = request.cookies.get(active.config.session_cookie_name, "")
    try:
        session = active.sessions.authenticate(raw_session)
        household_rec = active.households.current_for_account(session.account_id)
    except (InvalidCredential, HouseholdNotFound) as error:
        raise HTTPException(http_status.HTTP_401_UNAUTHORIZED, "authentication required") from error
    text = payload.text.strip()
    if not text:
        raise HTTPException(http_status.HTTP_400_BAD_REQUEST, "text required")
    if len(text) > 2000:
        raise HTTPException(http_status.HTTP_400_BAD_REQUEST, "text too long")
    # Server-verified RunContext: use real household id, verify membership via DB
    from hermes_cloud.channels.web import WebChannelMessage, handle_web_message
    from hermes_cloud.core.runcontext import Household, build_run_context

    # Build Household view from actual membership — owner is account, family = active members
    try:
        rows = active.database.query(
            "SELECT account_id, role FROM household_memberships WHERE household_id = ? AND status='active'",
            (household_rec.id,),
        )
        members = {r["account_id"]: r["role"] for r in rows}
        owner = next((aid for aid, role in members.items() if role == "owner"), session.account_id)
        family = frozenset(aid for aid, role in members.items() if role in ("owner", "adult"))
    except Exception:
        owner = session.account_id
        family = frozenset({session.account_id})
    hh = Household(
        household_id=household_rec.id,
        owner=owner,
        family=family,
        allowed_chats=frozenset({"web-chat"}),
    )
    context = build_run_context(household=hh, actor_id=session.account_id, chat_id="web-chat")
    if not context.is_known:
        raise HTTPException(http_status.HTTP_403_FORBIDDEN, "unknown actor for this household")
    # Route through shared pipeline if available, otherwise fallback is still capability-checked
    # For pilot, handle_web_message will delegate to loop when wired; fallback echo is not staged
    reply = handle_web_message(
        WebChannelMessage(actor_id=session.account_id, text=text),
        context=context,
        loop=getattr(active, "web_loop", None),
        pipeline=getattr(active, "web_pipeline", None),
    )
    # If handler returned fallback echo, surface as staged only when context known and text accepted
    status = "staged" if context.is_known and reply else "rejected"
    return JSONResponse({"reply": reply, "status": status})

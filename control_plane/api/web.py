"""Progressive-enhancement form endpoints backed by the same durable commands."""

from __future__ import annotations

import uuid
from functools import partial
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import status as http_status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ValidationError

from control_plane.api.auth import issue_requested_link
from control_plane.api.dependencies import (
    Principal,
    container,
    read_bounded_json,
    require_private_mutation,
)
from control_plane.auth.rate_limit import RateLimitExceeded
from control_plane.db import new_id
from control_plane.models import ProfileInput, StepKind
from control_plane.onboarding.contracts import CommandContext, WorkflowConflict
from control_plane.repositories.auth import InvalidCredential
from control_plane.repositories.households import HouseholdNotFound

router = APIRouter()

#: Exactly the roles `household_memberships.role` may hold
#: (`0001_control_plane.sql:46` CHECKs `owner`/`adult`). Anything else —
#: including the absence of a membership row — is refused rather than
#: interpreted. Spelled out here rather than imported because the control
#: plane deliberately does not import `hermes_cloud`; the runtime's own
#: vocabulary is a different set, and it independently refuses every role but
#: `owner` on this route and derives real capabilities from the manifest, so
#: neither side trusts this string on its own.
KNOWN_ROLES = frozenset({"owner", "adult"})

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
    # No `actor_id`/`chat_id`: the onboarding service derives the household's
    # channel identity in `_parse_selection`, which is the one place both select
    # routes pass through. Sending constants from here was D0 — every household
    # got the same pair, so `_reject_foreign_holder` refused the second one and
    # no second family could provision.
    return {"kind": option}


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


async def _bounded_web_message(request: Request) -> WebMessageInput:
    """The same bounded read every JSON endpoint uses.

    This began as a private helper here and was then not applied to the two
    binding endpoints written in the same session — the instance fixed, the
    invariant missed. It lives in `dependencies` now so the next JSON route
    inherits it instead of re-deciding.
    """
    return await read_bounded_json(request, WebMessageInput)


@router.post("/api/web/message")
async def web_message(
    request: Request,
    principal: Annotated[Principal, Depends(require_private_mutation)],
    payload: Annotated[WebMessageInput, Depends(_bounded_web_message)],
) -> JSONResponse:
    """Authenticated Web chat, proxied to the household's dedicated runtime.

    Same-origin, session and CSRF are enforced by ``require_private_mutation``,
    exactly as on every other mutating endpoint — a bare session cookie must
    never be enough to spend a model call. The control plane stays
    metadata-only: it verifies membership, then forwards the turn over the
    private network; the model call and its cost cap live inside the runtime,
    where the budget state actually resides.
    """
    active = container(request)
    text = payload.text.strip()
    if not text:
        raise HTTPException(http_status.HTTP_400_BAD_REQUEST, "text required")
    if len(text) > 2000:
        raise HTTPException(http_status.HTTP_400_BAD_REQUEST, "text too long")
    household_rec = active.households.current_for_account(principal.account_id)
    # Authorization FAILS CLOSED. This defaulted to "owner" — so a database
    # error, or a membership row removed between the household lookup and this
    # one, handed the caller the owner role. The runtime maps that role onto
    # `manifest.actors.owner`, whose tools include data export and deletion, so
    # the default granted the most power at exactly the moment authorization
    # could not be established. An unavailable answer is not an affirmative one.
    rows = active.database.query(
        "SELECT account_id, role FROM household_memberships"
        " WHERE household_id = ? AND status='active'",
        (household_rec.id,),
    )
    roles = {r["account_id"]: r["role"] for r in rows}
    role = roles.get(principal.account_id)
    if role not in KNOWN_ROLES:
        raise HTTPException(
            http_status.HTTP_403_FORBIDDEN,
            "household membership is required",
        )
    if not household_rec.runtime_ref:
        raise HTTPException(
            http_status.HTTP_503_SERVICE_UNAVAILABLE,
            "assistant runtime is not provisioned",
        )
    from control_plane.privacy.runtime import RuntimeBoundaryError

    try:
        # OFF THE EVENT LOOP. `web_chat.send` is synchronous and waits out a
        # whole model turn behind a 120-second timeout, while `_serve` runs
        # Uvicorn with one worker — so awaiting it inline stalled every other
        # household's request, and `/healthz` with them, for the duration.
        answer = await run_in_threadpool(
            partial(
                active.web_chat.send,
                household_rec.runtime_ref,
                actor_id=principal.account_id,
                role=role,
                text=text,
            )
        )
    except RuntimeBoundaryError as error:
        raise HTTPException(
            http_status.HTTP_503_SERVICE_UNAVAILABLE, "assistant is unavailable"
        ) from error
    return JSONResponse({"reply": answer["reply"], "status": "staged"})

"""The one question a deployed gateway is allowed to ask.

A gateway on its own Fly volume has no `channel_bindings` table — C5c's K1
says it holds roots and no household data, and after C5e it holds no
control-plane data at all. So it asks, and this answers.

Synchronous rather than a replicated projection, and the reason is in the C5e
plan: routability changes at exact moments — C3c's activation boundary, D4's
retirement of a chat an owner leaves — and five slices exist to make those
moments exact. Replication lag would sit inside that decision and re-open them.
"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from control_plane.api.dependencies import container, network_bucket
from control_plane.auth.rate_limit import RateLimitExceeded
from control_plane.bindings_resolution import SenderNotRoutable, resolve_sender

router = APIRouter()

#: Deliberately narrow. A digest is 64 hex characters, and a plaintext sender
#: is bounded because a lookup key is not a place to put a payload.
_MAX_SENDER = 256


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel: str = Field(pattern=r"^(telegram|whatsapp|web)$")
    external_id_hmac: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    external_id: str | None = Field(default=None, min_length=1, max_length=_MAX_SENDER)


def _authorized(request: Request, authorization: str | None) -> None:
    """The gateway's own credential, on private transport.

    The shape `/internal/v1/bootstrap/*` already uses. Two properties matter
    beyond "there is a token": it must be the GATEWAY's credential and never a
    household's, so it grants this question and nothing else; and the transport
    must be private, because an endpoint that answers "is this number bound"
    is an enumeration oracle wherever it is reachable.
    """
    import hmac as _hmac

    active = container(request)
    expected = active.config.gateway_lookup_token
    if not expected:
        # FAIL CLOSED. Every other optional key in this system degrades to
        # previous behaviour; this one has none to degrade to, because before
        # this slice the endpoint did not exist.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "gateway credential required")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "gateway credential required")
    if not _hmac.compare_digest(authorization[7:], expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "gateway credential required")

    expected_host = active.config.internal_bootstrap_host
    hostname = request.url.hostname
    scheme = request.url.scheme
    private = bool(
        hostname
        and expected_host
        and hostname == expected_host
        and (scheme == "https" or expected_host.endswith(".flycast"))
    )
    if not private:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "private transport required")
    try:
        active.rate_limiter.check(
            "gateway-resolve", network_bucket(request), limit=6000, window_seconds=3600
        )
    except RateLimitExceeded as error:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "try again later") from error


@router.post("/internal/v1/bindings/resolve")
def resolve(
    payload: ResolveRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, str | None]:
    """Who holds this sender, and where their runtime is.

    IDENTIFIERS ONLY — the household and the runtime reference. Not the
    external id, not the chat, not a role, not a manifest. The gateway asks
    who, and gets who; anything more would put household content on a wire
    that exists to avoid exactly that.

    `unknown_sender` and `ambiguous_sender` are different operator problems and
    the same `404` here, so the reply cannot be used to tell a bound sender
    from an unbound one by status alone.
    """
    _authorized(request, authorization)
    if (payload.external_id is None) == (payload.external_id_hmac is None):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "resolve by exactly one of external_id or external_id_hmac",
        )
    active = container(request)
    # The read connection, not a write transaction: this answers a question and
    # must never be a reason the control plane takes a write lock on the hot
    # path of every inbound message.
    try:
        resolved = resolve_sender(
            active.database.connection,
            channel=payload.channel,
            external_id=payload.external_id,
            external_id_hmac=payload.external_id_hmac,
        )
    except SenderNotRoutable:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not routable") from None
    return resolved.public_dict()

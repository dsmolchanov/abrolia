from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from control_plane.api.dependencies import container, network_bucket
from control_plane.auth.rate_limit import RateLimitExceeded
from control_plane.provisioning.bootstrap import (
    BootstrapConflict,
    BootstrapDenied,
    BootstrapGone,
)
from control_plane.provisioning.manifest_toml import manifest_to_toml

router = APIRouter()


class BootstrapBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    household_id: str = Field(min_length=32, max_length=64)
    runtime_ref: str = Field(min_length=1, max_length=256)
    config_revision: int = Field(gt=0)


class ActivationRequest(BootstrapBinding):
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    email_inbound_check: str | None = Field(default=None, pattern=r"^(healthy|failed)$")
    email_outbound_check: str | None = Field(default=None, pattern=r"^(healthy|failed)$")
    email_receipt_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bootstrap credential required")
    token = authorization[7:]
    if not 32 <= len(token) <= 512:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bootstrap credential required")
    return token


def _bootstrap_transport_allowed(*, scheme: str, hostname: str | None, expected_host: str | None) -> bool:
    if hostname is None:
        return False
    exact_host = expected_host is None or hostname == expected_host
    if scheme == "https":
        return exact_host
    return bool(
        scheme == "http"
        and expected_host
        and expected_host.endswith(".flycast")
        and hostname == expected_host
    )


def _limit(request: Request, token: str) -> None:
    active = container(request)
    expected_host = active.config.internal_bootstrap_host
    if not _bootstrap_transport_allowed(
        scheme=request.url.scheme,
        hostname=request.url.hostname,
        expected_host=expected_host,
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "private bootstrap transport required")
    try:
        active.rate_limiter.check("bootstrap-network", network_bucket(request), limit=60, window_seconds=3600)
        active.rate_limiter.check("bootstrap-token", token, limit=20, window_seconds=3600)
    except RateLimitExceeded as error:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "try again later") from error


def _raise_bootstrap(error: Exception) -> None:
    if isinstance(error, BootstrapGone):
        raise HTTPException(status.HTTP_410_GONE, "bootstrap credential unavailable") from error
    if isinstance(error, BootstrapDenied):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "bootstrap denied") from error
    raise HTTPException(status.HTTP_409_CONFLICT, "bootstrap state conflict") from error


@router.post("/internal/v1/bootstrap/claim")
def claim(
    payload: BootstrapBinding,
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict:
    token = _bearer(authorization)
    _limit(request, token)
    try:
        result = request.app.state.bootstrap.claim(
            token,
            household_id=payload.household_id,
            runtime_ref=payload.runtime_ref,
            config_revision=payload.config_revision,
        )
    except (BootstrapDenied, BootstrapConflict) as error:
        _raise_bootstrap(error)
    return {
        "household_id": result.household_id,
        "runtime_ref": result.runtime_ref,
        "config_revision": result.config_revision,
        "config_sha256": result.manifest_sha256,
        "manifest_toml": manifest_to_toml(result.manifest),
    }


@router.post("/internal/v1/bootstrap/activate")
def activate(
    payload: ActivationRequest,
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    receipt_acknowledged: bool = Header(
        default=False,
        alias="X-Hermes-Runtime-Receipt-Acknowledged",
    ),
) -> dict:
    token = _bearer(authorization)
    _limit(request, token)
    try:
        result = request.app.state.bootstrap.activate(
            token,
            household_id=payload.household_id,
            runtime_ref=payload.runtime_ref,
            config_revision=payload.config_revision,
            activated_sha256=payload.config_sha256,
            email_inbound_check=payload.email_inbound_check,
            email_outbound_check=payload.email_outbound_check,
            email_receipt_digest=payload.email_receipt_digest,
            receipt_acknowledged=receipt_acknowledged,
        )
    except (BootstrapDenied, BootstrapConflict) as error:
        _raise_bootstrap(error)
    return {
        "status": "active",
        "household_id": result.household_id,
        "runtime_ref": result.runtime_ref,
        "config_revision": result.config_revision,
        "config_sha256": result.manifest_sha256,
        "bootstrap_cleanup": ("pending" if result.cleanup_pending else "awaiting_runtime_receipt"),
    }

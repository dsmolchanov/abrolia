from __future__ import annotations

from dataclasses import replace
from hashlib import sha256

import httpx
import pytest

from control_plane.auth.mailer import (
    RESEND_EMAILS_URL,
    MailDeliveryError,
    MemoryMailer,
    ResendMailer,
)
from control_plane.auth.tokens import MagicLinkService
from control_plane.config import ControlPlaneConfig
from control_plane.container import ControlPlaneContainer

BASE_TIME = 1_700_000_000.0
LOGIN_FROM = "login@" + "abrolia.com"


def test_resend_mailer_sends_real_recipient_without_exposing_token_in_headers() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "email-1"}, request=request)

    url = "https://app.abrolia.com/auth/verify#token=one-time-secret"
    mailer = ResendMailer(
        api_key="re_secret-canary",
        sender=f"Abrolia <{LOGIN_FROM}>",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    mailer.send_magic_link(
        recipient="owner@example.com", url=url, purpose="login"
    )

    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == RESEND_EMAILS_URL
    assert request.headers["authorization"] == "Bearer re_secret-canary"
    assert request.headers["idempotency-key"] == (
        "magic-link/" + sha256(url.encode()).hexdigest()
    )
    assert "one-time-secret" not in request.headers["idempotency-key"]
    payload = request.content.decode()
    assert "owner@example.com" in payload
    assert "one-time-secret" in payload
    assert "15 minutes" in payload


@pytest.mark.parametrize("status_code", [400, 401, 429, 500])
def test_resend_mailer_returns_sanitized_provider_failure(status_code: int) -> None:
    response_body = "provider-secret-body"
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status_code, text=response_body, request=request
            )
        )
    )
    mailer = ResendMailer(
        api_key="re_secret-canary",
        sender=LOGIN_FROM,
        client=client,
    )

    with pytest.raises(MailDeliveryError) as captured:
        mailer.send_magic_link(
            recipient="owner@example.com",
            url="https://app.abrolia.com/auth/verify#token=one-time-secret",
            purpose="reauth",
        )

    representation = repr(captured.value)
    assert str(status_code) in representation
    assert response_body not in representation
    assert "re_secret-canary" not in representation
    assert "one-time-secret" not in representation
    assert "owner@example.com" not in representation


def test_resend_mailer_network_failure_is_sanitized() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("network-secret-canary", request=request)

    mailer = ResendMailer(
        api_key="re_secret-canary",
        sender=LOGIN_FROM,
        client=httpx.Client(transport=httpx.MockTransport(timeout)),
    )

    with pytest.raises(MailDeliveryError) as captured:
        mailer.send_magic_link(
            recipient="owner@example.com",
            url="https://app.abrolia.com/auth/verify#token=one-time-secret",
            purpose="login",
        )

    assert str(captured.value) == "magic-link provider request failed"


def test_resend_mailer_allows_real_magic_link_before_token_is_persisted(cp_stack) -> None:
    mailer = ResendMailer(
        api_key="re_secret-canary",
        sender=LOGIN_FROM,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"id": "email-1"}, request=request)
            )
        ),
    )
    service = MagicLinkService(cp_stack.auth, mailer, cp_stack.config.public_origin)

    issued = service.issue("owner@example.com", now=BASE_TIME)

    assert issued.token_id
    assert cp_stack.database.query_one(
        "SELECT id FROM auth_tokens WHERE id = ?", (issued.token_id,)
    ) is not None


def test_resend_mailer_rejects_invalid_recipient_before_token_is_persisted(cp_stack) -> None:
    mailer = ResendMailer(
        api_key="re_secret-canary", sender=LOGIN_FROM
    )
    service = MagicLinkService(cp_stack.auth, mailer, cp_stack.config.public_origin)

    with pytest.raises(ValueError, match="valid email"):
        service.issue("not-an-email", now=BASE_TIME)

    assert cp_stack.database.query("SELECT id FROM auth_tokens") == []


@pytest.mark.parametrize(
    ("enabled", "expected_type"),
    [(False, MemoryMailer), (True, ResendMailer)],
)
def test_container_selects_resend_only_when_delivery_gate_is_enabled(
    tmp_path, enabled: bool, expected_type: type
) -> None:
    config = replace(
        ControlPlaneConfig.for_test(tmp_path),
        magic_link_delivery_enabled=enabled,
        resend_api_key="re_secret-canary",
        magic_link_from=LOGIN_FROM,
    ).validate()

    with ControlPlaneContainer.build(config) as active:
        assert isinstance(active.magic_links.mailer, expected_type)

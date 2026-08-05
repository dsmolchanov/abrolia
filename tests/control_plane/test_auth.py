from __future__ import annotations

from urllib.parse import urlsplit

import pytest

from control_plane.auth.mailer import MemoryMailer
from control_plane.auth.sessions import FRESH_REAUTH_SECONDS, SessionService
from control_plane.auth.tokens import MAGIC_LINK_TTL_SECONDS, MagicLinkService
from control_plane.repositories.auth import InvalidCredential
from control_plane.services.accounts import AccountService

BASE_TIME = 1_800_000_000.0


def _fragment_token(url: str) -> str:
    fragment = urlsplit(url).fragment
    prefix = "token="
    assert fragment.startswith(prefix)
    return fragment.removeprefix(prefix)


def test_invite_magic_link_uses_fragment_and_is_consumed_once(cp_stack) -> None:
    mailer = MemoryMailer()
    links = MagicLinkService(cp_stack.auth, mailer, cp_stack.config.public_origin)
    issued = links.issue("new-owner@pilot.test", now=BASE_TIME)

    assert issued.expires_at == BASE_TIME + MAGIC_LINK_TTL_SECONDS
    assert len(mailer.sent) == 1
    sent = mailer.sent[0]
    parts = urlsplit(sent.url)
    assert parts.scheme == "https"
    assert parts.query == ""
    raw_token = _fragment_token(sent.url)
    token_row = cp_stack.database.query_one(
        "SELECT token_hash, email_reference_ciphertext FROM auth_tokens WHERE id = ?",
        (issued.token_id,),
    )
    assert token_row["token_hash"] != raw_token
    assert raw_token.encode() not in bytes(token_row["email_reference_ciphertext"])

    accounts = AccountService(
        cp_stack.auth,
        cp_stack.accounts,
        cp_stack.households,
        cp_stack.sessions,
    )
    result = accounts.consume_magic_link(raw_token, now=BASE_TIME + 1)
    assert result.account.recovery_email == "new-owner@pilot.test"
    assert result.household in cp_stack.households.for_account(result.account.id)
    assert cp_stack.sessions.authenticate(result.session.token, now=BASE_TIME + 2).account_id == (
        result.account.id
    )

    with pytest.raises(InvalidCredential, match="invalid or expired"):
        accounts.consume_magic_link(raw_token, now=BASE_TIME + 2)


def test_expired_tampered_and_wrong_purpose_links_are_rejected(cp_stack) -> None:
    cp_stack.auth.issue_token(
        "expired-token",
        purpose="login",
        account_id=cp_stack.account.id,
        expires_at=BASE_TIME + 5,
        now=BASE_TIME,
    )
    with pytest.raises(InvalidCredential, match="invalid or expired"):
        cp_stack.auth.consume_token("expired-token", now=BASE_TIME + 5)
    with pytest.raises(InvalidCredential, match="invalid or expired"):
        cp_stack.auth.consume_token("expired-token-tampered", now=BASE_TIME + 1)

    cp_stack.auth.issue_token(
        "purpose-token",
        purpose="reauth",
        account_id=cp_stack.account.id,
        expires_at=BASE_TIME + 10,
        now=BASE_TIME,
    )
    with pytest.raises(InvalidCredential, match="purpose mismatch"):
        cp_stack.auth.consume_token(
            "purpose-token", purpose="login", now=BASE_TIME + 1
        )
    # A failed purpose check is transactional and does not burn the credential.
    assert cp_stack.auth.consume_token(
        "purpose-token", purpose="reauth", now=BASE_TIME + 2
    ).purpose == "reauth"


def test_login_rotates_session_and_revokes_fixation_candidate(cp_stack) -> None:
    raw_login = "synthetic-login-token"
    cp_stack.auth.issue_token(
        raw_login,
        purpose="login",
        account_id=cp_stack.account.id,
        expires_at=BASE_TIME + 100,
        now=BASE_TIME,
    )
    service = AccountService(
        cp_stack.auth,
        cp_stack.accounts,
        cp_stack.households,
        cp_stack.sessions,
    )
    result = service.consume_magic_link(
        raw_login,
        now=BASE_TIME + 1,
        previous_session_id=cp_stack.session.id,
    )

    assert result.session.id != cp_stack.session.id
    assert result.session.token != cp_stack.session.token
    old = cp_stack.database.query_one(
        "SELECT revoked_at FROM sessions WHERE id = ?", (cp_stack.session.id,)
    )
    assert old["revoked_at"] == BASE_TIME + 1
    with pytest.raises(InvalidCredential):
        cp_stack.sessions.authenticate(cp_stack.session.token, now=BASE_TIME + 2)
    assert cp_stack.sessions.authenticate(
        result.session.token, now=BASE_TIME + 2
    ).id == result.session.id


def test_session_expiry_csrf_freshness_and_redaction(cp_stack) -> None:
    assert cp_stack.auth.verify_csrf(cp_stack.session.id, cp_stack.session.csrf_token)
    assert not cp_stack.auth.verify_csrf(cp_stack.session.id, "wrong-csrf")

    record = cp_stack.sessions.authenticate(cp_stack.session.token, now=BASE_TIME + 1)
    SessionService.require_fresh(record, now=BASE_TIME + FRESH_REAUTH_SECONDS)
    with pytest.raises(PermissionError, match="fresh re-authentication"):
        SessionService.require_fresh(
            record, now=BASE_TIME + FRESH_REAUTH_SECONDS + 0.001
        )

    expired_id = cp_stack.auth.create_session(
        raw_token="expired-session-token",
        raw_csrf="expired-session-csrf",
        account_id=cp_stack.account.id,
        idle_expires_at=BASE_TIME + 2,
        absolute_expires_at=BASE_TIME + 10,
        reauthenticated_at=BASE_TIME,
        now=BASE_TIME,
    )
    assert expired_id
    with pytest.raises(InvalidCredential):
        cp_stack.auth.authenticate_session(
            "expired-session-token", now=BASE_TIME + 2
        )

    representation = repr(cp_stack.session)
    assert cp_stack.session.token not in representation
    assert cp_stack.session.csrf_token not in representation
    assert "<redacted>" in representation


def test_phase_one_mailer_refuses_non_reserved_recipient(cp_stack) -> None:
    service = MagicLinkService(cp_stack.auth, MemoryMailer(), cp_stack.config.public_origin)
    with pytest.raises(ValueError, match="reserved .test"):
        service.issue("real-person@example.com", now=BASE_TIME)
    assert cp_stack.database.query(
        "SELECT id FROM auth_tokens WHERE email_lookup_hmac = ?",
        (cp_stack.lookup.email("real-person@example.com"),),
    ) == []

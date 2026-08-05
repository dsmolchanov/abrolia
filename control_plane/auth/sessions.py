from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from control_plane.repositories.auth import AuthRepository, SessionRecord

SESSION_IDLE_TTL_SECONDS = 24 * 60 * 60
SESSION_ABSOLUTE_TTL_SECONDS = 30 * 24 * 60 * 60
FRESH_REAUTH_SECONDS = 10 * 60


@dataclass(frozen=True)
class IssuedSession:
    id: str
    token: str
    csrf_token: str
    account_id: str
    idle_expires_at: float
    absolute_expires_at: float

    def __repr__(self) -> str:
        return (
            f"IssuedSession(id={self.id!r}, account_id={self.account_id!r}, "
            "token=<redacted>, csrf_token=<redacted>)"
        )


class SessionService:
    def __init__(self, repository: AuthRepository) -> None:
        self.repository = repository

    def issue(
        self,
        account_id: str,
        *,
        now: float | None = None,
        connection=None,
        security_metadata: dict | None = None,
    ) -> IssuedSession:
        now = time.time() if now is None else now
        raw_token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        idle = now + SESSION_IDLE_TTL_SECONDS
        absolute = now + SESSION_ABSOLUTE_TTL_SECONDS
        session_id = self.repository.create_session(
            raw_token=raw_token,
            raw_csrf=csrf,
            account_id=account_id,
            idle_expires_at=idle,
            absolute_expires_at=absolute,
            reauthenticated_at=now,
            security_metadata=security_metadata,
            now=now,
            connection=connection,
        )
        return IssuedSession(session_id, raw_token, csrf, account_id, idle, absolute)

    def authenticate(self, raw_token: str, *, now: float | None = None) -> SessionRecord:
        return self.repository.authenticate_session(raw_token, now=now)

    @staticmethod
    def require_fresh(session: SessionRecord, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if now - session.reauthenticated_at > FRESH_REAUTH_SECONDS:
            raise PermissionError("fresh re-authentication required")

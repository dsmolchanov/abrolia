from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from control_plane.auth.mailer import Mailer
from control_plane.repositories.auth import AuthRepository

MAGIC_LINK_TTL_SECONDS = 15 * 60


@dataclass(frozen=True)
class IssuedMagicLink:
    token_id: str
    expires_at: float


class MagicLinkService:
    def __init__(self, repository: AuthRepository, mailer: Mailer, public_origin: str) -> None:
        self.repository = repository
        self.mailer = mailer
        self.public_origin = public_origin.rstrip("/")

    def issue(
        self,
        email: str,
        *,
        purpose: str = "invite",
        account_id: str | None = None,
        now: float | None = None,
    ) -> IssuedMagicLink:
        self.mailer.validate_recipient(email)
        now = time.time() if now is None else now
        token = secrets.token_urlsafe(32)
        expires_at = now + MAGIC_LINK_TTL_SECONDS
        token_id = self.repository.issue_token(
            token,
            purpose=purpose,
            account_id=account_id,
            email=email if account_id is None else None,
            expires_at=expires_at,
            now=now,
        )
        # Fragment never reaches access logs. The confirmation page moves it to
        # a same-origin POST and clears browser history before consumption.
        url = f"{self.public_origin}/auth/verify#token={token}"
        self.mailer.send_magic_link(recipient=email, url=url, purpose=purpose)
        return IssuedMagicLink(token_id=token_id, expires_at=expires_at)

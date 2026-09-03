from __future__ import annotations

import time
from dataclasses import dataclass

from control_plane.auth.sessions import IssuedSession, SessionService
from control_plane.repositories.accounts import AccountRecord, AccountsRepository
from control_plane.repositories.auth import AuthRepository
from control_plane.repositories.households import HouseholdRecord, HouseholdsRepository


@dataclass(frozen=True)
class LoginResult:
    account: AccountRecord
    household: HouseholdRecord
    session: IssuedSession


@dataclass(frozen=True)
class RequestedLinkTarget:
    #: `None` for an invite: the account does not exist until the link is consumed.
    account_id: str | None
    email: str
    purpose: str


class AccountService:
    """Consume a one-time invite and atomically create the pilot account world."""

    def __init__(
        self,
        auth: AuthRepository,
        accounts: AccountsRepository,
        households: HouseholdsRepository,
        sessions: SessionService,
        *,
        self_signup_enabled: bool = False,
    ) -> None:
        self.auth = auth
        self.accounts = accounts
        self.households = households
        self.sessions = sessions
        self.self_signup_enabled = self_signup_enabled

    def requested_link_target(
        self,
        email: str,
        *,
        authenticated_account_id: str | None = None,
    ) -> RequestedLinkTarget | None:
        """Classify a public request.

        An unknown address becomes an invite only with self-signup on; otherwise
        it is nothing, and the public response reads the same either way. An
        account that exists but is not active is never re-invited from here:
        `consume_magic_link` would refuse the invite, and a disabled account is
        not a door a sign-up form reopens.
        """

        account = self.accounts.by_email(email)
        if account is None:
            if not self.self_signup_enabled:
                return None
            return RequestedLinkTarget(None, email, "invite")
        if account.status != "active":
            return None
        purpose = (
            "reauth" if authenticated_account_id == account.id else "login"
        )
        return RequestedLinkTarget(account.id, account.recovery_email, purpose)

    def consume_magic_link(
        self,
        raw_token: str,
        *,
        now: float | None = None,
        previous_session_id: str | None = None,
    ) -> LoginResult:
        now = time.time() if now is None else now
        with self.auth.db.write() as connection:
            token_row = self.auth.consume_token_in(connection, raw_token, now=now)
            token = self.auth.token_record(token_row)
            if token.purpose == "invite":
                if token.account_id is not None or token.email is None:
                    raise PermissionError("invite token has an invalid account binding")
                account = self.accounts.by_email(token.email)
                if account is None:
                    account = self.accounts.create_verified(
                        token.email, now=now, connection=connection
                    )
                elif account.status != "active":
                    raise PermissionError("invite does not identify an active account")
                households = self.households.for_account(account.id)
                household = (
                    households[0]
                    if households
                    else self.households.create_for_owner(
                        account.id, now=now, connection=connection
                    )
                )
            else:
                if (
                    token.purpose not in {"login", "reauth"}
                    or token.account_id is None
                    or token.email is not None
                ):
                    raise PermissionError("login token has an invalid account binding")
                account = self.accounts.get(token.account_id)
                if account is None or account.status != "active":
                    raise PermissionError("login token does not identify an active account")
                household = self.households.current_for_account(account.id)
            if previous_session_id:
                connection.execute(
                    "UPDATE sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                    (now, previous_session_id),
                )
            session = self.sessions.issue(account.id, now=now, connection=connection)
        return LoginResult(account, household, session)

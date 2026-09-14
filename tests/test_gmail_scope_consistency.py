"""The two copies of the Gmail scope set cannot drift.

The control plane requests `GMAIL_EMAIL_SCOPES` and writes it into the grant
bundle; the runtime refuses any bundle whose set differs from
`GMAIL_REQUIRED_SCOPES`. Edited alone, either constant makes every Gmail
household fail closed at activation — with nothing at review time to say so.
"""

from control_plane.email.models import GMAIL_EMAIL_SCOPES
from hermes_cloud.email.google_client import GMAIL_REQUIRED_SCOPES


def test_control_plane_and_runtime_request_the_same_scopes() -> None:
    assert set(GMAIL_EMAIL_SCOPES) == set(GMAIL_REQUIRED_SCOPES)
    assert len(GMAIL_EMAIL_SCOPES) == len(GMAIL_REQUIRED_SCOPES)


def test_the_set_is_send_only() -> None:
    """Owner decision 2026-09-13: no restricted scope, so no CASA assessment.
    `gmail.send` is sensitive; `openid` and `email` identify the mailbox."""
    assert set(GMAIL_EMAIL_SCOPES) == {
        "openid",
        "email",
        "https://www.googleapis.com/auth/gmail.send",
    }

"""What arrives in a Gmail household's relay inbox, and what the runtime did with it.

A Gmail household's mail comes in through forwarding the family turns on in
Gmail, into a hidden Nerve relay inbox. Three kinds of message reach that
inbox, observed on synthetic accounts in the Phase 0 spike
(`thoughts/shared/research/2026-09-14-gmail-forwarding-spike.md`):

- Google's forwarding **confirmation** — from `forwarding-noreply@google.com`,
  no code, one `https://mail-settings.google.com/mail/vf-…` link a human must
  open and press Confirm on, plus a `/mail/uf-…` cancel link that is never
  surfaced. It is not the family's mail and never enters extraction.
- Abrolia's own **check** message, sent from the relay to the Gmail address
  and forwarded straight back (Phase 4 sends it daily). It proves forwarding
  works and is not the family's mail either.
- Everything else is a **letter**. Gmail forwards it with the original sender
  as `from` and the relay as `to`, so the reply target is the original sender
  and nothing here needs rewriting.

Nerve's JSON carries no headers, so none of this is cryptographic: the sender
address, the link host and the path prefix are the whole test. A spoofed
confirmation from any other sender, or one pointing anywhere else, is a
letter, and a letter is untrusted content that only ever yields proposals.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

from hermes_cloud.core.db import Database
from hermes_cloud.core.observability import emit_alert
from hermes_cloud.email.contracts import EmailBinding

CONFIRMATION_SENDER = "forwarding-noreply@google.com"
CONFIRMATION_LINK_HOSTS = frozenset({"mail-settings.google.com"})
CONFIRMATION_PATH_PREFIX = "/mail/vf-"
CHECK_SUBJECT_PREFIX = "Abrolia forwarding check "
CHECK_SUBJECT = re.compile(r"^Abrolia forwarding check ([A-Za-z0-9]{8,64})$")
_LINK = re.compile(r"https://[^\s<>\"'()]+")

#: How often the runtime proves forwarding still works (Strategy A, spike S3:
#: a message from the relay to the agent Gmail comes back in seconds).
ENV_CHECK_HOURS = "ABROLIA_GMAIL_FORWARD_CHECK_HOURS"
DEFAULT_CHECK_HOURS = 24.0
#: A check that has not come back within this window is a miss.
CHECK_RETURN_WINDOW_SECONDS = 2 * 3600
#: Two consecutive misses declare forwarding stale.
STALE_AFTER_MISSES = 2
CHECK_BODY = (
    "Abrolia checks once a day that Gmail still forwards mail to it."
    " You can ignore this message."
)
#: The same switch every outgoing letter answers to (`EmailSender.send`).
ENV_OUTGOING_MAIL = "HERMES_EMAIL_SEND"

STALE_GUIDE = (
    "Gmail has stopped forwarding mail to Abrolia: the last two daily checks"
    " did not come back. In Gmail on a computer open Settings → Forwarding and"
    " POP/IMAP, make sure \"Forward a copy of incoming mail to …\" is selected"
    " for the Abrolia address, and Save Changes. Then tell me you have turned"
    " forwarding on and I will check again right away."
)

#: Shown to the owner once, with the link, when the confirmation arrives. The
#: three human steps are the ones the spike needed: Gmail forwards nothing
#: until the link is opened AND Confirm is pressed AND the forwarding radio is
#: saved.
CONFIRMATION_GUIDE = (
    "Gmail asks you to confirm forwarding to Abrolia. Open this link and press"
    " Confirm, then in Gmail on a computer open Settings → Forwarding and"
    " POP/IMAP, choose \"Forward a copy of incoming mail to …\" and Save"
    " Changes:\n{link}"
)


@dataclass(frozen=True)
class Confirmation:
    link: str


@dataclass(frozen=True)
class Check:
    token: str


@dataclass(frozen=True)
class Letter:
    pass


def _email_of(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("email") or "").strip().casefold()
    text = str(value or "").strip()
    if "<" in text and text.endswith(">"):
        text = text[text.rfind("<") + 1 : -1]
    return text.strip().casefold()


def _is_confirmation_link(link: str, allowed_hosts: frozenset[str]) -> bool:
    parts = urlsplit(link)
    return (
        parts.scheme == "https"
        and parts.username is None
        and parts.password is None
        and (parts.hostname or "").casefold() in allowed_hosts
        and parts.path.startswith(CONFIRMATION_PATH_PREFIX)
    )


def classify(
    message: Mapping[str, Any],
    *,
    agent_address: str,
    relay_address: str,
    allowed_link_hosts: frozenset[str] = CONFIRMATION_LINK_HOSTS,
) -> Confirmation | Check | Letter:
    """Decide what a relay-inbox message is, from Nerve's JSON of it."""
    sender = _email_of(message.get("from"))
    text = str(message.get("text") or "")
    if sender == CONFIRMATION_SENDER:
        links = {
            link.rstrip(".,;")
            for link in _LINK.findall(text)
            if _is_confirmation_link(link.rstrip(".,;"), allowed_link_hosts)
        }
        # Exactly one confirm link, and it must be about THIS agent address:
        # Google names the requesting mailbox in the body, and a confirmation
        # for some other mailbox is not one this household should act on.
        if len(links) == 1 and agent_address.casefold() in text.casefold():
            return Confirmation(links.pop())
        return Letter()
    if relay_address and sender == relay_address.casefold():
        matched = CHECK_SUBJECT.match(str(message.get("subject") or "").strip())
        if matched:
            return Check(matched.group(1))
    return Letter()


class ForwardingStateStore:
    """`pending` until the relay has delivered anything, `active` from then on.

    `stale` is Phase 4's: it needs the daily check message to have been sent
    and missed. Nothing here writes it.
    """

    def __init__(self, database: Database, *, clock=time.time) -> None:
        self.db = database
        self.clock = clock

    def ensure_pending(self, binding: EmailBinding, *, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        with self.db.write() as connection:
            connection.execute(
                "INSERT INTO gmail_forwarding_state (binding_identity_id, binding_revision,"
                " state, created_at, updated_at) VALUES (?, ?, 'pending', ?, ?)"
                " ON CONFLICT (binding_identity_id, binding_revision) DO NOTHING",
                (binding.identity_id, binding.revision, now, now),
            )

    def record_confirmation(
        self, binding: EmailBinding, link: str, *, now: float | None = None
    ) -> None:
        """Keep the newest confirmation, unshown, so the next chat turn shows it."""
        now = self.clock() if now is None else now
        self.ensure_pending(binding, now=now)
        with self.db.write() as connection:
            connection.execute(
                "UPDATE gmail_forwarding_state SET confirmation_link = ?, confirmation_at = ?,"
                " confirmation_shown_at = NULL, updated_at = ?"
                " WHERE binding_identity_id = ? AND binding_revision = ?",
                (link, now, now, binding.identity_id, binding.revision),
            )

    def mark_active(
        self,
        binding: EmailBinding,
        *,
        letter: bool,
        token: str | None = None,
        now: float | None = None,
    ) -> None:
        """Relay traffic proves forwarding: `active`, and a stale episode ends."""
        now = self.clock() if now is None else now
        self.ensure_pending(binding, now=now)
        column = "last_letter_at" if letter else "last_check_at"
        with self.db.write() as connection:
            connection.execute(
                f"UPDATE gmail_forwarding_state SET state = 'active', {column} = ?,"
                " misses = 0, stale_since = NULL, stale_notified_at = NULL,"
                " last_check_seen_token = COALESCE(?, last_check_seen_token),"
                " updated_at = ? WHERE binding_identity_id = ? AND binding_revision = ?",
                (now, token, now, binding.identity_id, binding.revision),
            )

    def row(self, binding: EmailBinding):
        return self.db.query_one(
            "SELECT * FROM gmail_forwarding_state WHERE binding_identity_id = ?"
            " AND binding_revision = ?",
            (binding.identity_id, binding.revision),
        )

    def state(self, binding: EmailBinding) -> str | None:
        row = self.row(binding)
        return None if row is None else str(row["state"])

    def outstanding_check_token(self, binding: EmailBinding) -> str | None:
        """The token of the check most recently sent, which a `check` must carry."""
        row = self.row(binding)
        return None if row is None else row["last_check_token"]

    def request_check(self, binding: EmailBinding, *, now: float | None = None) -> None:
        """The family says forwarding is on: check now rather than tomorrow."""
        now = self.clock() if now is None else now
        self.ensure_pending(binding, now=now)
        with self.db.write() as connection:
            connection.execute(
                "UPDATE gmail_forwarding_state SET check_requested_at = ?, updated_at = ?"
                " WHERE binding_identity_id = ? AND binding_revision = ?",
                (now, now, binding.identity_id, binding.revision),
            )

    def record_check_sent(
        self, binding: EmailBinding, token: str, *, now: float | None = None
    ) -> None:
        now = self.clock() if now is None else now
        with self.db.write() as connection:
            connection.execute(
                "UPDATE gmail_forwarding_state SET last_check_sent_at = ?, last_check_token = ?,"
                " last_check_miss_counted = 0, check_requested_at = NULL, updated_at = ?"
                " WHERE binding_identity_id = ? AND binding_revision = ?",
                (now, token, now, binding.identity_id, binding.revision),
            )

    def count_miss(self, binding: EmailBinding, *, now: float | None = None) -> int:
        """One miss per check that did not come back; returns the new count."""
        now = self.clock() if now is None else now
        with self.db.write() as connection:
            connection.execute(
                "UPDATE gmail_forwarding_state SET misses = misses + 1,"
                " last_check_miss_counted = 1, updated_at = ?"
                " WHERE binding_identity_id = ? AND binding_revision = ?",
                (now, binding.identity_id, binding.revision),
            )
        row = self.row(binding)
        return int(row["misses"]) if row is not None else 0

    def mark_stale(self, binding: EmailBinding, *, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        with self.db.write() as connection:
            connection.execute(
                "UPDATE gmail_forwarding_state SET state = 'stale', stale_since = ?,"
                " stale_notified_at = NULL, updated_at = ?"
                " WHERE binding_identity_id = ? AND binding_revision = ?",
                (now, now, binding.identity_id, binding.revision),
            )

    def take_unshown_stale_notice(
        self, binding: EmailBinding, *, now: float | None = None
    ) -> bool:
        """Whether a stale episode has not yet been shown to the owner; marks it shown."""
        now = self.clock() if now is None else now
        with self.db.write() as connection:
            row = connection.execute(
                "SELECT 1 FROM gmail_forwarding_state WHERE binding_identity_id = ?"
                " AND binding_revision = ? AND state = 'stale' AND stale_notified_at IS NULL",
                (binding.identity_id, binding.revision),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                "UPDATE gmail_forwarding_state SET stale_notified_at = ?, updated_at = ?"
                " WHERE binding_identity_id = ? AND binding_revision = ?",
                (now, now, binding.identity_id, binding.revision),
            )
        return True

    def take_unshown_confirmation(
        self, binding: EmailBinding, *, now: float | None = None
    ) -> str | None:
        """The confirmation link nobody has been shown yet, marked shown."""
        now = self.clock() if now is None else now
        with self.db.write() as connection:
            row = connection.execute(
                "SELECT confirmation_link FROM gmail_forwarding_state"
                " WHERE binding_identity_id = ? AND binding_revision = ?"
                " AND confirmation_link IS NOT NULL AND confirmation_shown_at IS NULL",
                (binding.identity_id, binding.revision),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE gmail_forwarding_state SET confirmation_shown_at = ?, updated_at = ?"
                " WHERE binding_identity_id = ? AND binding_revision = ?",
                (now, now, binding.identity_id, binding.revision),
            )
        return str(row["confirmation_link"])


@dataclass(frozen=True)
class ForwardingRelay:
    """What the Nerve worker needs to know that an inbox is a Gmail relay."""

    agent_address: str
    relay_address: str
    state: ForwardingStateStore


def check_token(secret: str, day: str) -> str:
    """HMAC(household secret, UTC date): what a check message carries in its
    subject, so a `check` seen in the relay can be matched to the one sent."""
    return hmac.new(secret.encode(), day.encode(), hashlib.sha256).hexdigest()[:32]


class CheckComposeClient(Protocol):
    def compose_email(
        self,
        *,
        inbox_id: str,
        to: str,
        subject: str,
        body: str,
        html: str | None,
        idempotency_key: str,
        attachments: list[dict[str, str]],
    ) -> dict[str, Any]: ...


class EgressBlocked(RuntimeError):
    """The outgoing-mail switch is off; the check is not sent."""


class ForwardingHealth:
    """Strategy A: prove forwarding daily with a message the relay sends itself.

    Every `ABROLIA_GMAIL_FORWARD_CHECK_HOURS` the relay mails the agent Gmail
    address `Abrolia forwarding check <token>`; Gmail forwards it straight
    back (spike S3, about 11 s) and the Nerve worker marks the state `active`.
    A check that has not come back within two hours is a miss; two consecutive
    misses declare forwarding `stale`, raise the `gmail_forwarding_stale`
    alert once, and queue the setup steps for the owner's next chat turn. A
    returning check ends the episode. The family's "I've turned forwarding on"
    (`forwarding_recheck` tool) asks for a check now rather than tomorrow.

    The send answers to the same outgoing-mail switch as every letter. It does
    not go through `EmailSender`: that path builds the message under the
    household's own address, and a check must leave from the relay — which
    is what Nerve composes from when given the relay inbox.
    """

    def __init__(
        self,
        store: ForwardingStateStore,
        *,
        binding: EmailBinding,
        relay: ForwardingRelay,
        client: CheckComposeClient,
        inbox_id: str,
        household_id: str,
        secret: str,
        env: Mapping[str, str] | None = None,
        clock=time.time,
        logger: logging.Logger | None = None,
    ) -> None:
        self.store = store
        self.binding = binding
        self.relay = relay
        self.client = client
        self.inbox_id = inbox_id
        self.household_id = household_id
        self.secret = secret
        self.env = dict(os.environ if env is None else env)
        self.clock = clock
        self.logger = logger or logging.getLogger(__name__)

    def _check_interval(self) -> float:
        try:
            hours = float(self.env.get(ENV_CHECK_HOURS, DEFAULT_CHECK_HOURS))
        except ValueError:
            hours = DEFAULT_CHECK_HOURS
        return max(hours, 0.01) * 3600

    def tick(self, *, now: float | None = None) -> str | None:
        """One pass: count a miss, declare stale, send a due check.

        Returns what happened — `stale`, `sent`, `blocked` — or None.
        """
        now = self.clock() if now is None else now
        row = self.store.row(self.binding)
        if row is None:
            return None
        outcome: str | None = None
        sent_at = row["last_check_sent_at"]
        if (
            sent_at is not None
            and not row["last_check_miss_counted"]
            and row["last_check_seen_token"] != row["last_check_token"]
            and now - sent_at > CHECK_RETURN_WINDOW_SECONDS
        ):
            misses = self.store.count_miss(self.binding, now=now)
            # Only forwarding that once worked can go stale: a household that
            # never turned it on is `pending`, and the confirmation guide is
            # what it needs, not an outage notice.
            if misses >= STALE_AFTER_MISSES and row["state"] == "active":
                self.store.mark_stale(self.binding, now=now)
                emit_alert(
                    self.logger,
                    "gmail_forwarding_stale",
                    env=self.env,
                    household_id=self.household_id,
                    misses=str(misses),
                )
                outcome = "stale"
        due = (
            row["check_requested_at"] is not None
            or sent_at is None
            or now - sent_at >= self._check_interval()
        )
        if due:
            try:
                self.send_check(now=now)
            except EgressBlocked:
                return outcome or "blocked"
            return outcome or "sent"
        return outcome

    def send_check(self, *, now: float | None = None) -> str:
        now = self.clock() if now is None else now
        if self.env.get(ENV_OUTGOING_MAIL, "0") != "1":
            raise EgressBlocked(f"outgoing mail is off (${ENV_OUTGOING_MAIL}=0)")
        day = datetime.fromtimestamp(now, UTC).date().isoformat()
        token = check_token(self.secret, day)
        self.client.compose_email(
            inbox_id=self.inbox_id,
            to=self.relay.agent_address,
            subject=f"{CHECK_SUBJECT_PREFIX}{token}",
            body=CHECK_BODY,
            html=None,
            idempotency_key=f"forwarding-check:{self.binding.identity_id}:{token}",
            attachments=[],
        )
        self.store.record_check_sent(self.binding, token, now=now)
        return token

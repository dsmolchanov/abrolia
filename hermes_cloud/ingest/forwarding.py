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

import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from hermes_cloud.core.db import Database
from hermes_cloud.email.contracts import EmailBinding

CONFIRMATION_SENDER = "forwarding-noreply@google.com"
CONFIRMATION_LINK_HOSTS = frozenset({"mail-settings.google.com"})
CONFIRMATION_PATH_PREFIX = "/mail/vf-"
CHECK_SUBJECT = re.compile(r"^Abrolia forwarding check ([A-Za-z0-9]{8,64})$")
_LINK = re.compile(r"https://[^\s<>\"'()]+")

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
        self, binding: EmailBinding, *, letter: bool, now: float | None = None
    ) -> None:
        now = self.clock() if now is None else now
        self.ensure_pending(binding, now=now)
        column = "last_letter_at" if letter else "last_check_at"
        with self.db.write() as connection:
            connection.execute(
                f"UPDATE gmail_forwarding_state SET state = 'active', {column} = ?,"
                " updated_at = ? WHERE binding_identity_id = ? AND binding_revision = ?",
                (now, now, binding.identity_id, binding.revision),
            )

    def state(self, binding: EmailBinding) -> str | None:
        row = self.db.query_one(
            "SELECT state FROM gmail_forwarding_state WHERE binding_identity_id = ?"
            " AND binding_revision = ?",
            (binding.identity_id, binding.revision),
        )
        return None if row is None else str(row["state"])

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

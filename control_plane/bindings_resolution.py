"""Which household holds a sender — the one rule, in one place.

L4. This existed as SQL inside `gateway/whatsapp_router.py`, and C5e adds a
second consumer: the control-plane endpoint the deployed gateway asks, because
a gateway on its own volume has no bindings table to read. Two SQL statements
answering one question is how the two ends of a comparison drift apart — the
C5a defect, where the gateway signed `body|timestamp` while the runtime
verified the bare body and each had passing tests of its own.

So the rule lives here and both call it: the gateway directly where it has a
database (tests, and a single-machine development run), and the endpoint on
behalf of a gateway that does not.

It answers with IDENTIFIERS and nothing else — the household, and the runtime
reference delivery needs. Not the external id, not the chat, not a role. The
gateway asks who, and gets who.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class ResolvedSender:
    """Everything the gateway is allowed to learn from one lookup."""

    household_id: str
    #: Where the delivery goes. Returned alongside the household because the
    #: alternative is a second question, and a gateway that asks twice can be
    #: answered inconsistently across the two.
    runtime_ref: str | None

    def public_dict(self) -> dict[str, str | None]:
        return {"household_id": self.household_id, "runtime_ref": self.runtime_ref}


class SenderNotRoutable(Exception):
    """Nobody holds this sender, or more than one does.

    TERMINAL, and deliberately one exception for both: `unknown_sender` and
    `ambiguous_sender` are different operator problems and the same answer to
    the caller, which is what keeps the reply from telling an unauthenticated
    party whether a number is bound.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BindingLookupUnavailable(Exception):
    """The lookup could not be made — not an answer about the sender.

    RETRYABLE, and named so the gateway catches exactly this rather than
    anything that goes wrong. A broad `except Exception` around a lookup turns
    a programming error into `lookup_unavailable`, which is a message quietly
    parked in the WAL and retried forever instead of a loud failure. It did
    exactly that once while this slice was being written.
    """


def resolve_sender(
    connection: sqlite3.Connection,
    *,
    channel: str,
    external_id: str | None = None,
    external_id_hmac: str | None = None,
) -> ResolvedSender:
    """The household holding this sender on this channel, or a refusal.

    `published_revision IS NOT NULL` is C3c and is the reason this is not a
    plain equality lookup: a binding is routable only once the revision
    carrying it has ACTIVATED. Without it, a member verified during a rollout
    was routed to a runtime still serving the previous revision, whose manifest
    had no pair for them — the runtime denied the turn and their message went
    nowhere, invisibly.

    Matched by digest when the deployment has a gateway sender key, and by
    plaintext when it does not, which is the same two modes the gateway has
    always had.
    """
    if (external_id is None) == (external_id_hmac is None):
        raise ValueError("resolve by exactly one of external_id or external_id_hmac")
    if external_id_hmac is not None:
        column, value = "external_id_hmac", external_id_hmac
    else:
        column, value = "external_id", external_id
    rows = connection.execute(
        "SELECT b.household_id AS household_id, h.runtime_ref AS runtime_ref"
        " FROM channel_bindings AS b"
        " JOIN households AS h ON h.id = b.household_id"
        f" WHERE b.channel = ? AND b.{column} = ?"
        " AND b.published_revision IS NOT NULL",
        (channel, value),
    ).fetchall()
    if not rows:
        raise SenderNotRoutable("unknown_sender")
    if len(rows) > 1:
        # One sender reaching two households is denied for BOTH, including the
        # household that was there first: delivering to either would be a guess
        # about whose message it is.
        raise SenderNotRoutable("ambiguous_sender")
    return ResolvedSender(
        household_id=str(rows[0]["household_id"]),
        runtime_ref=rows[0]["runtime_ref"],
    )

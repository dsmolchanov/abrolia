"""Who a household's FALLBACK OWNER is, asked in exactly one place.

C4a made an active owner's verified contact address the household's email
fallback. That created one question — *which account is that, and is this
address theirs?* — and four rounds of review found four places answering it
slightly differently:

* the preferences repository refused a pairing nothing asked it about;
* `EmailIdentityService.select` asked it for the managed and own-domain
  mailboxes, after the repository had already been given the answer too late
  to be correctable;
* `GoogleOAuthService.callback` compared a Gmail grant with the INITIATING
  account instead, so any other owner's address walked past;
* and the planner chose the owner by membership status alone, so a LOCKED
  account could be selected as the fallback and refused afterwards, from
  inside a provisioning job.

`AGENTS.md`, "Fix the invariant, not the instance", says a class reported
twice is one missing rule. This module is that rule, and
`tests/control_plane/test_owner_predicate.py` is the check that keeps it one.

**Active means both lifecycles.** A membership can be `active` while the
account behind it is `locked`, `deleting` or `deleted`. Such an account cannot
receive a fallback, so it must not be chosen as one — and a mailbox matching
its address must not be refused on its behalf either, which is the same
mistake pointing the other way.

**The choice is ordered.** `LIMIT 1` over an unordered set makes which owner
is the fallback depend on storage order, so two runs of the planner over one
household could disagree — and `config_sha256` would move for a household that
changed nothing.
"""

from __future__ import annotations

from control_plane.crypto import LookupHasher, normalize_email

#: Both lifecycles, stated once. Every query below builds on it.
_ACTIVE_OWNER = (
    " FROM accounts AS a"
    " JOIN household_memberships AS m ON m.account_id = a.id"
    " WHERE m.household_id = ? AND m.role = 'owner' AND m.status = 'active'"
    " AND a.status = 'active'"
)
#: Oldest membership first, so the fallback does not move between two runs
#: that changed nothing. `a.id` breaks a tie no data can.
_ORDER = " ORDER BY m.created_at, a.id"


def fallback_owner_query(household_id: str) -> tuple[str, tuple[str]]:
    """The account this household's email fallback is delivered to."""
    return f"SELECT a.id{_ACTIVE_OWNER}{_ORDER} LIMIT 1", (household_id,)


def fallback_owner_check_query(
    *, household_id: str, account_id: str
) -> tuple[str, tuple[str, str]]:
    """Whether THIS account may be that owner — asked before recording it."""
    return f"SELECT a.id{_ACTIVE_OWNER} AND a.id = ? LIMIT 1", (household_id, account_id)


def owner_contact_query(
    lookup: LookupHasher, *, household_id: str, address: str
) -> tuple[str, tuple[str, str]]:
    """Whether an address is a fallback owner's own — the self-ingestion check.

    A mailbox equal to it makes every failed delivery arrive back as a new
    inbound message, so each path that can create a mailbox asks this before
    the mailbox becomes durable.

    The comparison is between lookup digests: `accounts` and `email_identities`
    hash addresses with the same `LookupHasher`, so equality of address is
    equality of digest, and no caller needs a key or a plaintext.
    """
    return (
        f"SELECT a.id{_ACTIVE_OWNER} AND a.recovery_email_lookup_hmac = ? LIMIT 1",
        (household_id, lookup.email(normalize_email(address))),
    )

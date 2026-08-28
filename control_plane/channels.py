"""What a channel identity looks like when its own ingest reports it.

`AGENTS.repo-invariants.md` records the rule this module exists to enforce: a
stored identity must be the string the channel's ingest actually produces,
because `Household.knows_binding` compares by string and nothing else. Two
spellings of one identity are two rows, and the second authorizes nobody — it
issues, verifies, publishes a revision and rolls out, and then matches no
inbound turn at all.

`strip()` alone was the first version of this and it closed only the reported
case. Whitespace is the easiest way to spell an identity wrongly, not the only
one: a WhatsApp sender is `+`-prefixed by the time `trusted_run_context` sees
it and bare inside `parse_webhook`, a JID's domain is case-insensitive where
its local part is not, and a Telegram chat is a signed integer rendered as
text.

The rules live HERE rather than beside each channel, which is where the debt
plan first put them, because the dependency runs one way: `hermes_cloud`
imports `control_plane` (`runtime/service.py`, `gateway/whatsapp_router.py`)
and never the reverse. Putting them next to the ingest code would have meant
the control plane importing the runtime to write a row. What keeps the two
honest instead is a test that drives a real webhook and a real Telegram update
through their own parsers and asserts the canonical form equals what came out
— agreement proven by running something, rather than by two modules promising
to stay in step.
"""

from __future__ import annotations

import re

#: B-07 keeps real transport identities out until an explicit gate, so the
#: synthetic namespace is a legitimate identity everywhere a real one will
#: later go. It has no transport form to canonicalize INTO — `synthetic-owner`
#: is not a phone number waiting to be normalized — so the rule for it is the
#: rule for any opaque token: strip it and store what you were given.
_SYNTHETIC = "synthetic-"

_WHATSAPP_SENDER = re.compile(r"^\+[0-9]{1,31}$")
_WHATSAPP_CHAT = re.compile(r"^[0-9][0-9-]{0,63}@[a-z0-9.-]{1,64}$")
#: Telegram sends its IDs as JSON NUMBERS, so `parse_update` renders them with
#: `str()` and a leading zero can never survive the round trip: `00123` arrives
#: as `123`. A padded spelling is therefore not a variant of the canonical form
#: the way a bare WhatsApp number is a variant of `+999…` — it is a value no
#: inbound turn can carry, which is what `_stripped` already says and what this
#: pattern now enforces. Refused rather than normalized, because nothing
#: produces it: a household that typed one gets told, instead of getting a row
#: that quietly authorizes nobody.
_TELEGRAM_ID = re.compile(r"^(?:0|-?[1-9][0-9]{0,30})$")


class ChannelIdentityError(ValueError):
    """An identity no inbound turn on that channel could ever carry."""


def canonical_sender(channel: str, value: str) -> str:
    """The SENDER as the gateway will match it and the runtime will authorize it."""
    text = _stripped(value, "external ID")
    if text.startswith(_SYNTHETIC):
        return text
    if channel == "whatsapp":
        # `as_eml` writes `X-Abrolia-WhatsApp-Actor` with a leading `+`, adding
        # one if the webhook omitted it, so that is the form authorization sees.
        # Accepting both spellings and storing one is the whole point: bare and
        # prefixed are the same person and must not be two rows.
        candidate = text if text.startswith("+") else f"+{text}"
        if not _WHATSAPP_SENDER.fullmatch(candidate):
            raise ChannelIdentityError("a WhatsApp sender is a phone number")
        return candidate
    if channel == "telegram":
        if not _TELEGRAM_ID.fullmatch(text):
            raise ChannelIdentityError(
                "a Telegram sender is a numeric user ID as JSON renders it"
            )
        return text
    return text


def canonical_chat(channel: str, value: str) -> str:
    """The CONVERSATION as that channel's ingest reports it."""
    text = _stripped(value, "chat ID")
    if text.startswith(_SYNTHETIC):
        return text
    if channel == "whatsapp":
        # A JID, not a number: `parse_webhook` reads `remote_jid` and falls back
        # to `<digits>@s.whatsapp.net`, and a group is `@g.us`. The domain is
        # case-insensitive and the local part is digits, so lowercasing the
        # whole thing cannot change which conversation it names.
        candidate = text.lower()
        if not _WHATSAPP_CHAT.fullmatch(candidate):
            raise ChannelIdentityError(
                "a WhatsApp conversation is a JID, not a phone number"
            )
        return candidate
    if channel == "telegram":
        if not _TELEGRAM_ID.fullmatch(text):
            raise ChannelIdentityError(
                "a Telegram chat is a numeric chat ID as JSON renders it"
            )
        return text
    return text


def _stripped(value: str, field: str) -> str:
    # Every ingest path strips before it reports — `whatsapp_webhook._text`
    # returns `value.strip()`, and Telegram's IDs arrive as JSON numbers — so a
    # padded identity is not a variant of the canonical one. It is a value no
    # inbound turn can carry.
    text = value.strip()
    if not text:
        raise ChannelIdentityError(f"{field} is required")
    return text

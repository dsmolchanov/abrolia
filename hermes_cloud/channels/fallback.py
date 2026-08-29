"""When the primary channel refuses a message, tell the family by email.

`channel_preferences` has said since `0006` that a household has a primary
channel and an email fallback, C4a gave it a writer, and
`hermes_cloud/core/observability.py` has carried the label
`primary_unavailable` — "primary unavailable — routing fallback triggered" —
with nothing that emits it. This is the consumer of all three.

It is a DECORATOR around `Transport` rather than a branch at each send. The
runtime writes to the family from 28 call sites across the pipeline, the
scheduler and the CLI, and every one of them would have had to remember; the
transport is built once, in `hermes_cloud/cli.py::_pipeline`, so wrapping it
there is one seam that covers all of them and cannot be forgotten by the next
one added.

**Only a refusal falls back, and only a definitive one.** Three outcomes, not
two. `SendOutcomeUnknown` means the request may well have arrived — a second
copy by email would duplicate it and assert a failure nobody can prove; the
repository has spent two slices on that distinction (C3e's stranded rollouts,
the email send store's `outcome_unknown`). A `TransportError` carrying
`definitive=False` — a 429 or a 5xx — is the channel being busy or broken,
where a letter about a message that arrives a second later is worse than
silence. Only `definitive=True` reaches the family's inbox: a blocked bot, a
chat that no longer exists, an API answering `ok: false`.

**The notice carries no household content.** `docs/privacy/data-map.md` says
the fallback is link-only, and the reason survives the missing link: email is
the channel the family did NOT choose, reaching an address that may be read on
a shared device. So the notice says that something is waiting and where, and
never what it says. There is no link in it today because the runtime does not
know its own web address — that is a manifest field this does not invent.

**The original error still reaches the caller.** The notice is a courtesy, not
a delivery: pretending the send succeeded would tell the pipeline a card was
shown when it was not.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Protocol

from hermes_cloud.channels.telegram import Transport, TransportError
from hermes_cloud.core.observability import emit_alert
from hermes_cloud.execute.email_send import EgressBlocked, Outgoing

logger = logging.getLogger(__name__)

#: One notice per channel per hour. The send store settles by effect id, so a
#: repeated failure inside the window reuses the receipt instead of sending
#: again — a family whose channel is down for an afternoon gets one letter,
#: while the alert below fires every time, because observability wants the
#: count and the family does not.
NOTICE_WINDOW_SECONDS = 3600.0

SUBJECT = "Abrolia: сообщение не доставлено"
BODY = (
    "Мы не смогли доставить сообщение в {channel}.\n\n"
    "Оно ждёт вас в Abrolia. Содержимое письмом не отправляем."
)


class NoticeSender(Protocol):
    """The part of `EmailSender` this needs, and nothing more."""

    def send_notice(self, letter: Outgoing, *, notice_id: str): ...


class FallbackTransport:
    """A `Transport` that writes to the family when the channel refuses."""

    def __init__(
        self,
        inner: Transport,
        *,
        mail: NoticeSender | None,
        fallback: str,
        channel: str,
        household_id: str = "",
        clock=time.time,
    ) -> None:
        self.inner = inner
        self.mail = mail
        self.fallback = fallback
        self.channel = channel
        self.household_id = household_id
        self.clock = clock

    # --- the two ways the runtime reaches the family ----------------------

    def send_message(
        self,
        *,
        chat: str,
        text: str,
        thread: int | None = None,
        buttons: tuple[tuple[str, str], ...] = (),
    ) -> str:
        try:
            return self.inner.send_message(
                chat=chat, text=text, thread=thread, buttons=buttons
            )
        except TransportError as error:
            if error.definitive:
                self._refused(error)
            raise

    def send_document(
        self,
        *,
        chat: str,
        filename: str,
        content: bytes,
        caption: str = "",
        thread: int | None = None,
    ) -> str:
        try:
            return self.inner.send_document(
                chat=chat,
                filename=filename,
                content=content,
                caption=caption,
                thread=thread,
            )
        except TransportError as error:
            if error.definitive:
                self._refused(error)
            raise

    # --- everything else is the channel's own business --------------------

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.inner.answer_callback(callback_id, text)

    def get_updates(self, *, offset: int | None = None, timeout: int = 25) -> list[dict]:
        return self.inner.get_updates(offset=offset, timeout=timeout)

    # --- the fallback -----------------------------------------------------

    def _notice_id(self) -> str:
        """An effect id that is safe in a header and true about the request.

        `build_message` puts the effect id verbatim into `Message-ID`, where a
        colon is not a legal character: the readable
        `primary-unavailable:telegram:1` serialized as the truncated
        `<hermes-primary-unavailable` with two `InvalidHeaderDefect`s, and every
        window produced the same broken header for a provider to reject or
        deduplicate.

        A digest fixes more than the syntax. The send store settles by effect
        id, so an id must identify the request it settles: it covers the
        RECIPIENT and the sending address as well as the window, because a
        fallback that changed mid-window would otherwise reuse the receipt of a
        letter addressed somewhere else — and `EmailSendStore.begin` would
        refuse the changed request, which `_refused` swallows, so the new
        address would silently get nothing.
        """
        window = int(self.clock() // NOTICE_WINDOW_SECONDS)
        material = "\x1f".join(
            (
                "primary-unavailable",
                self.channel,
                str(window),
                self.fallback,
                str(getattr(self.mail, "sender", "")),
            )
        )
        return f"primary-unavailable-{hashlib.sha256(material.encode()).hexdigest()[:32]}"

    def _refused(self, error: TransportError) -> None:
        # The alert fires on every refusal, including the ones whose letter is
        # suppressed by the window: an operator counting outages must not have
        # to know about de-duplication that exists for the family's sake.
        emit_alert(
            logger,
            "primary_unavailable",
            channel=self.channel,
            household_id=self.household_id,
        )
        if self.mail is None or not self.fallback:
            # Nothing to write with, or nowhere to write to. Said once, at
            # warning level, because a household with no fallback configured is
            # a household nobody can reach right now.
            logger.warning(
                "primary channel refused and no email fallback is configured"
            )
            return
        letter = Outgoing(
            to=self.fallback,
            subject=SUBJECT,
            body=BODY.format(channel=self.channel),
        )
        try:
            self.mail.send_notice(letter, notice_id=self._notice_id())
        except EgressBlocked:
            # The operator brake is on. That is an answer, not an error.
            logger.warning("fallback notice suppressed: outgoing mail is disabled")
        except Exception:
            # A fallback that raises would replace the channel's own failure
            # with its own, and the caller is about to be told about the first
            # one — which is the failure that actually happened.
            logger.exception("fallback notice could not be sent")

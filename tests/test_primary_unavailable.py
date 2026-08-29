"""C4b: what happens when the channel the family chose refuses a message.

`primary_unavailable` has existed as a label in
`hermes_cloud/core/observability.py` with nothing emitting it, and
`channel_preferences` has promised an email fallback since `0006`. These cases
are the consumer of both, and each one pins a sentence of
`hermes_cloud/channels/fallback.py`'s contract rather than the implementation
that happens to satisfy it.
"""

from __future__ import annotations

import logging

import pytest

from hermes_cloud.channels.fallback import NOTICE_WINDOW_SECONDS, FallbackTransport
from hermes_cloud.channels.telegram import SendOutcomeUnknown, TransportError
from hermes_cloud.execute.email_send import EgressBlocked, Outgoing

FALLBACK = "owner@family.test"


class RefusingTransport:
    """A channel that answers, and says no."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error or TransportError("sendMessage: HTTP 403 Forbidden")
        self.sent: list[str] = []

    def send_message(self, *, chat, text, thread=None, buttons=()) -> str:
        raise self.error

    def send_document(self, *, chat, filename, content, caption="", thread=None) -> str:
        raise self.error

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.sent.append(callback_id)

    def get_updates(self, *, offset=None, timeout=25) -> list[dict]:
        return []


class RecordingMail:
    def __init__(self, error: BaseException | None = None) -> None:
        self.letters: list[tuple[Outgoing, str]] = []
        self.error = error

    def send_notice(self, letter: Outgoing, *, notice_id: str):
        self.letters.append((letter, notice_id))
        if self.error is not None:
            raise self.error
        return None


def _transport(inner, mail, *, clock=lambda: 0.0, fallback=FALLBACK):
    return FallbackTransport(
        inner,
        mail=mail,
        fallback=fallback,
        channel="telegram",
        household_id="synthetic-household",
        clock=clock,
    )


@pytest.mark.parametrize("method", ("send_message", "send_document"))
def test_a_refused_message_writes_to_the_family_and_raises_anyway(
    method, caplog
) -> None:
    """Both ways the runtime reaches the family, and the error still lands.

    The notice is a courtesy, not a delivery: swallowing the refusal would tell
    the pipeline a card was shown when it was not.
    """
    mail = RecordingMail()
    transport = _transport(RefusingTransport(), mail)

    with caplog.at_level(logging.WARNING), pytest.raises(TransportError):
        if method == "send_message":
            transport.send_message(chat="c", text="привет")
        else:
            transport.send_document(chat="c", filename="a.ics", content=b"x")

    (letter, notice_id) = mail.letters[0]
    assert letter.to == FALLBACK
    assert notice_id.startswith("primary-unavailable:telegram:")
    assert "ALERT primary_unavailable" in caplog.text
    # The identifier is hashed by `emit_alert`, never printed.
    assert "synthetic-household" not in caplog.text


def test_an_unknown_outcome_is_not_a_failure_and_sends_nothing() -> None:
    """`SendOutcomeUnknown` means the message may well have arrived.

    A second copy by email would duplicate it, and would assert a failure
    nobody can prove. The two errors are siblings rather than parent and child,
    which is what makes catching one of them possible at all — this case is
    what keeps them that way.
    """
    mail = RecordingMail()
    transport = _transport(RefusingTransport(SendOutcomeUnknown("connection lost")), mail)

    with pytest.raises(SendOutcomeUnknown):
        transport.send_message(chat="c", text="привет")

    assert mail.letters == []


def test_the_notice_carries_no_household_content() -> None:
    """Email is the channel the family did not choose, read where they are not.

    `docs/privacy/data-map.md` calls the fallback link-only. The link is absent
    today because the runtime does not know its own web address; the property
    that matters is that the message being delivered never appears.
    """
    mail = RecordingMail()
    transport = _transport(RefusingTransport(), mail)

    with pytest.raises(TransportError):
        transport.send_message(chat="c", text="Заберите Машу из школы в 15:40")

    letter = mail.letters[0][0]
    assert "Маша" not in letter.body and "15:40" not in letter.body
    assert "Маша" not in letter.subject
    assert "telegram" in letter.body


def test_one_letter_per_window_while_every_refusal_is_alerted(caplog) -> None:
    """An outage is one letter and many alerts.

    The send store settles by effect id, so a stable id inside the window makes
    the repeat harmless; the operator counting outages must not have to know
    about de-duplication that exists for the family's sake.
    """
    mail = RecordingMail()
    now = [0.0]
    transport = _transport(RefusingTransport(), mail, clock=lambda: now[0])

    with caplog.at_level(logging.WARNING):
        for offset in (0.0, NOTICE_WINDOW_SECONDS / 2, NOTICE_WINDOW_SECONDS * 1.5):
            now[0] = offset
            with pytest.raises(TransportError):
                transport.send_message(chat="c", text="привет")

    assert len({notice_id for _letter, notice_id in mail.letters}) == 2
    assert caplog.text.count("ALERT primary_unavailable") == 3


@pytest.mark.parametrize(
    ("mail", "fallback", "reason"),
    (
        (None, FALLBACK, "no email sender"),
        (RecordingMail(), "", "no fallback address"),
        (RecordingMail(EgressBlocked("outgoing mail is off")), FALLBACK, "kill switch"),
        (RecordingMail(RuntimeError("smtp exploded")), FALLBACK, "sender failure"),
    ),
)
def test_a_fallback_that_cannot_be_sent_never_replaces_the_channel_s_error(
    mail, fallback, reason, caplog
) -> None:
    """Whatever goes wrong here, the caller hears about the CHANNEL.

    Including the operator brake: `HERMES_EMAIL_SEND=0` is an answer, not an
    error, and a notice that ignored it would be egress the operator believes
    is stopped.
    """
    transport = _transport(RefusingTransport(), mail, fallback=fallback)

    with caplog.at_level(logging.WARNING), pytest.raises(TransportError):
        transport.send_message(chat="c", text="привет")

    assert "ALERT primary_unavailable" in caplog.text, reason

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
from pathlib import Path

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
    assert notice_id.startswith("primary-unavailable-")
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


@pytest.mark.parametrize("method", ("send_message", "send_document"))
@pytest.mark.parametrize(
    ("status", "definitive"),
    ((403, True), (400, True), (429, False), (500, False), (503, False)),
)
def test_a_busy_channel_is_not_a_refusal(method, status, definitive) -> None:
    """"Not now" is not "no", and only "no" writes to the family.

    `TelegramTransport` raises `TransportError` for every HTTP answer,
    including the rate limit it hits on a chatty afternoon and the 5xx it hits
    while Telegram is having an outage. A letter about a message that arrives a
    second later is worse than silence, and the worker's own retry would repeat
    it across windows.
    """
    error = TransportError(
        f"sendMessage: HTTP {status}", status=status, definitive=status not in (429, 500, 503)
    )
    mail = RecordingMail()
    transport = _transport(RefusingTransport(error), mail)

    with pytest.raises(TransportError):
        if method == "send_message":
            transport.send_message(chat="c", text="привет")
        else:
            transport.send_document(chat="c", filename="a.ics", content=b"x")

    assert bool(mail.letters) is definitive


@pytest.mark.parametrize(
    ("status", "definitive"),
    ((403, True), (404, True), (408, False), (429, False), (502, False)),
)
def test_the_transport_classifies_the_status_it_was_given(status, definitive) -> None:
    """The classification lives with the channel that knows its own statuses.

    Asserted against `TelegramTransport` itself rather than against a fixture's
    idea of it, because `FallbackTransport` trusts the flag entirely: a channel
    that mislabels its errors turns the rule above into decoration.
    """
    from hermes_cloud.channels.telegram import _definitive

    assert _definitive(status) is definitive


def test_no_family_transport_is_built_without_the_fallback_around_it() -> None:
    """The next outbound path must not be able to forget this one.

    The deployed image runs `hermes_cloud.runtime.service`, which builds no
    channel transport at all: it serves HTTP and runs the inbound email and
    Gmail workers, so there is no primary-channel send there to fall back
    from. The pipeline that does send is built in `hermes_cloud/cli.py`, and
    that is where the decorator is installed.

    What this pins is the day that changes. Whoever gives the runtime an
    outbound channel will construct a transport, and this fails until it is
    wrapped — which is the enforceable half of "route every primary channel
    through the fallback", the other half being a delivery path that does not
    exist yet.
    """
    package = Path(__file__).resolve().parents[1] / "hermes_cloud"
    builders = {"TelegramTransport(", "ConsoleTransport("}
    offenders = sorted(
        path.relative_to(package).as_posix()
        for path in package.rglob("*.py")
        if any(builder in path.read_text(encoding="utf-8") for builder in builders)
        and "FallbackTransport" not in path.read_text(encoding="utf-8")
        and path.name not in {"console.py", "telegram.py"}
    )
    assert offenders == [], (
        "these modules build a family-facing transport without wrapping it in"
        f" FallbackTransport: {', '.join(offenders)}"
    )


@pytest.mark.parametrize("method", ("send_message", "send_document"))
def test_the_notice_id_is_legal_in_a_message_header(method) -> None:
    """`build_message` puts the effect id into `Message-ID` verbatim.

    The readable form was `primary-unavailable:telegram:1`, and a colon is not
    legal there: it serialized as the truncated `<hermes-primary-unavailable`
    with two `InvalidHeaderDefect`s, identically for every window, which a
    provider may reject or deduplicate.
    """
    from hermes_cloud.execute.email_send import build_message

    mail = RecordingMail()
    transport = _transport(RefusingTransport(), mail)
    with pytest.raises(TransportError):
        if method == "send_message":
            transport.send_message(chat="c", text="привет")
        else:
            transport.send_document(chat="c", filename="a.ics", content=b"x")

    letter, notice_id = mail.letters[0]
    message = build_message(letter, sender="agent@abrolia.test", approval_id=notice_id)
    assert message["Message-ID"].defects == ()
    assert notice_id in str(message["Message-ID"])


def test_the_notice_id_changes_with_the_window_and_with_the_recipient() -> None:
    """It settles a send, so it has to identify the send it settles.

    Two letters with one effect id are one receipt: a fallback address changed
    mid-window would otherwise reuse the receipt of a letter addressed
    somewhere else, and `EmailSendStore.begin` would refuse the changed
    request — which `_refused` swallows, so the new address would get nothing.
    """
    now = [0.0]

    def notice_for(fallback: str, sender: str) -> str:
        mail = RecordingMail()
        mail.sender = sender
        transport = _transport(
            RefusingTransport(), mail, clock=lambda: now[0], fallback=fallback
        )
        with pytest.raises(TransportError):
            transport.send_message(chat="c", text="привет")
        return mail.letters[0][1]

    first = notice_for(FALLBACK, "agent@abrolia.test")
    assert notice_for(FALLBACK, "agent@abrolia.test") == first
    assert notice_for("other@family.test", "agent@abrolia.test") != first
    assert notice_for(FALLBACK, "moved@abrolia.test") != first
    now[0] = NOTICE_WINDOW_SECONDS
    assert notice_for(FALLBACK, "agent@abrolia.test") != first

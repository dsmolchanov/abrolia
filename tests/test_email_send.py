"""Исходящее письмо: адрес подтверждается, флаг перечитывается, повтора нет."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cloud.channels.telegram import FakeTransport
from hermes_cloud.core.approvals import ApprovalStore, payload_sha
from hermes_cloud.core.db import open_database
from hermes_cloud.core.runcontext import Household, build_run_context
from hermes_cloud.execute.email_send import (
    ENV_KILL_SWITCH,
    EgressBlocked,
    EmailOutcomeUnknown,
    EmailRejected,
    EmailSender,
    FakeSmtp,
    Outgoing,
    build_message,
    message_id_for,
    validate,
)
from hermes_cloud.execute.reminder import ReminderStore
from hermes_cloud.runner.bundle import Item, bundle_payload
from hermes_cloud.runner.card import ACTION_CONFIRM, KIND_BUNDLE, KIND_EMAIL
from hermes_cloud.runner.pipeline import Pipeline
from hermes_cloud.runner.tools import REGISTRY, Services, ToolInputError

CHAT = "-100990000101"
PARENT = "990000001"
SCHOOL = "sekretariat@grundschule.example"
FAMILY_ADDRESS = "family@example.com"

HOUSEHOLD = Household(
    owner=PARENT, family=frozenset({PARENT}), allowed_chats=frozenset({CHAT})
)

LETTER = Outgoing(
    to=SCHOOL,
    subject="Klassenfahrt 3b — Teilnahme",
    body="Guten Tag, unser Kind nimmt teil. Der Beitrag ist überwiesen.",
)


def context():
    return build_run_context(household=HOUSEHOLD, actor_id=PARENT, chat_id=CHAT)


@pytest.fixture()
def world(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(ENV_KILL_SWITCH, "1")
    database = open_database(tmp_path / "hermes.db")
    smtp = FakeSmtp()
    transport = FakeTransport()
    pipeline = Pipeline(
        approvals=ApprovalStore(database),
        reminders=ReminderStore(database),
        transport=transport,
        extractor=None,
        chat=CHAT,
        mail=EmailSender(smtp, sender=FAMILY_ADDRESS),
    )
    return pipeline, transport, smtp


def stage_email(pipeline: Pipeline, **overrides):
    payload = bundle_payload(
        [Item(payload={
            "kind": KIND_EMAIL, "to": LETTER.to, "subject": LETTER.subject,
            "body": LETTER.body, "in_reply_to": None, **overrides,
        })],
        header=f"Письмо для {LETTER.to}",
    )
    return pipeline.approvals.stage(
        kind=KIND_BUNDLE, payload=payload, chat=CHAT, actor=PARENT
    )


# --- проверки письма ----------------------------------------------------------


def test_header_injection_is_refused() -> None:
    """Перевод строки в теме — это чужой Bcc в нашем письме."""
    with pytest.raises(EmailRejected):
        validate(Outgoing(to=SCHOOL, subject="Тема\r\nBcc: chef@example.com", body="текст"))
    with pytest.raises(EmailRejected):
        validate(Outgoing(to=f"{SCHOOL}\nBcc: chef@example.com", subject="Тема", body="текст"))


@pytest.mark.parametrize(
    "letter",
    [
        Outgoing(to="не адрес", subject="Тема", body="текст"),
        Outgoing(to=SCHOOL, subject="", body="текст"),
        Outgoing(to=SCHOOL, subject="Тема", body="   "),
        Outgoing(to=SCHOOL, subject="Т" * 300, body="текст"),
    ],
)
def test_a_bad_letter_never_reaches_smtp(letter: Outgoing) -> None:
    with pytest.raises(EmailRejected):
        validate(letter)


def test_a_reply_lands_in_the_original_thread() -> None:
    letter = Outgoing(
        to=SCHOOL, subject="Re: Klassenfahrt", body="Danke!",
        in_reply_to="<original@grundschule.example>",
    )

    message = build_message(letter, sender=FAMILY_ADDRESS, approval_id="a1")

    assert message["In-Reply-To"] == "<original@grundschule.example>"
    assert message["References"] == "<original@grundschule.example>"
    assert message["From"] == FAMILY_ADDRESS


def test_the_message_id_is_deterministic_per_approval() -> None:
    assert message_id_for("a1") == message_id_for("a1") != message_id_for("a2")


# --- kill-switch --------------------------------------------------------------


def test_the_kill_switch_blocks_egress_with_an_honest_message(world, monkeypatch) -> None:
    """Флаг перечитывается перед транспортом: ранняя проверка защищает до первого
    нового вызывающего."""
    pipeline, transport, smtp = world
    monkeypatch.setenv(ENV_KILL_SWITCH, "0")
    staged = stage_email(pipeline)

    handled = pipeline.handle_callback(
        action=ACTION_CONFIRM, approval_id=staged.id, context=context()
    )

    assert handled.executed is None
    assert smtp.sent == [], "наружу ничего не ушло"
    assert "не получилось" in handled.message.lower()


def test_the_sender_itself_refuses_when_the_switch_is_off(monkeypatch) -> None:
    monkeypatch.delenv(ENV_KILL_SWITCH, raising=False)
    sender = EmailSender(FakeSmtp(), sender=FAMILY_ADDRESS)

    with pytest.raises(EgressBlocked):
        sender.send(LETTER, approval_id="a1")


# --- подтверждение получателя -------------------------------------------------


def test_the_card_shows_the_recipient_on_its_own_line(world) -> None:
    pipeline, transport, smtp = world
    services = Services.on(pipeline.approvals.db)

    REGISTRY.invoke(
        context(), "propose_email",
        {"to": SCHOOL, "subject": "Klassenfahrt 3b", "body": "Guten Tag."},
        services=services,
    )

    from hermes_cloud.runner.bundle import item_line, items_of

    staged = services.approvals.pending_for(chat=CHAT)[0]
    line = item_line(items_of(staged.payload)[0])
    assert f"Кому: {SCHOOL}" in line


def test_swapping_the_recipient_after_the_card_invalidates_the_confirmation(world) -> None:
    """payload_sha связывает подтверждение с тем, что человек видел."""
    from hermes_cloud.core.approvals import PayloadTampered

    pipeline, transport, smtp = world
    staged = stage_email(pipeline)
    approval = pipeline.approvals.get(staged.id)
    tampered = dict(approval.payload)
    tampered["items"] = [
        {**tampered["items"][0], "to": "attacker@example.net"}
    ]
    import json

    with pipeline.approvals.db.write() as connection:
        # Подменяем payload, оставляя прежний payload_sha — ровно то, что
        # делает злоумышленник с доступом к базе.
        connection.execute(
            "UPDATE approvals SET payload = ? WHERE id = ?",
            (json.dumps(tampered, ensure_ascii=False), staged.id),
        )

    with pytest.raises(PayloadTampered):
        pipeline.approvals.claim_by_id(
            approval_id=staged.id, chat=CHAT, thread=None, actor=PARENT
        )
    assert smtp.sent == []


def test_a_consistent_payload_still_carries_the_confirmed_recipient(world) -> None:
    """Согласованная подмена меняет и хэш — и это видно по нему, а не по письму."""
    pipeline, transport, smtp = world
    staged = stage_email(pipeline)
    before = pipeline.approvals.get(staged.id)

    assert payload_sha(before.payload) != payload_sha(
        {**before.payload, "items": [{**before.payload["items"][0], "to": "x@example.net"}]}
    )


def test_a_guest_cannot_propose_a_letter(world) -> None:
    from hermes_cloud.core.runcontext import CapabilityDenied

    pipeline, transport, smtp = world
    services = Services.on(pipeline.approvals.db)
    guest = build_run_context(
        household=Household(
            owner=PARENT, family=frozenset({PARENT}), guests=frozenset({"990000003"}),
            allowed_chats=frozenset({CHAT}),
        ),
        actor_id="990000003", chat_id=CHAT,
    )

    with pytest.raises(CapabilityDenied):
        REGISTRY.invoke(
            guest, "propose_email",
            {"to": SCHOOL, "subject": "Тема", "body": "Текст"}, services=services,
        )


def test_a_bad_address_is_refused_before_the_family_sees_a_card(world) -> None:
    pipeline, transport, smtp = world
    services = Services.on(pipeline.approvals.db)

    with pytest.raises(ToolInputError):
        REGISTRY.invoke(
            context(), "propose_email",
            {"to": "не адрес", "subject": "Тема", "body": "Текст"}, services=services,
        )
    assert services.approvals.pending_for(chat=CHAT) == []


# --- исполнение ---------------------------------------------------------------


def test_a_confirmed_letter_is_sent_once(world) -> None:
    pipeline, transport, smtp = world
    staged = stage_email(pipeline)

    handled = pipeline.handle_callback(
        action=ACTION_CONFIRM, approval_id=staged.id, context=context()
    )

    assert handled.executed == KIND_BUNDLE
    assert len(smtp.sent) == 1
    assert smtp.sent[0]["To"] == SCHOOL
    effect = next(
        effect for effect in pipeline.effects.for_run(staged.id)
        if effect.kind == KIND_EMAIL
    )
    assert smtp.sent[0]["Message-ID"] == message_id_for(effect.id)

    # Повторное нажатие не отправляет второе письмо.
    pipeline.handle_callback(
        action=ACTION_CONFIRM, approval_id=staged.id, context=context()
    )
    assert len(smtp.sent) == 1


def test_a_broken_connection_is_never_retried(world) -> None:
    """SMTP мог принять письмо и умереть до ответа — повтор означал бы второе."""
    pipeline, transport, smtp = world
    smtp.fail_with = EmailOutcomeUnknown("связь оборвалась")
    staged = stage_email(pipeline)

    handled = pipeline.handle_callback(
        action=ACTION_CONFIRM, approval_id=staged.id, context=context()
    )

    assert handled.executed is None
    assert "не знаю, дошло ли" in transport.messages[-1].text
    item_effect = next(
        effect for effect in pipeline.effects.for_run(staged.id)
        if effect.kind == KIND_EMAIL
    )
    assert item_effect.status == "outcome_unknown"

    # Разбор после падения не переотправляет: письмо не доигрывают.
    import time

    from hermes_cloud.core.effects import DEFAULT_LEASE_SECONDS

    smtp.fail_with = None
    pipeline.reconcile(now=time.time() + 10 * DEFAULT_LEASE_SECONDS)
    assert smtp.sent == []


def test_email_is_not_replayable_after_a_crash(world) -> None:
    """Письмо — единственное действие без ключа, по которому видно, ушло ли оно."""
    from hermes_cloud.runner.pipeline import REPLAYABLE_KINDS

    assert KIND_EMAIL not in REPLAYABLE_KINDS

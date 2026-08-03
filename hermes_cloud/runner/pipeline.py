"""Сборка конвейера Фазы 1: событие → карточка → подтверждение → исполнение.

Здесь сходятся все части, и здесь же проходит главная граница продукта:
**всё, что до подтверждения, — предложение; всё, что после, — исполнение.**
Между ними стоит `ApprovalStore.claim`, и обойти его нельзя ни кнопкой, ни
кодом, ни неизвестным актором.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime

from hermes_cloud.channels.telegram import Origin, Transport
from hermes_cloud.core.approvals import (
    STATUS_DONE,
    STATUS_FAILED,
    ApprovalStore,
    RateLimited,
)
from hermes_cloud.core.events import Event
from hermes_cloud.execute.ics import build_event, filename_for, render_ics
from hermes_cloud.execute.reminder import ReminderStore, due_timestamp
from hermes_cloud.ingest.eml import parse_eml
from hermes_cloud.runner.card import (
    ACTION_CONFIRM,
    ACTION_EDIT,
    ACTION_REJECT,
    KIND_ICS,
    KIND_REMINDER,
    render_card,
)
from hermes_cloud.runner.extraction import Extractor

logger = logging.getLogger(__name__)

TEXT_REJECTED = "Отменил. Ничего не создаю."
TEXT_EDIT = (
    "Хорошо, что нужно исправить? Прежний код больше не действует — "
    "после правки пришлю новое предложение."
)
TEXT_UNKNOWN_ACTOR = (
    "Я вас не знаю и поэтому ничего не подтверждаю. "
    "Попросите взрослого из семьи добавить вас."
)
TEXT_GONE = "Это предложение уже неактуально — код истёк или его отменили."
TEXT_RATE_LIMITED = "Слишком много неверных кодов. Попробуйте позже."


@dataclass(frozen=True)
class Handled:
    """Что сделал конвейер — для тестов и логов (без содержимого письма)."""

    approval_id: str | None = None
    executed: str | None = None
    message: str | None = None


class Pipeline:
    def __init__(
        self,
        *,
        approvals: ApprovalStore,
        reminders: ReminderStore,
        transport: Transport,
        extractor: Extractor,
        chat: str,
        thread: int | None = None,
        actor: str = "system",
    ) -> None:
        self.approvals = approvals
        self.reminders = reminders
        self.transport = transport
        self.extractor = extractor
        self.chat = chat
        self.thread = thread
        self.actor = actor

    # --- входящее событие ---------------------------------------------------

    def handle_event(self, event: Event) -> Handled:
        """Обработчик воркера: письмо → карточка. Наружу отсюда ничего не уходит."""
        parsed = parse_eml(event.raw)
        extraction = self.extractor.extract_email(parsed)
        result = extraction.result

        preview = render_card(result)
        if preview.proposal is None:
            # Информационное письмо: сообщение без кнопок, подтверждать нечего.
            self.transport.send_message(
                chat=self.chat, text=preview.text, thread=self.thread
            )
            return Handled(message=preview.text)

        staged = self.approvals.stage(
            kind=preview.proposal["kind"],
            payload=preview.proposal,
            chat=self.chat,
            thread=self.thread,
            actor=self.actor,
            context_key=event.context_key,
            event_id=event.id,
        )
        card = render_card(result, approval_id=staged.id, code=staged.code)
        self.transport.send_message(
            chat=self.chat,
            text=card.text,
            thread=self.thread,
            buttons=tuple((button.label, button.callback_data) for button in card.buttons),
        )
        logger.info("staged approval %s for event %s", staged.id, event.id)
        return Handled(approval_id=staged.id, message=card.text)

    # --- подтверждение ------------------------------------------------------

    def handle_callback(
        self, *, action: str, approval_id: str, origin: Origin, now: float | None = None
    ) -> Handled:
        """Нажатие кнопки. Неизвестный актор не подтверждает ничего."""
        if not origin.is_known:
            self.transport.send_message(
                chat=origin.chat, text=TEXT_UNKNOWN_ACTOR, thread=origin.thread
            )
            return Handled(message=TEXT_UNKNOWN_ACTOR)

        if action in {ACTION_REJECT, ACTION_EDIT}:
            self.approvals.cancel(approval_id, now=now)
            text = TEXT_REJECTED if action == ACTION_REJECT else TEXT_EDIT
            self.transport.send_message(chat=origin.chat, text=text, thread=origin.thread)
            return Handled(approval_id=approval_id, message=text)

        if action != ACTION_CONFIRM:
            return Handled(message=None)

        try:
            approval = self.approvals.claim_by_id(
                approval_id=approval_id, chat=origin.chat,
                thread=origin.thread, actor=origin.actor, now=now,
            )
        except RateLimited:
            self.transport.send_message(
                chat=origin.chat, text=TEXT_RATE_LIMITED, thread=origin.thread
            )
            return Handled(approval_id=approval_id, message=TEXT_RATE_LIMITED)
        if approval is None:
            self.transport.send_message(
                chat=origin.chat, text=TEXT_GONE, thread=origin.thread
            )
            return Handled(approval_id=approval_id, message=TEXT_GONE)

        return self.execute(approval, now=now)

    # --- исполнение ---------------------------------------------------------

    def execute(self, approval, *, now: float | None = None) -> Handled:
        """Исполнить подтверждённое предложение. Вызывается только после claim."""
        payload = approval.payload
        try:
            if payload["kind"] == KIND_REMINDER:
                text = self._execute_reminder(approval, payload, now=now)
            elif payload["kind"] == KIND_ICS:
                text = self._execute_ics(approval, payload)
            else:
                raise ValueError(f"неизвестный вид предложения: {payload['kind']!r}")
        except Exception as error:
            self.approvals.mark(
                approval.id, STATUS_FAILED, error=f"{type(error).__name__}: {error}", now=now
            )
            message = "Не получилось выполнить. Попробуйте ещё раз или напишите мне."
            self.transport.send_message(
                chat=approval.chat, text=message, thread=approval.thread
            )
            logger.warning("approval %s failed: %s", approval.id, type(error).__name__)
            return Handled(approval_id=approval.id, message=message)

        self.approvals.mark(approval.id, STATUS_DONE, receipt=text, now=now)
        self.transport.send_message(chat=approval.chat, text=text, thread=approval.thread)
        return Handled(approval_id=approval.id, executed=payload["kind"], message=text)

    def _execute_reminder(self, approval, payload: dict, *, now: float | None) -> str:
        due = date.fromisoformat(payload["due_date"])
        reminder = self.reminders.create(
            chat=approval.chat,
            thread=approval.thread,
            text=payload["text"],
            due_at=due_timestamp(due),
            approval_id=approval.id,
            now=now,
        )
        logger.info("reminder %s created from approval %s", reminder.id, approval.id)
        return f"Готово: напомню {due.strftime('%d.%m.%Y')} — {payload['text']}."

    def _execute_ics(self, approval, payload: dict) -> str:
        start = datetime.fromisoformat(payload["start"])
        end = datetime.fromisoformat(payload["end"]) if payload.get("end") else None
        event = build_event(
            approval_id=approval.id,
            title=payload["title"],
            start=start,
            end=end,
            description=payload.get("description"),
            location=payload.get("location"),
        )
        self.transport.send_document(
            chat=approval.chat,
            thread=approval.thread,
            filename=filename_for(payload["title"]),
            content=render_ics(event).encode("utf-8"),
            caption="Откройте файл, чтобы добавить событие в календарь.",
        )
        return f"Готово: файл события «{payload['title']}» отправлен."

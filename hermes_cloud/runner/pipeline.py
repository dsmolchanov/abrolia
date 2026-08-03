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

from hermes_cloud.channels.telegram import SendOutcomeUnknown, Transport
from hermes_cloud.core.approvals import (
    STATUS_DONE,
    STATUS_FAILED,
    ApprovalStore,
    RateLimited,
)
from hermes_cloud.core.effects import APPROVAL_TOOL_USE_ID, EffectJournal
from hermes_cloud.core.events import Event
from hermes_cloud.core.runcontext import (
    WRITE_CALENDAR,
    WRITE_REMINDER,
    RunContext,
)
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
from hermes_cloud.runner.model import ToolLoop

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
TEXT_NO_RIGHT = (
    "Это подтверждает кто-то из взрослых в семье — у вас нет такого права."
)
TEXT_OUTCOME_UNKNOWN = (
    "Связь оборвалась на полпути, и я не знаю, дошло ли отправленное. "
    "Проверьте, пожалуйста, — повторять сам не буду, чтобы не сделать дважды."
)

# Право, необходимое для подтверждения предложения этого вида. Подтверждение —
# это разрешение на эффект, поэтому спрашивается mutate-cap, а не «знакомость».
CAPABILITY_FOR_KIND = {
    KIND_REMINDER: WRITE_REMINDER,
    KIND_ICS: WRITE_CALENDAR,
}

# Виды эффектов, которые можно доделать после падения: локальные и
# идемпотентные по id подтверждения. Всё остальное — наружу, и повтору не
# подлежит (`Locked Decisions` → «Идемпотентность», `docs/SECURITY.md`, T9).
REPLAYABLE_KINDS = frozenset({KIND_REMINDER})


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
        effects: EffectJournal | None = None,
        loop: ToolLoop | None = None,
    ) -> None:
        self.approvals = approvals
        self.reminders = reminders
        self.transport = transport
        self.extractor = extractor
        self.chat = chat
        self.thread = thread
        self.actor = actor
        # Журнал живёт в той же базе: запись попытки обязана коммититься вместе
        # с claim'ом, а через две базы это невозможно.
        self.effects = effects or EffectJournal(approvals.db)
        # Диалог необязателен: воркер и tick обходятся без него.
        self.loop = loop

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
        self, *, action: str, approval_id: str, context: RunContext, now: float | None = None
    ) -> Handled:
        """Нажатие кнопки. Неизвестный актор не подтверждает ничего."""
        if not context.is_known:
            self.transport.send_message(
                chat=context.chat_id, text=TEXT_UNKNOWN_ACTOR, thread=context.thread_id
            )
            return Handled(message=TEXT_UNKNOWN_ACTOR)

        if action in {ACTION_REJECT, ACTION_EDIT}:
            # Отмена — не эффект: отменить чужое предложение вправе любой, кого
            # семья знает. Ошибка в эту сторону безопасна.
            self.approvals.cancel(approval_id, now=now)
            text = TEXT_REJECTED if action == ACTION_REJECT else TEXT_EDIT
            self.transport.send_message(
                chat=context.chat_id, text=text, thread=context.thread_id
            )
            return Handled(approval_id=approval_id, message=text)

        if action != ACTION_CONFIRM:
            return Handled(message=None)

        staged = self.approvals.get(approval_id)
        if staged is not None and not context.can(
            CAPABILITY_FOR_KIND.get(staged.kind, "")
        ):
            # Права проверяются до claim: иначе одноразовый код сгорел бы на
            # том, кому и так нельзя.
            self.transport.send_message(
                chat=context.chat_id, text=TEXT_NO_RIGHT, thread=context.thread_id
            )
            return Handled(approval_id=approval_id, message=TEXT_NO_RIGHT)

        try:
            approval = self.approvals.claim_by_id(
                approval_id=approval_id, chat=context.chat_id,
                thread=context.thread_id, actor=context.actor_id, now=now,
            )
        except RateLimited:
            self.transport.send_message(
                chat=context.chat_id, text=TEXT_RATE_LIMITED, thread=context.thread_id
            )
            return Handled(approval_id=approval_id, message=TEXT_RATE_LIMITED)
        if approval is None:
            self.transport.send_message(
                chat=context.chat_id, text=TEXT_GONE, thread=context.thread_id
            )
            return Handled(approval_id=approval_id, message=TEXT_GONE)

        return self.execute(approval, now=now)

    def handle_update(self, update: dict, household) -> Handled | None:
        """Апдейт из канала → действие. None — апдейт нам не интересен.

        Нажатие кнопки идёт через `claim`, обычное сообщение — в диалог, и
        только если известен актор: неизвестному отвечают одной строкой и без
        инструментов.
        """
        from hermes_cloud.channels.telegram import IncomingCallback, parse_update

        parsed = parse_update(update, household)
        if parsed is None:
            return None
        if isinstance(parsed, IncomingCallback):
            handled = self.handle_callback(
                action=parsed.action, approval_id=parsed.approval_id, context=parsed.context
            )
            if parsed.callback_id:
                self.transport.answer_callback(parsed.callback_id)
            return handled
        if not parsed.context.is_known:
            self.transport.send_message(
                chat=parsed.context.chat_id, text=TEXT_UNKNOWN_ACTOR,
                thread=parsed.context.thread_id,
            )
            return Handled(message=TEXT_UNKNOWN_ACTOR)
        if self.loop is None or not parsed.text.strip():
            return None
        # Ход диалога. Права уже собраны на входе: `run` получает их готовыми и
        # сам ничего не расширяет.
        answer = self.loop.run(parsed.context, parsed.text)
        self.transport.send_message(
            chat=parsed.context.chat_id, text=answer.text, thread=parsed.context.thread_id
        )
        logger.info(
            "ход %s: %s итераций, %s токенов, конец — %s",
            parsed.context.run_id, answer.iterations, answer.tokens, answer.stopped,
        )
        return Handled(message=answer.text)

    # --- исполнение ---------------------------------------------------------

    def execute(self, approval, *, now: float | None = None) -> Handled:
        """Исполнить подтверждённое предложение. Вызывается только после claim.

        Запись в журнале эффектов к этому моменту уже есть — её сделал `claim`
        в своей транзакции. Если её нет (прямой вызов мимо claim), заводим
        сейчас: эффект без записи невозможен по построению.
        """
        payload = approval.payload
        effect = self.effects.find(
            run_id=approval.id, tool_use_id=APPROVAL_TOOL_USE_ID
        )
        if effect is None:
            effect, _ = self.effects.begin(
                run_id=approval.id, tool_use_id=APPROVAL_TOOL_USE_ID,
                kind=payload["kind"], approval_id=approval.id, now=now,
            )
        elif effect.settled:
            # Исход уже известен: повторное исполнение того же подтверждения
            # не создаёт второй эффект (`effects_run_tool` гарантирует одну
            # строку, эта ветка — что мы не вызовем исполнителя дважды).
            return Handled(approval_id=approval.id, message=effect.result or TEXT_GONE)

        try:
            if payload["kind"] == KIND_REMINDER:
                text = self._execute_reminder(approval, payload, now=now)
            elif payload["kind"] == KIND_ICS:
                text = self._execute_ics(approval, payload)
            else:
                raise ValueError(f"неизвестный вид предложения: {payload['kind']!r}")
        except SendOutcomeUnknown as error:
            # Отправка могла дойти. Повторять нельзя, молчать — тем более.
            self.effects.outcome_unknown(effect.id, f"{type(error).__name__}: {error}", now=now)
            self.approvals.mark(
                approval.id, STATUS_FAILED, error=f"outcome_unknown: {error}", now=now
            )
            self.transport.send_message(
                chat=approval.chat, text=TEXT_OUTCOME_UNKNOWN, thread=approval.thread
            )
            logger.warning("approval %s outcome unknown", approval.id)
            return Handled(approval_id=approval.id, message=TEXT_OUTCOME_UNKNOWN)
        except Exception as error:
            self.effects.fail(effect.id, f"{type(error).__name__}: {error}", now=now)
            self.approvals.mark(
                approval.id, STATUS_FAILED, error=f"{type(error).__name__}: {error}", now=now
            )
            message = "Не получилось выполнить. Попробуйте ещё раз или напишите мне."
            self.transport.send_message(
                chat=approval.chat, text=message, thread=approval.thread
            )
            logger.warning("approval %s failed: %s", approval.id, type(error).__name__)
            return Handled(approval_id=approval.id, message=message)

        self.effects.complete(effect.id, text, now=now)
        self.approvals.mark(approval.id, STATUS_DONE, receipt=text, now=now)
        self.transport.send_message(chat=approval.chat, text=text, thread=approval.thread)
        return Handled(approval_id=approval.id, executed=payload["kind"], message=text)

    def _execute_reminder(self, approval, payload: dict, *, now: float | None) -> str:
        due = date.fromisoformat(payload["due_date"])
        existing = self.reminders.for_approval(approval.id)
        if existing is not None:
            # Доделка после падения: напоминание уже создано, второго не будет.
            return f"Готово: напомню {due.strftime('%d.%m.%Y')} — {existing.text}."
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

    # --- разбор после падения -----------------------------------------------

    def reconcile(self, *, now: float | None = None) -> list[Handled]:
        """Разобрать эффекты, повисшие после падения. Вызывается при старте.

        Стратегия — по виду эффекта, и это не деталь реализации, а суть:
        локальный и идемпотентный по ключу подтверждения (напоминание) можно
        доделать; наружный (файл в чат, письмо, календарь чужого сервиса) —
        нельзя, потому что «аренда истекла» не означает «не дошло». Такой
        эффект закрывается как `outcome_unknown`, и человеку говорят правду.
        """
        handled: list[Handled] = []
        for effect in self.effects.stale(now=now):
            approval = (
                self.approvals.get(effect.approval_id) if effect.approval_id else None
            )
            if approval is None:
                self.effects.outcome_unknown(
                    effect.id, "подтверждение не найдено", now=now
                )
                continue
            if effect.kind in REPLAYABLE_KINDS:
                logger.info("reconcile: доделываю %s (%s)", effect.id, effect.kind)
                handled.append(self.execute(approval, now=now))
                continue
            logger.warning("reconcile: исход %s (%s) неизвестен", effect.id, effect.kind)
            self.effects.outcome_unknown(effect.id, "процесс упал во время отправки", now=now)
            self.approvals.mark(
                approval.id, STATUS_FAILED, error="outcome_unknown: рестарт", now=now
            )
            self.transport.send_message(
                chat=approval.chat, text=TEXT_OUTCOME_UNKNOWN, thread=approval.thread
            )
            handled.append(Handled(approval_id=approval.id, message=TEXT_OUTCOME_UNKNOWN))
        return handled

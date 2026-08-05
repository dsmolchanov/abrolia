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
    STATUS_STAGED,
    ApprovalStore,
    RateLimited,
)
from hermes_cloud.core.commitments import STATUS_CANDIDATE as COMMITMENT_CANDIDATE
from hermes_cloud.core.commitments import STATUS_CONFIRMED as MEMORY_CONFIRMED
from hermes_cloud.core.commitments import CommitmentStore
from hermes_cloud.core.dsar import EXTERNAL_SURFACES, export_bytes, wipe_household
from hermes_cloud.core.effects import APPROVAL_TOOL_USE_ID, EffectJournal
from hermes_cloud.core.events import Event
from hermes_cloud.core.evidence import EvidenceStore, content_sha
from hermes_cloud.core.matching import find_match
from hermes_cloud.core.memory import MemoryStore
from hermes_cloud.core.runcontext import (
    DATA_DELETE,
    DATA_EXPORT,
    WRITE_CALENDAR,
    WRITE_EMAIL,
    WRITE_MEMORY,
    WRITE_REMINDER,
    RunContext,
)
from hermes_cloud.execute.email_send import (
    EmailOutcomeUnknown,
    EmailSender,
    Outgoing,
)
from hermes_cloud.execute.gcal import (
    Calendar,
    CalendarOutcomeUnknown,
    build_calendar_event,
)
from hermes_cloud.execute.ics import build_event, filename_for, render_ics
from hermes_cloud.execute.reminder import ReminderStore, due_timestamp
from hermes_cloud.ingest.eml import parse_eml
from hermes_cloud.runner.bundle import (
    enabled_items,
    item_line,
    items_for,
    items_of,
    reconcile_items,
    render_bundle,
    toggled,
    update_lines,
)
from hermes_cloud.runner.card import (
    ACTION_CONFIRM,
    ACTION_EDIT,
    ACTION_REJECT,
    ACTION_TOGGLE,
    KIND_BUNDLE,
    KIND_CALENDAR,
    KIND_DELETE,
    KIND_EMAIL,
    KIND_EXPORT,
    KIND_ICS,
    KIND_MEMORY,
    KIND_REMINDER,
    header_lines,
    render_info,
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
    KIND_CALENDAR: WRITE_CALENDAR,
    KIND_ICS: WRITE_CALENDAR,
    KIND_MEMORY: WRITE_MEMORY,
    KIND_EMAIL: WRITE_EMAIL,
    KIND_EXPORT: DATA_EXPORT,
    KIND_DELETE: DATA_DELETE,
}

# Виды эффектов, которые можно доделать после падения: локальные и
# идемпотентные по id подтверждения. Всё остальное — наружу, и повтору не
# подлежит (`Locked Decisions` → «Идемпотентность», `docs/SECURITY.md`, T9).
# Календарь здесь не по недосмотру: у события есть заданный нами id, поэтому
# повтор находит его и обновляет, а не создаёт второе (`execute/gcal.py`).
REPLAYABLE_KINDS = frozenset({KIND_REMINDER, KIND_MEMORY, KIND_CALENDAR})


def fact_payload(result) -> dict:
    """Факт письма — то, во что семья поверит, а не то, что мы сделаем.

    В `commitments` едет именно он, а не карточка: обязательство отвечает на
    «что мы знаем», действия — на «что с этим делать». Смешав их, мы получили бы
    факт, у которого нельзя спросить срок, потому что там лежит список кнопок.
    """
    return {
        "kind": result.kind,
        "title": result.title,
        "summary": result.summary,
        "start": result.event_start.isoformat() if result.event_start else None,
        "end": result.event_end.isoformat() if result.event_end else None,
        "due_date": result.due_date.isoformat() if result.due_date else None,
        "amount_cents": result.amount.amount_cents if result.amount else None,
        "currency": result.amount.currency if result.amount else None,
        "location": result.location,
        "responsible": result.responsible,
    }


def evidence_needles(result) -> list[str]:
    """Опоры для поиска цитаты: то, что человек и так будет проверять глазами.

    Ищем именно так, как это написано в письме (сумма «15,00», дата
    «08.09.2026»), а не так, как мы её нормализовали, — иначе цитата не найдётся
    там, где она есть.
    """
    needles: list[str] = []
    if result.amount is not None:
        major, minor = divmod(result.amount.amount_cents, 100)
        needles += [f"{major},{minor:02d}", f"{major}.{minor:02d}", str(major)]
    for value in (result.due_date, result.event_start):
        if value is None:
            continue
        moment = value.date() if hasattr(value, "date") else value
        needles += [
            moment.strftime("%d.%m.%Y"), moment.strftime("%d.%m."), moment.isoformat()
        ]
    if result.location:
        needles.append(result.location)
    return needles


class BundlePartiallyFailed(RuntimeError):
    """Часть пунктов связки не прошла. Текст исключения — то, что видит семья."""


def may_confirm(context: RunContext, payload: dict) -> bool:
    """Хватает ли прав подтвердить это предложение.

    У связки прав ровно столько, сколько нужно её включённым пунктам: право
    подтвердить целое не может быть слабее права подтвердить любую его часть.
    Неизвестный вид — отказ: список видов закрытый, и «не знаю, что это» не
    повод разрешать.
    """
    if payload.get("kind") == KIND_BUNDLE:
        items = enabled_items(payload)
        return bool(items) and all(
            context.can(CAPABILITY_FOR_KIND.get(item.kind, "")) for item in items
        )
    return context.can(CAPABILITY_FOR_KIND.get(payload.get("kind", ""), ""))


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
        calendar: Calendar | None = None,
        mail: EmailSender | None = None,
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
        # Календарь household'а. Без него событие уезжает файлом .ics — тот же
        # результат для семьи, просто мы не можем писать сами.
        self.calendar = calendar
        # Исходящая почта. Её отсутствие — не деградация, а выключенный тракт:
        # предложить письмо можно, отправить — нет.
        self.mail = mail
        # Онтологический слой: провенанс, обязательства, память. Живёт в той же
        # базе — иначе «откуда эта сумма» пришлось бы собирать из двух мест.
        self.evidence = EvidenceStore(approvals.db)
        self.commitments = CommitmentStore(approvals.db)
        self.memory = MemoryStore(approvals.db)

    # --- входящее событие ---------------------------------------------------

    def handle_event(self, event: Event) -> Handled:
        """Обработчик воркера: письмо → карточка. Наружу отсюда ничего не уходит."""
        parsed = parse_eml(event.raw)
        extraction = self.extractor.extract_email(parsed)
        result = extraction.result

        run = self.evidence.record_run(
            event_id=event.id,
            model=extraction.model,
            prompt_sha=content_sha(self.extractor.system_prompt()),
            input_tokens=extraction.input_tokens,
            output_tokens=extraction.output_tokens,
        )
        ref = self.evidence.add_ref(
            extraction_run_id=run.id,
            event_id=event.id,
            text=parsed.text,
            sender=parsed.original_sender.email if parsed.original_sender else parsed.from_email,
            message_date=parsed.date,
            needles=evidence_needles(result),
        )
        source = self.evidence.render_source(ref)

        items = items_for(result, calendar=self.calendar is not None)
        if not items:
            # Информационное письмо: сообщение без кнопок, подтверждать нечего.
            info = render_info(result, source=source)
            self.transport.send_message(
                chat=self.chat, text=info.text, thread=self.thread
            )
            return Handled(message=info.text)
        header = "\n".join(header_lines(result, source=source))

        # Не про то же ли самое, что мы уже знаем? «Экскурсия перенесена» —
        # это новая версия факта, а не второй факт.
        match = find_match(
            self.commitments,
            self.evidence,
            kind=result.kind,
            title=result.title,
            sender_domain=ref.sender_domain,
        )
        previous = match.commitment if match and match.sure else None
        fact = fact_payload(result)
        if previous is not None:
            items = reconcile_items(items, previous)
            header = "\n".join([header, "", *update_lines(previous.payload, fact)])
        elif match and match.maybe:
            # Похоже, но не наверняка: показываем оба и говорим прямо.
            header = "\n".join([
                header, "",
                f"⚠️ Возможно, это про то же, что и «{match.commitment.payload.get('title')}»"
                " — если да, отклоните одно из двух.",
            ])
        # Карточка собирается после сопоставления: до него ещё не известно,
        # предлагаем мы сделать или поправить.
        preview = render_bundle(result, items, header=header)

        # Кандидат — единственный статус, который может появиться из модели.
        commitment = self.commitments.propose(
            kind=result.kind,
            payload=fact,
            extraction_run_id=run.id,
            confidence=result.confidence,
            supersedes=previous.id if previous is not None else None,
            observed_at=(
                result.event_start.timestamp() if result.event_start is not None else None
            ),
        )
        staged = self.approvals.stage(
            kind=preview.proposal["kind"],
            payload={**preview.proposal, "commitment_id": commitment.id},
            chat=self.chat,
            thread=self.thread,
            actor=self.actor,
            context_key=event.context_key,
            event_id=event.id,
        )
        card = render_bundle(
            result, items, approval_id=staged.id, code=staged.code, header=header
        )
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
        self,
        *,
        action: str,
        approval_id: str,
        context: RunContext,
        argument: str | None = None,
        now: float | None = None,
    ) -> Handled:
        """Нажатие кнопки. Неизвестный актор не подтверждает ничего."""
        if not context.is_known:
            self.transport.send_message(
                chat=context.chat_id, text=TEXT_UNKNOWN_ACTOR, thread=context.thread_id
            )
            return Handled(message=TEXT_UNKNOWN_ACTOR)

        if action == ACTION_TOGGLE:
            return self._toggle_item(
                approval_id=approval_id, index=argument, context=context, now=now
            )

        if action in {ACTION_REJECT, ACTION_EDIT}:
            # Отмена — не эффект: отменить чужое предложение вправе любой, кого
            # семья знает. Ошибка в эту сторону безопасна.
            self.approvals.cancel(approval_id, now=now)
            self._reject_commitment(approval_id, now=now)
            text = TEXT_REJECTED if action == ACTION_REJECT else TEXT_EDIT
            self.transport.send_message(
                chat=context.chat_id, text=text, thread=context.thread_id
            )
            return Handled(approval_id=approval_id, message=text)

        if action != ACTION_CONFIRM:
            return Handled(message=None)

        staged = self.approvals.get(approval_id)
        if staged is not None and not may_confirm(context, staged.payload):
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
                action=parsed.action, approval_id=parsed.approval_id,
                context=parsed.context, argument=parsed.argument,
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
            elif payload["kind"] == KIND_CALENDAR:
                text = self._execute_calendar(approval, payload, now=now)
            elif payload["kind"] == KIND_ICS:
                text = self._execute_ics(approval, payload)
            elif payload["kind"] == KIND_MEMORY:
                text = self._execute_memory(approval, payload, now=now)
            elif payload["kind"] == KIND_EXPORT:
                text = self._execute_export(approval)
            elif payload["kind"] == KIND_DELETE:
                text = self._execute_delete(approval, now=now)
            elif payload["kind"] == KIND_BUNDLE:
                text = self._execute_bundle(approval, payload, now=now)
            else:
                raise ValueError(f"неизвестный вид предложения: {payload['kind']!r}")
        except BundlePartiallyFailed as partial:
            # Удавшиеся пункты остаются сделанными: их эффекты уже закрыты в
            # журнале. Подтверждение помечается неудавшимся, потому что
            # выполнено не всё, о чём договорились.
            self.effects.fail(effect.id, "часть пунктов связки не прошла", now=now)
            self.approvals.mark(
                approval.id, STATUS_FAILED, error="bundle: частичный отказ", now=now
            )
            self.transport.send_message(
                chat=approval.chat, text=str(partial), thread=approval.thread
            )
            return Handled(approval_id=approval.id, message=str(partial))
        except (SendOutcomeUnknown, CalendarOutcomeUnknown, EmailOutcomeUnknown) as error:
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
        self._confirm_commitment(approval, payload, now=now)
        self.approvals.mark(approval.id, STATUS_DONE, receipt=text, now=now)
        self.transport.send_message(chat=approval.chat, text=text, thread=approval.thread)
        return Handled(approval_id=approval.id, executed=payload["kind"], message=text)

    def _fact_root(self, commitment_id: str | None) -> str | None:
        """Корень цепочки версий: id события в календаре не меняется с версией.

        Иначе «экскурсия перенесена» создала бы второе событие вместо переноса
        первого — ровно то, ради чего вся суперсессия и затевалась.
        """
        if not commitment_id:
            return None
        chain = self.commitments.chain(commitment_id)
        return chain[0].id if chain else commitment_id

    def _execute_reminder(self, approval, payload: dict, *, now: float | None) -> str:
        due = date.fromisoformat(payload["due_date"])
        replaced = payload.get("supersedes_reminder_id")
        if replaced:
            # Новая версия факта отменяет напоминание прежней: два напоминания
            # об одном и том же с разными датами — худшее из возможного.
            self.reminders.cancel(replaced, now=now)
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

    def _execute_memory(self, approval, payload: dict, *, now: float | None) -> str:
        """Подтверждённая запись в память. До этого момента её там не было."""
        statement = self.memory.get(payload["statement_id"])
        if statement is not None and statement.status == MEMORY_CONFIRMED:
            # Доделка после падения: запись уже подтверждена, второй раз нечего.
            return f"Запомнил: {statement.text}"
        statement = self.memory.confirm(payload["statement_id"], approval, now=now)
        logger.info("memory statement %s confirmed", statement.id)
        return f"Запомнил: {statement.text}"

    def _reject_commitment(self, approval_id: str, *, now: float | None) -> None:
        """«Нет» человека закрывает и гипотезу модели, а не только кнопку."""
        approval = self.approvals.get(approval_id)
        if approval is None:
            return
        commitment_id = approval.payload.get("commitment_id")
        statement_id = approval.payload.get("statement_id")
        if commitment_id:
            self.commitments.reject(commitment_id, now=now)
        if statement_id:
            self.memory.reject(statement_id, now=now)

    def _execute_export(self, approval) -> str:
        """Выгрузка уходит файлом, а не текстом в чат: её сохраняют, а не читают."""
        content = export_bytes(self.approvals.db)
        self.transport.send_document(
            chat=approval.chat,
            thread=approval.thread,
            filename="hermes-export.json",
            content=content,
            caption="Полная выгрузка данных household'а.",
        )
        logger.info("export delivered: %s байт", len(content))
        return f"Готово: выгрузка отправлена ({len(content)} байт)."

    def _execute_delete(self, approval, *, now: float | None) -> str:
        """Стирание. После него отмечать в журнале уже нечего — журнала нет."""
        removed = wipe_household(self.approvals.db, now=now)
        logger.warning("household wiped: %s строк", sum(removed.values()))
        surfaces = "\n".join(f"— {item}" for item in EXTERNAL_SURFACES)
        return (
            f"Данные household'а стёрты ({sum(removed.values())} записей).\n"
            "Вне нашего контроля осталось:\n" + surfaces
        )

    def _confirm_commitment(self, approval, payload: dict, *, now: float | None) -> None:
        """Подтверждение действия подтверждает и сам факт — но только его версию.

        Обязательство переходит в `confirmed` здесь, а не в момент извлечения:
        до нажатия ✅ это была гипотеза модели.
        """
        commitment_id = payload.get("commitment_id")
        if not commitment_id:
            return
        commitment = self.commitments.get(commitment_id)
        if commitment is None or commitment.status != COMMITMENT_CANDIDATE:
            return
        self.commitments.confirm(commitment_id, approval, now=now)
        reminder = self.reminders.for_approval(approval.id)
        if reminder is not None:
            self.commitments.link(commitment_id, reminder_id=reminder.id, now=now)
        event_id = payload.get("calendar_event_id")
        if event_id:
            self.commitments.link(commitment_id, calendar_event_id=event_id, now=now)

    def _execute_bundle(self, approval, payload: dict, *, now: float | None) -> str:
        """Исполнить связку по-пунктно: у каждого пункта свой эффект.

        Упавший пункт не отменяет удавшиеся и не откатывает их: две трети
        сделанного лучше, чем ничего, — при условии, что человеку честно
        сказано, какая треть не сделана.
        """
        lines: list[str] = []
        failures = 0
        for index, item in enumerate(items_of(payload)):
            if not item.enabled:
                continue
            effect, fresh = self.effects.begin(
                run_id=approval.id,
                tool_use_id=f"item-{index}",
                kind=item.kind,
                approval_id=approval.id,
                now=now,
            )
            if not fresh and effect.settled:
                # Доделка после падения: этот пункт уже исполнен.
                lines.append(f"✓ {effect.result or item.kind}")
                continue
            try:
                # Пункт наследует ссылку на обязательство: от неё зависит id
                # события в календаре, а он обязан пережить версии факта.
                text = self._execute_item(
                    approval,
                    {**item.payload, "commitment_id": payload.get("commitment_id")},
                    now=now,
                )
            except (SendOutcomeUnknown, CalendarOutcomeUnknown, EmailOutcomeUnknown) as error:
                # «Не получилось» здесь было бы враньём: письмо могло уйти.
                failures += 1
                self.effects.outcome_unknown(
                    effect.id, f"{type(error).__name__}: {error}", now=now
                )
                logger.warning("исход пункта %s связки %s неизвестен", index, approval.id)
                lines.append(f"⚠️ {item_line(item)} — не знаю, дошло ли; повторять не буду")
                continue
            except Exception as error:
                failures += 1
                self.effects.fail(effect.id, f"{type(error).__name__}: {error}", now=now)
                logger.warning(
                    "пункт %s связки %s не сработал: %s",
                    index, approval.id, type(error).__name__,
                )
                lines.append(f"✗ {item_line(item)} — не получилось")
                continue
            self.effects.complete(effect.id, text, now=now)
            lines.append(f"✓ {text}")

        if failures:
            lines.append("")
            lines.append("Что не получилось — попробуйте ещё раз или напишите мне.")
            raise BundlePartiallyFailed("\n".join(lines))
        return "\n".join(lines)

    def _execute_item(self, approval, payload: dict, *, now: float | None) -> str:
        """Один пункт связки. Те же исполнители, что и у одиночного действия."""
        kind = payload["kind"]
        if kind == KIND_REMINDER:
            return self._execute_reminder(approval, payload, now=now)
        if kind == KIND_CALENDAR:
            return self._execute_calendar(approval, payload, now=now)
        if kind == KIND_ICS:
            return self._execute_ics(approval, payload)
        if kind == KIND_EMAIL:
            return self._execute_email(approval, payload)
        raise ValueError(f"неизвестный вид пункта: {kind!r}")

    def _toggle_item(
        self, *, approval_id: str, index: str | None, context: RunContext, now: float | None
    ) -> Handled:
        """Включить или выключить пункт связки.

        Пересобирает предложение целиком: старый код перестаёт действовать, а
        новая карточка приходит с новым. Подтверждать теперь предлагается
        другое — значит и код должен быть другим (та же логика, что у ✏️).
        """
        approval = self.approvals.get(approval_id)
        if approval is None or approval.status != STATUS_STAGED:
            self.transport.send_message(
                chat=context.chat_id, text=TEXT_GONE, thread=context.thread_id
            )
            return Handled(approval_id=approval_id, message=TEXT_GONE)
        try:
            position = int(index or "")
        except ValueError:
            return Handled(approval_id=approval_id)

        payload = toggled(approval.payload, position)
        if payload == approval.payload:
            # Последний включённый пункт выключить нельзя: для «ничего не делать»
            # есть ❌.
            return Handled(approval_id=approval_id)

        self.approvals.cancel(approval_id, now=now)
        staged = self.approvals.stage(
            kind=KIND_BUNDLE,
            payload=payload,
            chat=approval.chat,
            thread=approval.thread,
            actor=approval.actor,
            context_key=approval.context_key,
            event_id=approval.event_id,
            now=now,
        )
        card = render_bundle(
            None, items_of(payload), approval_id=staged.id, code=staged.code,
            header=payload.get("header", ""),
        )
        self.transport.send_message(
            chat=approval.chat,
            text=card.text,
            thread=approval.thread,
            buttons=tuple((button.label, button.callback_data) for button in card.buttons),
        )
        return Handled(approval_id=staged.id, message=card.text)

    def _execute_calendar(self, approval, payload: dict, *, now: float | None) -> str:
        """Событие в календарь семьи. Повтор находит его по id и обновляет."""
        if self.calendar is None:  # pragma: no cover — карточка бы не появилась
            raise ValueError("календарь не подключён")
        start = datetime.fromisoformat(payload["start"])
        event = build_calendar_event(
            approval_id=approval.id,
            commitment_id=self._fact_root(payload.get("commitment_id")),
            title=payload["title"],
            start=start,
            end=datetime.fromisoformat(payload["end"]) if payload.get("end") else None,
            description=payload.get("description"),
            location=payload.get("location"),
        )
        written = self.calendar.upsert(event)
        commitment_id = payload.get("commitment_id")
        if commitment_id:
            self.commitments.link(commitment_id, calendar_event_id=event.id, now=now)
        verb = "обновил" if written.updated else "добавил"
        link = f"\n{written.link}" if written.link else ""
        return f"Готово: {verb} в календаре «{payload['title']}».{link}"

    def _execute_email(self, approval, payload: dict) -> str:
        """Отправить подтверждённое письмо. Отозвать его будет уже нельзя."""
        if self.mail is None:
            raise ValueError("исходящая почта не настроена")
        letter = Outgoing.from_payload(payload)
        message_id = self.mail.send(letter, approval_id=approval.id)
        logger.info("письмо %s отправлено по подтверждению %s", message_id, approval.id)
        return f"Готово: письмо для {letter.to} отправлено."

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

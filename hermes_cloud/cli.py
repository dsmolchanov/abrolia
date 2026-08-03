"""CLI `hermes-cloud`: инъекция письма, состояние очереди, DLQ, replay.

Команды намеренно узкие: это инструмент оператора household'а, а не
администратора сервиса. Ничего наружу отсюда не уходит — только приём
события и просмотр очереди.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from hermes_cloud.channels.console import ConsoleTransport
from hermes_cloud.channels.telegram import TelegramTransport
from hermes_cloud.core.approvals import ApprovalStore
from hermes_cloud.core.config import load_config, load_dotenv
from hermes_cloud.core.db import open_database
from hermes_cloud.core.events import EventStore
from hermes_cloud.execute.reminder import ReminderStore
from hermes_cloud.ingest.inject import ingest_file
from hermes_cloud.ingest.worker import Worker
from hermes_cloud.runner.extraction import Extractor
from hermes_cloud.runner.pipeline import Pipeline

DEFAULT_DB_PATH = "data/hermes.db"
DB_ENV = "HERMES_DB"


def _database(args: argparse.Namespace):
    load_dotenv()
    path = args.db or os.environ.get(DB_ENV) or DEFAULT_DB_PATH
    return open_database(Path(path))


def _store(args: argparse.Namespace) -> EventStore:
    return EventStore(_database(args))


def _pipeline(args: argparse.Namespace, database) -> Pipeline:
    """Собрать конвейер из окружения. Без токена (или с --console) — в консоль."""
    config = load_config()
    use_telegram = config.has_telegram and not getattr(args, "console", False)
    transport = (
        TelegramTransport(config.telegram_token) if use_telegram else ConsoleTransport()
    )
    if not use_telegram:
        print("карточки печатаю в консоль; подтверждать командой `confirm <id>`")
    return Pipeline(
        approvals=ApprovalStore(database),
        reminders=ReminderStore(database),
        transport=transport,
        extractor=Extractor(
            model=config.model, effort=config.effort, family_language=config.language
        ),
        chat=config.require_chat(),
        thread=config.thread,
    )


def cmd_worker(args: argparse.Namespace) -> int:
    """Обработать очередь: событие → извлечение → карточка."""
    database = _database(args)
    events = EventStore(database)
    pipeline = _pipeline(args, database)
    processed = Worker(events, pipeline.handle_event, worker_id="cli").drain(limit=args.limit)
    for item in processed:
        state = "ок" if item.ok else f"ошибка: {item.error}"
        print(f"{item.event.id}  {state}")
    if not processed:
        print("очередь пуста")
    return 0


OFFSET_KEY = "telegram_update_offset"


def _read_offset(database) -> int | None:
    row = database.query_one("SELECT value FROM channel_state WHERE key = ?", (OFFSET_KEY,))
    return int(row["value"]) if row else None


def _write_offset(database, offset: int) -> None:
    import time as _time

    with database.write() as connection:
        connection.execute(
            "INSERT INTO channel_state (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at",
            (OFFSET_KEY, str(offset), _time.time()),
        )


def cmd_listen(args: argparse.Namespace) -> int:
    """Слушать канал и обрабатывать нажатия ✅/✏️/❌.

    Long-polling, а не webhook: webhook требует публичного адреса и приезжает
    в Фазе 4 вместе с nerve-webhook. Смещение хранится в базе — перезапуск не
    переигрывает уже обработанные нажатия.
    """
    from hermes_cloud.channels.telegram import HouseholdChannels

    database = _database(args)
    pipeline = _pipeline(args, database)
    config = load_config()
    channels = HouseholdChannels(
        allowed_chats=frozenset({config.require_chat()}),
        family_actors=config.family_actors,
    )
    offset = _read_offset(database)
    print("слушаю канал; Ctrl+C чтобы остановить")
    rounds = 0
    while args.rounds == 0 or rounds < args.rounds:
        rounds += 1
        updates = pipeline.transport.get_updates(offset=offset, timeout=args.timeout)
        for update in updates:
            offset = int(update.get("update_id", 0)) + 1
            handled = pipeline.handle_update(update, channels)
            if handled is not None:
                print(f"{handled.executed or 'обработано'}: {(handled.message or '')[:80]}")
        if offset is not None and updates:
            _write_offset(database, offset)
    return 0


def cmd_confirm(args: argparse.Namespace) -> int:
    """Подтвердить предложение из консоли — то же, что нажать ✅ в чате.

    Проходит через тот же `claim`: чат, тред и актор проверяются, код
    одноразовый. Отличается только транспорт, а не граница доверия.
    """
    from hermes_cloud.channels.telegram import HouseholdChannels
    from hermes_cloud.runner.card import ACTION_CONFIRM, ACTION_REJECT

    database = _database(args)
    pipeline = _pipeline(args, database)
    config = load_config()
    channels = HouseholdChannels(
        allowed_chats=frozenset({config.require_chat()}),
        family_actors=config.family_actors,
    )
    approval = pipeline.approvals.get(args.approval_id)
    if approval is None:
        print(f"предложение {args.approval_id} не найдено", file=sys.stderr)
        return 1
    actor = args.actor or (next(iter(config.family_actors), None) or approval.chat)
    handled = pipeline.handle_callback(
        action=ACTION_REJECT if args.reject else ACTION_CONFIRM,
        approval_id=approval.id,
        origin=channels.origin_for(approval.chat, approval.thread, actor),
    )
    print(handled.message or "готово")
    return 0


def cmd_pending(args: argparse.Namespace) -> int:
    """Показать предложения, ждущие подтверждения."""
    database = _database(args)
    pipeline = _pipeline(args, database)
    config = load_config()
    items = pipeline.approvals.pending_for(config.require_chat(), config.thread)
    if not items:
        print("нечего подтверждать")
        return 0
    for approval in items:
        label = approval.payload.get("text") or approval.payload.get("title") or "—"
        print(f"{approval.id}  {approval.kind}  {label}")
    return 0


def cmd_tick(args: argparse.Namespace) -> int:
    """Доставить созревшие напоминания. В Фазе 5 это делает scheduler-loop."""
    database = _database(args)
    pipeline = _pipeline(args, database)
    delivered = 0
    while True:
        reminder = pipeline.reminders.claim_due()
        if reminder is None:
            break
        pipeline.transport.send_message(
            chat=reminder.chat, thread=reminder.thread,
            text=f"Напоминание: {reminder.text}",
        )
        pipeline.reminders.mark_delivered(reminder.id)
        delivered += 1
        print(f"{reminder.id}  доставлено")
    if not delivered:
        print("созревших напоминаний нет")
    return 0


def cmd_inject_eml(args: argparse.Namespace) -> int:
    store = _store(args)
    result = ingest_file(store, args.path)
    parsed = result.parsed
    state = "принято" if result.created else "уже было (дубль)"
    print(f"{result.event_id}  {state}")
    print(f"  тема:        {parsed.subject or '—'}")
    print(f"  от:          {parsed.from_email or '—'}")
    if parsed.original_sender:
        original = parsed.original_sender
        print(
            f"  оригинал:    {original.email} "
            f"(способ: {original.method}, уверенность {original.confidence:.2f})"
        )
    print(f"  цепочка:     {parsed.thread_key or '—'}")
    print(f"  вложений:    {len(parsed.attachments)}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    counts = _store(args).counts()
    if not counts:
        print("очередь пуста")
        return 0
    for status, count in sorted(counts.items()):
        print(f"{status:<12} {count}")
    return 0


def cmd_dlq(args: argparse.Namespace) -> int:
    events = _store(args).dead_letters(limit=args.limit)
    if not events:
        print("DLQ пуста")
        return 0
    for event in events:
        print(f"{event.id}  попыток={event.attempts}  {event.external_id}")
        print(f"  причина: {event.last_error or '—'}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    store = _store(args)
    try:
        event = store.replay(args.event_id)
    except KeyError:
        print(f"событие {args.event_id} не найдено", file=sys.stderr)
        return 1
    print(f"{event.id} возвращено в очередь (статус {event.status})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-cloud", description=__doc__)
    parser.add_argument("--db", help=f"путь к базе (иначе ${DB_ENV} или {DEFAULT_DB_PATH})")
    parser.add_argument(
        "--console", action="store_true",
        help="печатать карточки в консоль даже при заданном токене Telegram",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    inject = commands.add_parser("inject-eml", help="принять письмо из файла .eml")
    inject.add_argument("path", type=Path)
    inject.set_defaults(func=cmd_inject_eml)

    status = commands.add_parser("status", help="счётчики очереди по статусам")
    status.set_defaults(func=cmd_status)

    dlq = commands.add_parser("dlq", help="события, ушедшие в DLQ")
    dlq.add_argument("--limit", type=int, default=20)
    dlq.set_defaults(func=cmd_dlq)

    replay = commands.add_parser("replay", help="вернуть событие в очередь")
    replay.add_argument("event_id")
    replay.set_defaults(func=cmd_replay)

    worker = commands.add_parser("worker", help="обработать очередь событий")
    worker.add_argument("--limit", type=int, default=10)
    worker.set_defaults(func=cmd_worker)

    tick = commands.add_parser("tick", help="доставить созревшие напоминания")
    tick.set_defaults(func=cmd_tick)

    pending = commands.add_parser("pending", help="предложения, ждущие подтверждения")
    pending.set_defaults(func=cmd_pending)

    confirm = commands.add_parser("confirm", help="подтвердить предложение из консоли")
    confirm.add_argument("approval_id")
    confirm.add_argument("--actor", help="от чьего имени (по умолчанию — первый family-актор)")
    confirm.add_argument("--reject", action="store_true", help="отклонить вместо подтверждения")
    confirm.set_defaults(func=cmd_confirm)

    listen = commands.add_parser("listen", help="обрабатывать нажатия кнопок в канале")
    listen.add_argument("--rounds", type=int, default=0, help="0 — бесконечно")
    listen.add_argument("--timeout", type=int, default=25)
    listen.set_defaults(func=cmd_listen)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

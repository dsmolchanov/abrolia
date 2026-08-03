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

from hermes_cloud.core.db import open_database
from hermes_cloud.core.events import EventStore
from hermes_cloud.ingest.inject import ingest_file

DEFAULT_DB_PATH = "data/hermes.db"
DB_ENV = "HERMES_DB"


def _store(args: argparse.Namespace) -> EventStore:
    path = args.db or os.environ.get(DB_ENV) or DEFAULT_DB_PATH
    return EventStore(open_database(Path(path)))


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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

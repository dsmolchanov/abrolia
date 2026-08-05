"""CLI `hermes-cloud`: инъекция письма, состояние очереди, DLQ, replay.

Команды намеренно узкие: это инструмент оператора household'а, а не
администратора сервиса. Ничего наружу отсюда не уходит — только приём
события и просмотр очереди.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

from hermes_cloud.channels.console import ConsoleTransport
from hermes_cloud.channels.telegram import TelegramTransport
from hermes_cloud.core.approvals import ApprovalStore
from hermes_cloud.core.config import load_config, load_dotenv
from hermes_cloud.core.db import open_database
from hermes_cloud.core.effects import EffectJournal
from hermes_cloud.core.events import EventStore
from hermes_cloud.email.contracts import EmailBinding
from hermes_cloud.email.receipts import EmailBindingStore, EmailSendStore
from hermes_cloud.execute.reminder import ReminderStore
from hermes_cloud.ingest.inject import ingest_file
from hermes_cloud.ingest.worker import Worker
from hermes_cloud.runner.extraction import Extractor
from hermes_cloud.runner.model import ToolLoop
from hermes_cloud.runner.pipeline import Pipeline
from hermes_cloud.runner.tools import Services

DEFAULT_DB_PATH = "data/hermes.db"
DB_ENV = "HERMES_DB"

PROVISIONED_GATED_COMMANDS = frozenset({
    "inject-eml", "worker", "reconcile", "gmail-poll", "replay", "tick",
    "confirm", "listen", "export", "delete",
})


def _database(args: argparse.Namespace):
    load_dotenv()
    path = args.db or os.environ.get(DB_ENV) or DEFAULT_DB_PATH
    return open_database(Path(path))


def _store(args: argparse.Namespace) -> EventStore:
    return EventStore(_database(args))


def _household():
    """Кто есть кто в household'е. Пусто — значит никто: контур fail-closed."""
    from hermes_cloud.core.runcontext import load_household

    load_dotenv()
    household = load_household()
    if not household.allowed_chats:
        print(
            "внимание: HERMES_CHAT не задан — ни один чат не доверен,"
            " подтверждения будут отклоняться",
            file=sys.stderr,
        )
    return household


def _pipeline(args: argparse.Namespace, database) -> Pipeline:
    """Собрать конвейер из окружения. Без токена (или с --console) — в консоль."""
    config = load_config()
    use_telegram = config.has_telegram and not getattr(args, "console", False)
    transport = (
        TelegramTransport(config.telegram_token) if use_telegram else ConsoleTransport()
    )
    if not use_telegram:
        print("карточки печатаю в консоль; подтверждать командой `confirm <id>`")
    approvals = ApprovalStore(database)
    reminders = ReminderStore(database)
    calendar = _calendar(config)
    email_binding = _email_binding(config, database)
    mail = _mail(config, database, email_binding)
    return Pipeline(
        approvals=approvals,
        reminders=reminders,
        transport=transport,
        extractor=Extractor(
            model=config.model,
            effort=config.effort,
            family_language=config.language,
            timezone=config.timezone,
        ),
        chat=config.require_chat(),
        thread=config.thread,
        calendar=calendar,
        mail=mail,
        loop=ToolLoop(
            journal=EffectJournal(database),
            services=Services.on(
                database, calendar=calendar, email_binding=email_binding
            ),
            family_language=config.language,
        ),
    )


def _email_binding(config, database) -> EmailBinding | None:
    if not config.has_email_identity:
        return None
    binding = EmailBinding(
        identity_id=config.email_identity_id,
        revision=config.email_binding_revision,
        provider=config.email_provider,
        address=config.email_address,
        provider_ref=config.email_identity_id,
        secret_names=config.email_secret_names,
    )
    return EmailBindingStore(database).activate(binding)


def _mail(config, database, binding: EmailBinding | None):
    """Исходящая почта. Нет учётных данных — нет и отправки, но предложить можно."""
    if binding is not None and binding.provider.startswith("nerve"):
        from hermes_cloud.email.nerve_client import (
            DEFAULT_REST_URL,
            DEFAULT_RUNTIME_URL,
            NerveEmailClient,
        )
        from hermes_cloud.execute.email_send import EmailSender
        from hermes_cloud.execute.nerve_send import NerveSendProvider

        try:
            refs = json.loads(binding.provider_ref or "")
            secret_name = binding.secret_names[0]
            secrets = json.loads(os.environ.get(secret_name, ""))
            inbox_id = str(refs["inbox_id"])
            api_key = str(secrets["api_key"])
        except (IndexError, KeyError, TypeError, json.JSONDecodeError):
            return None
        client = NerveEmailClient(
            api_key=api_key,
            runtime_url=os.environ.get("ABROLIA_NERVE_RUNTIME_URL", DEFAULT_RUNTIME_URL),
            rest_url=os.environ.get("ABROLIA_NERVE_REST_URL", DEFAULT_REST_URL),
        )
        return EmailSender(
            NerveSendProvider(client, inbox_id=inbox_id),
            sender=binding.address,
            identity_id=binding.identity_id,
            binding_revision=binding.revision,
            provider=binding.provider,
            binding_store=EmailBindingStore(database),
            send_store=EmailSendStore(database),
        )
    if not config.has_gmail:
        return None
    from hermes_cloud.execute.email_send import EmailSender, SmtpSsl

    return EmailSender(
        SmtpSsl(
            address=config.gmail_address,
            password=config.gmail_app_password,
            host=config.smtp_host,
            port=config.smtp_port,
        ),
        sender=config.gmail_address,
        identity_id=(binding.identity_id if binding else None),
        binding_revision=(binding.revision if binding else 1),
        provider=(binding.provider if binding else "legacy-smtp-test"),
        binding_store=EmailBindingStore(database) if binding else None,
        send_store=EmailSendStore(database),
    )


def _calendar(config):
    """Календарь household'а — только при живом токене ассистента."""
    if not config.has_calendar:
        return None
    from hermes_cloud.execute.gcal import Calendar, GoogleCalendar

    return Calendar(
        GoogleCalendar.from_token_file(str(config.google_token_path)),
        calendar_id=config.calendar_id,
    )


def _reconcile(pipeline: Pipeline) -> None:
    """Разобрать эффекты, повисшие после прошлого падения.

    Делается до приёма новой работы: сначала честно закрываем прошлое, потом
    беремся за настоящее.
    """
    for handled in pipeline.reconcile():
        print(f"после падения: {handled.approval_id} — {(handled.message or '')[:60]}")


def cmd_reconcile(args: argparse.Namespace) -> int:
    """Разобрать повисшие эффекты вручную (то же делают worker и listen)."""
    pipeline = _pipeline(args, _database(args))
    handled = pipeline.reconcile()
    if not handled:
        print("повисших эффектов нет")
        return 0
    for item in handled:
        print(f"{item.approval_id}  {(item.message or '')[:80]}")
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    """Обработать очередь: событие → извлечение → карточка."""
    database = _database(args)
    events = EventStore(database)
    pipeline = _pipeline(args, database)
    _reconcile(pipeline)
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
    database = _database(args)
    pipeline = _pipeline(args, database)
    household = _household()
    offset = _read_offset(database)
    print("слушаю канал; Ctrl+C чтобы остановить")
    rounds = 0
    while args.rounds == 0 or rounds < args.rounds:
        rounds += 1
        # Разбор каждый круг, а не только на старте: аренда упавшего процесса
        # истекает уже после запуска, и на старте видеть ещё нечего.
        _reconcile(pipeline)
        updates = pipeline.transport.get_updates(offset=offset, timeout=args.timeout)
        for update in updates:
            offset = int(update.get("update_id", 0)) + 1
            handled = pipeline.handle_update(update, household)
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
    from hermes_cloud.core.runcontext import build_run_context
    from hermes_cloud.runner.card import ACTION_CONFIRM, ACTION_REJECT

    database = _database(args)
    pipeline = _pipeline(args, database)
    household = _household()
    approval = pipeline.approvals.get(args.approval_id)
    if approval is None:
        print(f"предложение {args.approval_id} не найдено", file=sys.stderr)
        return 1
    actor = args.actor or household.owner or next(iter(household.family), approval.chat)
    handled = pipeline.handle_callback(
        action=ACTION_REJECT if args.reject else ACTION_CONFIRM,
        approval_id=approval.id,
        context=build_run_context(
            household=household, actor_id=actor,
            chat_id=approval.chat, thread_id=approval.thread,
        ),
    )
    print(handled.message or "готово")
    return 0


def cmd_gmail_poll(args: argparse.Namespace) -> int:
    """Забрать письма с ярлыком `Hermes` из ящика семьи (email-опция c)."""
    from hermes_cloud.ingest.gmail_poll import (
        GmailPoller,
        MailError,
        UidValidityChanged,
    )

    database = _database(args)
    config = load_config()
    if not config.has_gmail:
        print(
            "legacy IMAP выключен: для синтетического compatibility-теста нужны "
            "HERMES_LEGACY_IMAP_TEST_ONLY=1, HERMES_GMAIL_ADDRESS и "
            "HERMES_GMAIL_APP_PASSWORD; provisioned runtime его не поддерживает",
            file=sys.stderr,
        )
        return 1
    poller = GmailPoller(
        EventStore(database),
        address=config.gmail_address,
        app_password=config.gmail_app_password,
        label=config.gmail_label,
    )
    try:
        if args.baseline:
            result = poller.baseline()
            print(f"базис снят: {len(result.cursor.seen)} писем помечены виденными")
            return 0
        if args.rebaseline:
            result = poller.rebaseline()
            print(f"перебазирование: {len(result.cursor.seen)} писем помечены виденными")
            return 0
        result = poller.poll()
    except UidValidityChanged as error:
        print(f"{error}\nсделайте `gmail-poll --rebaseline` осознанно", file=sys.stderr)
        return 2
    except MailError as error:
        print(f"почта недоступна: {error}", file=sys.stderr)
        return 1
    print(f"принято: {result.accepted}, уже видели: {result.skipped}")
    return 0


def cmd_retention(args: argparse.Namespace) -> int:
    """Стереть то, чему вышел срок. Матрица — в `docs/privacy/data-map.md`."""
    from hermes_cloud.core.retention import RetentionJob

    report = RetentionJob(_database(args)).run()
    print(f"стёрто записей: {report.total_deleted}")
    print(f"  содержимое писем обезличено: {report.raw_blanked}")
    print(f"  события: {report.events_deleted}, напоминания: {report.reminders_deleted}")
    print(
        f"  подтверждения: {report.approvals_deleted}, эффекты: {report.effects_deleted},"
        f" прогоны: {report.extraction_runs_deleted}"
    )
    print(
        f"  неподтверждённые гипотезы: {report.candidates_deleted},"
        f" памяти: {report.memory_candidates_deleted}"
    )
    print(
        f"  email ingress receipts: {report.email_ingress_receipts_deleted},"
        f" sends: {report.email_sends_deleted}"
    )
    if report.memory_due_for_review:
        print(
            f"  память на пересмотр ({len(report.memory_due_for_review)} записей) — "
            "удаляет человек, не джоба"
        )
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    """Сделать зашифрованный снимок базы и убрать просроченные архивы."""
    from hermes_cloud.core.backup import BackupError, make_backup, prune_backups

    try:
        archive = make_backup(_database(args), args.directory)
    except BackupError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"{archive.path}  {archive.size} байт")
    for removed in prune_backups(args.directory):
        print(f"{removed}  удалён по сроку хранения")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    """Восстановить базу из архива. Существующий файл не перезаписывается без `--force`."""
    from hermes_cloud.core.backup import BackupError, restore_backup

    # Восстановление — единственная команда, которая не открывает базу и потому
    # не проходит через `_database`: ключ бэкапа надо подтянуть самим.
    load_dotenv()
    target = Path(args.target or os.environ.get(DB_ENV) or DEFAULT_DB_PATH)
    try:
        restored = restore_backup(args.archive, target, overwrite=args.force)
    except BackupError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"восстановлено в {restored}")
    print("проверьте: hermes-cloud status && hermes-cloud pending")
    return 0


def _owner_request(args: argparse.Namespace, kind: str, question: str) -> int:
    """Поставить owner-операцию на подтверждение кодом.

    Экспорт и удаление необратимы каждый по-своему, поэтому идут тем же путём,
    что и действия: предложение → код → ✅. Права проверяются здесь, второй раз —
    при подтверждении (`CAPABILITY_FOR_KIND`).
    """
    from hermes_cloud.core.runcontext import CapabilityDenied, build_run_context
    from hermes_cloud.runner.pipeline import CAPABILITY_FOR_KIND

    database = _database(args)
    pipeline = _pipeline(args, database)
    household = _household()
    actor = args.actor or household.owner
    if not actor:
        print("не задан владелец: HERMES_OWNER или --actor", file=sys.stderr)
        return 1
    context = build_run_context(
        household=household, actor_id=actor,
        chat_id=pipeline.chat, thread_id=pipeline.thread,
    )
    try:
        context.require(CAPABILITY_FOR_KIND[kind])
    except CapabilityDenied as denied:
        print(f"отказано: {denied}", file=sys.stderr)
        return 1
    staged = pipeline.approvals.stage(
        kind=kind,
        payload={"kind": kind, "requested_by": actor},
        chat=pipeline.chat,
        thread=pipeline.thread,
        actor=actor,
    )
    pipeline.transport.send_message(
        chat=pipeline.chat,
        thread=pipeline.thread,
        text=f"{question}\nКод подтверждения: {staged.code}",
        buttons=(("✅ Да", f"confirm:{staged.id}"), ("❌ Нет", f"reject:{staged.id}")),
    )
    print(f"{staged.id}  ждёт подтверждения владельцем")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """Запросить полную выгрузку данных household'а."""
    return _owner_request(
        args, "export", "Выгрузить все данные household'а одним файлом?"
    )


def cmd_delete(args: argparse.Namespace) -> int:
    """Запросить стирание household'а. Необратимо."""
    return _owner_request(
        args,
        "delete",
        "Стереть все данные household'а? Это необратимо — "
        "внешние поверхности закрываются отдельно по delete-runbook.",
    )


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


def cmd_validate_manifest(args: argparse.Namespace) -> int:
    """Validate a versioned household manifest without starting runtime work."""
    from hermes_cloud.core.runtime_manifest import ManifestError, load_runtime_manifest

    load_dotenv()
    try:
        manifest = load_runtime_manifest(args.path)
    except ManifestError as error:
        print(f"manifest invalid: {error}", file=sys.stderr)
        return 1
    print(
        f"manifest ok: household={manifest.household_id} "
        f"revision={manifest.config_revision} sha256={manifest.config_sha256}"
    )
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    """Claim and activate one manifest; bootstrap token is accepted from env only."""
    from hermes_cloud.core.runtime_manifest import ENV_CONFIG_REVISION, ENV_HOUSEHOLD_ID
    from hermes_cloud.runtime.bootstrap import (
        ENV_BOOTSTRAP_TOKEN,
        ENV_CONTROL_PLANE_URL,
        ENV_RUNTIME_REF,
        BootstrapError,
        ControlPlaneBootstrapClient,
        RuntimeBootstrapper,
    )

    load_dotenv()
    token = os.environ.get(ENV_BOOTSTRAP_TOKEN, "")
    base_url = args.control_plane_url or os.environ.get(ENV_CONTROL_PLANE_URL, "")
    runtime_ref = args.runtime_ref or os.environ.get(ENV_RUNTIME_REF, "")
    household_id = os.environ.get(ENV_HOUSEHOLD_ID, "")
    config_revision = os.environ.get(ENV_CONFIG_REVISION, "")
    if not all((token, base_url, runtime_ref, household_id, config_revision)):
        print(
            f"bootstrap requires ${ENV_BOOTSTRAP_TOKEN}, ${ENV_CONTROL_PLANE_URL}, "
            f"${ENV_RUNTIME_REF}, ${ENV_HOUSEHOLD_ID}, and ${ENV_CONFIG_REVISION}",
            file=sys.stderr,
        )
        return 1
    try:
        revision = int(config_revision)
    except ValueError:
        print(
            f"bootstrap failed: ${ENV_CONFIG_REVISION} must be an integer",
            file=sys.stderr,
        )
        return 1
    try:
        manifest = RuntimeBootstrapper(
            ControlPlaneBootstrapClient(base_url, timeout=args.timeout),
            runtime_ref=runtime_ref,
            household_id=household_id,
            config_revision=revision,
            manifest_path=args.manifest,
            activation_path=args.activation_state,
        ).run(token)
    except BootstrapError as error:
        print(f"bootstrap failed: {error}", file=sys.stderr)
        return 1
    print(
        f"runtime active: household={manifest.household_id} "
        f"revision={manifest.config_revision}"
    )
    return 0


def cmd_revoke_google_grant(_args: argparse.Namespace) -> int:
    """Revoke the staged Gmail refresh grant without exposing secret material."""

    raw = os.environ.get("ABROLIA_GMAIL_OAUTH_GRANT", "")
    try:
        payload = json.loads(raw)
        credential = payload["refresh_credential"]
        if not isinstance(payload, dict) or not isinstance(credential, str) or not credential:
            raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        print("Google revoke failed (configuration)", file=sys.stderr)
        return 2
    try:
        response = httpx.post(
            "https://oauth2.googleapis.com/revoke",
            params={"token": credential},
            timeout=20.0,
        )
    except (httpx.TimeoutException, httpx.TransportError):
        print("Google revoke failed (network)", file=sys.stderr)
        return 3
    if response.status_code in {200, 400}:
        return 0
    print("Google revoke failed (provider)", file=sys.stderr)
    return 4


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

    reconcile = commands.add_parser("reconcile", help="разобрать эффекты, повисшие после падения")
    reconcile.set_defaults(func=cmd_reconcile)

    gmail = commands.add_parser("gmail-poll", help="забрать письма с ярлыком Hermes")
    gmail.add_argument(
        "--baseline", action="store_true",
        help="первый запуск: пометить уже существующие письма виденными",
    )
    gmail.add_argument(
        "--rebaseline", action="store_true",
        help="после смены UIDVALIDITY: сбросить курсор осознанно",
    )
    gmail.set_defaults(func=cmd_gmail_poll)

    retention = commands.add_parser("retention", help="стереть то, чему вышел срок хранения")
    retention.set_defaults(func=cmd_retention)

    backup = commands.add_parser("backup", help="зашифрованный снимок базы")
    backup.add_argument("--directory", default="backups", help="куда класть архивы")
    backup.set_defaults(func=cmd_backup)

    restore = commands.add_parser("restore", help="восстановить базу из архива")
    restore.add_argument("archive", type=Path)
    restore.add_argument("--target", help="куда восстанавливать (иначе — рабочая база)")
    restore.add_argument("--force", action="store_true", help="перезаписать существующий файл")
    restore.set_defaults(func=cmd_restore)

    export = commands.add_parser("export", help="запросить полную выгрузку данных (владелец)")
    export.add_argument("--actor", help="от чьего имени (по умолчанию — HERMES_OWNER)")
    export.set_defaults(func=cmd_export)

    delete = commands.add_parser("delete", help="запросить стирание household'а (владелец)")
    delete.add_argument("--actor", help="от чьего имени (по умолчанию — HERMES_OWNER)")
    delete.set_defaults(func=cmd_delete)

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

    validate_manifest = commands.add_parser(
        "validate-manifest", help="проверить versioned household.toml"
    )
    validate_manifest.add_argument("path", nargs="?", type=Path)
    validate_manifest.set_defaults(func=cmd_validate_manifest)

    bootstrap = commands.add_parser(
        "bootstrap", help="получить и активировать manifest control plane"
    )
    bootstrap.add_argument("--control-plane-url")
    bootstrap.add_argument("--runtime-ref")
    bootstrap.add_argument("--manifest", type=Path, default=Path("/data/household.toml"))
    bootstrap.add_argument(
        "--activation-state", type=Path, default=Path("/data/runtime-activation.json")
    )
    bootstrap.add_argument("--timeout", type=float, default=20.0)
    bootstrap.set_defaults(func=cmd_bootstrap)

    revoke_google = commands.add_parser(
        "revoke-google-grant", help="отозвать staged Gmail OAuth grant"
    )
    revoke_google.set_defaults(func=cmd_revoke_google_grant)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in PROVISIONED_GATED_COMMANDS:
        load_dotenv()
        provisioned = bool(
            os.environ.get("HERMES_CONFIG_REVISION")
            or os.environ.get("HERMES_REQUIRE_MANIFEST", "").strip().casefold()
            in {"1", "true", "yes", "on"}
        )
        if provisioned:
            from hermes_cloud.runtime.service import RuntimeNotReady, RuntimeService

            try:
                RuntimeService().require_ready()
            except RuntimeNotReady as error:
                print(str(error), file=sys.stderr)
                return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

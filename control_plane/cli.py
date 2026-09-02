"""Operator commands for the single-writer synthetic control plane."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from control_plane.api.app import create_app
from control_plane.auth.mailer import ConsoleMailer, Mailer
from control_plane.backup import (
    BackupError,
    create_backup,
    install_rollback,
    require_control_plane_database,
    restore_backup,
)
from control_plane.config import ControlPlaneConfig, backup_key_from_env
from control_plane.container import ControlPlaneContainer
from control_plane.db import ControlPlaneDatabase, ProcessAlreadyRunning
from control_plane.observability import StructuredLogger
from control_plane.privacy.consent import CONSENT_TEXTS
from control_plane.privacy.withdraw import ConsentNotHeld
from control_plane.provisioning.rollout import reconcile_stale_bindings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="abrolia-control-plane")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="run API and embedded durable worker")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--worker-interval", type=float, default=0.5)
    worker = commands.add_parser("worker", help="drain durable jobs while API is stopped")
    worker.add_argument("--limit", type=int, default=100)
    commands.add_parser("jobs", help="list only redacted job metadata")
    reconcile = commands.add_parser("reconcile", help="inspect one outcome-unknown job")
    reconcile.add_argument("job_id")
    dry_run = commands.add_parser("dry-run", help="drain fake providers without Fly writes")
    dry_run.add_argument("--limit", type=int, default=100)
    commands.add_parser("retention", help="run the daily retention sweep")
    reconcile_bindings = commands.add_parser(
        "reconcile-bindings",
        help="re-plan households whose runtime serves a stale binding set",
    )
    reconcile_bindings.add_argument(
        "--apply",
        action="store_true",
        help="schedule the rollouts instead of only reporting them",
    )
    commands.add_parser(
        "backfill-sender-digests",
        help="give bindings written before the gateway key their lookup digest",
    )
    commands.add_parser(
        "runtime-health", help="reconcile dedicated runtime readiness receipts"
    )
    resume_deletions = commands.add_parser(
        "resume-deletions", help="resume durable partial deletion requests"
    )
    resume_deletions.add_argument("--limit", type=int, default=100)
    commands.add_parser("migrate", help="apply control-plane migrations")
    invite = commands.add_parser("invite", help="emit one operator-only synthetic invite")
    invite.add_argument("email")
    backup = commands.add_parser("backup", help="create an authenticated SQLite archive")
    backup.add_argument("target")
    restore = commands.add_parser("restore", help="restore into a new, worker-paused DB")
    restore.add_argument("archive")
    restore.add_argument("--target", required=True)
    restore.add_argument(
        "--no-migrate",
        action="store_true",
        help="rollback restore: keep the archived schema, apply no migration",
    )
    install = commands.add_parser(
        "install-rollback",
        help="move a restored database to the path the rolled-back image opens",
    )
    install.add_argument(
        "--restored",
        required=True,
        help="the database written by `restore --no-migrate`",
    )
    install.add_argument(
        "--target",
        required=True,
        help="the canonical path from fly.toml, currently holding the superseded database",
    )
    install.add_argument(
        "--target-already-freed",
        action="store_true",
        help=(
            "the superseded database was archived off-volume and deleted to make"
            " room, so --target does not exist yet"
        ),
    )
    commands.add_parser("resume-jobs", help="remove the exact post-restore worker pause")
    # The operator boundary for Art. 7(3). The consent copy advertises a
    # mailbox, not a button, so withdrawal arrives as mail to a human — this
    # is the command that human runs. A self-service route needs an
    # authenticated session and belongs with the account UI; until it exists,
    # a right that only tests can exercise is not a right.
    withdraw = commands.add_parser(
        "withdraw-consent",
        help="withdraw one consent for one household (Art. 7(3))",
    )
    withdraw.add_argument("household_id")
    withdraw.add_argument(
        "purpose",
        choices=sorted(CONSENT_TEXTS),
        help="the consent purpose being withdrawn",
    )
    return parser


def _container(*, lock: bool = True, mailer: Mailer | None = None) -> ControlPlaneContainer:
    return ControlPlaneContainer.build(
        ControlPlaneConfig.from_env(), acquire_process_lock=lock, mailer=mailer
    )


def _resume_deletions_if_running(
    active: ControlPlaneContainer, *, limit: int, now: float | None = None
) -> list:
    """A restored database must stay side-effect free until explicit resume."""
    if active.database.workers_paused:
        return []
    return active.deletion.resume_pending(limit=limit, now=now)


def _reconcile_runtime_health(active: ControlPlaneContainer, *, now: float) -> float:
    """Run one sweep and schedule the next interval from its completion."""

    active.runtime_health.reconcile_all(now=now)
    return time.time() + 60


def _take_periodic_archive(active, logger, *, now: float) -> None:
    """Keep the durability archive fresh without a deploy.

    Never raises, and never fails the service: the same rule the boot path
    follows, for the same reason — a full volume must not become an outage.

    `Exception`, not `(BackupError, OSError)`. The narrow tuple was wrong and
    quietly so: `take_periodic_archive` reaches `PRAGMA integrity_check`, a
    second `sqlite3.connect`, and `connection.backup()`, every one of which
    raises `sqlite3.Error` — which is neither. Those escaped to the worker
    loop's generic handler WITHOUT recording an attempt, so `/readyz` went on
    reporting the previous `written` while nothing was being written. That is
    exactly the invisibility the `backup_writer` signal exists to end, so the
    one thing this must never do is fail without saying so.
    """
    from control_plane.backup import record_boot_archive_attempt, take_periodic_archive

    if not active.config.backup_key:
        return
    try:
        archive = take_periodic_archive(
            active.database.path, backup_key=active.config.backup_key, now=now
        )
    except Exception as error:  # noqa: BLE001 - recorded, never propagated
        logger.emit(
            "periodic_archive_failed",
            status="failed",
            error_code=error.__class__.__name__,
        )
        record_boot_archive_attempt(
            active.database, outcome="failed", detail=str(error), now=now
        )
        return
    if archive is not None:
        record_boot_archive_attempt(
            active.database, outcome="written", detail=str(archive), now=now
        )


def _run_maintenance(active, logger, schedule: dict[str, float], *, now: float) -> None:
    """One pass of the periodic tasks, each in its own failure boundary.

    Previously these shared a single `try`, and the archive was last. A task
    that failed persistently — `retention.run()` raising, say, leaving its own
    "next at" unadvanced so it retried on every tick — jumped to the outer
    handler before the archive branch was ever reached. The service stayed up,
    nothing looked wrong, and 26 hours later the backup was stale again: the
    exact defect O0a describes, returning through a different door.

    So each task advances its own schedule BEFORE running, and a failure in
    one cannot starve another. The archive is also first, because it is the
    one whose starvation is silent.
    """
    tasks: list[tuple[str, float, callable]] = [
        ("archive", 300.0, lambda: _take_periodic_archive(active, logger, now=now)),
        ("retention", 24 * 60 * 60.0, lambda: active.retention.run(now=now)),
        (
            "deletion_resume",
            30.0,
            lambda: _resume_deletions_if_running(active, limit=10, now=now),
        ),
    ]
    if active.config.runtime_provider == "fly-runtime":
        tasks.append(
            (
                "runtime_health",
                60.0,
                lambda: active.runtime_health.reconcile_all(now=now),
            )
        )

    for name, interval, run in tasks:
        if now < schedule.get(name, 0.0):
            continue
        # Advanced BEFORE the call: a task that raises must not retry on every
        # 0.5s tick, which is what let one failure crowd out everything else.
        schedule[name] = now + interval
        try:
            run()
        except Exception as error:  # noqa: BLE001 - isolated, never propagated
            logger.emit(
                "maintenance_task_failed",
                status="failed",
                error_code=f"{name}:{error.__class__.__name__}",
            )


def _serve(args: argparse.Namespace) -> int:
    active = _container()
    stop = threading.Event()
    logger = StructuredLogger(sys.stderr)

    def worker_loop() -> None:
        # The archive is not due immediately: the boot that started this
        # process has just taken one, and the interval check would refuse
        # anyway. Asking every five minutes costs a directory listing.
        schedule: dict[str, float] = {"archive": time.time() + 300.0}
        while not stop.wait(args.worker_interval):
            try:
                if active.database.workers_paused:
                    continue
                active.worker.run_once()
            except Exception as error:
                logger.emit(
                    "worker_loop_failed",
                    status="failed",
                    error_code=error.__class__.__name__,
                )
            # Outside the block above, so a failing job drain cannot starve
            # the maintenance tasks either.
            try:
                if not active.database.workers_paused:
                    _run_maintenance(active, logger, schedule, now=time.time())
            except Exception as error:
                logger.emit(
                    "worker_loop_failed",
                    status="failed",
                    error_code=error.__class__.__name__,
                )

    worker = threading.Thread(target=worker_loop, name="abrolia-worker", daemon=True)
    worker.start()
    try:
        uvicorn.run(
            create_app(active_container=active),
            host=args.host,
            port=args.port,
            access_log=False,
        )
    finally:
        stop.set()
        worker.join(timeout=5)
        active.close()
    return 0


def _jobs(active: ControlPlaneContainer) -> int:
    rows = active.database.query(
        "SELECT id, household_id, workflow_id, kind, operation, status, provider, attempts,"
        " desired_revision, error_code, not_before, lease_until, created_at, updated_at"
        " FROM provisioning_jobs ORDER BY created_at, id"
    )
    print(json.dumps([dict(row) for row in rows], sort_keys=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "serve":
        return _serve(args)
    if args.command == "restore":
        # `backup_key_from_env`, NOT `ControlPlaneConfig.from_env`. Recovery
        # must not require the rest of the deployment to be intact: reading the
        # key through the full config made this refuse to run when any unrelated
        # secret was missing, which withdrew the rollback path in precisely the
        # state it exists for.
        restored = restore_backup(
            args.archive,
            args.target,
            backup_key=backup_key_from_env(),
            apply_migrations=not args.no_migrate,
        )
        restored.close()
        print(json.dumps({
            "status": "restored",
            "workers": "paused",
            "migrated": not args.no_migrate,
        }, sort_keys=True))
        return 0
    if args.command == "backup":
        # NOT through `_container()`. That builds the whole application and
        # `ControlPlaneContainer.build` migrates — so on the principal call path
        # for this command, a persistently failing pending migration, it would
        # repeat that migration and exit before writing anything. The one
        # command an operator reaches for when a deployment is broken must not
        # require the deployment to work.
        #
        # The writer lock is still taken, and the same way, because archiving a
        # database another process is writing produces an archive of a moment
        # that never existed.
        database_path = Path(
            os.environ.get("ABROLIA_CONTROL_PLANE_DB", "data/control-plane.db")
        )
        # `sqlite3.connect` CREATES a missing file, so an unset, mistyped or
        # unmounted path produced a new empty database, a valid authenticated
        # archive of nothing, and a verification restore that PASSED — an empty
        # database satisfies `integrity_check` and `foreign_key_check`. The
        # operator then deletes the real `/data` bundle believing it is
        # archived.
        #
        # `is_file()` alone did not close that: a zero-byte file, an unrelated
        # SQLite database, or a symlink to either all pass it. What this command
        # must establish is that the path IS the control-plane database, so it
        # checks the shape of the entry, that it opens as SQLite, and that it
        # carries the migration ledger every control-plane database has. A
        # recovery command archiving the wrong file is worse than one that
        # refuses.
        # A side-effect-free existence check first, because acquiring the lock
        # CREATES the parent directory and the lock file — and doing that for an
        # unmounted or mistyped path is itself the mutation this command must
        # not make.
        if not os.path.lexists(database_path):
            raise SystemExit(
                f"backup found no database at {database_path};"
                " refusing rather than archiving an empty one"
            )
        # Then LOCK, then validate. Validating and then locking leaves a
        # window in which another process replaces the database, so the command
        # authenticates one file and archives another — and `_readable_sqlite`
        # can remove sidecars a writer starting in that window has just created.
        # Exclusive ownership is a precondition of the check, not a companion
        # to it.
        database = ControlPlaneDatabase(database_path)
        try:
            database.acquire_process_lock()
        except ProcessAlreadyRunning as error:
            raise SystemExit(
                f"backup refused: {error}. Stop the service first."
            ) from error
        try:
            # The ENTRY that was proved, carried into the snapshot. Validation
            # opens and closes its own read-only connection, `database` has not
            # opened one yet, and the writer lock is advisory — so a rename in
            # between would have this authenticate a file nobody checked and
            # report a valid archive of the wrong generation, after which the
            # runbook permits deleting the real bundle.
            identity = require_control_plane_database(database_path)
        except BackupError as error:
            database.release_process_lock()
            database.close()
            raise SystemExit(f"backup refused: {error}") from error
        try:
            result = create_backup(
                database,
                args.target,
                backup_key=backup_key_from_env(),
                identity=identity,
            )
        except BackupError as error:
            # A refusal, not a crash. `create_backup` rejects a target that
            # aliases the live bundle — `<db>.workers-paused` being the one that
            # matters, because it is normally absent and publishing an archive
            # there pauses every worker — and an operator who mistyped `--target`
            # needs the message, not a traceback.
            raise SystemExit(f"backup refused: {error}") from error
        finally:
            database.release_process_lock()
            database.close()
        print(json.dumps({"status": "created", "path": str(result)}))
        return 0
    if args.command == "install-rollback":
        # No container: this command runs with the service stopped, and building
        # one would open the target and create WAL sidecars on the very volume
        # it is trying to free. Same recovery-only key as `restore`, for the
        # same reason — the two are one documented procedure and must have the
        # same preconditions.
        # No backup key either: this command reads no archive. It moves files
        # and refuses when it cannot, so the only credential-shaped dependency
        # it had came from the space-freeing it no longer does.
        print(json.dumps(
            install_rollback(
                args.restored,
                args.target,
                target_already_freed=args.target_already_freed,
            ),
            sort_keys=True,
        ))
        return 0
    if args.command == "runtime-health":
        # This read/compare/projection command is safe to run beside the
        # embedded worker and must remain available while ``serve`` owns the
        # single-writer process lock.
        with _container(lock=False) as active:
            if (
                active.config.runtime_provider != "fly-runtime"
                or active.database.workers_paused
            ):
                print("[]")
                return 0
            results = active.runtime_health.reconcile_all(now=time.time())
            print(json.dumps([result.__dict__ for result in results], sort_keys=True))
            return 0
    if args.command == "invite":
        # Handled before the locking container below, for the same reason as
        # `withdraw-consent`: `serve` holds the nonblocking writer flock for the
        # life of the process, so an invite that takes it can only be issued
        # with production STOPPED — every tester admitted cost a restart. The
        # work is one token row and one line on the operator's stdout, which
        # SQLite serialises on its own; the flock guards against a second
        # long-running WORKER, which this is not.
        with _container(lock=False, mailer=ConsoleMailer()) as active:
            result = active.magic_links.issue(args.email)
            print(json.dumps({"status": "issued", "expires_at": result.expires_at}))
            return 0
    if args.command == "withdraw-consent":
        # Handled before the locking container below. `serve` holds the
        # nonblocking writer flock for the life of the process, so a command
        # that takes it can only run with production stopped — and an Art. 7(3)
        # withdrawal that requires taking the service down is not the one-step
        # withdrawal the consent copy promises. The work here is one short
        # transaction plus a queued job, which SQLite serialises on its own;
        # the flock guards against a second long-running WORKER, which this is
        # not. Routing it through the running process instead would need an
        # authenticated admin surface, and that belongs with the account UI.
        with _container(lock=False) as active:
            try:
                result = active.withdrawal.withdraw(
                    args.household_id, args.purpose, now=time.time()
                )
            except ConsentNotHeld as error:
                raise SystemExit(str(error)) from error
            # No identifiers beyond the one supplied: the operator already knows
            # it, and the log must not gain a new copy.
            print(json.dumps({
                "purpose": result.purpose,
                "receipts_revoked": result.receipts_revoked,
                "revisions_revoked": result.revisions_revoked,
                "runtime_notified": result.runtime_notified,
            }, sort_keys=True))
            return 0

    with _container() as active:
        if args.command == "migrate":
            print(json.dumps({"status": "ok", "applied": active.database.migrate()}))
            return 0
        if args.command == "worker":
            results = active.worker.drain(limit=args.limit)
            deletions = _resume_deletions_if_running(active, limit=args.limit)
            print(json.dumps({
                "jobs": [result.__dict__ for result in results],
                "deletions": [result.public_dict() for result in deletions],
            }, sort_keys=True))
            return 0
        if args.command == "dry-run":
            if active.config.runtime_provider != "dry-run-runtime":
                raise SystemExit("dry-run refuses a non-dry-run runtime provider")
            results = active.worker.drain(limit=args.limit)
            print(json.dumps({"mode": "synthetic", "processed": len(results)}))
            return 0
        if args.command == "jobs":
            return _jobs(active)
        if args.command == "reconcile":
            result = active.worker.reconcile(args.job_id)
            print(json.dumps(result.__dict__, sort_keys=True))
            return 0
        if args.command == "reconcile-bindings":
            # Dry run by default: this schedules real deployments for
            # households nobody asked about, and the operator should read what
            # it would do before it does it.
            with active.database.write() as connection:
                report = reconcile_stale_bindings(
                    connection,
                    planner=active.planner,
                    jobs=active.jobs,
                    onboarding=active.onboarding_repository,
                    configs=active.configs,
                    bindings=active.bindings,
                    runtime_provider=active.config.runtime_provider,
                    apply=args.apply,
                )
            print(json.dumps(list(report), sort_keys=True))
            return 0
        if args.command == "backfill-sender-digests":
            # The upgrade step for a database that predates
            # `ABROLIA_GATEWAY_SENDER_HMAC_KEY`. A strict-mode gateway matches
            # ONLY `external_id_hmac`, so on such a database every existing
            # binding resolves as `unknown_sender` the moment the key is
            # configured — only bindings written afterwards would work, and the
            # households already onboarded would go dark.
            #
            # Run it BEFORE enabling strict lookup. It is idempotent and fills
            # only NULL digests, so running it twice, or on a database that
            # never needed it, costs one query and changes nothing.
            #
            # A command rather than a migration because SQL cannot compute a
            # keyed digest: the key belongs to the application, and a migration
            # that needed it would have to be handed a secret the schema has no
            # business holding.
            with active.database.write() as connection:
                repaired = active.bindings.backfill_sender_digests(connection)
            print(json.dumps({"repaired": repaired}, sort_keys=True))
            return 0
        if args.command == "retention":
            result = active.retention.run()
            print(json.dumps({"deleted": result.deleted, "scrubbed": result.scrubbed}, sort_keys=True))
            return 0
        if args.command == "resume-deletions":
            results = active.deletion.resume_pending(limit=args.limit)
            print(json.dumps(
                [result.public_dict() for result in results], sort_keys=True
            ))
            return 0
        if args.command == "resume-jobs":
            active.database.resume_workers()
            print(json.dumps({"status": "resumed"}))
            return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

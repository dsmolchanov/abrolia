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


def _serve(args: argparse.Namespace) -> int:
    active = _container()
    stop = threading.Event()
    logger = StructuredLogger(sys.stderr)

    def worker_loop() -> None:
        next_retention_at = 0.0
        next_deletion_resume_at = 0.0
        next_runtime_health_at = 0.0
        while not stop.wait(args.worker_interval):
            try:
                if active.database.workers_paused:
                    continue
                active.worker.run_once()
                now = time.time()
                if now >= next_retention_at:
                    active.retention.run(now=now)
                    next_retention_at = now + 24 * 60 * 60
                if now >= next_deletion_resume_at:
                    _resume_deletions_if_running(active, limit=10, now=now)
                    next_deletion_resume_at = now + 30
                if (
                    active.config.runtime_provider == "fly-runtime"
                    and now >= next_runtime_health_at
                ):
                    next_runtime_health_at = _reconcile_runtime_health(
                        active, now=now
                    )
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
        try:
            require_control_plane_database(database_path)
        except BackupError as error:
            raise SystemExit(f"backup refused: {error}") from error
        database = ControlPlaneDatabase(database_path)
        try:
            database.acquire_process_lock()
        except ProcessAlreadyRunning as error:
            raise SystemExit(
                f"backup refused: {error}. Stop the service first."
            ) from error
        try:
            result = create_backup(
                database, args.target, backup_key=backup_key_from_env()
            )
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
    mailer = ConsoleMailer() if args.command == "invite" else None
    with _container(mailer=mailer) as active:
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
        if args.command == "invite":
            result = active.magic_links.issue(args.email)
            print(json.dumps({"status": "issued", "expires_at": result.expires_at}))
            return 0
        if args.command == "resume-jobs":
            active.database.resume_workers()
            print(json.dumps({"status": "resumed"}))
            return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Allowlisted structured telemetry with PII/credential canary rejection."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, TextIO

from control_plane.crypto import SecretFieldError, reject_secret_fields
from control_plane.db import ControlPlaneDatabase

ALLOWED_FIELDS = frozenset({
    "event",
    "workflow_id",
    "step_kind",
    "job_id",
    "status",
    "duration_ms",
    "attempts",
    "error_code",
    "provider",
    "config_revision",
})
PII_PATTERN = re.compile(
    r"(?:[^\s@]+@[^\s@]+\.[^\s@]+)"
    r"|(?:(?<![A-Za-z0-9_-])\+?\d[\d\s().-]{7,}\d(?![A-Za-z0-9_-]))"
    r"|(?:[A-Za-z0-9_-]{40,})"
)


class UnsafeTelemetry(ValueError):
    pass


def safe_event(event: str, **fields: Any) -> dict[str, Any]:
    payload = {"event": event, **fields}
    unknown = set(payload) - ALLOWED_FIELDS
    if unknown:
        raise UnsafeTelemetry(f"telemetry field is not allowlisted: {sorted(unknown)[0]}")
    try:
        reject_secret_fields(payload)
    except SecretFieldError as error:
        raise UnsafeTelemetry("telemetry resembles credential material") from error
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if PII_PATTERN.search(encoded):
        raise UnsafeTelemetry("telemetry resembles PII or credential material")
    return payload


class StructuredLogger:
    def __init__(self, stream: TextIO) -> None:
        self.stream = stream

    def emit(self, event: str, **fields: Any) -> None:
        print(json.dumps(safe_event(event, **fields), sort_keys=True), file=self.stream)


@dataclass(frozen=True)
class HealthSnapshot:
    database_ok: bool
    volume_ok: bool
    volume_free_bytes: int | None
    workers_paused: bool
    pending_jobs: int | None
    stale_leases: int | None
    unknown_outcomes: int | None
    expired_bootstrap_tokens: int | None
    backup_age_seconds: float | None
    #: What the last boot archive attempt did, or None when none is recorded.
    #: `backup_age_seconds` cannot carry this: a stale archive means "no deploy
    #: lately" — benign, and the reason the deploy gate stopped blocking on it
    #: — OR "the writer is broken", which is not benign. They need different
    #: names to be actionable.
    boot_archive_outcome: str | None = None

    def liveness_blockers(self) -> tuple[str, ...]:
        blockers: list[str] = []
        if not self.database_ok:
            blockers.append("database_unavailable")
        if not self.volume_ok:
            blockers.append("volume_unavailable")
        return tuple(blockers)

    def readiness_blockers(
        self, *, maximum_backup_age_seconds: float
    ) -> tuple[str, ...]:
        blockers = list(self.liveness_blockers())
        if self.workers_paused:
            blockers.append("workers_paused")
        if self.stale_leases:
            blockers.append("stale_worker_leases")
        if self.unknown_outcomes:
            blockers.append("provider_outcomes_unknown")
        if self.expired_bootstrap_tokens:
            blockers.append("expired_bootstrap")
        if (
            self.backup_age_seconds is not None
            and self.backup_age_seconds > maximum_backup_age_seconds
        ):
            blockers.append("backup_stale")
        if self.boot_archive_outcome == "failed":
            # Named separately from `backup_stale` on purpose. The deploy gate
            # excuses both — a deploy is not the remedy for either, and gating
            # on them is what deadlocked production for nine days — but this
            # one has to be SAYABLE. It appears in the blocker list the deploy
            # workflow prints and an operator scans, which is the visibility
            # that was missing while a broken writer looked healthy.
            blockers.append("backup_writer_failed")
        return tuple(blockers)


class HealthReporter:
    def __init__(self, database: ControlPlaneDatabase) -> None:
        self.database = database

    def snapshot(
        self,
        *,
        backup_completed_at: float | None = None,
        boot_archive_outcome: str | None = None,
        now: float | None = None,
    ) -> HealthSnapshot:
        now = time.time() if now is None else now
        volume_ok, volume_free_bytes = self._volume_status()
        try:
            database_ok = self.database.query_one("SELECT 1") is not None
            pending = self._count(
                "SELECT COUNT(*) AS count FROM provisioning_jobs WHERE status = 'pending'"
            )
            stale = self._count(
                "SELECT COUNT(*) AS count FROM provisioning_jobs"
                " WHERE status = 'running' AND (lease_until IS NULL OR lease_until < ?)",
                (now,),
            )
            unknown = self._count(
                "SELECT COUNT(*) AS count FROM provisioning_jobs"
                " WHERE status = 'outcome_unknown'"
            )
            bootstrap = self._count(
                "SELECT COUNT(*) AS count FROM bootstrap_tokens"
                " WHERE expires_at < ? AND used_at IS NULL AND revoked_at IS NULL",
                (now,),
            )
        except (OSError, sqlite3.Error):
            database_ok = False
            pending = stale = unknown = bootstrap = None
        try:
            workers_paused = self.database.workers_paused
        except OSError:
            workers_paused = True
        age = None if backup_completed_at is None else max(0.0, now - backup_completed_at)
        return HealthSnapshot(
            database_ok,
            volume_ok,
            volume_free_bytes,
            workers_paused,
            pending,
            stale,
            unknown,
            bootstrap,
            age,
            boot_archive_outcome,
        )

    def latest_boot_archive_outcome(self) -> str | None:
        """What the boot recorded, without exposing the detail string."""
        try:
            row = self.database.query_one(
                "SELECT outcome FROM boot_archive_attempts WHERE id = 1"
            )
        except sqlite3.Error:
            return None
        return None if row is None else str(row["outcome"])

    def latest_backup_completed_at(self) -> float | None:
        """Return the newest conventional archive mtime without exposing its path."""
        backup_directory = self.database.path.parent / "backups"
        try:
            mtimes = [archive.stat().st_mtime for archive in backup_directory.glob("*.cpb")]
        except OSError:
            return None
        return max(mtimes, default=None)

    def _count(self, sql: str, params: tuple = ()) -> int:
        row = self.database.query_one(sql, params)
        if row is None:
            raise sqlite3.DatabaseError("health query returned no row")
        return int(row["count"])

    def _volume_status(self) -> tuple[bool, int | None]:
        database_path = self.database.path
        parent = database_path.parent
        try:
            stats = os.statvfs(parent)
            free_bytes = int(stats.f_bavail * stats.f_frsize)
            volume_ok = database_path.is_file() and parent.is_dir() and free_bytes > 0
        except OSError:
            return False, None
        return volume_ok, free_bytes

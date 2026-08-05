"""Durable fixed-window limits without retaining raw network identifiers."""

from __future__ import annotations

import time

from control_plane.crypto import LookupHasher
from control_plane.db import ControlPlaneDatabase


class RateLimitExceeded(PermissionError):
    pass


class RateLimiter:
    def __init__(self, database: ControlPlaneDatabase, hasher: LookupHasher) -> None:
        self.database = database
        self.hasher = hasher

    def check(
        self,
        kind: str,
        identifier: str,
        *,
        limit: int,
        window_seconds: float,
        now: float | None = None,
    ) -> None:
        now = time.time() if now is None else now
        bucket = self.hasher.digest(f"rate:{kind}:{identifier}")
        with self.database.write() as connection:
            row = connection.execute(
                "SELECT * FROM rate_limit_buckets WHERE bucket_hmac = ?", (bucket,)
            ).fetchone()
            if row is None or row["window_started_at"] + window_seconds <= now:
                connection.execute(
                    "INSERT INTO rate_limit_buckets (bucket_hmac, kind, window_started_at,"
                    " attempts, updated_at) VALUES (?, ?, ?, 1, ?)"
                    " ON CONFLICT (bucket_hmac) DO UPDATE SET kind = excluded.kind,"
                    " window_started_at = excluded.window_started_at, attempts = 1,"
                    " updated_at = excluded.updated_at",
                    (bucket, kind, now, now),
                )
                return
            if row["attempts"] >= limit:
                raise RateLimitExceeded("request limit reached")
            connection.execute(
                "UPDATE rate_limit_buckets SET attempts = attempts + 1, updated_at = ?"
                " WHERE bucket_hmac = ?",
                (now, bucket),
            )

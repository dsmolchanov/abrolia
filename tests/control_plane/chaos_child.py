"""Tiny subprocess entrypoint used by process-lock and SIGKILL recovery tests."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from control_plane.db import ControlPlaneDatabase, ProcessAlreadyRunning


def main() -> int:
    if len(sys.argv) >= 4 and sys.argv[2] in {"lease-only", "lease-and-accept"}:
        database = ControlPlaneDatabase(Path(sys.argv[1]))
        try:
            with database.write() as connection:
                connection.execute(
                    "UPDATE provisioning_jobs SET status = 'running', leased_by = 'chaos-child',"
                    " lease_until = 1800000001, attempts = attempts + 1, updated_at = 1800000000"
                    " WHERE id = ? AND status = 'pending'",
                    (sys.argv[3],),
                )
            if sys.argv[2] == "lease-and-accept":
                Path(sys.argv[4]).write_text("accepted", encoding="utf-8")
            print("crash-window-open", flush=True)
            while True:
                time.sleep(1)
        finally:
            database.close()
    database = ControlPlaneDatabase(Path(sys.argv[1]))
    try:
        database.acquire_process_lock()
    except ProcessAlreadyRunning:
        return 23
    finally:
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

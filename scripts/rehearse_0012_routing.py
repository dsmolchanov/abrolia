"""Rehearse migration 0012 against a copy of a real database.

C3c makes the gateway route only PUBLISHED bindings, and `0012` decides which
existing ones count as published. A household the backfill misses stops
receiving messages — an outage rather than a failing test, which is why this is
rehearsed by hand on real data before the migration ships.

The check is the only one that matters: every sender that routes BEFORE the
migration still routes to the same household AFTER it.

    python3 scripts/rehearse_0012_routing.py /path/to/copy-of-control-plane.db

Works on a COPY. It applies migrations, so never point it at a live file.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control_plane.db import ControlPlaneDatabase  # noqa: E402


def _routable(database: ControlPlaneDatabase, published_only: bool) -> dict[str, str]:
    """Which sender reaches which household, by the gateway's own rule."""
    clause = " WHERE published_revision IS NOT NULL" if published_only else ""
    rows = database.query(
        "SELECT channel, external_id, household_id FROM channel_bindings" + clause
    )
    seen: dict[str, str] = {}
    for row in rows:
        key = f"{row['channel']}:{row['external_id']}"
        # Two households on one sender is `ambiguous_sender` at the gateway:
        # denied for both, so it is not routable either way.
        seen[key] = "<ambiguous>" if key in seen else row["household_id"]
    return {k: v for k, v in seen.items() if v != "<ambiguous>"}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    source = Path(argv[1])
    if not source.is_file():
        print(f"not a file: {source}")
        return 2

    work = Path(tempfile.mkdtemp()) / "rehearsal.db"
    shutil.copy2(source, work)
    database = ControlPlaneDatabase(work)
    try:
        pending = database.pending_migrations()
        if "0012_channel_binding_published.sql" not in pending:
            print("0012 is not pending on this database; nothing to rehearse.")
            print(f"pending: {pending or 'none'}")
            return 2

        before = _routable(database, published_only=False)
        applied = database.migrate()
        after = _routable(database, published_only=True)

        lost = {k: v for k, v in before.items() if k not in after}
        moved = {k: (v, after[k]) for k, v in before.items() if after.get(k) not in (None, v)}

        print(f"applied: {', '.join(applied)}")
        print(f"routable before: {len(before)}")
        print(f"routable after:  {len(after)}")
        if lost:
            print(f"\nWOULD GO DARK ({len(lost)}):")
            for key, household in sorted(lost.items())[:20]:
                print(f"  {key} -> {household}")
        if moved:
            print(f"\nWOULD ROUTE ELSEWHERE ({len(moved)}):")
            for key, (was, now) in sorted(moved.items())[:20]:
                print(f"  {key}: {was} -> {now}")
        if not lost and not moved:
            print("\nOK — every sender that routed before still routes to the "
                  "same household.")
            return 0
        return 1
    finally:
        database.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

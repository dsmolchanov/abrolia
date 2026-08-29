from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path

from control_plane.config import ControlPlaneConfig
from control_plane.container import ControlPlaneContainer
from control_plane.provisioning.bootstrap import BootstrapDenied, BootstrapGone

TOKEN_PATH = Path("/data/.phase-b-retained-bootstrap")
ACCOUNT_ID = "5c413546-717e-4984-b119-fe0701dc9fde"
HOUSEHOLD_ID = "3d7ebc3a-fd59-416a-b6c3-b9c6467f6a5e"
RUNTIME_REF = "abrolia-hh-hv7lyox5lfawvnwdxhdem73kly"


def service_pid() -> int:
    for item in Path("/proc").iterdir():
        if not item.name.isdigit() or int(item.name) == os.getpid():
            continue
        try:
            command = (item / "cmdline").read_bytes().replace(b"\x00", b" ")
        except OSError:
            continue
        if b"abrolia-control-plane serve" in command:
            return int(item.name)
    raise RuntimeError("control-plane service process not found")


def main() -> None:
    pid = service_pid()
    os.kill(pid, signal.SIGSTOP)
    active = None
    try:
        raw_token = TOKEN_PATH.read_text(encoding="ascii")
        active = ControlPlaneContainer.build(
            ControlPlaneConfig.from_env(), acquire_process_lock=False
        )
        account = active.accounts.get(ACCOUNT_ID)
        if account is None:
            replay_status = "unexpected_success"
            try:
                active.bootstrap.claim(
                    raw_token,
                    household_id=HOUSEHOLD_ID,
                    runtime_ref=RUNTIME_REF,
                    config_revision=1,
                )
            except (BootstrapDenied, BootstrapGone) as error:
                replay_status = error.__class__.__name__
            if replay_status == "unexpected_success":
                raise AssertionError("deleted bootstrap credential was accepted")
            raw_token = ""
            TOKEN_PATH.unlink()
            print(json.dumps({
                "deleted_household_id": HOUSEHOLD_ID,
                "bootstrap_replay": replay_status,
                "result": "replay_cleanup_pass",
            }, sort_keys=True))
            return
        email = account.recovery_email
        first = active.deletion.delete(
            ACCOUNT_ID,
            HOUSEHOLD_ID,
            idempotency_key="phase-b-retained-bootstrap-delete",
        )
        final_status = first.completion_status
        for _ in range(60):
            active.worker.drain(limit=20)
            resumed = active.deletion.resume_pending(limit=10)
            if resumed:
                final_status = resumed[-1].completion_status
            tombstone = active.database.query_one(
                "SELECT completion_status FROM deletion_tombstones"
                " WHERE household_id_hmac = ?",
                (active.lookup.digest(HOUSEHOLD_ID),),
            )
            if tombstone is not None and tombstone["completion_status"] == "complete":
                final_status = "complete"
                break
            time.sleep(0.5)
        if final_status != "complete":
            raise RuntimeError(f"deletion did not converge: {final_status}")

        replay_status = "unexpected_success"
        try:
            active.bootstrap.claim(
                raw_token,
                household_id=HOUSEHOLD_ID,
                runtime_ref=RUNTIME_REF,
                config_revision=1,
            )
        except (BootstrapDenied, BootstrapGone) as error:
            replay_status = error.__class__.__name__
        if replay_status == "unexpected_success":
            raise AssertionError("deleted bootstrap credential was accepted")
        raw_token = ""
        TOKEN_PATH.unlink()

        replacement_account = active.accounts.create_verified(email)
        replacement = active.households.create_for_owner(replacement_account.id)
        if replacement.id == HOUSEHOLD_ID:
            raise AssertionError("deleted household identity was reused")
        tombstone = active.database.query_one(
            "SELECT completion_status FROM deletion_tombstones"
            " WHERE household_id_hmac = ?",
            (active.lookup.digest(HOUSEHOLD_ID),),
        )
        if tombstone is None or tombstone["completion_status"] != "complete":
            raise AssertionError("old tombstone was not retained")
        replacement_delete = active.deletion.delete(
            replacement_account.id,
            replacement.id,
            idempotency_key="phase-b-replacement-cleanup",
        )
        if replacement_delete.completion_status != "complete":
            raise AssertionError("replacement cleanup did not complete")
        print(json.dumps({
            "deleted_household_id": HOUSEHOLD_ID,
            "deletion_status": final_status,
            "bootstrap_replay": replay_status,
            "replacement_household_id": replacement.id,
            "replacement_cleanup": replacement_delete.completion_status,
            "tombstone_status": tombstone["completion_status"],
            "result": "pass",
        }, sort_keys=True))
    finally:
        if active is not None:
            active.close()
        os.kill(pid, signal.SIGCONT)


if __name__ == "__main__":
    main()

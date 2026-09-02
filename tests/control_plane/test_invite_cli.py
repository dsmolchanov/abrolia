"""The operator `invite` command must not require stopping production."""

from __future__ import annotations

import json

import pytest


def test_invite_runs_while_the_service_holds_the_writer_lock(tmp_path, capsys) -> None:
    """`serve` owns the nonblocking flock for the life of the process.

    An invite that takes the same lock can only be issued with production
    stopped, which made every tester admitted cost a restart. The command is
    one token row and one line of operator output; it belongs beside
    `withdraw-consent`, outside the lock.
    """
    from control_plane.cli import main
    from control_plane.config import ControlPlaneConfig
    from control_plane.container import ControlPlaneContainer

    config = ControlPlaneConfig.for_test(tmp_path)
    monkey = pytest.MonkeyPatch()
    monkey.setattr(ControlPlaneConfig, "from_env", staticmethod(lambda: config))
    try:
        # Stand in for the running service: hold the writer flock.
        with ControlPlaneContainer.build(config, acquire_process_lock=True) as serving:
            assert main(["invite", "tester@example.test"]) == 0
            out = capsys.readouterr().out.splitlines()
            # The link is printed once, to stdout, for the operator — then the
            # JSON receipt. Neither goes through the application logger.
            assert out[0].startswith("synthetic invite link for tester@example.test: ")
            assert "/auth/verify#token=" in out[0]
            receipt = json.loads(out[1])
            assert receipt["status"] == "issued"
            # The running service sees the token the CLI wrote: same database,
            # and it consumes as an invite.
            token = out[0].rsplit("#token=", 1)[1]
            record = serving.auth.consume_token(token, purpose="invite")
            assert record.purpose == "invite"
    finally:
        monkey.undo()


def test_invite_still_refuses_non_test_recipients(tmp_path) -> None:
    from control_plane.cli import main
    from control_plane.config import ControlPlaneConfig

    config = ControlPlaneConfig.for_test(tmp_path)
    monkey = pytest.MonkeyPatch()
    monkey.setattr(ControlPlaneConfig, "from_env", staticmethod(lambda: config))
    try:
        with pytest.raises(ValueError, match="reserved .test addresses"):
            main(["invite", "person@example.com"])
    finally:
        monkey.undo()

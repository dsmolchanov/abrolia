"""The gate is CALLED from here, never carried.

The behavioural suite — blockers red, missing verdict red, API failure red,
quota exhaustion red, fork-token failure red — lives beside the code it executes
in dsmolchanov/codex-review-gate and runs in that repository's CI. Duplicating
it here would assert against a file this repository does not ship, which is why
the local copy was retired.

What IS this repository's contract is the caller, and these are the two ways it
can silently stop being a gate at all.
"""

import pathlib
import re

import yaml

GATE = (
    pathlib.Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "codex-review-window.yml"
)
DOC = yaml.safe_load(GATE.read_text(encoding="utf-8"))
JOB = DOC["jobs"]["codex-review-window"]

HOST = "dsmolchanov/codex-review-gate"


def test_the_gate_is_called_not_copied():
    """A pasted body puts this file in front of a reviewer that cannot run it.

    Copying the gate into eight repositories produced 21 blocking findings on
    one file, no two repositories agreeing.
    """
    assert "uses" in JOB, "the gate job has no `uses:` — the body was pasted back"
    assert JOB["uses"].startswith(HOST + "/"), JOB["uses"]
    assert "steps" not in JOB


def test_the_host_is_pinned_to_a_commit():
    """A mutable ref hands this repo's secrets to whatever it later points at.

    The caller forwards CODEX_REQUEST_TOKEN, SLACK_WEBHOOK and a write-capable
    workflow token. Retagging the host — by compromise or by accident — would
    replace the code holding them with no diff here to review.
    """
    ref = JOB["uses"].split("@", 1)[1]
    assert re.fullmatch(r"[0-9a-f]{40}", ref), f"not a pinned commit SHA: {ref}"


def test_the_reported_check_context_is_two_part():
    """Branch protection must require `<caller job> / <called job>`.

    GitHub names a reusable workflow's check run after both halves. Renaming
    either one leaves protection waiting on a context nothing reports, and every
    pull request blocks — which looks exactly like a hung gate.
    """
    assert "codex-review-window" in DOC["jobs"], list(DOC["jobs"])
    assert len(DOC["jobs"]) == 1, "a second job would change nothing, but say why here"

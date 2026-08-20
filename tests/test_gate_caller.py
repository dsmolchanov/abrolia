"""The gate is CALLED from here, never carried.

The behavioural suite — blockers red, missing verdict red, API failure red,
quota exhaustion red, fork-token failure red — lives beside the code it executes
in dsmolchanov/codex-review-gate and runs in that repository's CI. Duplicating
it here would assert against a file this repository does not ship, which is why
the local copy was retired.

What IS this repository's contract is the caller, and these are the two ways it
can silently stop being a gate at all.

Deliberately NO `import yaml`. An earlier revision of this file used PyYAML,
which this repository does not declare in any manifest; on a clean runner the
import would raise ModuleNotFoundError during collection and take the WHOLE
suite down — a test meant to protect merge authorization would have broken it.
Adding a dependency to every consumer to check two lines is the wrong trade, so
these assertions read the text.
"""

import pathlib
import re

GATE = (
    pathlib.Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "codex-review-window.yml"
)
TEXT = GATE.read_text(encoding="utf-8")

HOST = "dsmolchanov/codex-review-gate"
# The ref carries a trailing `  # v1` comment for humans, so anchoring at
# end-of-line matches nothing. Capture the token and stop.
USES = re.search(r"^\s*uses:\s*(\S+)", TEXT, re.M)


def test_the_gate_is_called_not_copied():
    """A pasted body puts this file in front of a reviewer that cannot run it.

    Copying the gate into eight repositories produced 21 blocking findings on
    one file, no two repositories agreeing, several asking for tests that
    existed in two of the eight.
    """
    assert USES, "no `uses:` line — the gate body was pasted back in"
    assert USES.group(1).startswith(HOST + "/"), USES.group(1)
    # The gate's own shell is the tell-tale of a copy.
    assert "gh api" not in TEXT, "the gate body was copied instead of called"


def test_the_host_is_pinned_to_a_commit():
    """A mutable ref hands this repo's secrets to whatever it later points at.

    The caller forwards CODEX_REQUEST_TOKEN, SLACK_WEBHOOK and a write-capable
    workflow token. Retagging the host — by compromise or by accident — would
    replace the code holding them, with no diff here to review.
    """
    assert USES, "no `uses:` line"
    ref = USES.group(1).split("@", 1)[1]
    assert re.fullmatch(r"[0-9a-f]{40}", ref), f"not a pinned commit SHA: {ref}"


def test_the_calling_job_keeps_the_name_branch_protection_requires():
    """Branch protection requires `codex-review-window / codex-review-window`.

    GitHub names a reusable workflow's check run `<caller job> / <called job>`.
    Renaming the caller job leaves protection waiting on a context nothing
    reports, and every pull request blocks — which looks exactly like a hung
    gate rather than a misconfiguration.
    """
    assert re.search(r"^  codex-review-window:\s*$", TEXT, re.M), (
        "the calling job key changed; branch protection would wait forever"
    )

"""Static invariants of the `codex-review-window` gate.

`tests/test_gate_behavior.py` executes the script and asserts exit codes. This
module asserts the properties that are cheaper to read than to run — above all
the number of ways the script can say "may merge".

Every assertion here exists because the property it names was broken at least
once. `AGENTS.md` requires the deterministic check after the second occurrence
of a class; the clean-verdict contract reached three in one review.
"""

from __future__ import annotations

import re
import subprocess

import pytest

from tests.gate_support import WORKFLOW, gate_script


@pytest.fixture(scope="module")
def script() -> str:
    return gate_script()


def test_exactly_two_exit_zero_paths(script: str) -> None:
    """Each `exit 0` is a merge authorisation. Adding one is a policy change.

    An earlier revision carried a third — a `fast-merge` repository topic that
    disabled the gate for every pull request, indefinitely, before any verdict
    was read. If this fails because a path was added deliberately, update the
    audit comment in the workflow header in the same commit.
    """
    assert script.count("exit 0") == 2


def test_no_repository_wide_bypass(script: str) -> None:
    """No opt-out may be readable from repository or PR metadata.

    A topic, a label, or a title prefix is unattributable — `GITHUB_TOKEN`
    cannot tell who set it — so none of them may open the gate.
    """
    assert "repos/${REPO}/topics" not in script
    assert "fast-merge" not in script


def test_clean_comment_requires_the_verbatim_verdict(script: str) -> None:
    """Absence of a severity marker is not an approval.

    Codex posts head-naming comments that are not verdicts: quota notices,
    "unable to review" errors, progress updates. None carries a marker, so a
    predicate keyed on marker-absence promoted all of them to approvals.
    """
    assert 'CLEAN_VERDICT="Codex Review: Didn\'t find any major issues."' in script
    assert 'contains(\\"${CLEAN_VERDICT}\\")' in script
    assert 'contains(\\"Reviewed commit\\")' not in script


def test_clean_signal_is_rechecked_for_a_late_review(script: str) -> None:
    """A formal review published moments later must still win.

    The wait loop breaks the instant a clean signal appears; without a re-query
    a P1 review landing seconds afterwards loses the race to auto-merge.
    """
    grace = script.index('sleep "${GRACE_SECONDS:-15}"')
    requery = script.index('RIDS="$(review_ids_for_head)"', grace)
    clean_exit = script.index("Codex reports no findings", requery)
    assert grace < requery < clean_exit


def test_severity_patterns_cover_p0_and_stay_in_lockstep(script: str) -> None:
    """A P0-only review must not read as marker-free.

    The shell and jq spellings differ only in backslash doubling; when they
    drift, one of the two verdict reads silently stops matching.
    """
    pattern = re.search(r"^\s*PATTERN='([^']+)'", script, re.M)
    jq_pattern = re.search(r"^\s*JQ_PATTERN='([^']+)'", script, re.M)
    assert pattern and jq_pattern
    assert pattern.group(1) == jq_pattern.group(1).replace("\\\\", "\\")
    for spelling in ("P[01] Badge", "badge/P[01]"):
        assert spelling in pattern.group(1)


def test_review_request_covers_every_event_that_can_cancel_a_requester(
    script: str,
) -> None:
    """`submitted` must request too.

    `cancel-in-progress` is keyed on the PR number, so a `pull_request_review`
    event cancels the synchronize run that would have asked. If `submitted`
    does not ask in its place, nobody asked for the current head and the gate
    times out on a verdict that was never requested.
    """
    case = re.search(r'case "\$\{ACTION\}" in\n\s*([a-z_|]+)\)', script)
    assert case
    actions = set(case.group(1).split("|"))
    assert {"synchronize", "reopened", "ready_for_review", "submitted"} <= actions


def test_review_request_uses_a_codex_connected_identity(script: str) -> None:
    """Codex serves requests per identity, and refuses `github-actions[bot]`.

    The anchor must go out under `CODEX_REQUEST_TOKEN` when it is configured,
    and `ANCHOR_AUTHOR` must follow it — an anchor looked up under the wrong
    author is never found, so every run would request a redundant review.
    """
    assert "CODEX_REQUEST_TOKEN: ${{ secrets.CODEX_REQUEST_TOKEN }}" in (
        WORKFLOW.read_text(encoding="utf-8")
    )
    assert 'GH_TOKEN="${REQUEST_TOKEN:-${GH_TOKEN}}"' in script
    assert 'ANCHOR_AUTHOR="${REQUEST_LOGIN}"' in script


def test_gating_step_is_unconditional() -> None:
    """A skipped required check SATISFIES branch protection.

    So the step that decides may never carry an `if:`, and the job's only
    condition may be the draft check.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    step = text[text.index("- name: Gate on Codex verdict for this commit") :]
    assert "\n        if:" not in step
    assert text.count("    if: ${{ !github.event.pull_request.draft }}") == 1


def test_script_is_valid_shell(script: str, tmp_path) -> None:
    path = tmp_path / "gate.sh"
    path.write_text(script, encoding="utf-8")
    result = subprocess.run(
        ["bash", "-n", str(path)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_api_wrapper_terminates_its_output(script: str) -> None:
    """`api()` feeds `wc -l`, which counts newlines, not results.

    An unterminated last line made the clean-comment count one short, so a
    single clean verdict — the ordinary case — counted as zero and the
    clean-verdict path could never fire.
    """
    assert "printf '%s\\n' \"${out}\"" in script
    assert "printf '%s' \"${out}\"" not in script

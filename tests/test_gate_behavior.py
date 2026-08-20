"""Execute the `codex-review-window` gate and assert its exit code.

`tests/test_gate_workflow.py` reads the script. This module RUNS it, against a
stubbed GitHub API, because the only property that matters is the exit code and
a grep cannot tell you what a `while` loop and three `if`s do together.

The stub speaks real jq: the gate's verdict reads are jq programs, and two of
the three false-green defects this suite exists for lived inside a jq filter,
not in the shell around it. A stub that pattern-matched the filter instead of
running it would have passed on all three.

Exit 0 means "this pull request may merge", so every case below states which of
the two authorised paths it takes, or which fail-closed path denies it.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.gate_support import gate_script, jq_available

pytestmark = pytest.mark.skipif(
    not jq_available(), reason="the gate's verdict reads are jq programs"
)

HEAD = "a1b2c3d4e5" + "f" * 30
HEAD_SHORT = HEAD[:10]
OTHER_HEAD = "9" * 40
CODEX = "chatgpt-codex-connector[bot]"
CLEAN_BODY = f"Codex Review: Didn't find any major issues.\n\nReviewed commit: {HEAD_SHORT}"
P1_BADGE = "![P1 Badge](https://img.shields.io/badge/P1-orange?style=flat) [BLOCKER] x"
P0_BADGE = "![P0 Badge](https://img.shields.io/badge/P0-red?style=flat) [BLOCKER] x"

# A `gh` that answers from a fixture file and logs every call. Sequenced
# responses (`__sequence__`) let a test make a review appear only on a later
# query, which is how the late-review race is reproduced.
GH_STUB = r'''#!/usr/bin/env python3
import json, os, subprocess, sys

argv = sys.argv[1:]
if not argv or argv[0] != "api":
    sys.exit("gh stub: only `gh api` is supported, got %r" % (argv,))

endpoint = jq_filter = None
fields = {}
include_headers = False
args = argv[1:]
i = 0
while i < len(args):
    a = args[i]
    if a == "--jq":
        jq_filter = args[i + 1]; i += 2
    elif a == "-f":
        k, _, v = args[i + 1].partition("="); fields[k] = v; i += 2
    elif a == "-H":
        i += 2
    elif a == "--paginate":
        i += 1
    elif a == "-i":
        include_headers = True; i += 1
    elif a.startswith("-"):
        i += 1
    else:
        endpoint = a; i += 1

state = json.load(open(os.environ["GATE_STUB_STATE"]))
counts_path = os.environ["GATE_STUB_COUNTS"]
try:
    counts = json.load(open(counts_path))
except Exception:
    counts = {}
nth = counts.get(endpoint, 0)
counts[endpoint] = nth + 1
json.dump(counts, open(counts_path, "w"))

with open(os.environ["GATE_STUB_LOG"], "a") as log:
    log.write(json.dumps({
        "endpoint": endpoint,
        "token": os.environ.get("GH_TOKEN", ""),
        "fields": fields,
    }) + "\n")

if endpoint in state.get("errors", []):
    sys.stderr.write("gh stub: simulated API failure for %s\n" % endpoint)
    sys.exit(1)

if fields:
    data = state.get("post_response", {"id": 4242})
    if include_headers:
        status = str(data.get("status", "201")) if isinstance(data, dict) else "201"
        headers = ["HTTP/2.0 %s" % status]
        for name, value in (state.get("post_headers") or {}).items():
            headers.append("%s: %s" % (name, value))
        sys.stdout.write("\n".join(headers) + "\n\n" + json.dumps(data))
        sys.exit(0)
else:
    if endpoint not in state["responses"]:
        sys.stderr.write("gh stub: no fixture for %s\n" % endpoint)
        sys.exit(1)
    data = state["responses"][endpoint]
    if isinstance(data, dict) and "__sequence__" in data:
        seq = data["__sequence__"]
        data = seq[min(nth, len(seq) - 1)]

payload = json.dumps(data)
if jq_filter is None:
    sys.stdout.write(payload)
    sys.exit(0)

proc = subprocess.run(
    ["jq", "-r", jq_filter], input=payload, capture_output=True, text=True
)
if proc.returncode != 0:
    sys.stderr.write(proc.stderr)
    sys.exit(1)
sys.stdout.write(proc.stdout)
'''


class GateResult:
    def __init__(self, proc: subprocess.CompletedProcess, calls: list[dict]) -> None:
        self.returncode = proc.returncode
        self.output = proc.stdout + proc.stderr
        self.calls = calls

    @property
    def may_merge(self) -> bool:
        return self.returncode == 0

    def posted_comments(self) -> list[dict]:
        return [c for c in self.calls if c["fields"]]


@pytest.fixture
def run_gate(tmp_path: Path):
    script = tmp_path / "gate.sh"
    script.write_text(gate_script(), encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "gh"
    stub.write_text(GH_STUB, encoding="utf-8")
    stub.chmod(0o755)

    state_path = tmp_path / "state.json"
    log_path = tmp_path / "calls.jsonl"
    counts_path = tmp_path / "counts.json"

    def _run(
        *,
        comments: list[dict] | None = None,
        reviews: object = None,
        inline: list[dict] | None = None,
        review_bodies: dict[str, str] | None = None,
        reactions: list[dict] | None = None,
        pr_reactions: list[dict] | None = None,
        errors: list[str] | None = None,
        post_response: object | None = None,
        post_headers: dict[str, str] | None = None,
        action: str = "opened",
        request_token: str = "",
        user_login: str = "codex-connected-human",
        env: dict[str, str] | None = None,
    ) -> GateResult:
        responses: dict[str, object] = {
            "repos/o/r/pulls/1": {"head": {"sha": HEAD}, "number": 1},
            f"repos/o/r/commits/{HEAD}": {
                "commit": {"committer": {"date": "2026-01-01T00:00:00Z"}}
            },
            "repos/o/r/issues/1/comments": comments or [],
            "repos/o/r/pulls/1/reviews": reviews if reviews is not None else [],
            "repos/o/r/pulls/1/comments": inline or [],
            "repos/o/r/issues/1/reactions": pr_reactions or [],
            "repos/o/r/issues/comments/4242/reactions": reactions or [],
            "user": {"login": user_login},
            "repos/o/r": {"full_name": "o/r"},
        }
        for rid, body in (review_bodies or {}).items():
            responses[f"repos/o/r/pulls/1/reviews/{rid}"] = {"body": body}

        state = {"responses": responses, "errors": errors or []}
        if post_response is not None:
            state["post_response"] = post_response
        if post_headers is not None:
            state["post_headers"] = post_headers
        state_path.write_text(json.dumps(state), encoding="utf-8")
        log_path.write_text("", encoding="utf-8")
        counts_path.write_text("{}", encoding="utf-8")

        environment = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GATE_STUB_STATE": str(state_path),
            "GATE_STUB_LOG": str(log_path),
            "GATE_STUB_COUNTS": str(counts_path),
            "GH_TOKEN": "workflow-token",
            "REPO": "o/r",
            "PR": "1",
            "CODEX_BOT": CODEX,
            "ACTION": action,
            "ANCHOR_AUTHOR": "github-actions[bot]",
            "SLACK_WEBHOOK": "",
            "RUN_URL": "http://run",
            "CODEX_REQUEST_TOKEN": request_token,
            # Every wait collapses: the tests assert ordering, not duration.
            "DEBOUNCE_SECONDS": "0",
            "VERDICT_WINDOW_SECONDS": "0",
            "GRACE_SECONDS": "0",
            "SETTLE_SECONDS": "0",
            **(env or {}),
        }
        proc = subprocess.run(
            ["bash", str(script)],
            capture_output=True,
            text=True,
            env=environment,
            timeout=120,
        )
        calls = [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        return GateResult(proc, calls)

    return _run


def comment(body: str, *, user: str = CODEX, cid: int = 1) -> dict:
    return {"id": cid, "user": {"login": user}, "body": body,
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"}


def review(rid: int, sha: str = HEAD) -> dict:
    return {"id": rid, "user": {"login": CODEX}, "commit_id": sha}


# --------------------------------------------------------------------------
# The two authorised exit-0 paths.
# --------------------------------------------------------------------------


def test_clean_verdict_comment_allows_merge(run_gate) -> None:
    """Authorised path 1: the verbatim verdict, naming this head, no review."""
    result = run_gate(comments=[comment(CLEAN_BODY)])
    assert result.may_merge
    assert "Codex reports no findings" in result.output


def test_review_without_a_severity_marker_allows_merge(run_gate) -> None:
    """Authorised path 2: a head-bound review that found nothing major."""
    result = run_gate(
        reviews=[review(77)],
        inline=[{"pull_request_review_id": 77, "body": "nit: rename this"}],
        review_bodies={"77": "Looks reasonable."},
    )
    assert result.may_merge
    assert "Codex review is green" in result.output


# --------------------------------------------------------------------------
# The three false-green defects. Each of these merged an unreviewed head.
# --------------------------------------------------------------------------


def test_head_naming_comment_that_is_not_a_verdict_blocks(run_gate) -> None:
    """Codex saying it COULD NOT review is not Codex saying the diff is clean.

    This is the defect that mattered most in practice: the identity error Codex
    returns to an unconnected requester names the commit and carries no severity
    marker, so a marker-absence predicate turned every rejection into approval.
    """
    result = run_gate(
        comments=[
            comment(
                "To use Codex here, create a Codex account and connect to github. "
                f"Reviewed commit: {HEAD_SHORT}"
            )
        ]
    )
    assert not result.may_merge
    assert "No Codex verdict" in result.output


def test_review_arriving_during_the_grace_window_overrides_a_clean_signal(
    run_gate,
) -> None:
    """The late-review race: a P1 published moments after the clean comment."""
    result = run_gate(
        comments=[comment(CLEAN_BODY)],
        reviews={"__sequence__": [[], [review(88)]]},
        inline=[{"pull_request_review_id": 88, "body": P1_BADGE}],
        review_bodies={"88": ""},
    )
    assert not result.may_merge
    assert "arrived during the grace window" in result.output
    assert "P0/P1/[BLOCKER] finding(s)" in result.output


def test_no_repository_topic_can_open_the_gate(run_gate) -> None:
    """The removed `fast-merge` bypass: the gate must not read repo topics."""
    result = run_gate()
    assert not result.may_merge
    assert not any(c["endpoint"] == "repos/o/r/topics" for c in result.calls)


# --------------------------------------------------------------------------
# Fail-closed paths.
# --------------------------------------------------------------------------


def test_p1_review_blocks(run_gate) -> None:
    result = run_gate(
        reviews=[review(90)],
        inline=[{"pull_request_review_id": 90, "body": P1_BADGE}],
        review_bodies={"90": ""},
    )
    assert not result.may_merge


def test_p0_review_blocks(run_gate) -> None:
    """A P0-only review was the one class that used to pass."""
    result = run_gate(
        reviews=[review(91)],
        inline=[{"pull_request_review_id": 91, "body": P0_BADGE}],
        review_bodies={"91": ""},
    )
    assert not result.may_merge


def test_finding_in_the_review_body_alone_blocks(run_gate) -> None:
    """A body-only finding has no inline comment to fall back on."""
    result = run_gate(
        reviews=[review(92)],
        inline=[],
        review_bodies={"92": P1_BADGE},
    )
    assert not result.may_merge


def test_absent_verdict_blocks(run_gate) -> None:
    result = run_gate()
    assert not result.may_merge
    assert "The verdict is UNKNOWN" in result.output


def test_verdict_for_another_head_blocks(run_gate) -> None:
    """A review of a superseded commit says nothing about this one."""
    result = run_gate(
        reviews=[review(93, sha=OTHER_HEAD)],
        comments=[comment("Codex Review: Didn't find any major issues.\nReviewed commit: 9999999999")],
    )
    assert not result.may_merge


def test_clean_verdict_from_a_non_codex_author_blocks(run_gate) -> None:
    """Anyone can type the sentence; only Codex's account means it."""
    result = run_gate(comments=[comment(CLEAN_BODY, user="passer-by")])
    assert not result.may_merge


def test_api_failure_blocks(run_gate) -> None:
    """An empty result from a swallowed error reads exactly like `no findings`."""
    result = run_gate(
        comments=[comment(CLEAN_BODY)],
        errors=["repos/o/r/pulls/1/reviews"],
    )
    assert not result.may_merge
    assert "GitHub API call failed" in result.output


def test_pr_level_thumbs_up_is_not_a_verdict_for_this_head(run_gate) -> None:
    """A reaction carries no commit id, so it cannot be attributed to a head."""
    result = run_gate(pr_reactions=[{"id": 5, "user": {"login": CODEX}, "content": "+1"}])
    assert not result.may_merge
    assert "cannot be attributed" in result.output


# --------------------------------------------------------------------------
# Requesting the review at all — the failure that stalled four PRs for six days.
# --------------------------------------------------------------------------


def test_submitted_requests_a_review_when_none_exists_for_this_head(run_gate) -> None:
    """`pull_request_review` cancels the synchronize run that would have asked.

    So the replacement must ask in its place, or nobody does.
    """
    result = run_gate(action="submitted")
    posted = result.posted_comments()
    assert len(posted) == 1
    assert posted[0]["fields"]["body"].startswith("@codex review")


def test_request_goes_out_under_the_codex_connected_identity(run_gate) -> None:
    """Codex refuses `github-actions[bot]`, so the PAT must carry the request."""
    result = run_gate(action="synchronize", request_token="pat-for-connected-user")
    posted = result.posted_comments()
    assert len(posted) == 1
    assert posted[0]["token"] == "pat-for-connected-user"
    assert "codex-connected-human" in result.output


def test_unauthenticated_request_token_blocks(run_gate) -> None:
    """A rotated PAT must fail loudly, not fall back to the refused identity."""
    result = run_gate(
        action="synchronize",
        request_token="expired",
        errors=["user"],
    )
    assert not result.may_merge
    assert "does not authenticate" in result.output


def test_existing_review_for_this_head_is_not_re_requested(run_gate) -> None:
    result = run_gate(
        action="synchronize",
        reviews=[review(94)],
        inline=[],
        review_bodies={"94": ""},
    )
    assert result.may_merge
    assert result.posted_comments() == []


def test_a_rejected_anchor_post_is_not_announced_as_success(run_gate) -> None:
    """`--jq .id` on an error emits the error body, not nothing.

    Observed in production: a `CODEX_REQUEST_TOKEN` missing the right permission
    gets 403 "Resource not accessible by personal access token". The gate
    captured that JSON as the comment id, logged `Posted ... (comment
    {"message":"Resource not accessible...})`, and then timed out 900s later with
    nothing to explain why. A gate whose whole purpose is stopping errors from
    reading as success was doing it to itself.
    """
    result = run_gate(
        action="synchronize",
        request_token="pat-without-the-right-permission",
        post_response={
            "message": "Resource not accessible by personal access token",
            "status": "403",
        },
    )

    assert not result.may_merge
    assert "Could not post the anchor comment" in result.output
    assert "Resource not accessible" in result.output
    assert "Posted an inventory-scoped" not in result.output


def test_a_successful_anchor_post_is_still_recognised(run_gate) -> None:
    """The counterpart: a real id must still be accepted.

    `-i` prepends response headers, so the id extraction has to find the JSON
    body among them — a success wrongly read as a failure would cost a review
    generation on every push.
    """
    result = run_gate(
        action="synchronize",
        request_token="pat-with-the-right-permission",
        post_response={"id": 5334512611},
        post_headers={"x-github-request-id": "ABC:123"},
    )

    assert "Posted an inventory-scoped '@codex review' (comment 5334512611)" in (
        result.output
    )
    assert "Could not post the anchor comment" not in result.output


def test_the_failure_quotes_githubs_own_permission_answer(run_gate) -> None:
    """GitHub says which permission would work; the gate should not guess.

    An earlier version of this probe tested whether the token could READ the
    repository and the pull request. On a public repository both are true for an
    unauthenticated request, so the checks discriminated nothing — and the
    conclusion drawn from them, that a permission was present but read-only, was
    wrong. `x-accepted-github-permissions` is the API answering directly.
    """
    result = run_gate(
        action="synchronize",
        request_token="pat-without-pull-requests",
        post_response={
            "message": "Resource not accessible by personal access token",
            "status": "403",
        },
        post_headers={
            "x-accepted-github-permissions": "issues=write; pull_requests=write"
        },
    )

    assert "would be accepted with: issues=write; pull_requests=write" in (
        result.output
    )
    assert "Read and write" in result.output
    # And it must not resurrect the read probes that proved nothing.
    assert "can read" not in result.output


def test_a_failure_without_the_header_still_says_what_to_check(run_gate) -> None:
    """No header — say what to check rather than inventing a diagnosis."""
    result = run_gate(
        action="synchronize",
        request_token="pat-scoped-to-another-repository",
        post_response={"message": "Not Found", "status": "404"},
    )

    assert "Repository access" in result.output
    assert "Pull requests" in result.output
    assert "would be accepted with" not in result.output

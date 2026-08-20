"""Every changed implementation path must be declared by ONE plan step.

Three separate review rounds have been spent on the same defect: a branch
touched a file its plan did not list, and the omission was found by reading
rather than by running anything. `AGENTS.md` makes an undeclared change a
merge-blocking deviation, so the cheapest place to find one is here.

The rule is *one step covers everything*, not *every path appears somewhere*.
The union of all inventories is far too generous — `control_plane/cli.py` is
declared by Step E9, so under a union an E1 branch could change it and pass
while E1 says nothing about it, which is precisely the class of deviation this
file was added to catch. A branch implements one step; that step's `**Files:**`
block has to account for every implementation path the branch touched.

A branch that genuinely spans two steps fails this, and that is the intended
answer rather than a limitation: the remedy is to declare the path under the
step it belongs to, which is what `AGENTS.md` asks for anyway.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
PLANS = REPOSITORY / "thoughts" / "shared" / "plans"

#: Backticked tokens in an inventory line. Prose outside the backticks —
#: "(if new columns needed)" — is commentary, not a path.
_QUOTED = re.compile(r"`([^`]+)`")
_INVENTORY = re.compile(r"^\*\*Files:\*\*(.*)$", re.MULTILINE)
#: `hermes_cloud/execute/{email_send,nerve_send}.py`, as plans write it.
_BRACES = re.compile(r"\{([^}]*)\}")

#: Paths the plans do not inventory, and are not expected to.
#:
#: `thoughts/` is the record of the work rather than the work: a plan that had
#: to declare its own revision, and every handoff written beside it, would fail
#: this check on the commit that fixed it.
EXEMPT = ("thoughts/",)


def _inventories(plans: Path | None = None) -> list[tuple[str, set[str]]]:
    """Each step's declared paths, kept SEPARATE from every other step's."""
    steps: list[tuple[str, set[str]]] = []
    for plan in sorted((plans or PLANS).glob("*.md")):
        for index, line in enumerate(
            _INVENTORY.findall(plan.read_text(encoding="utf-8"))
        ):
            patterns = {
                token
                for quoted in _QUOTED.findall(line)
                for token in _expand(quoted.strip().rstrip(",."))
            }
            if patterns:
                steps.append((f"{plan.name}#{index + 1}", patterns))
    return steps


def _expand(token: str) -> list[str]:
    match = _BRACES.search(token)
    if match is None:
        return [token]
    return [
        token[: match.start()] + alternative.strip() + token[match.end() :]
        for alternative in match.group(1).split(",")
    ]


def _git(*arguments: str, repository: Path | None = None) -> str | None:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository or REPOSITORY,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _in_ci() -> bool:
    return bool(os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"))


def _is_shallow(repository: Path | None = None) -> bool:
    return _git("rev-parse", "--is-shallow-repository", repository=repository) == "true"


def _merge_base(repository: Path | None = None) -> str | None:
    """Where this branch left the base, deepening the clone if it has to.

    PR CI checks out at depth 1, so neither `origin/main` nor `main` exists and
    the first version of this returned None and SKIPPED — a scope gate that
    reported green while enforcing nothing, in exactly the environment it was
    written for. `GITHUB_BASE_REF` names the base branch on a pull request;
    fetching it, and unshallowing if the histories still do not meet, is what
    makes the comparison possible there.
    """
    base = (os.environ.get("GITHUB_BASE_REF") or "").strip() or "main"
    candidates = [f"origin/{base}", base, "origin/main", "main"]
    # An EXPLICIT refspec, because a depth-1 checkout is also `--single-branch`:
    # its configured refspec covers one branch, so `git fetch origin main`
    # writes FETCH_HEAD and never creates `origin/main`, and the next
    # `merge-base origin/main HEAD` fails exactly as it did before the fetch.
    refspec = f"+refs/heads/{base}:refs/remotes/origin/{base}"
    for attempt in range(3):
        for candidate in candidates:
            found = _git("merge-base", candidate, "HEAD", repository=repository)
            if found:
                return found
        if attempt == 0:
            _git("fetch", "origin", refspec, repository=repository)
        # A shallow fetch stays shallow, so the base can arrive with no common
        # ancestor inside the boundary. Unshallow FIRST, and only when the clone
        # actually is shallow: `--depth` on a complete repository would truncate
        # the operator's own history.
        elif attempt == 1 and _is_shallow(repository):
            _git("fetch", "--unshallow", "origin", repository=repository)
            _git("fetch", "origin", refspec, repository=repository)
    return None


def _changed_paths() -> list[str] | None:
    merge_base = _merge_base()
    if merge_base is None:
        return None
    changed = _git("diff", "--name-only", merge_base, "HEAD")
    if changed is None:
        return None
    return [line for line in changed.splitlines() if line]


def test_every_changed_path_is_declared_by_one_plan_step() -> None:
    changed = _changed_paths()
    if changed is None:
        # FAILS in CI, where a scope gate that cannot run is worse than no gate
        # at all: the check reports green and the deviation merges.
        message = (
            "no merge base with the base branch could be established, so the"
            " plan-scope check cannot run"
        )
        if _in_ci():
            pytest.fail(message)
        pytest.skip(f"{message}; meaningful only on a branch with a base")

    steps = _inventories()
    assert steps, "no plan inventory was parsed, so this check proves nothing"
    subject = [path for path in changed if not path.startswith(EXEMPT)]
    if not subject:
        return

    def uncovered(patterns: set[str]) -> list[str]:
        return [
            path
            for path in subject
            if not any(fnmatch.fnmatch(path, pattern) for pattern in patterns)
        ]

    shortfalls = {name: uncovered(patterns) for name, patterns in steps}
    if any(not missing for missing in shortfalls.values()):
        return

    # The closest step, so the message names what to add rather than making the
    # reader diff two lists themselves.
    closest, missing = min(shortfalls.items(), key=lambda item: len(item[1]))
    raise AssertionError(
        "no single plan step declares every implementation path this branch"
        " changed, which AGENTS.md makes a merge-blocking deviation. The"
        f" closest is {closest}, which does not account for:"
        f" {', '.join(sorted(missing))}"
    )


def test_a_shallow_checkout_still_establishes_the_comparison(tmp_path) -> None:
    """PR CI checks out at depth 1, which is where this has to work.

    Neither `origin/main` nor `main` exists in that clone, and the first version
    of `_changed_paths` answered None and SKIPPED — a scope gate reporting green
    while enforcing nothing, in exactly the environment it was written for. The
    clone here is from the working repository over the filesystem, so this
    exercises the real deepening path without touching the network.
    """
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    if not branch or branch == "main":
        pytest.skip("this only reproduces CI from a branch that is not the base")
    shallow = tmp_path / "shallow"
    # The FEATURE branch, at depth 1. Cloning `main` instead left `origin/main`
    # present and the merge base resolvable on the first try, so the deepening
    # this test exists for never ran. `--depth` implies `--single-branch`, which
    # is what makes the base genuinely absent — the same shape as a
    # `pull_request` checkout.
    cloned = _git(
        "clone", "--depth=1", "--branch", branch, f"file://{REPOSITORY}", str(shallow)
    )
    if cloned is None or not (shallow / ".git").exists():
        pytest.skip("the working repository could not be cloned locally")
    assert _is_shallow(shallow), "the fixture clone is not shallow"
    assert _git("rev-parse", "--verify", "origin/main", repository=shallow) is None, (
        "the fixture clone already has the base, so it does not reproduce CI"
    )
    (shallow / "control_plane" / "undeclared_probe.py").write_text(
        "# a path no plan step declares\n", encoding="utf-8"
    )
    _git("config", "user.email", "probe@example.com", repository=shallow)
    _git("config", "user.name", "probe", repository=shallow)
    _git("add", "-A", repository=shallow)
    _git("commit", "-m", "probe: an undeclared file", repository=shallow)

    assert _merge_base(repository=shallow) is not None, (
        "the shallow clone could not be deepened to reach its base, so the"
        " check would skip in CI rather than reject the undeclared file"
    )


def test_the_check_fails_rather_than_skips_when_the_base_is_unreachable(
    monkeypatch,
) -> None:
    """A gate that cannot run must not report green."""
    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr(
        "tests.control_plane.test_plan_inventory._changed_paths", lambda: None
    )

    # Caught by hand rather than with `pytest.raises`. Both outcomes here are
    # `OutcomeException`s, which descend from `BaseException` and not from
    # `Exception`; worse, a `Skipped` raised anywhere inside a test SKIPS it,
    # `pytest.raises` included — so the version that expected a failure quietly
    # became a skipped test when the behaviour regressed, which is the same
    # green-while-enforcing-nothing this whole check is about.
    try:
        test_every_changed_path_is_declared_by_one_plan_step()
    except pytest.fail.Exception as failure:
        assert "plan-scope check cannot run" in str(failure)
    except BaseException as other:  # noqa: BLE001 - a skip lands here on purpose
        raise AssertionError(
            "a gate that cannot run must fail, not"
            f" {type(other).__name__}: {other}"
        ) from other
    else:
        raise AssertionError(
            "the check passed silently when it could not establish a comparison"
        )


def test_a_path_declared_only_by_another_step_does_not_cover_this_branch(
    tmp_path, monkeypatch
) -> None:
    """The union of every inventory is far too generous.

    `control_plane/cli.py` is declared by Step E9, so under a union an E1 branch
    could change it and pass while E1 says nothing about it — precisely the
    class of deviation this file was added to catch. One step has to account for
    everything the branch touched.
    """
    plans = tmp_path / "plans"
    plans.mkdir()
    (plans / "a-plan.md").write_text(
        "#### Step ONE\n\n"
        "**Files:** `control_plane/onboarding/provision.py`.\n\n"
        "#### Step TWO\n\n"
        "**Files:** `control_plane/cli.py`.\n",
        encoding="utf-8",
    )
    steps = _inventories(plans)
    assert len(steps) == 2, steps
    monkeypatch.setattr(
        "tests.control_plane.test_plan_inventory._inventories", lambda: steps
    )
    monkeypatch.setattr(
        "tests.control_plane.test_plan_inventory._changed_paths",
        lambda: ["control_plane/onboarding/provision.py", "control_plane/cli.py"],
    )

    with pytest.raises(AssertionError, match="no single plan step declares"):
        test_every_changed_path_is_declared_by_one_plan_step()

    # And a branch inside one step still passes, so the rule is not simply
    # rejecting everything.
    monkeypatch.setattr(
        "tests.control_plane.test_plan_inventory._changed_paths",
        lambda: ["control_plane/cli.py"],
    )
    test_every_changed_path_is_declared_by_one_plan_step()

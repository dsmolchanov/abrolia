"""Every changed implementation path must be declared by THIS BRANCH'S plan step.

Three separate review rounds have been spent on the same defect: a branch
touched a file its plan did not list, and the omission was found by reading
rather than by running anything. `AGENTS.md` makes an undeclared change a
merge-blocking deviation, so the cheapest place to find one is here.

Which step that is has to be stated, not inferred. Two earlier versions tried
to infer it and both leaked: the union of every inventory let an E1 branch
change `control_plane/cli.py` because Step E9 declares it, and "some single step
covers the whole diff" has the same hole the moment a diff is small — a one-file
change to `control_plane/onboarding/state.py` is covered by an older phase's
inventory whatever E1 says. Any rule that searches for a step that fits will
find one.

So each step names the branches that implement it, in a `**Branches:**` line
beside its `**Files:**` line, and this reads that. A branch no step claims is a
hard failure with the line to add — the scope of a branch is a fact its author
knows and nothing here can derive.
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
#: A `**Files:**` inventory, to the end of its PARAGRAPH. Anchoring to the end
#: of the LINE read only the first line of a wrapped list — Step E9's runs to
#: four — so most of its declared paths were invisible and every one of them
#: looked undeclared. It failed closed, which is the right direction to be
#: wrong in, and it was still wrong.
_INVENTORY = re.compile(
    r"^\*\*Files:\*\*(.*?)(?=\n[ \t]*\n|\Z)", re.MULTILINE | re.DOTALL
)
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


#: The branches a step is implemented by, beside that step's `**Files:**`.
_BRANCHES = re.compile(r"^\*\*Branches:\*\*(.*)$", re.MULTILINE)
#: Any markdown heading, which is what separates one step from the next.
_HEADING = re.compile(r"^#{2,6} .*$", re.MULTILINE)


def _steps(plans: Path | None = None) -> list[tuple[str, set[str], set[str]]]:
    """Every step that declares files: its name, its paths, and its branches."""
    steps: list[tuple[str, set[str], set[str]]] = []
    for plan in sorted((plans or PLANS).glob("*.md")):
        text = plan.read_text(encoding="utf-8")
        headings = list(_HEADING.finditer(text))
        for index, heading in enumerate(headings):
            end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
            body = text[heading.end() : end]
            patterns = _tokens(_INVENTORY.findall(body))
            if not patterns:
                continue
            steps.append(
                (
                    f"{plan.name} {heading.group().lstrip('# ').strip()}",
                    patterns,
                    _tokens(_BRANCHES.findall(body)),
                )
            )
    return steps


def _tokens(lines: list[str]) -> set[str]:
    return {
        token
        for line in lines
        for quoted in _QUOTED.findall(line)
        for token in _expand(quoted.strip().rstrip(",."))
    }


def _branch_name() -> str:
    """What this branch is called, in CI and out of it.

    `GITHUB_HEAD_REF` is the PR's source branch; a `pull_request` checkout is
    detached, so asking git directly there answers `HEAD` and matches nothing.
    """
    for variable in ("GITHUB_HEAD_REF", "GITHUB_REF_NAME"):
        value = (os.environ.get(variable) or "").strip()
        if value:
            return value
    return _git("rev-parse", "--abbrev-ref", "HEAD") or ""


def test_every_changed_path_is_declared_by_this_branch_s_plan_step() -> None:
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

    steps = _steps()
    assert steps, "no plan inventory was parsed, so this check proves nothing"
    subject = [path for path in changed if not path.startswith(EXEMPT)]
    if not subject:
        return

    branch = _branch_name()
    applicable = [
        (name, patterns) for name, patterns, branches in steps if branch in branches
    ]
    if len(applicable) != 1:
        pytest.fail(
            f"{len(applicable)} plan steps claim the branch `{branch}`, and the"
            " scope of a change is not something this can infer. Add"
            f" `**Branches:** `{branch}`.` beside the `**Files:**` line of the"
            " step this branch implements — exactly one of them."
        )

    name, patterns = applicable[0]
    undeclared = sorted(
        path
        for path in subject
        if not any(fnmatch.fnmatch(path, pattern) for pattern in patterns)
    )
    assert not undeclared, (
        f"{name} is the step this branch implements, and it does not declare"
        " these changed paths — which AGENTS.md makes a merge-blocking"
        " deviation. Another step declaring them is not enough; add them here"
        f" with the reason: {', '.join(undeclared)}"
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
        test_every_changed_path_is_declared_by_this_branch_s_plan_step()
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


def test_a_path_declared_only_by_another_step_is_still_undeclared(
    tmp_path, monkeypatch
) -> None:
    """Searching for a step that fits will always find one.

    `control_plane/onboarding/state.py` is declared by an older phase, so a
    one-file E1 change to it passed both earlier rules: the union obviously, and
    "some single step covers the whole diff" too, because that older step covers
    a one-file diff completely. Only the step this branch SAYS it implements can
    answer the question.
    """
    plans = tmp_path / "plans"
    plans.mkdir()
    (plans / "a-plan.md").write_text(
        "#### Step E1\n\n"
        "**Files:** `control_plane/onboarding/provision.py`.\n\n"
        "**Branches:** `codex/phase-E-provision-dry-run`.\n\n"
        "#### Phase B foundation\n\n"
        "**Files:** `control_plane/onboarding/state.py`.\n\n"
        "**Branches:** `codex/phase-B-foundation`.\n",
        encoding="utf-8",
    )
    steps = _steps(plans)
    assert len(steps) == 2, steps
    monkeypatch.setattr(
        "tests.control_plane.test_plan_inventory._steps", lambda: steps
    )
    monkeypatch.setattr(
        "tests.control_plane.test_plan_inventory._branch_name",
        lambda: "codex/phase-E-provision-dry-run",
    )
    monkeypatch.setattr(
        "tests.control_plane.test_plan_inventory._changed_paths",
        lambda: ["control_plane/onboarding/state.py"],
    )

    with pytest.raises(AssertionError, match="does not declare"):
        test_every_changed_path_is_declared_by_this_branch_s_plan_step()

    # And a path this step does declare still passes, so the rule is not simply
    # rejecting everything.
    monkeypatch.setattr(
        "tests.control_plane.test_plan_inventory._changed_paths",
        lambda: ["control_plane/onboarding/provision.py"],
    )
    test_every_changed_path_is_declared_by_this_branch_s_plan_step()


def test_a_branch_no_step_claims_is_a_hard_failure(tmp_path, monkeypatch) -> None:
    """The scope of a branch is a fact its author knows and this cannot derive."""
    plans = tmp_path / "plans"
    plans.mkdir()
    (plans / "a-plan.md").write_text(
        "#### Step E1\n\n"
        "**Files:** `control_plane/onboarding/provision.py`.\n\n"
        "**Branches:** `codex/phase-E-provision-dry-run`.\n",
        encoding="utf-8",
    )
    declared = _steps(plans)
    monkeypatch.setattr(
        "tests.control_plane.test_plan_inventory._steps", lambda: declared
    )
    monkeypatch.setattr(
        "tests.control_plane.test_plan_inventory._branch_name",
        lambda: "codex/something-nobody-declared",
    )
    monkeypatch.setattr(
        "tests.control_plane.test_plan_inventory._changed_paths",
        lambda: ["control_plane/onboarding/provision.py"],
    )

    with pytest.raises(pytest.fail.Exception, match="0 plan steps claim the branch"):
        test_every_changed_path_is_declared_by_this_branch_s_plan_step()


def test_a_wrapped_inventory_is_read_in_full(tmp_path) -> None:
    """Real inventories wrap, and a line-anchored pattern reads one line of them.

    Step E9's `**Files:**` runs to four lines. Matching to the end of the LINE
    saw the first and reported every path on the others undeclared — a check
    that fails closed, which is the right direction, and is still wrong.
    """
    plans = tmp_path / "plans"
    plans.mkdir()
    (plans / "wrapped.md").write_text(
        "#### Step W\n\n"
        "**Files:** `a/first.py`, `a/second.py`,\n"
        "`a/third.py`,\n"
        "`a/fourth.py`.\n\n"
        "**Branches:** `some/branch`.\n\n"
        "Prose after the inventory mentioning `a/not_declared.py` in passing.\n",
        encoding="utf-8",
    )

    (_name, patterns, branches), = _steps(plans)

    assert patterns == {"a/first.py", "a/second.py", "a/third.py", "a/fourth.py"}
    assert branches == {"some/branch"}
    # The paragraph, and not a word past it: prose below an inventory is not a
    # declaration, or a plan could accidentally declare anything it discusses.
    assert "a/not_declared.py" not in patterns

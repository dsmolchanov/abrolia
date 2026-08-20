"""Every changed implementation path must be declared by a plan inventory.

Three separate review rounds have been spent on the same defect: a branch
touched a file its plan did not list, and the omission was found by reading
rather than by running anything. `AGENTS.md` makes an undeclared change a
merge-blocking deviation, so the cheapest place to find one is here.

The check is deliberately blunt. It reads every `**Files:**` inventory in
`thoughts/shared/plans/`, takes the backticked paths out of them, and requires
each implementation path this branch changed to match one. It does not try to
work out WHICH step a file belongs to — that is a judgement, and a judgement
this cannot make is a judgement it would get wrong. What it can say without
ambiguity is that a path appears in no inventory at all, which is the failure
that has actually happened.
"""

from __future__ import annotations

import fnmatch
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


def _declared() -> set[str]:
    patterns: set[str] = set()
    for plan in sorted(PLANS.glob("*.md")):
        for line in _INVENTORY.findall(plan.read_text(encoding="utf-8")):
            for quoted in _QUOTED.findall(line):
                for token in _expand(quoted.strip().rstrip(",.")):
                    patterns.add(token)
    return patterns


def _expand(token: str) -> list[str]:
    match = _BRACES.search(token)
    if match is None:
        return [token]
    return [
        token[: match.start()] + alternative.strip() + token[match.end() :]
        for alternative in match.group(1).split(",")
    ]


def _git(*arguments: str) -> str | None:
    result = subprocess.run(
        ["git", *arguments], cwd=REPOSITORY, capture_output=True, text=True
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _changed_paths() -> list[str] | None:
    for base in ("origin/main", "main"):
        merge_base = _git("merge-base", base, "HEAD")
        if merge_base is None:
            continue
        changed = _git("diff", "--name-only", merge_base, "HEAD")
        if changed is not None:
            return [line for line in changed.splitlines() if line]
    return None


def test_every_changed_path_appears_in_a_plan_inventory() -> None:
    changed = _changed_paths()
    if changed is None:
        pytest.skip(
            "no merge base with main is reachable, so there is no branch to"
            " compare against; this check is meaningful only on a PR branch"
        )
    patterns = _declared()
    assert patterns, "no plan inventory was parsed, so this check proves nothing"

    undeclared = [
        path
        for path in changed
        if not path.startswith(EXEMPT)
        and not any(fnmatch.fnmatch(path, pattern) for pattern in patterns)
    ]

    assert not undeclared, (
        "these changed paths appear in no plan's `**Files:**` inventory, which"
        " AGENTS.md makes a merge-blocking deviation — add them to the step"
        " they belong to, with the reason, or move the change into a file the"
        f" plan already declares: {', '.join(sorted(undeclared))}"
    )

"""Every command a plan tells you to RUN must resolve to something that exists.

This class of defect has now cost three separate passes. The go-live checklist
opened a `C6. Box hygiene` box for it ("stale boxes mislead exactly like the
retired flags did"); the 2026-08-30 canon validation found four nonexistent test
paths in canon's own Phase E acceptance and fixed them; and review on #101 found
the same defect surviving in `phase-DE-pilot.md`'s Cross-Phase block, in
`phase-A`'s sanitizer line, and in `phase-C`'s BYO coverage block — including in
the very commit that claimed to have fixed it.

`AGENTS.md` says a defect class that keeps coming back is a missing rule rather
than a missing fix. This is the rule.

What makes it worth a test rather than care: the failure is SILENT in the
direction that matters. `pytest a_file_that_does_not_exist.py` exits 4 and
`python -m check_fixtures` exits 1 with `No module named check_fixtures`, so a
gate that was supposed to prove something proves nothing, and the box above it
still gets ticked by whoever ran it and saw no test failures scroll past.

Scope is deliberately narrow: paths inside fenced blocks, which is where a
reader finds something to paste. Prose and `**Files:**` inventories name files a
plan INTENDS to create, and `tests/control_plane/test_plan_inventory.py` already
governs those.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[1]
PLANS = REPOSITORY / "thoughts" / "shared" / "plans"

#: A path argument handed to a command: a FILE or a DIRECTORY. Matching only
#: `*.py` left the larger half of the class invisible — review on #102 pointed
#: out that `pytest tests/control_plane/email -k byo` is a directory target, and
#: five plan commands use that form. A stale directory exits 5 ("no tests ran")
#: or 4, just as silently as a stale file.
_PATH_ARGUMENT = re.compile(r"(?:^|\s)((?:tests|scripts)/[\w./-]*[\w/])")
#: `python -m control_plane.cli` — the module must be importable.
_MODULE = re.compile(r"python3?\s+-m\s+([\w.]+)")


def _command_lines(plan: Path) -> list[tuple[int, str]]:
    """Lines inside fenced code blocks, with their 1-indexed line numbers."""
    lines: list[tuple[int, str]] = []
    inside = False
    for number, line in enumerate(plan.read_text().splitlines(), 1):
        if line.strip().startswith("```"):
            inside = not inside
            continue
        if inside:
            lines.append((number, line))
    return lines


def _plans() -> list[Path]:
    return sorted(PLANS.glob("*.md"))


@pytest.mark.parametrize("plan", _plans(), ids=lambda p: p.name)
def test_every_referenced_path_in_a_plan_command_exists(plan: Path) -> None:
    """A `pytest …` or script line must name paths that are on disk.

    Files AND directories: a directory target that goes stale is the quieter
    half, because pytest answers "no tests ran" rather than an error.
    """
    broken: list[str] = []
    for number, line in _command_lines(plan):
        for reference in _PATH_ARGUMENT.findall(line):
            # `pytest tests/x.py::test_name` names a case inside a file.
            path = reference.split("::", 1)[0].rstrip("/")
            if not (REPOSITORY / path).exists():
                broken.append(f"{plan.name}:{number}: {reference}")
    assert not broken, (
        "plan command blocks name paths that do not exist, so these gates exit "
        "without running anything:\n  " + "\n  ".join(broken)
    )


@pytest.mark.parametrize("plan", _plans(), ids=lambda p: p.name)
def test_every_module_invoked_by_a_plan_command_is_importable(plan: Path) -> None:
    """`python -m X` must resolve; `python -m check_fixtures` never did."""
    broken: list[str] = []
    for number, line in _command_lines(plan):
        for module in _MODULE.findall(line):
            if module in {"pytest", "pip", "venv"}:
                continue
            try:
                found = importlib.util.find_spec(module) is not None
            except (ImportError, ValueError):
                found = False
            if not found:
                broken.append(f"{plan.name}:{number}: python -m {module}")
    assert not broken, (
        "plan command blocks invoke modules that cannot be imported:\n  "
        + "\n  ".join(broken)
    )

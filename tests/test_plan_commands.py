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
import shlex
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[1]
PLANS = REPOSITORY / "thoughts" / "shared" / "plans"

#: Directories this guard governs. A path argument under either must resolve.
_GOVERNED = ("tests/", "scripts/")


def referenced_paths(line: str) -> list[str]:
    """Every governed path a command line hands to a program.

    Tokenised with `shlex` rather than matched with a regex, because a regex
    over raw text only ever recognises the spellings whoever wrote it happened
    to picture. Two rounds of review made that concrete: the first version
    matched only `*.py`, so directory targets were invisible; the second
    anchored on a bare `tests/` prefix, so `"tests/x"` and `./tests/x` — both
    ordinary shell, both accepted by pytest — slipped past a guard that was
    supposed to be the rule for this class. Tokenising first means quoting and
    `./` are handled by the same code the shell uses, and the guard stops
    depending on the author's imagination.
    """
    try:
        tokens = shlex.split(line, comments=True)
    except ValueError:
        # An unbalanced quote means this is prose rather than a command. Fall
        # back rather than failing the whole plan on it — and strip the quote
        # characters `shlex` would have consumed, so the fallback still SEES a
        # stale path instead of quietly passing one through.
        tokens = [token.strip("\"'") for token in line.split()]
    found: list[str] = []
    for token in tokens:
        if token.startswith("-"):
            continue
        # `./tests/x` and `tests/x` are the same file; `tests/x.py::test_y`
        # names a case inside one.
        candidate = token.split("::", 1)[0]
        while candidate.startswith("./"):
            candidate = candidate[2:]
        if candidate.startswith(_GOVERNED):
            found.append(candidate.rstrip("/"))
    return found
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
        for reference in referenced_paths(line):
            if not (REPOSITORY / reference).exists():
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


# --------------------------------------------------------------------------
# The parser's own tests.
#
# Until now this guard was exercised only by whatever the plans happened to
# contain, which is how it shipped twice with holes: the plans had no quoted or
# `./`-prefixed target, so nothing failed when the parser could not read one.
# A guard whose only coverage is the data it currently governs proves that the
# data is clean, not that the guard works.
# --------------------------------------------------------------------------

#: The spellings a plan author can reasonably write, all accepted by pytest.
_SPELLINGS = pytest.mark.parametrize(
    "spelling",
    [
        pytest.param("{path}", id="bare"),
        pytest.param('"{path}"', id="double-quoted"),
        pytest.param("'{path}'", id="single-quoted"),
        pytest.param("./{path}", id="dot-slash"),
        pytest.param('"./{path}"', id="quoted-dot-slash"),
    ],
)


@_SPELLINGS
@pytest.mark.parametrize(
    "path",
    ["tests/control_plane/missing_file.py", "tests/control_plane/missing_dir"],
    ids=["file", "directory"],
)
def test_a_missing_target_is_seen_in_every_ordinary_spelling(
    spelling: str, path: str
) -> None:
    """The mutation case: a stale target must be visible however it is written."""
    line = f"pytest {spelling.format(path=path)} -q"
    assert referenced_paths(line) == [path], (
        f"the guard cannot read {spelling!r}, so a stale target written that "
        "way would enter a plan undetected"
    )
    assert not (REPOSITORY / path).exists(), "fixture path must stay absent"


@_SPELLINGS
def test_a_present_target_is_resolved_in_every_ordinary_spelling(
    spelling: str,
) -> None:
    """The other direction: real paths must not be reported as broken."""
    path = "tests/test_plan_commands.py"
    line = f"pytest {spelling.format(path=path)} -q"
    assert referenced_paths(line) == [path]
    assert (REPOSITORY / path).exists()


@pytest.mark.parametrize(
    "line",
    [
        "pytest -k eu_strict -q",
        "ruff check .",
        "gitleaks detect --no-git --source . 2>&1 | head",
        'pytest -p no:cacheprovider -m "not live" -q',
        "python3 -m control_plane.cli dry-run --limit 5",
    ],
)
def test_lines_that_name_no_governed_path_report_nothing(line: str) -> None:
    """No false positives: flags, markers and bare `.` are not paths."""
    assert referenced_paths(line) == []


def test_a_case_selector_resolves_to_its_file() -> None:
    """`file.py::test_name` is a case inside a file, not a path of its own."""
    assert referenced_paths(
        "pytest tests/test_plan_commands.py::test_a_case_selector_resolves_to_its_file -q"
    ) == ["tests/test_plan_commands.py"]


def test_an_unbalanced_quote_falls_back_instead_of_failing_the_plan() -> None:
    """Prose inside a fence must not take the whole guard down with it."""
    assert referenced_paths('echo "tests/test_plan_commands.py') == [
        "tests/test_plan_commands.py"
    ]

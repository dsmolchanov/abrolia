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

**The contract: plan commands are space-separated.** This guard is not a shell,
and three rounds of review were spent discovering that trying to be one has no
end — each round shaved another spelling (`*.py` only, then unquoted only, then
operators attached to a path) and the next was always waiting. So the scope is
now stated rather than chased: an operator written flush against a path
(`pytest tests/x.py; echo`) is reported as a STYLE error naming the fix, not
parsed around. That converts an unbounded parsing problem into a bounded rule
the plans already follow — there is no such line in any plan today — and keeps
the guard's real job, catching stale paths, from drifting behind a shell parser
nobody asked for.
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

#: A governed path with a shell operator written flush against it, which
#: `shlex` keeps attached to the word: `tests/x.py;` rather than `tests/x.py`.
#: Reported as a style error, per the contract in the module docstring.
_ATTACHED_OPERATOR = re.compile(r"(?:tests|scripts)/[\w./-]*[;|&<>()]")


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
    unreadable: list[str] = []
    for number, line in _command_lines(plan):
        # Checked BEFORE parsing: an attached operator makes the token
        # unreadable, and reporting "tests/x.py; does not exist" would send the
        # author looking for a missing file instead of a missing space.
        if _ATTACHED_OPERATOR.search(line):
            unreadable.append(f"{plan.name}:{number}: {line.strip()}")
            continue
        for reference in referenced_paths(line):
            if not (REPOSITORY / reference).exists():
                broken.append(f"{plan.name}:{number}: {reference}")
    assert not unreadable, (
        "plan commands are space-separated (see this module's docstring). Put a "
        "space before `;` `|` `&` `>` `<` so the path can be read:\n  "
        + "\n  ".join(unreadable)
    )
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


# --------------------------------------------------------------------------
# The contract: space-separated commands, and a style error when they are not.
#
# Round four of review found that `shlex` keeps an operator attached to the
# word before it, so `pytest tests/x.py; echo done` tokenised as
# `tests/x.py;` and the guard reported an existing file as missing. Rather than
# teach this module more shell — the previous three rounds each shaved one
# spelling and found another waiting — the supported form is now stated, and a
# violation is reported as what it is.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "operator", [";", "|", "&", ">", "<"], ids=["semicolon", "pipe", "amp", "gt", "lt"]
)
def test_an_operator_flush_against_a_path_is_a_style_error_not_a_missing_file(
    operator: str, tmp_path: Path
) -> None:
    """The author is told to add a space, not sent hunting for a missing file."""
    plan = tmp_path / "plan.md"
    plan.write_text(
        f"```bash\npytest tests/test_plan_commands.py{operator}cat\n```\n",
        encoding="utf-8",
    )
    lines = [line for _, line in _command_lines(plan)]
    assert lines, "the fixture must produce a command line"
    assert _ATTACHED_OPERATOR.search(lines[0]), (
        f"an operator flush against a path ({operator!r}) must be caught before "
        "the path is parsed, or the guard blames a file that exists"
    )


@pytest.mark.parametrize(
    "line",
    [
        "pytest tests/test_plan_commands.py ; echo done",
        "pytest tests/test_plan_commands.py | cat",
        "pytest tests/test_plan_commands.py 2>&1 | head",
        'pytest -p no:cacheprovider -m "not live" -q',
        "pytest tests/control_plane -q && ruff check .",
    ],
    ids=["semicolon", "pipe", "redirect-then-pipe", "marker", "and-then"],
)
def test_the_space_separated_forms_the_plans_actually_use_are_accepted(
    line: str,
) -> None:
    """The contract must not reject what every plan already writes.

    `2>&1` is the case worth pinning: the redirection is its own word, so it is
    space-separated and legal, and only a path with the operator glued to it is
    a violation.
    """
    assert not _ATTACHED_OPERATOR.search(line), (
        "this is an ordinary space-separated command and must not be reported "
        "as a style error"
    )

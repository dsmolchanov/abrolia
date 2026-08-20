"""Shared helpers for the `codex-review-window` gate tests.

The gate is a shell script embedded in a workflow file, and the only thing that
matters about it is the exit code: 0 means "this pull request may merge". Both
gate test modules need the script text, so the extractor lives here.

Deliberately no PyYAML. It is not a declared dependency of this project, and a
gate test that silently skips when a parser is missing is a gate test that stops
running the day the invariant it protects is broken.
"""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "codex-review-window.yml"

_STEP_NAME = "- name: Gate on Codex verdict for this commit"
_RUN_MARKER = "run: |"


def gate_script() -> str:
    """Return the shell body of the gate's single step, dedented.

    Raises rather than returning something empty: an empty script would make
    every assertion below vacuously pass.
    """
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()

    step_index = next(
        (i for i, line in enumerate(lines) if line.strip() == _STEP_NAME), None
    )
    if step_index is None:
        raise AssertionError(
            f"{WORKFLOW} has no step named {_STEP_NAME!r}. If the step was renamed, "
            "update _STEP_NAME here — do not delete the assertion it feeds."
        )

    run_index = next(
        (
            i
            for i in range(step_index + 1, len(lines))
            if lines[i].strip() == _RUN_MARKER
        ),
        None,
    )
    if run_index is None:
        raise AssertionError(f"the gate step in {WORKFLOW} has no {_RUN_MARKER!r} body")

    indent = len(lines[run_index]) - len(lines[run_index].lstrip())
    body_indent = indent + 2

    body: list[str] = []
    for line in lines[run_index + 1 :]:
        if not line.strip():
            body.append("")
            continue
        if len(line) - len(line.lstrip()) < body_indent:
            break
        body.append(line[body_indent:])

    script = "\n".join(body).rstrip() + "\n"
    if "set -euo pipefail" not in script:
        raise AssertionError(
            "extracted gate script does not start with the expected shell preamble; "
            "the extractor is out of step with the workflow layout"
        )
    return script


def jq_available() -> bool:
    """The gate's verdict reads are jq programs, so the behaviour tests need jq."""
    return shutil.which("jq") is not None

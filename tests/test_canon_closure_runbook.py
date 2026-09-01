"""Every O-number in the closure runbook resolves to a box that exists.

The runbook is followed by an operator turning real provider paths on, so a
dangling cross-reference is not a typo — it is an instruction to run a gate
that is not there, or to skip one that is. This drifted once already: boxes
were renumbered when three were added, and the dependency graph kept pointing
at the old numbers, which sent the reader to the CI gate where the
per-transition gate belonged.
"""

from __future__ import annotations

import re
from pathlib import Path

RUNBOOK = Path(__file__).resolve().parents[1] / "docs" / "canon-closure-runbook.md"

HEADING = re.compile(r"^## (O\d+) — ", re.MULTILINE)
REFERENCE = re.compile(r"\bO(\d+)\b")


def _headings() -> list[str]:
    return HEADING.findall(RUNBOOK.read_text(encoding="utf-8"))


def test_the_boxes_are_numbered_contiguously_from_one() -> None:
    headings = _headings()
    assert headings, "no boxes were parsed, so this check proves nothing"
    assert headings == [f"O{n}" for n in range(1, len(headings) + 1)], (
        f"box numbering has a gap or a repeat: {headings}"
    )


def test_every_reference_resolves_to_a_box() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    defined = set(_headings())
    # `O0a` is a go-live checklist box, cited by name inside O4 and
    # deliberately not a box here; it is excluded rather than silently matched.
    body = text.replace("O0a", "")
    referenced = {f"O{number}" for number in REFERENCE.findall(body)}

    dangling = sorted(referenced - defined, key=lambda name: int(name[1:]))
    assert not dangling, (
        f"the runbook points at boxes that do not exist: {dangling}. "
        "A dangling reference tells an operator to run a gate that is not "
        "there, or to skip one that is."
    )


def test_the_live_batteries_do_not_require_the_gate_they_supply() -> None:
    """Gate 5 IS the live battery, so requiring it first is circular.

    O7 and O8 produce the manual live gate that the promotion in O10 consumes.
    An earlier draft pointed both at "the full rollout gate set", which made
    each battery a prerequisite of itself.
    """
    text = RUNBOOK.read_text(encoding="utf-8")
    for box in ("## O7 — ", "## O8 — "):
        start = text.index(box)
        section = text[start : text.index("\n## ", start + 1)]
        assert "prerequisites 1–4" in section, (
            f"{box.strip()} must name the pre-battery prerequisites, not the "
            "full set that includes the gate it supplies"
        )
        assert "full rollout gate set" not in section, (
            f"{box.strip()} requires the gate it produces — a circular gate"
        )

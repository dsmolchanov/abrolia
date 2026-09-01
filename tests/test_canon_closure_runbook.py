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

HEADING = re.compile(r"^## (O\d+) — (.+)$", re.MULTILINE)
REFERENCE = re.compile(r"\bO(\d+)\b")

#: What each box IS, not merely that it exists. Checking identifiers alone lets
#: two boxes swap meaning while every reference still resolves and the
#: numbering stays contiguous — and the reference that then sends an operator
#: to the CI gate instead of the per-transition gate reads perfectly.
#: Each entry is a word that must appear in that box's own heading.
CANONICAL = {
    "O1": "legal",
    "O2": "cross-org",
    "O3": "restore drill",
    "O4": "depend on deploys",
    "O5": "dry-run",
    "O6": "pilot onboarding",
    "O7": "BYO domain live battery",
    "O8": "Gmail live path",
    "O9": "`rg` check",
    "O10": "Per-transition live gates",
    "O11": "check_fixtures --require-deny",
}


def _headings() -> list[str]:
    return [number for number, _title in HEADING.findall(_text())]


def _titles() -> dict[str, str]:
    return dict(HEADING.findall(_text()))


def _text() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def _section(box: str) -> str:
    text = _text()
    start = text.index(f"\n## {box} — ")
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else len(text)]


def test_every_box_still_means_what_the_document_relies_on() -> None:
    """The mutation the identifier check cannot see.

    Swap the meanings of O9 and O10 and leave both headings in place: numbering
    stays contiguous, every reference still resolves, and the dependency graph
    now routes an operator to the `rg` check where the promotion gate belongs.
    Pinning the number-to-meaning mapping is what refuses that.
    """
    titles = _titles()
    assert set(titles) == set(CANONICAL), (
        "a box was added or removed without updating the canonical mapping: "
        f"{sorted(set(titles) ^ set(CANONICAL))}"
    )
    wrong = {
        box: titles[box]
        for box, expected in CANONICAL.items()
        if expected.casefold() not in titles[box].casefold()
    }
    assert not wrong, (
        f"these boxes no longer mean what the document's cross-references "
        f"assume: {wrong}"
    )


def test_the_dependency_graph_routes_to_the_right_boxes() -> None:
    """The graph is what an operator reads first, so it is pinned by ROLE.

    Naming the box beside its number in the graph means a renumbering that
    forgets the graph fails here rather than in production.
    """
    text = _text()
    graph = text[text.index("## Dependency order") : text.index("## O1 — ")]
    for box, role in (
        ("O1", "legal"),
        ("O2", "nerve cross-org"),
        ("O3", "release tag"),
        ("O4", "backup independence"),
        ("O5", "dry-run"),
        ("O6", "pilot onboarding"),
        ("O7", "BYO"),
        ("O8", "Gmail"),
        ("O10", "promotion"),
        ("O11", "CI deny-patterns"),
    ):
        assert re.search(rf"\b{box}\b[^\n]*{re.escape(role)}", graph, re.IGNORECASE), (
            f"the dependency graph no longer shows {box} as {role!r}; a graph "
            "that names the wrong box is an instruction to run the wrong gate"
        )


def test_the_boxes_are_numbered_contiguously_from_one() -> None:
    headings = _headings()
    assert headings, "no boxes were parsed, so this check proves nothing"
    assert headings == [f"O{n}" for n in range(1, len(headings) + 1)], (
        f"box numbering has a gap or a repeat: {headings}"
    )


def test_every_reference_resolves_to_a_box() -> None:
    text = _text()
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
    for box in ("O7", "O8"):
        section = _section(box)
        assert "prerequisites 1–4" in section, (
            f"{box} must name the pre-battery prerequisites, not the "
            "full set that includes the gate it supplies"
        )
        assert "full rollout gate set" not in section, (
            f"{box} requires the gate it produces — a circular gate"
        )

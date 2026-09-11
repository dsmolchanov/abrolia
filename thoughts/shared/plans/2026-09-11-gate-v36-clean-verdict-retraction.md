# Gate v3.6: a clean re-review retracts the older finding on the same head

Status: approved

## Goal

Move both stubs to gate revision `dbd0d8429c58ec2a444597ae1a08419fa962c163` (codex-review-gate#16). It carries one
change over the revision this repository pins today.

**A head-bound clean verdict retracts older findings on the same head.** Codex
answers the same commit twice: a formal review carrying what it found, then —
once the finding is argued away — the verbatim "Didn't find any major issues."
summary naming that same commit. The gate's marker count was a union over EVERY
Codex review for the head, with no notion of recency, and the clean path was
consulted only when that list was empty — so any formal review disabled the
clean path for its head permanently, and the only escape was pushing a new
commit, the "push to shake a verdict loose" the gate's own error text forbids.
Observed on BoardAi#183: an inline P1 `[BLOCKER]` for head `c63d88f` at
16:39:51, the clean summary naming that same head at 16:44:09, the gate still
red on it at 16:47:43.

A clean verdict now supersedes every head-bound verdict on that head that is
**strictly older** than it. It is not a waiver: it reads the same verbatim
sentence that already releases a review-less head — no marker, no label, no
human action — and it can only turn the gate green on a head Codex itself last
spoke about as clean. Everything unproven is kept: an unreadable timestamp
disables the filter entirely, a row whose timestamp does not parse is kept, and
a tie keeps the finding. A review published *after* the clean signal still
decides the merge. Retraction on its own is never a verdict — an emptied review
list still reaches green only through a counted clean signal.

## Files

**Files:** `.github/workflows/codex-verdict-waker.yml`, `.github/workflows/codex-review-window.yml`, `thoughts/shared/plans/2026-09-11-gate-v36-clean-verdict-retraction.md`.

**Branches:** `chore/gate-pin-v36-clean-verdict-retraction`.

| File | Change |
| --- | --- |
| `.github/workflows/codex-verdict-waker.yml` | waker stub, re-synced byte for byte from the dev-agent fleet template at the new pin |
| `.github/workflows/codex-review-window.yml` | gate stub, re-synced the same way |
| `thoughts/shared/plans/2026-09-11-gate-v36-clean-verdict-retraction.md` | this plan (an applicable plan must list itself) |

Both stubs move in lockstep: the gate's short window and the waker's re-entry
are two halves of one protocol and must name the same revision. Outside comments
the only change is the pin; the gate stub's comment now describes the revision
it pins, at the same 79 lines (the template's ceiling is `< 80`).

## Verification

Both stubs pin the same 40-hex revision; the waker remains an active workflow
after merge; the gate check goes green on this pull request through the pinned
revision. BoardAi#183 goes green once this pin reaches its branch, and a pull
request with a genuine P1 still goes red.

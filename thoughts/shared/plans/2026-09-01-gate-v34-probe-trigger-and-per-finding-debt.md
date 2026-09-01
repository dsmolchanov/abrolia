# Gate v3.4: the waker probe reads a trigger, and review debt is one issue per finding

Status: approved

## Goal

Move both stubs to gate revision `23518dbd89add6333ed0d147bd2acd8d898ef04e`
(codex-review-gate#10). It carries two changes over the revision this
repository pins today.

**The waker-liveness probe reads the stub's TRIGGER, not the workflow's name.**
The gate takes its short 120s verdict window only where a waker can re-enter
it, and it decided that by reading `state: active` at a fixed workflow path.
That is equally true of the reusable HOST, which is `workflow_call` only and
can never be started by an event — so a repository whose file at that path is
the host took the short window with nothing able to wake it, and every clean
verdict there needed a manual re-run. The probe now also reads the
default-branch file and requires an `issue_comment` trigger in its `on:` block,
with comments stripped first. Failure direction is unchanged: either read
failing means "no waker", which costs minutes and never the merge.

**Review debt is recorded one issue per finding.** A degraded round that merges
over still-open P1s used to file ONE bundled issue per pull request, which is
open or closed for everything in it: a single fixed finding could not be retired
without either closing its unfixed siblings or leaving the issue open as a
reminder of nothing in particular. Each deferred finding now gets its own issue,
keyed on a fingerprint of the path and the complete finding line — so truncation
cannot collapse two findings into one — with the title bounded below GitHub's
256-character limit, because a title over that limit makes the POST fail into a
warning and the finding merges unrecorded. This matches what the weekly trunk
review already files.

## Files

- `.github/workflows/codex-review-window.yml` — gate stub: pin bump only.
- `.github/workflows/codex-verdict-waker.yml` — waker stub: pin bump only, in
  lockstep. The gate's short window and the waker's re-entry are two halves of
  one protocol and must name the same revision.
- `thoughts/shared/plans/2026-09-01-gate-v34-probe-trigger-and-per-finding-debt.md`
  — this plan (an applicable plan must list itself).

## Verification

Both stubs pin the same 40-hex revision; the waker remains an active workflow
after merge; a pull request whose Codex verdict arrives as a clean summary
comment goes green without a manual re-run.

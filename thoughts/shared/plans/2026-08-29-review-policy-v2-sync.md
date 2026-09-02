# Fleet review-policy sync: two lanes and the gate v2 round budget

**Status:** approved. **Owner:** plaintalk-dev-agent (fleet policy roll,
2026-08-29).

The fleet's Codex review gate moved to a round-budgeted contract
(dsmolchanov/codex-review-gate v2): completed review generations are counted
statelessly from head-bound verdicts, re-reviews are scoped to the commit
range since the last completed verdict with still-open findings re-emitted,
and after three completed rounds only P0 holds the merge (P1s keep their
badges for the batch fix). Repositories carrying the `review-lane-fast` topic
run the check as informational. This plan step syncs this repository's pinned
calling stub and the managed policy prose, and updates the repo-owned gate
invariant that still described the pre-reusable-gate contract.

## Step 1 — sync the pin, the policy block, and the gate invariant

**Files:** `.github/workflows/codex-review-window.yml`, `AGENTS.md`,
`CLAUDE.md`, `AGENTS.repo-invariants.md`.

Merged 2026-08-29; the branch name is reused by Step 2, which now carries the
branch claim — exactly one step may claim a branch.

Applied by `bootstrap-dsmolchanov-repo.sh --all` from dsmolchanov/dev-agent
(see docs/architecture-plan.md there, "two review lanes" enhancement), plus a
follow-up commit updating the stale merge-authorisation invariant to the v2
contract.

## Step 2 — gate v3: the verdict waker and the round cap (2026-08-30)

The gate moved again (dsmolchanov/codex-review-gate v3, pin 6569100): it holds
a runner ~2 minutes instead of 15 wherever a verdict waker is active — a Codex
clean-summary comment re-runs the gate's PR-bound run, since a comment event
alone can never satisfy the required check — and keeps the long window until
the waker has merged here. Past the three-round budget the gate merges over
open P1s and files them as one review-debt issue per PR (P0 blocks on every
round), which is why the calling stub now grants `issues: write`, and
`actions: read` for the waker probe.

**Files:** `.github/workflows/codex-review-window.yml`,
`.github/workflows/codex-verdict-waker.yml`.

Merged 2026-08-30; the branch name is reused by Step 3, which now carries
the branch claim — exactly one step may claim a branch.

Applied by `bootstrap-dsmolchanov-repo.sh --gate-only` from
dsmolchanov/dev-agent; the two stubs pin the same revision in lockstep.

## Step 3 — gate v3.3: the waker re-enters on a formal review (2026-09-01)

A Codex verdict delivered as a FORMAL REVIEW left a stale failed gate run on the
commit: the gate's own re-entry starts a new check run while the earlier run
that failed for want of a verdict stays, and GitHub's status rollup counts the
red one — the pull request reports BLOCKED with a green run of the same context
beside it. Gate revision `ae5d20884828bee1bdec72fcca998d1d6ed1c2c6`
(codex-review-gate#9) adds the `pull_request_review` trigger to the waker,
accepts either event shape in its guard and concurrency group, and drains every
stale gate run for the head — serialized, because the gate's concurrency group
is `cancel-in-progress` and back-to-back re-runs would cancel each other.

**Files:** `.github/workflows/codex-verdict-waker.yml`,
`.github/workflows/codex-review-window.yml`.

**Branches:** `chore/plaintalk-dev-agent-policy-v2`.

Applied by `bootstrap-dsmolchanov-repo.sh --gate-only` from dsmolchanov/dev-agent;
the two stubs pin the same revision in lockstep.

## Step 4 — gate v3.4: the waker probe reads a trigger, and review debt is one issue per finding (2026-09-01)

Two changes over Step 3's revision.

The waker-liveness probe read `state: active` at a fixed workflow path to decide
whether a late verdict could re-enter the gate, and took the short 120s window
where it could. That state is equally true of the reusable HOST, which is
`workflow_call` only and can never be started by an event — so a repository
whose file at that path is the host took the short window with nothing able to
wake it, and every clean verdict there needed a manual re-run. The probe now
also reads the default-branch file and requires an `issue_comment` trigger in
its `on:` block, comments stripped first so the host's own prose cannot satisfy
it. Failure direction is unchanged: either read failing means "no waker", which
costs minutes and never the merge.

Review debt is now recorded one issue per finding. A degraded round that merges
over still-open P1s used to file one bundled issue per pull request, which is
open or closed for everything in it — a single fixed finding could not be
retired without either closing its unfixed siblings or leaving the issue open as
a reminder of nothing in particular. Each deferred finding gets its own issue,
keyed on a fingerprint of the path and the complete finding line so truncation
cannot collapse two findings into one, with the title bounded below GitHub's
256-character limit; over that limit the POST fails into a warning and the
finding merges unrecorded. This matches what the weekly trunk review already
files here.

Gate revision `23518dbd89add6333ed0d147bd2acd8d898ef04e` (codex-review-gate#10).

**Files:** `.github/workflows/codex-verdict-waker.yml`,
`.github/workflows/codex-review-window.yml`,
`thoughts/shared/plans/2026-08-29-review-policy-v2-sync.md`.

**Branches:** `chore/gate-pin-23518db`.

Pin bump only in both stubs, in lockstep: the gate's short window and the
waker's re-entry are two halves of one protocol.

## Step 5 — gate v3.5: review-debt identity lives in a tested script (2026-09-02)

One change over Step 4's revision.

What identifies a deferred finding is decided in one tested file. On a round
past the review budget the gate merges over still-open P1s and files one issue
per finding; the key that makes two findings the same finding was a sed
pipeline inside the workflow's heredoc, and four of the five review rounds on
the per-finding change were spent re-deriving it. The rule now lives in
`scripts/review_debt.py` in the gate repository — fingerprint of the path and
the complete finding line, display title bounded under GitHub's 256-character
limit, in-batch and cross-round dedup — with the policy stated once (no line
number, no comment id, no PR number) and pinned by unit tests. The workflow
fetches that file at its own commit, the one this stub pins, so identity and
workflow cannot drift apart. Titles are byte-identical to the previous rule, so
issues already filed here still dedupe. The fetch is fail-soft: a failure warns
and merges the finding unrecorded rather than guessing a key.

Gate revision `4d05f6909650381037fee2cfed40c6a5cab591af` (codex-review-gate#15).

**Files:** `.github/workflows/codex-verdict-waker.yml`,
`.github/workflows/codex-review-window.yml`,
`thoughts/shared/plans/2026-08-29-review-policy-v2-sync.md`.

**Branches:** `chore/gate-pin-4d05f69`.

Both stubs re-synced byte for byte from the dev-agent fleet template at the
new pin, in lockstep: the gate's short window and the waker's re-entry are two
halves of one protocol. Outside comments the only change is the pin; the
comments now describe the revision the stub pins.

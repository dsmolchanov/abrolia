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

**Branches:** `chore/plaintalk-dev-agent-policy-v2`.

Applied by `bootstrap-dsmolchanov-repo.sh --gate-only` from
dsmolchanov/dev-agent; the two stubs pin the same revision in lockstep.

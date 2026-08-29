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

**Branches:** `chore/plaintalk-dev-agent-policy-v2`.

Applied by `bootstrap-dsmolchanov-repo.sh --all` from dsmolchanov/dev-agent
(see docs/architecture-plan.md there, "two review lanes" enhancement), plus a
follow-up commit updating the stale merge-authorisation invariant to the v2
contract.

# Guidance for Claude Code on the web (Auto-fix)

This file is read by any Claude Code on the web session spawned against this
repository, including Auto-fix sessions started from `plaintalk-dev-agent`.

## Durable artifacts on PR branches

Every PR opened by `plaintalk-dev-agent` commits these files before the PR
becomes visible:

- `thoughts/shared/plans/<date>-<slug>.md` — the authoritative plan.
- `thoughts/shared/tests/<date>-TEST-<slug>.md` — the test plan the
  implementation was required to satisfy.
- `thoughts/shared/implementations/<date>-<slug>-validation.md` — the
  validation report comparing the commit history to the plan.

Start every Auto-fix session by reading all three files in full. They are the
source of truth for what the PR was supposed to implement. Deviations from the
plan are P1 `[BLOCKER]`s under `AGENTS.md`.

## Session model

Auto-fix sessions start from a fresh clone. Nothing on the Fly machine that
created the PR carries over; only committed branch content is available.

- The `CLAUDE_CODE_OAUTH_TOKEN` belongs to `dsmolchanov`.
- `plaintalk-dev-agent` activates Auto-fix with
  `claude -p --from-pr <url>` and keeps the session attached to that PR.

## Fix-commit conventions

- Batch every open blocker and current CI failure into one fix commit.
- Commit messages: `fix(<scope>): <short summary> (responds to Codex review)`.
- Never force-push or amend already published commits.
- Never modify `AGENTS.md`, `CLAUDE.md`, branch protection, or
  `.github/workflows/codex-review-window.yml` from an Auto-fix pass.
- Stop and request a human decision when a blocker is architecturally
  ambiguous; do not guess.

## What counts as done

- Every open `[BLOCKER]` has a corresponding code change.
- CI is green.
- The fix stays within the PR plan.
- `codex-review-window` is green for the current head commit.

Each Auto-fix push re-runs CI and requests another Codex review. Auto-merge
fires only after the new head is green in both systems.

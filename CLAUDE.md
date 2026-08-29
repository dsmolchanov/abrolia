<!-- dev-agent:policy v2 start -->
# Guidance for Claude Code on the web (Auto-fix)

This file is read by any Claude Code on the web session spawned against this
repository, including Auto-fix sessions started from `plaintalk-dev-agent`.

> This section is owned by `plaintalk-dev-agent` and is replaced wholesale by
> `bootstrap-dsmolchanov-repo.sh --policy-only`. Repository-specific guidance
> belongs outside the `dev-agent:policy` markers, where it is preserved across
> refreshes.

## Durable artifacts on PR branches

Every PR opened by `plaintalk-dev-agent` commits these files before the PR
becomes visible:

- `thoughts/shared/plans/<date>-<slug>.md` — the authoritative plan.
- `thoughts/shared/tests/<date>-TEST-<slug>.md` — the test plan (Red-phase
  spec) the implementation was required to satisfy.
- `thoughts/shared/implementations/<date>-<slug>-validation.md` — the
  `/validate_plan` report comparing the commit history to the plan.

**Start every Auto-fix session by reading all three files in full.** They are
the source of truth for "what was supposed to happen" on this PR.

A plan constrains this PR only when it is the plan this PR names, and — for each
of `repository:` and `status:` **that the plan actually carries** — that field
does not disqualify it: `repository:` must name this repository, `status:` must
not be `draft`. A plan that omits a field is not disqualified by it. Most plans
omit both: `templates/plan_prompt.md` never asks for them and the plan schema
lint does not require them, so reading the fields as mandatory would make every
ordinary plan non-binding.

See "Plan applicability" in `AGENTS.md`, which is the authority — these two files
must not disagree. A mismatch against a plan's narrative — approach, naming,
ordering, design notes — is a `[NIT]`, not a blocker.

## Session model

Auto-fix sessions start from a **fresh clone**. Nothing on the Fly machine
that created the PR carries over. You only have what is checked into the
branch.

- The `CLAUDE_CODE_OAUTH_TOKEN` that started this session belongs to
  `dsmolchanov`. All follow-up commits land under that identity.
- The session is billed against the Claude Max subscription.
- When `plaintalk-dev-agent` triggers a session via
  `claude -p "/autofix-pr"`, the Claude GitHub App relays subsequent PR
  review-comment and CI-failure events into this session.

## Fix-commit conventions

- **Batch** all open blockers + CI failures into one commit. Do not reply
  per-comment.
- The batch includes badge-carrying findings listed under a `Non-blocking
  notes` heading: on a round past the review budget, P1s no longer hold the
  merge but they are still yours to fix in the same pass.
- Fix the **invariant**, not the reported instance: find every sibling consumer
  of the affected contract and fix them in the same batch, with one test that
  represents the class. See "Fix the invariant, not the instance" in
  `AGENTS.md`. Each additional round costs a full review generation.
- Commit messages: `fix(<scope>): <short summary> (responds to Codex review)`.
- Never force-push. Never amend merged commits.
- Never touch `AGENTS.md`, `CLAUDE.md`, `.github/workflows/codex-review-window.yml`,
  or branch protection in a fix commit.
- Checkpoint commits are fine locally, but push once per fix generation. The
  gate debounces pushes, so a burst of pushes does not buy extra review
  generations — it just delays the verdict.

## What counts as "done"

A fix pass is done when every open `[BLOCKER]` has a corresponding code change,
CI is green, and the fix introduced nothing out of scope.

Do not try to merge manually. Auto-merge is already queued at the PR level and
fires when branch protection is satisfied.

Absence of a Codex verdict is not approval, and the gate treats it as such: a
missing, quota-blocked, or unparseable verdict holds the merge. If the gate
reports one of those, the remedy is to restore the reviewer or re-run the job,
never to push a commit hoping to shake a verdict loose.

Which checks branch protection waits for is a per-repository lane decision. On
the prod lane (the default) the `codex-review-window` check is a hard merge
gate. In repositories carrying the `review-lane-fast` topic the check runs and
posts findings but does not hold the merge — green CI merges via auto-merge, so
a fix pass there is best-effort: address what has arrived while the PR is open,
and leave anything that lands after the merge to the weekly trunk review.

## When to stop and hand back

You are not obliged to keep patching. Stop and say so plainly in the PR when:

- a defect class keeps coming back. `AGENTS.md`, "Fix the invariant, not the
  instance", says when that becomes a missing rule and what to do about it. Read
  it there — this file states no part of that rule, deliberately;
- a finding cannot be fixed within the current design, because the honest fix
  needs an identity, permission, store, or lifecycle the code does not have.
  That is a scope signal, not a bug report — escalate to the design rather than
  patching around it;
- a finding needs an architectural decision, or contradicts the plan's stated
  scope;
- findings have started arriving in files this PR was never about — the PR has
  outgrown its review unit and should be split;
- you believe a finding is wrong. Argue it in the thread with evidence. Do not
  implement something you think is incorrect because a reviewer was confident.

A human applies the `needs-human` label, which stops further Auto-fix
activation. Nothing machine-applies it, so nobody is waiting for a counter to
expire — if you think it is time, ask for it in the thread.

Each additional round costs a full review generation, and every fix enlarges the
diff being reviewed, which tends to produce further findings. Handing back early
is usually cheaper than one more pass.
<!-- dev-agent:policy v2 end -->

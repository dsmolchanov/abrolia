---
name: reusable-codex-gate
status: in-progress
created: 2026-08-20T14:30:00Z
updated: 2026-08-20T14:30:00Z
---

# Review policy v2 and a called, not copied, Codex gate

## Current state

Every repository in the fleet carries its own ~460-line copy of
`.github/workflows/codex-review-window.yml`. The copies are kept in step by a
template sync from `dsmolchanov/dev-agent`.

Distributing the gate as *content* means every sync is a reviewable change to a
large file in each repository, reviewed by an agent that cannot see the file's
tests — they live only in `dev-agent`. Rolling one sync to eight repositories on
2026-08-19 produced **21 blocking findings on that one file**, no two
repositories agreeing, and several asking for tests that existed in two of the
eight.

## Desired state

The gate lives once, in `dsmolchanov/codex-review-gate` (public), together with
its executable tests and its own CI. Each consumer installs a ~52-line stub that
calls it, pinned to a commit SHA.

A gate change is then reviewed once, in the repository where it can be run.

## Why the host is public

A **public** repository cannot call a reusable workflow from a **private** one —
the run fails with zero jobs and no annotation. `nerve-oss` and `abrolia` are
public. Both directions were verified by probe, not by reading documentation.

## The second half: review policy v2

The same branch installs the fleet review policy, and it is in scope here
because it changes review severity and Auto-fix behaviour — the plan would be
lying by omission if it covered only the workflow.

Three artifacts:

**`AGENTS.md`** gains a `dev-agent:policy` managed block, replaced wholesale by
`bootstrap-dsmolchanov-repo.sh --policy-only`. It carries `## Code Review
Rules` (Codex reads this natively), a severity model, narrowed plan
applicability, a **finite** list of blocking invariants, an Auto-fix response
budget, and three rules whose purpose is to end review loops rather than feed
them:

- a class of defect that recurs is one missing rule, not several findings —
  the block states the threshold, and **this plan deliberately does not repeat
  the number**, for the reason given below;
- a finding that cannot be fixed within the current design is a scope signal,
  not a bug report;
- a valid finding is not, by itself, a reason to continue.

The behavioural change is deliberate and is the point of the work: previously
two open-ended predicates were elevated to P1, and an open-ended predicate is
satisfiable on any non-trivial diff, so the loop could not terminate. Rolling
one fleet change to eight repositories on 2026-08-19 produced 21 blocking
findings on a single file.

**The recurrence threshold is stated in exactly one paragraph** of that block,
and this plan is bound by that rule like every other steering surface: it points
at the paragraph rather than restating the figure. A number restated across
files is a promise that they agree, kept by grepping prose — it drifted in two
separate review rounds, once to a line break. The first draft of this very plan
restated it, which is the point.

**`AGENTS.repo-invariants.md`** is created once and never overwritten, so
repository-specific invariants survive every fleet refresh. It is empty on
install (`_None yet._`); entries are added when a P1 class recurs, together with
the deterministic check that would have caught it. Each entry must be a closed
question a reviewer can answer yes or no.

**`CLAUDE.md`** gains the matching Auto-fix guidance so a cloud session reads
the same rules.

### Rollback for the policy half

The managed block is delimited; removing it restores the previous file. The
block is replaced wholesale on refresh, so nothing outside the markers is at
risk. `AGENTS.repo-invariants.md` is additive and empty, and deleting it loses
nothing on install.

### Validation for the policy half

`scripts/upsert_policy_block.py` in dev-agent refuses to write unless the
markers are exactly one clean pair, which is what prevents a malformed block
from eating surrounding content. `tests/test_policy_blocks.py` covers install,
legacy-wrap, and replacement, and asserts the recurrence threshold appears in
exactly one file. Neither the block nor this plan may add a second statement of
that number.

## Scope

In scope, per repository:

1. Install the three policy artifacts above.
2. Replace the copied workflow with the calling stub.
3. Flip the required status check from `codex-review-window` to
   `codex-review-window / codex-review-window`. GitHub names a reusable
   workflow's check run `<caller job> / <called job>`; requiring the bare name
   waits on a context that never reports and blocks every pull request.

Out of scope: adding any repository-specific invariant to
`AGENTS.repo-invariants.md` — it ships empty by design, and an entry is written
when a P1 class actually recurs, not in advance. Also out of scope: moving
`CODEX_REQUEST_TOKEN` out of PR-executed workflows. That
needs an App-owned controller holding the credential outside CI, and is tracked
separately. Until it exists the containment is a fine-grained token limited to
these repositories with `Issues: Read and write` and `Pull requests: Read and
write` and nothing else.

## Ordering (this is load-bearing)

Flip branch protection **before** merging the stub PR, never after. A
`pull_request` event runs the PR's own copy of the workflow, so once the stub is
on the branch its run already reports the new context — flipping first lets that
PR satisfy its own gate with no admin bypass. Flipping after leaves protection
requiring a context nothing reports, and every open PR blocks.

## Verification

- `codex-review-window / codex-review-window` appears in
  `GET /repos/{repo}/branches/{branch}/protection/required_status_checks`.
- The stub's run reports under that name and reaches a verdict.
- The installed workflow contains no `gh api` call — i.e. it calls the gate
  rather than carrying its body. Asserted by
  `tests/test_bootstrap_modes.py::test_the_installed_gate_is_a_stub_not_a_copy`
  in `dev-agent`.
- The `uses:` ref is a 40-character commit SHA, not a tag. A mutable ref would
  let a retag silently replace the code that receives `CODEX_REQUEST_TOKEN`,
  `SLACK_WEBHOOK` and a write-capable workflow token in every consumer at once.
  Asserted by `test_the_host_is_pinned_to_a_commit_not_a_tag`.
- The gate's own behaviour (blockers red, missing verdict red, API failure red,
  quota exhaustion red, fork-token failure red) is asserted by
  `tests/test_gate_behavior.py` in `codex-review-gate`, which executes the
  workflow's `run:` block under bash against a stubbed `gh` and checks exit
  codes. It runs in that repository's CI on every push.

## Rollback

Restore the previous copied workflow from git history and flip the required
context back to `codex-review-window`, in that order.

# Codex Review Gate Green Verdict Implementation Plan

## Overview

Make the required `codex-review-window` check recognize both GitHub result
shapes emitted by the configured Codex bot for the current PR head: a formal
pull-request review, or a bot issue comment that explicitly reports no major
issues and names the reviewed commit. Preserve fail-closed behavior for every
P1/`[BLOCKER]` formal review.

## Current State Analysis

`.github/workflows/codex-review-window.yml` accepts only a formal review object
whose `commit_id` equals the full PR head SHA. PR #47 demonstrates a second
successful result shape: Codex posted `Didn't find any major issues` and the
ten-character reviewed head in an issue comment, but created no review object.
The existing gate therefore times out despite a head-bound green verdict.

The temporary advisory deadline also ended at the start of August 9 UTC, before
the operator-stated night-of-August-9 limit reset.

## Desired End State

- A formal review for the current full head SHA continues through the existing
  P1/`[BLOCKER]` inspection.
- A green Codex issue comment is accepted only when it is authored by the
  configured bot, contains the exact no-major-issues verdict, and contains the
  current head's ten-character SHA.
- Before accepting that comment, the gate waits for GitHub propagation and
  queries formal reviews again; any formal review found remains authoritative
  and goes through the blocker scan.
- Stale green comments, generic comments, connector errors, and comments for a
  different head cannot satisfy the gate.
- Missing reviews remain temporarily advisory only through
  `2026-08-10T01:00:00Z`; after that instant they fail closed.

## What We're NOT Doing

- Changing ordinary CI, gitleaks, branch protection, or auto-merge policy.
- Treating comments with findings as green.
- Bypassing a formal P1/`[BLOCKER]` review.
- Changing Codex account or GitHub connector configuration.

## Implementation Approach

### Phase 1: Recognize the head-bound green comment

**File:** `.github/workflows/codex-review-window.yml`

Add a query for the Codex bot's green issue-comment result and poll it alongside
the existing formal review query. Return success immediately only when that
comment names the current head. Before returning, allow a short propagation
grace period and re-query formal reviews. Keep formal reviews authoritative
whenever a review object exists, including the existing inline/body blocker
scan.

Extend the explicit, time-bounded operator exception to the stated reset night;
do not create an unbounded fail-open path.

### Success Criteria

#### Automated Verification

- [ ] `actionlint .github/workflows/codex-review-window.yml` passes.
- [ ] `git diff --check` passes.
- [ ] The workflow's jq predicate matches PR #47's bot verdict for head
      `31e7b3e9cf`.
- [ ] Repository CI and full-history gitleaks pass on the PR.
- [ ] A formal P1/`[BLOCKER]` review still makes `codex-review-window` fail.

#### Manual Verification

- [ ] GitHub reports `codex-review-window` green for a head-bound Codex
      no-major-issues comment.
- [ ] PR #47 can rerun the corrected gate without weakening its other required
      checks.

## Testing Strategy

Validate YAML/shell structure with `actionlint`, exercise the exact jq predicate
against the observed PR #47 comment through `gh api`, and use the PR's own
required check as the integration test. After merge, rerun/update PR #47 so its
check executes the corrected workflow from `main`.

## Rollback

Revert the workflow commit. This restores formal-review-only behavior; ordinary
CI and gitleaks are unaffected.

## References

- `.github/workflows/codex-review-window.yml`
- `AGENTS.md` review and merge-gate rules
- PR #47 green Codex comment for reviewed head `31e7b3e9cf`

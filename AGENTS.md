<!-- dev-agent:policy v2 start -->
# Review guidelines (Codex + Claude Auto-fix)

This file steers Codex's native GitHub review and Claude Code on the web's
Auto-fix behavior on every PR in this repository.

> This section is owned by `plaintalk-dev-agent` and is **replaced wholesale** by
> `bootstrap-dsmolchanov-repo.sh --policy-only`. Anything written between the
> `dev-agent:policy` markers is deleted on the next refresh.
>
> Repository-specific guidance belongs outside the markers. Repository-specific
> **blocking invariants** belong in `AGENTS.repo-invariants.md`, which this
> installer creates once and never overwrites.

## Code Review Rules

On the **first** review of a pull request, produce one **comprehensive
inventory** rather than a sample. Enumerate every P0/P1 you can find in the
diff, then group the findings by the root invariant each one violates.

For each finding, include:

- the affected symbol, not only the file and line;
- every **sibling consumer** of the same contract that has the same defect —
  one entry per instance, grouped under the shared root cause;
- evidence: the concrete input, state, or call path that makes it fail;
- impact, and your confidence;
- the safe remediation path;
- the regression test that would represent the whole class.

Completeness on the first pass matters more than brevity. A finding withheld
for a later pass costs a full review generation, so surface it now.

On a **follow-up** review, do two things and only these two: state explicitly,
for each finding in the earlier inventory, whether it is now fixed or still
open; and inspect the causal impact cone of the fix — the changed symbols'
callers, schemas, permissions, migrations, and deployment configuration — for
regressions the fix introduced.

## Severity

Codex surfaces **P0** and **P1** findings in GitHub by default. Elevate the
following to **P1 `[BLOCKER]`**:

- Any violation of a `## Blocking invariants` entry below.
- Any test specified in `thoughts/shared/tests/*.md` that is missing, skipped,
  or weakened.
- Security issues, correctness bugs, data-loss risks, auth regressions.
- Any change to files listed in `CODEOWNERS` without a corresponding entry in
  the plan.

Mark the following as **P2 `[NIT]`**:

- Style, naming, formatting, micro-optimizations.
- Anything the existing linter would catch (let CI handle it).
- A mismatch between the implementation and the *narrative* of a plan —
  approach, naming, ordering, or design notes. Report it; do not block on it.

### Plan applicability

A plan constrains review only when all of the following hold. Otherwise it is
context, and a mismatch is at most a `[NIT]`:

- it is the plan explicitly named by this PR;
- its `repository:` field, if present, names **this** repository;
- its `status:` field, if present, is not `draft`.

Only a violation of an explicitly named, approved, repository-matching
obligation can block. A plan that names another repository cannot generate a
blocker here — a cross-repository plan once blocked a PR for not changing a
workflow that exists only in a different repo.

## Blocking invariants

A finite, checkable list. Check *these*, rather than searching openly for
problems — a closed question has a terminating answer, an open one does not.

**Also read `AGENTS.repo-invariants.md`** if it exists. It holds this
repository's own invariants and is equally blocking. Add repository-specific
entries there, never here: everything between the `dev-agent:policy` markers is
deleted on the next fleet refresh.

**When a P1 recurs, it is a missing rule, not a new finding.** Add the class to
`AGENTS.repo-invariants.md` and ship the deterministic check that would have
caught it, in the same fix commit. A rule that runs in 200ms and answers
identically every time beats asking a reviewer the same question again.

Fleet-wide invariants:

- No HTTP route, RPC, or handler added or modified without an auth dependency.
- No database function or RPC granted `EXECUTE` to `anon` or `PUBLIC`.
- No tenant-scoped query without a tenant predicate bound to the caller.
- No PII or PHI field in a payload leaving the system to a third party.
- No unbounded request body, upload, or collection on a publicly reachable
  endpoint.
- No secret, token, or credential in tracked files, logs, or error responses.

## Auto-fix response budget

When Claude Code on the web Auto-fix responds to review comments or CI
failures on a PR in this repo:

1. **Batch ALL open `[BLOCKER]` comments and current CI failures into ONE fix
   commit.** Do not reply per-comment. Do not churn on `[NIT]`s.
2. **Resolve every `[BLOCKER]` before touching any `[NIT]`.** Never flip the
   order.
3. **If a comment is genuinely ambiguous, implement the narrowest reading that
   satisfies it, record the assumption in the PR body under "Assumptions", and
   continue.** Do not stop and wait. An ambiguity costs a note, not a day.
4. **Do not open follow-up PRs from an Auto-fix session.** If something is
   out of scope for this PR, note it in the PR body under "Out-of-scope
   findings" and leave it for the humans.

### Fix the invariant, not the instance

> **If the same class of defect has been reported twice — on this pull request
> or across two of them — that is one missing rule, not two findings.** Stop
> patching instances. Write the rule into `AGENTS.repo-invariants.md`, ship the
> check that enforces it, and say in the thread that this is what you did.
>
> Two, not three: a third report costs another full review generation, which is
> the cost this whole policy exists to avoid. A rule written for what turns out
> to be a coincidence costs almost nothing — it is a lint rule that rarely fires.
>
> **This paragraph is the only place the recurrence threshold is stated.** Every
> other steering surface points here instead of repeating the number. That is
> deliberate: five files restating one figure is a promise that they agree, kept
> by grepping prose — and it leaked twice in two review rounds, once to a line
> break. A number that exists once cannot drift.

A finding is a symptom of a rule that is not enforced. Before fixing:

1. Reproduce and classify it.
2. Find **every** consumer and sibling of the affected contract.
3. Fix the root invariant or state transition, not the reported line.
4. Add a parameterized, property, or regression test that represents the whole
   class — not a case pinned to the one reported input.
5. Fix all in-scope instances in the same batch.

Patching the reported instance and waiting for the next one to be found is the
single most expensive pattern available: each round costs a full review
generation, and every fix enlarges the diff being reviewed, which tends to
produce further findings. One PR centralized a shared helper only after its
third separate consumer was reported; that sibling audit belonged in the first
fix.

### Two more rules that end loops rather than feed them

> **A finding that cannot be fixed within the current design is a scope signal,
> not a bug report.** If the honest fix requires an identity, a permission, a
> store, or a lifecycle the code does not have, say so and escalate to the
> design. Do not patch around it. One review loop spent five rounds treating
> three architecturally impossible findings as bugs; the correct response was to
> delete the feature.

> **A valid finding is not, by itself, a reason to continue.** Reviewers can be
> right every round while the artifact diverges — each fix grows the object, and
> a larger object yields more findings. Count the rounds. If they are climbing
> while findings stay valid, the object is wrong, not the reviewer: shrink the
> PR, cut the contested feature, or hand back.

## Scope discipline

- Stay inside the plan. If you find a bug that isn't in scope, note it in the
  PR body and do NOT fix it in the same commit.
- Do not refactor unrelated code.
- Do not add dependencies, feature flags, or backwards-compat shims unless the
  plan explicitly lists them.
- Do not modify `.github/`, `CODEOWNERS`, `AGENTS.md`, or this file itself
  unless the plan says so.

## Merge gate

CI and the `codex-review-window` check are hard merge gates. Codex review is
**not advisory** — the check fails while the Codex verdict for the current head
commit reports P1 `[BLOCKER]` findings, and it also fails when no verdict
arrives at all. Absence of a verdict is not approval, so a missing,
quota-blocked, or unparseable verdict holds the merge. The remedy for those is to
restore the reviewer and re-run the job, never to push a commit hoping to shake a
verdict loose.

The gate answers that one question and nothing else. It counts no fix
generations, enforces no diff budget, and accepts no waiver command. What keeps
the loop short is upstream of it: the two open-ended P1 classes are gone from the
severity list above, and the review is asked for one complete inventory rather
than a finding at a time. Those reduce rounds; they do not cap them.

**A human stops automation by applying the `needs-human` label.** No new Auto-fix
session starts while it is present, and removing it is a deliberate decision to
resume. Use it when the review is circling, when a finding needs an architectural
call, or when the PR should be split instead of patched again.

If a finding is wrong, say so in the thread with evidence rather than
implementing something you believe is incorrect. A reviewer being confident is
not the same as a reviewer being right.
<!-- dev-agent:policy v2 end -->

---
date: 2026-08-21T00:58:08Z
researcher: Dmitry Molchanov
git_commit: e66e71d0c4742ec41ada38b01606cee7db5ca74d
branch: codex/phase-E9-backup-before-migrate
repository: dsmolchanov/abrolia
topic: "Closing PR #55 (Step E9) and integrating PR #56 — Implementation Strategy"
tags: [codex-review-window, backup, restore, install-rollback, repo-invariants, toctou, branch-protection, mutation-testing]
status: complete
last_updated: 2026-08-21
last_updated_by: Dmitry Molchanov
type: implementation_strategy
---

# Closing #55, and what #56 needs

Continues `thoughts/shared/handoffs/general/2026-08-20_23-05-29_general_codex-round-closing-three-prs.md`
(on branch `docs/handoff-2026-08-20-round-closing`, pushed, no PR opened).

## Task(s)

| PR | Branch | Head | State | Status |
|----|--------|------|-------|--------|
| #53 Phase E1 dry-run | `codex/phase-E-provision-dry-run` | — | **MERGED** 23:40Z | **done** — `main` is `a8a430b` |
| #55 Phase E9 backup-before-migrate | `codex/phase-E9-backup-before-migrate` | `e66e71d` | BLOCKED | **in progress** — gate running |
| #56 Phase A controller identity | `codex/phase-A-controller-identity` | `79ca75e` | **DIRTY** | **not started** — conflicts with `main`, Codex has never answered for this head |

`responds to Codex review` commits since `main`: #55 = 41, #56 = 26.

Standing user decisions still in force:
- **Keep closing rounds.** Merge each PR as its gate goes green.
- **Land #53 and #55 first**; #56 last, because their merges change its diff anyway.
- The user fixes branch protection themselves; I never touch it.

## Critical References

- `AGENTS.repo-invariants.md` — now the centre of gravity for #55. Three invariants:
  the gate-authorisation one (pre-existing), "a report describes the branch the worker
  will actually take" (from #53), and "a validated pathname is not a claim on the entry
  behind it" (from #55, extended four times).
- `AGENTS.md` "Fix the invariant, not the instance" — a class reported **twice** is one
  missing rule; write it into `AGENTS.repo-invariants.md` and ship the check in the same
  commit. This is the single most load-bearing rule in the repo for this work.
- `thoughts/shared/plans/2026-08-06-phase-DE-pilot.md` — Step E9 inventory at `:299`,
  `**Branches:**` at `:303`.

## Recent changes

All on #55 unless noted. Every one is in `control_plane/backup.py` plus tests in
`tests/control_plane/test_migrate_on_start.py`.

- **Identity travels with the operation.** `_content_identity` returns `(inode, digest)`
  from ONE `O_NOFOLLOW` descriptor via `os.fstat`, comparing the name to the descriptor
  before AND after hashing (`_Substituted` otherwise). Absent is `(None, None)`;
  unreadable RAISES — a failure is not an identity, or two failures compare equal.
- **`_claim` returns what it published**, taken from the source BEFORE `os.link`;
  `_publish` passes it through; `_copy_install` and restore's marker publication use it.
  Nothing reads a destination back to learn what it owns.
- **Ownership compares content everywhere it is asked**: `_claim`, `_copy_install`,
  `_withdraw`, `_withdraw_pause`, `_require_identity`, `_read_only_sqlite`'s sidecar
  cleanup, `_is_a_copy`'s probes, `_undo`, and both install verification loops.
- **`_read_only_sqlite` captures sidecar ownership at CREATION**, forcing `-wal`/`-shm`
  into existence with `PRAGMA schema_version` before yielding.
- **`_Move` carries `published_digest` and `identity_proven`**; `_undo` never moves an
  unproven or altered member into an authoritative namespace, and reports it instead.
- **`IncompleteReversal`** installs a durable fail-safe pause (`O_CREAT|O_EXCL|O_NOFOLLOW`)
  and names every outstanding move before the writer locks are released.
- **`create_backup` stages the image in a per-invocation 0700 `mkdtemp` directory.**
- `control_plane/cli.py` — the backup command carries the validated identity into
  `create_backup` and turns a `BackupError` into an exit message.

## Learnings

1. **Branch protection required five checks that nothing produces.** `main` required
   `lint`, `unit`, `integration`, `e2e`, `coverage` — none of which any workflow emits —
   so every PR was permanently `BLOCKED` no matter how green. #61 had been merged by
   admin override. The user fixed it to the three real contexts, and #53 auto-merged
   within seconds. **If a PR is green everywhere and still BLOCKED, read
   `/branches/main/protection` before anything else.**

2. **I handed the user a command with a literal `...` placeholder and they ran it**,
   which briefly left `main` requiring a context named `...` and NOT requiring the Codex
   gate. Never write a placeholder into a command that looks runnable.

3. **Switching branches deletes a file tracked on one and not the other.**
   `AGENTS.repo-invariants.md` is tracked on #53's branch; on #55's it was not, so
   `cat >>` created it fresh holding only the new section. Merging would have deleted
   main's gate invariant. Caught only because the PR went `DIRTY`. **After appending to
   a shared file, check `git diff` shows an append and not a rewrite.**

4. **Inode equality is not identity, for two measured reasons.** A descriptor rewrites in
   place without the inode moving; and Linux hands the just-freed inode straight back, so
   an unrelated file at a vacated name carries the departed one's inode. Four of my tests
   passed on macOS and failed on CI for exactly this. Both reasons are recorded.

5. **Writing the invariant down changed the argument.** Since `AGENTS.repo-invariants.md`
   gained the ownership rule, every Codex finding on it CITES the file. The rounds stopped
   being "is this a bug" and became "here is another site your own rule covers". The class
   check `test_no_ownership_check_accepts_a_recycled_inode` is parameterised over four
   sites so the fifth is found by running something.

6. **A test that agrees with itself is the recurring failure mode of this session.**
   Instances: a coverage check using an invented operation name on both sides; a fixture
   rewriting a member during `_publish`, which is before `_copy_install` records what it
   landed; interposing on a primitive AFTER it returns, which is after its unlink; a copy
   window deriving its source from the destination by string substitution when the two
   bundles have different basenames; `_constrain` making the install cross-filesystem so a
   `_rename_or_exdev` hook never fires. **Mutate every fix. A surviving mutation is a
   broken test, not a safe one.**

7. **The scope check's `**Files:**` regex was line-anchored** and E9's inventory wraps
   across four lines, so it reported nearly every E9 path undeclared the moment #55
   inherited it from main. It failed closed — but a rule that rejects correct branches
   gets switched off, and then it enforces nothing.

8. **Assertions that were too strong taught something each time.** Refusing to move an
   interloper back is correct and leaves the candidate short a member, so the promise is a
   disjunction: intact candidate, or a reported incomplete reversal with a pause. And a
   symlink at the marker name means no fail-safe pause can be written without following
   it — "do not start the service" is the other acceptable outcome.

## Artifacts

Created this session:
- `thoughts/shared/handoffs/general/2026-08-21_02-58-08_general_e9-ownership-invariant-and-pr-56.md` (this file)

Modified on `codex/phase-E9-backup-before-migrate` (#55):
- `control_plane/backup.py`, `control_plane/cli.py`, `control_plane/db.py`,
  `control_plane/config.py`, `deploy/control-plane/Dockerfile`,
  `docs/control-plane-restore.md`, `AGENTS.repo-invariants.md`,
  `tests/control_plane/test_migrate_on_start.py`, `tests/control_plane/test_db.py`,
  `tests/control_plane/test_plan_inventory.py`,
  `thoughts/shared/plans/2026-08-06-phase-DE-pilot.md`

Merged to `main` via #53: the Step E1 dry-run rehearsal, the report/worker invariant, and
`tests/control_plane/test_plan_inventory.py`.

## Action Items & Next Steps

1. **Finish #55.** Read each verdict's INLINE comments — findings never appear in the
   review body:
   ```
   RID=$(gh api repos/dsmolchanov/abrolia/pulls/55/reviews --paginate \
     --jq '.[] | select(.body|contains("<head7>")) | .id')
   gh api repos/dsmolchanov/abrolia/pulls/55/comments --paginate \
     --jq ".[] | select(.pull_request_review_id == $RID) | \"=== \(.path):\(.line) ===\n\(.body)\""
   ```
   Distinguish findings from a Codex timeout with the run's last `##[error]` line.

2. **Then integrate #56, which is `DIRTY`.** Scouted, not started. One real conflict, in
   `control_plane/provisioning/worker.py`, and it is a design collision rather than a
   textual one: #56 generalised the consent check to any purpose (`CURRENT_RECEIPT_SQL` +
   `current_receipt_params(household_id, purpose)`) while `main` now carries the narrow
   `_has_current_email_content_restriction` that #53's `provision.py` shares as its
   predicate. Both symbol sets survive the `consent.py` auto-merge. **The resolution is to
   keep #56's general form and implement the narrow one on top of it**, preserving both
   PRs' invariants — and then `requires_current_content_restriction` must still exist,
   because `provision.py` imports it. The other two conflict hunks (an import block and
   `COMPENSATED_STEP_KINDS` beside `requires_current_content_restriction`) are unions.

3. **#56's gate has never answered for `79ca75e`** — one run, `32410575176`, from
   2026-08-20T19:47Z, timed out at 900s; two earlier attempts did the same. Merging #55
   moves #56's diff and forces a fresh generation, which is the cheapest thing to try.
   The user chose that over pushing a no-op commit, the `needs-human` label, or splitting
   the PR. If it times out again after the rebase, ask before spending more generations.

4. **Every merge re-`BEHIND`s the rest.** `gh pr update-branch`, wait for the gate on the
   new head, then merge. Budget one review generation per PR per rebase.

## Other Notes

- Verified at handoff: `ruff check control_plane tests` clean; `pytest -m "not live"` fully
  passing on #55. `tests/live/test_nerve_phase3_contract.py` needs
  `ABROLIA_NERVE_LIVE_CONFIRM` and is expected to fail without `-m "not live"`.
- Findings per round on #55, last ten: `1 4 3 3 4 1 2 3 1 4`. Not converging on that
  measure. What did change is their character: the last several rounds cite the recorded
  invariant and name sites rather than arguing defects. One round was clean and the only
  reason it did not merge was CI, which my own tests had broken.
- Local venv: `/private/tmp/claude-501/-Users-dmitrymolchanov-Programs-abrolia/0ff9f1c3-dfc0-4b2a-829f-40fef9f98099/scratchpad/venv/bin/python`
  (session-scoped; a new session must build its own).
- Untracked and deliberately uncommitted: `.phase-b-delete-operator.py`,
  `.phase-b-runtime-operator.py`, `.phase-b-tamper.py`, `landing/`.
- Eight stale worktrees under `/private/tmp`, all prunable; `git worktree prune` clears
  them. Left alone as out of scope.
- A background Monitor watches #55's gate only. It dies with the session, and its dedup is
  a plain string — an associative-array version silently failed to seed.

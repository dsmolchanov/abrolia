---
date: 2026-08-20T21:05:29Z
researcher: Dmitry Molchanov
git_commit: 99c32ad5fb19041870460ecfb5622667a784e987
branch: codex/phase-E9-backup-before-migrate
repository: dsmolchanov/abrolia
topic: "Closing Codex review rounds on PRs #53, #55, #56 — Implementation Strategy"
tags: [codex-review-window, control-plane, backup, restore, provisioning, consent, plan-inventory, mutation-testing]
status: complete
last_updated: 2026-08-20
last_updated_by: Dmitry Molchanov
type: implementation_strategy
---

# Closing Codex review rounds on PRs #53, #55, #56

Continues `thoughts/shared/handoffs/general/2026-08-20_07-58_general_codex-gate-repair-and-pr-queue.md`
(landed on `main` via #60; not present on these branches).

## Task(s)

| PR | Branch | Head | State | Status |
|----|--------|------|-------|--------|
| #53 Phase E1 provision dry-run | `codex/phase-E-provision-dry-run` | `0ce5113` | BEHIND | **in progress** — gate running |
| #55 Phase E9 backup-before-migrate | `codex/phase-E9-backup-before-migrate` | `99c32ad` | BEHIND | **in progress** — gate running |
| #56 Phase A controller identity | `codex/phase-A-controller-identity` | `79ca75e` | BEHIND | **blocked on Codex, not on findings** |
| #59 gate-sync | `chore/gate-sync-hardened` | — | CLOSED | **done** — closed as stale, per user decision |
| #61 gate stub | `chore/gate-stub` | `fc5a1fe` | MERGED 20:55Z | **done** — not my work; changes the gate under all three PRs |

Fix-commit counts (`responds to Codex review`) since `main`: #53 = 26, #55 = 27, #56 = 26.

Standing user decisions from this session, still in force:
- **Keep closing rounds** rather than escalating to a human or splitting PRs.
- **Merge each PR as its gate goes green** — no separate human review gate.
- #53 was **shrunk** (report scope narrowed) after its findings diverged 2→4→5→6.
- #55 stays **one PR**; its `install-rollback` command was **shrunk** (refuse rather than free space).

## Critical References

- `thoughts/shared/plans/2026-08-06-phase-DE-pilot.md` — the authoritative plan.
  Step E1 inventory at `:170`, Step E9 at `:295`, Phase E acceptance smoke check at `:377`.
- `AGENTS.md` — "Code Review Rules"; `AGENTS.md:8-17` is what makes an undeclared
  changed file a merge-blocking deviation.
- `CLAUDE.md` — fix-commit conventions; never modify `AGENTS.md`, `CLAUDE.md`,
  branch protection, or `.github/workflows/codex-review-window.yml` from a fix pass.

## Recent changes

### #53 — `codex/phase-E-provision-dry-run`

Round of 3 (post-shrink), commit `07f3648`:
- Shared consent predicate `CURRENT_RESTRICTION_RECEIPT_SQL` in `control_plane/privacy/consent.py`,
  read by `_holds_current_restriction` in `control_plane/onboarding/provision.py` and by
  `ProvisioningWorker._has_current_email_content_restriction` in `control_plane/provisioning/worker.py`.
  Same remedy as `LEASABLE_SQL`: the report asks the worker's own question rather than restating it.
- `STEP_WRITES_BY_KIND` re-attached **per job** in `pending_step_jobs` at
  `control_plane/onboarding/provision.py:980` rather than chosen for the top-level `table_writes` —
  the write set of a kind is static; only the CHOICE of next job was a prediction.
- Phase E smoke check rewritten to describe the shrunk schema, `thoughts/shared/plans/2026-08-06-phase-DE-pilot.md:377`.

Round of 1, commit `0ce5113`:
- `consent.py`, `worker.py`, `repositories/jobs.py` added to the Step E1 `**Files:**` inventory
  (`jobs.py` had been declared in prose only).
- **New:** `tests/control_plane/test_plan_inventory.py` — the class regression Codex asked for.

### #55 — `codex/phase-E9-backup-before-migrate`

Round of 4, commit `d67197a`, all in `control_plane/backup.py`:
- `_Move` dataclass (`control_plane/backup.py:256`) replaces `tuple[Path, Path]` journal entries.
  Records `published_inode` and `source_removed` separately. `_claim` sets `source_removed`
  only after the unlink returns; `_copy_install` does the same.
- `_undo` (`control_plane/backup.py:~1230`) now branches on three states: destination gone,
  published-and-source-removed (move back), published-and-source-still-there (withdraw the
  destination durably). Guards every action on `_inode(destination) == move.published_inode`.
  A cross-filesystem reversal falls back to `_copy_install` instead of failing `EXDEV`.
- `_read_only_sqlite` context manager replaces the two separate read-only opens. Refuses
  non-regular bundle members before opening; snapshots with `_present` (lexists); removes a
  created sidecar only while its inode still matches.
- `require_control_plane_database` validates the ledger against `_shipped_migrations()`
  (read from `control_plane.db.MIGRATIONS_DIR`). Accepts a prefix (failed pending migration)
  and a longer tail (image rolled back); refuses anything that diverges. `_MIGRATION_NAME`
  regex deleted — the shipped list is the authority.
- `_require_free_bundle` extracted and called at three points in the restore:
  before the lock, under the lock, immediately before publication.

Commit `99c32ad`: fixed a flake in my own test (see Learnings).

## Learnings

1. **The gate workflow changed under all three PRs mid-session.** #61 merged at
   `2026-08-20T20:55:23Z` (`main` = `7970d80`), replacing the 460-line in-repo workflow with a
   52-line stub calling `dsmolchanov/codex-review-gate` pinned at `8cafcd27`. The required check
   name became `codex-review-window / codex-review-window` (GitHub names a reusable workflow's
   run `<caller job> / <called job>`). #56's head predates this, so it still reports the OLD name
   `codex-review-window`. **Any script matching the check by exact name will silently miss one of
   the two forms** — match with `startswith("codex-review-window")`.

2. **#56 is blocked on Codex's silence, not on findings.** Codex's last review of that branch was
   of `07e8089` at 19:42Z. Head `79ca75e` has had three gate attempts (two runs plus a rerun of
   `32410575176`) and every one ended `No Codex verdict ... within 900s`. Meanwhile #55 got a
   review at 20:21Z and #53 at 20:39Z, so Codex is alive — it is specifically not answering for
   this PR. Each rerun costs a review generation. **Do not keep rerunning it blindly.**

3. **`/tmp` recycles freed inodes, and a test about inode identity must not depend on luck.**
   `test_sidecar_cleanup_removes_only_the_entry_it_created` unlinked an entry and wrote a new one
   at the same name; locally the inodes differed, on the CI runner the filesystem handed the freed
   inode straight back, the cleanup deleted the replacement, and the test failed — reporting the
   exact defect it exists to catch. Fixed by writing the replacement elsewhere and `os.replace`-ing
   it in, so the new inode is allocated while the old is still linked, plus an assertion that the
   two actually differ before drawing any conclusion.

4. **A mutation that survives means the test names the wrong thing.**
   `test_a_sidecar_appearing_under_the_lock_stops_the_restore` passed with the locked recheck
   reverted, because a LATER check caught it. Split into two tests: the locked one asserts
   `_restore_locked` was never entered (fail fast, before a database-sized image is decrypted);
   the pre-publication one interposes on `sqlite3.connect` during the integrity check.
   Eight mutations were run against #55's four fixes; all eight are killed.

5. **Three rounds went to the same plan-inventory defect**, each found by reading. That is a
   mechanism failing, not three oversights. `tests/control_plane/test_plan_inventory.py` now parses
   every `**Files:**` block under `thoughts/shared/plans/`, extracts backticked paths (expanding
   `{a,b}.py` braces and `*` globs), and fails on a changed path matching none. It deliberately
   does NOT decide which step a file belongs to — that is a judgement it would get wrong.
   `thoughts/` is exempt, or the commit that fixed the plan would fail the check.

6. **Shrinking works.** #53's findings per round ran 2 3 2 3 3 3 2 3 2 5 3 6 1 2 2 2 3 2 4 5 6 —
   diverging. After the `−207`-line shrink: 3, then 1. #55's ran 1 2 1 1 1 3 1 3 1 3 4 4 5 2 3 5 3
   3 2 4 2 3 1, then 4. #56's have stayed at 1–3 throughout.

7. **The plan file list is the gate's real target on doc rounds.** Codex reads `**Files:**` lines,
   not the prose beneath them. `repositories/jobs.py` was described at length in Step E1's prose
   and still counted as undeclared.

## Artifacts

Created:
- `tests/control_plane/test_plan_inventory.py` (#53)
- `thoughts/shared/handoffs/general/2026-08-20_23-05-29_general_codex-round-closing-three-prs.md` (this file)

Modified on `codex/phase-E-provision-dry-run` (#53):
- `control_plane/onboarding/provision.py`, `control_plane/privacy/consent.py`,
  `control_plane/provisioning/worker.py`, `tests/control_plane/test_provision_dry_run.py`,
  `thoughts/shared/plans/2026-08-06-phase-DE-pilot.md`

Modified on `codex/phase-E9-backup-before-migrate` (#55):
- `control_plane/backup.py`, `tests/control_plane/test_migrate_on_start.py`

## Action Items & Next Steps

1. **Read the gate verdicts for `0ce5113` (#53) and `99c32ad` (#55).** Findings arrive as INLINE
   review comments, not in the review body:
   ```
   RID=$(gh api repos/dsmolchanov/abrolia/pulls/<N>/reviews --paginate \
     --jq '.[] | select(.body|contains("<head7>")) | .id')
   gh api repos/dsmolchanov/abrolia/pulls/<N>/comments --paginate \
     --jq ".[] | select(.pull_request_review_id == $RID) | \"=== \(.path):\(.line) ===\n\(.body)\""
   ```
   The workflow log's final `##[error]` line distinguishes findings from a timeout.

2. **Decide what to do about #56.** Four options, in the order I would try them: (a) push a
   trivial no-op commit to move the head, which sometimes shakes Codex loose; (b) post
   `@codex review` manually as `dsmolchanov` and watch whether a review lands at all;
   (c) apply the `needs-human` label, which the gate honours, and merge on human review;
   (d) leave it and land #53/#55 first, since a merge changes #56's diff anyway.
   **This needs a user decision — do not pick silently.**

3. **Merging serializes.** All three are BEHIND `7970d80`. Auto-merge is armed but does NOT
   update stale branches, and branch protection requires up-to-date. Each merge re-BEHINDs the
   others. For each: `gh pr update-branch <N>`, wait for both checks, then merge.
   Note the update-branch merge commit re-triggers the gate — budget a review generation per PR
   per rebase.

4. **Mutation-test every fix.** A MISSED result means the test is wrong, not the fix. Roughly a
   dozen broken tests were caught this way across the session.

## Other Notes

- Verified state at handoff: `ruff check control_plane tests` clean; `pytest -m "not live"` fully
  passing on both #53 and #55 branches. `tests/live/test_nerve_phase3_contract.py` requires
  `ABROLIA_NERVE_LIVE_CONFIRM` and is expected to fail without `-m "not live"`.
- Local venv: `/private/tmp/claude-501/-Users-dmitrymolchanov-Programs-abrolia/0ff9f1c3-dfc0-4b2a-829f-40fef9f98099/scratchpad/venv/bin/python`
  (session-scoped; a new session must create its own).
- Untracked and deliberately not committed: `.phase-b-delete-operator.py`,
  `.phase-b-runtime-operator.py`, `.phase-b-tamper.py`, `landing/`.
- A background Monitor (task `bsmbjh9md`) is watching all three gates and reports
  `PR #N <head> gate=<status> merge=<state>` on each completion. It dies with the session.
- Memory worth keeping is already saved: `pr-merges-serialize.md` and
  `codex-gate-needs-connected-identity.md` in the project memory directory.

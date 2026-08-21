# Repository blocking invariants

This file is **owned by this repository**. `plaintalk-dev-agent` installs it once
and never overwrites it, so anything added here survives every fleet-wide policy
refresh.

The fleet-wide invariants live in the `dev-agent:policy` managed block in
`AGENTS.md` and are replaced wholesale on each refresh — do not add
repository-specific entries there, they will be deleted.

## How to use this file

Add an entry when a P1 class keeps coming back; `AGENTS.md`, "Fix the invariant,
not the instance", says at what point. A repeated finding is
a missing rule, not a new discovery: semantic review is an expensive way to
rediscover the same defect, and each round costs a full review generation.

Per the fleet policy, the commit that fixes a recurring P1 should also add the
deterministic check that would have caught it — a lint rule, a test, or a
migration assertion. This list is the human-readable index of those rules, not a
replacement for them.

Each entry should be a closed question a reviewer can answer yes or no. "No route
without an auth dependency" is checkable; "input should be validated" is not, and
an open-ended predicate is satisfiable on any non-trivial diff, which is what
makes a review loop unable to terminate.

## Invariants

<!-- Add entries below. Example shape:

- No handler under `apps/api/routes/` may be registered without a
  `Depends(require_tenant)` argument. Enforced by `tests/test_route_auth.py`.

-->

### Merge authorisation comes only from a verified current-head verdict

- **No `exit 0` in `.github/workflows/codex-review-window.yml` may be reached
  except from a Codex signal bound to the CURRENT head commit and verified to
  carry no P0/P1 marker.** There are exactly two such paths, and the count
  itself is asserted. Absence of a verdict, absence of a severity marker in a
  comment that is not the verdict, a verdict for another head, a signal from any
  account other than the Codex bot, a repository-wide opt-out, and any GitHub
  API failure are all NOT authorisations. Enforced by
  `tests/test_gate_workflow.py` (static invariants, including the exit-0 count)
  and `tests/test_gate_behavior.py` (executes the script against a stubbed
  GitHub API and asserts the exit code on every path).

  Recorded after three instances of this class arrived in a single review, plus
  a fourth found by the tests themselves: a marker-absence predicate that read
  Codex's "create a Codex account" error as approval; a clean signal honoured
  without re-querying for a formal review published moments later; a
  `fast-merge` repository topic that disabled the gate for every pull request
  indefinitely; and an unterminated `printf` that made the clean-comment count
  one short. They are one missing rule, not four findings.

  A corollary worth stating because it cost six days: the gate must ASK from an
  identity Codex will serve. Codex refuses requests from `github-actions[bot]`,
  which has no connected account, so the anchor goes out under
  `CODEX_REQUEST_TOKEN` and an unauthenticated token fails closed rather than
  falling back.


### A validated pathname is not a claim on the entry behind it

- **No recovery operation may delete, publish over, or install a filesystem
  entry on the strength of a check made at an earlier moment. A removal is
  authorised only while the entry is still the one this call published or
  copied; an install is authorised only while the member is byte-identical to
  what preflight validated; and a file this module intends to trust is opened
  ONCE, with `O_NOFOLLOW`, and every later question about it — size, digest,
  contents — is answered from that descriptor.** Enforced by
  `tests/control_plane/test_migrate_on_start.py::test_no_move_primitive_deletes_an_entry_it_stopped_owning`
  (every move primitive × regular-file, hard-link and symlink sentinels),
  `::test_no_validated_member_is_installed_after_it_changes` (in-place rewrite
  through a descriptor, which leaves the inode unchanged) and
  `::test_a_landed_member_removed_before_success_is_not_a_success`.

  Recorded after this class arrived in seven separate rounds on one pull
  request. The instances: a read-only validation deleting a dangling sidecar
  symlink it never created; the volume probe unlinking both names
  unconditionally after one had been vacated; `_undo` moving a destination
  another process had replaced; restore's cleanup unlinking a pause marker
  belonging to somebody else's restore; `create_backup` archiving a database
  renamed after it was authenticated; the fail-safe pause following a symlink
  and truncating its target; and the install loop consuming candidate
  pathnames rather than the entries that passed preflight. They are one
  missing rule, not seven findings.

  The general shape is TOCTOU, and the general remedy is that identity travels
  with the operation rather than being re-derived from the filesystem. Inode
  equality is necessary and NOT sufficient, for two measured reasons. A process
  holding an open descriptor rewrites a file in place without the inode moving.
  And a filesystem hands the just-freed inode straight back — Linux did, on the
  CI runner, where an unrelated file written at a name this operation had
  vacated landed on the very same `(st_dev, st_ino)` and an inode-only check
  called it ours. Anything whose correctness depends on WHICH FILE this is
  compares a digest taken at validation time, at every point that asks and not
  only at the reversal. "Every point" has had to be swept twice; the class
  check `test_no_ownership_check_accepts_a_recycled_inode` parameterises the
  sites so the next one is found by running something rather than by reading.

  Ownership is captured when the entry is CREATED, not when it is cleaned up.
  Capturing at cleanup records whoever holds the name by then, which for
  `_read_only_sqlite` meant an interloper's sidecar was recorded as owned and
  unlinked. Where creation is somebody else's to do — SQLite makes `-wal` and
  `-shm` on first read, not at connect — the operation forces it, with a
  statement of its own, before any caller code can run.

  An identity needs something to distinguish it: the volume probes carry random
  bytes because every empty file has the same digest, so identifying them by
  content would be identifying them by nothing.

  This applies to REVERSAL as much as to publication, which is the half that
  was missed first: `_undo` verified the inode alone, so a landed member
  rewritten in place was carried back into the canonical namespace as though it
  were the validated generation, with nothing reported and no fail-safe pause.
  A move records what it published, and anything else at that name — a
  different entry or the same entry with different bytes — is not this
  operation's to move.

  Two corollaries, each of which was a finding before it was a rule. An
  identity is read from ONE descriptor: taking the digest through the open file
  and the inode by pathname afterwards pairs one file's bytes with another
  file's inode. And a failure is not an identity: answering `(inode, None)` for
  a member that would not open made two transiently failing states compare
  EQUAL, so an install or a reversal could be licensed by a pair of failures.
  Absent is an identity; unreadable is not. The identity of a publication is
  taken from the entry the call already OWNS, before the move — read back from
  the destination afterwards, it describes whoever holds that name by then, and
  the cleanup is authorised against their file. And where a path must be handed
  to something that cannot take a descriptor — SQLite, which is given a
  filename — the file lives in a per-invocation directory created 0700 with an
  unpredictable name, because the window cannot be removed but everyone else's
  ability to use it can.
### A report describes the branch the worker will actually take

- **Any job the dry-run report annotates with `blocked_by` and `table_writes`
  must be one the worker settles WITHOUT resolving a provider, and settling it
  must write exactly the declared tables. While any such annotation is present,
  the report must claim no top-level `table_writes`.** Enforced by
  `tests/control_plane/test_provision_dry_run.py::test_every_annotated_job_agrees_with_one_real_worker_call`,
  which crosses every gated job shape with every invalid receipt state, traces
  one real `run_once()`, and asserts the provider registry was never consulted;
  and by `test_the_gated_shapes_cover_the_worker_s_own_predicate`, which asks
  `requires_current_content_restriction` about every kind the schema allows so a
  newly gated shape cannot slip past that parameter list.

  Recorded after five instances of one class arrived across the E1 review
  rounds. Each was the report describing an operation other than the one the
  worker would perform: a stale consent receipt reported as an ordinary
  provider call; the receipt checked for a runtime job and not for its email
  siblings; a configuration mismatch narrated over a household being deleted or
  an unresolved earlier intent; a cancellation label narrated over a quarantined
  intent that had already reached its provider; and a single top-level write set
  chosen from an unordered scan, which is a claim about which queued job the
  worker reaches first. They are one missing rule, not five findings.

  The rule has two halves and both are load-bearing. A report may state what a
  job's next act writes only where durable state fully determines it — which is
  exactly the case where no provider is consulted — and it may never rank jobs,
  because `JobsRepository.lease` decides that and its answer depends on
  `not_before`, held leases, paused workers and creation order. Every attempt to
  predict the ranking has produced a finding; attaching facts to each job has
  produced none.

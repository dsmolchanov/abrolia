---
date: 2026-08-20T07:58:00+0000
researcher: Claude Opus 5
git_commit: 79666e96
branch: codex/phase-E-provision-dry-run
repository: abrolia
topic: "Codex gate repair, the identity/permission root cause, and the four-PR queue"
tags: [codex-review-window, gate, phase-A, phase-E1, phase-E9, art9, withdrawal, backup]
status: complete
last_updated: 2026-08-20
last_updated_by: Claude Opus 5
type: implementation_strategy
---

# Handoff — the gate is fixed; four PRs, five findings left

## The one thing to know

**The six-day stall was never a timeout.** It was two failures stacked:

1. The gate asked Codex as `github-actions[bot]`. Codex refuses identities with
   no connected account and replies *"To use Codex here, create a Codex account
   and connect to github"* within seconds. **13 requests over 6 days, 0 reviews.**
   The gate then waited out its window and reported "no verdict arrived", which
   reads as flakiness. Meanwhile #53/#55/#56 were each carrying real, unseen P1s.
2. Once `CODEX_REQUEST_TOKEN` was added, the PAT authenticated but lacked
   **`Pull requests: Read and write`** — `Issues` is not enough, because
   `POST /issues/{n}/comments` enforces the permission matching the RESOURCE,
   and every comment the gate posts is on a pull request.

Both are resolved. **Verified working 2026-08-20 07:31Z**: the gate posted its
own anchored requests on #53 and #55 as `dsmolchanov`, and real reviews came
back. Do not go back to posting `@codex review` by hand — the gate's own request
carries the inventory-scoped prompt, and the review quality is visibly better
for it (findings now name a root invariant, sibling consumers, confidence, and a
proposed class regression).

## PR queue

| PR | Head | Gate | State |
|---|---|---|---|
| #57 | merged | — | Policy v2 gate on `main` |
| #58 | `c0a4f37` | **pass** | **Merge this first.** Gate misreport fix + permission probe |
| #53 | `79666e9` | pending | 24 blockers fixed; awaiting verdict on this head |
| #55 | `68c0ecf` | fail | 3 findings open (below) |
| #56 | `eafb317` | fail | 1 finding open (below) |

`origin/main` is still `2588026`. Nothing from the feature branches has merged.

## Open findings

**#55 — Phase E9 (3):**
- `docs/control-plane-restore.md:124` — the `/tmp` staging path still leaves the
  superseded db, WAL, SHM and pause marker on `/data`, so installing the staged
  restore copies a third database-sized file onto an already-full volume and
  still ends in `ENOSPC`. Codex asks for a constrained-filesystem integration
  test that executes the low-space branch, not prose assertions.
- `control_plane/backup.py:72` — `create_backup` fsyncs the temp archive and
  renames, but never fsyncs the destination DIRECTORY. On power loss after the
  call returns and while the migration commits, the directory entry can vanish
  while the upgraded database survives — defeating the only rollback guarantee.
- `thoughts/shared/plans/2026-08-06-phase-DE-pilot.md:299` — `tests/control_plane/test_db.py`
  is changed but not in the Step E9 file list.

**#56 — Phase A (1):**
- `control_plane/onboarding/service.py:938` — `disconnect_email_for_withdrawal`
  takes a snapshot of `external_resources`; an email `ensure`/`inspect` that has
  crossed the Nerve/Google boundary but not yet recorded its row gets no
  cleanup. `_handle_provider_waiting` then records a late reference with no
  teardown job, and `_finish_step` fails `mark_verified` because the identity is
  disconnecting and discards the result. Leaves an untracked inbox receiving
  after withdrawal. Needs barrier-controlled tests for both a ready result and
  `ProviderWaiting` arriving after withdrawal.

## What was fixed this session (~55 blockers, all mutation-verified)

**#57 / #58 — the gate itself.** Policy v2 arrived with three fail-open paths
(marker-absence read as approval, no grace re-query before honouring a clean
signal, an unbounded `fast-merge` repo-topic bypass). All closed. Two test
suites added: `tests/test_gate_workflow.py` (static invariants incl. the exit-0
count) and `tests/test_gate_behavior.py` (executes the script against a stubbed
API with real jq). They immediately found a fourth defect review had missed:
`api()` left its last line unterminated, so `wc -l` counted one short and the
clean-verdict path could NEVER fire. #58 then fixed the gate announcing a 403 as
a successful post, and added the probe that diagnosed the PAT permission.

**#56 — Phase A.** Web onboarding couldn't satisfy its own gate; consent
enforced by presence rather than currency at four boundaries; no country gate
existed at all (fixture was `CZ`); the restriction copy forbade what the
Art 9(2)(a) consent permits (bumped to v2); Art 7(3) withdrawal was promised in
the consent text and implemented nowhere — now marks the receipt, revokes
standing revisions, disconnects the upstream inbox, and pushes a stop to the
live runtime; DSAR survived neither withdrawal nor the v2 bump until fixed;
Gmail bypassed the gate entirely with `ABROLIA_REAL_EMAIL_ENABLED=0`.

**#55 — Phase E9.** Unpadded backup key decoded to `b""` and stopped the
container booting; rollback restored to a path `fly.toml` never opens; migration
batch not atomic across files; snapshots accumulated on a restart loop; reuse
compared timestamps (which a failed migration moves) instead of content; RAM
then volume exhaustion; the ledger created outside the batch transaction.

**#53 — Phase E1.** The dry-run migrated the database it promised not to touch,
created one that was absent, rewrote a persistent journal mode, checkpointed the
live WAL on close, read its report non-atomically, traced a discarded revision
N+1, reported Fly resources for a provider that creates none, and inventoried
deleted secret bindings as installed. It now rehearses against an online backup
taken from a READ-ONLY source, which makes "mutates nothing" structural.

## Learnings

1. **Make the property structural, not a list.** #53 spent six rounds closing
   individual ways SQLite writes; the answer was to rehearse against a snapshot,
   after which the property holds by construction. Same shape in #56 (page and
   gate now share one predicate) and in `api()` (fix the wrapper, not the caller).
2. **Mutation-test every fix, and distrust a MISSED result.** Three mutations
   this session came back MISSED because the TEST was wrong, not because the fix
   was unnecessary — most instructively, a byte-identity test that needed a
   writer which *dies*, an explicitly closed seed handle, and `-shm` excluded
   before it caught anything. A green suite proves nothing on its own.
3. **Say what a test does not prove.** The concurrency test on #53 does not
   reproduce the torn-copy race — the old code still passes it. Its docstring
   says so. A test that looks like a guard and is not one is worse than none.
4. **Measure before building on a mechanism.** SQLite's file change counter
   looked like the right "has this changed" signal and does not move reliably
   under WAL; a disk-backed backup is not byte-identical to `serialize()`;
   read-only opens do not checkpoint but do touch `-shm`. Each was checked.
5. **Several findings were consequences of my own previous fixes** — a RAM
   problem moved onto the volume, DSAR fixed for withdrawal but not for the copy
   bump shipped hours earlier, one half of a report fixed while the other
   contradicted it. Expect this when moving fast across four PRs.

## Action items

1. **Merge #58.** It is green, and it is the diagnostic that unblocked
   everything. Without it a future token misconfiguration is again logged as a
   success.
2. **Take #53's verdict** when the gate finishes on `79666e9`.
3. **Fix #55's three and #56's one** (above).
4. **Phase A's real blocker is still not code**: unsigned DPAs / SCC annexes /
   TIAs for P1 Anthropic, P2 Fly, P4 Resend, P11, and the undesignated Art 27
   representative (`processors.md` §2 p.4a). No real family data may be
   processed until both are done.
5. **Italy is suspended** from the pilot by owner decision 2026-08-19, recorded
   in `art9.py` and `lawful-bases.md`. It returns to scope only when the Garante
   misure di garanzia are reconciled against our TOMs and recorded.
6. **A second repo invariant is owed** once #56 lands: the consent-currency
   class, whose check ships on that branch
   (`tests/control_plane/test_art9_household_consent.py`). It cannot be
   referenced from `AGENTS.repo-invariants.md` until both are on `main`.
7. **#51 and #54 stay drafts** pending the live batteries on `abrolia-synthetic`;
   **#13 and #29** still need a keep-or-close decision.

## Other notes

- `CLAUDE.md:61` forbids Auto-fix from touching `codex-review-window.yml` in a
  fix commit, which means blockers on the gate itself can only be cleared by a
  human or an interactive session — worth an explicit carve-out for the case
  where the gate file is the PR's own subject.
- The counsel question was settled 2026-08-19: the product owner signs *acting as
  counsel for the controller*, both roles named, with the absence of independent
  review stated in `lawful-bases.md` §3 and `dpia.md` §5 p.7 rather than implied.
- Local checkouts: `~/Programs/abrolia`. Untracked `.phase-b-*.py` operator
  scripts and `landing/` predate this session.

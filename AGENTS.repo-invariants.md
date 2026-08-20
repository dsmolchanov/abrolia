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

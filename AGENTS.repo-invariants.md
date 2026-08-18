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

_None yet._

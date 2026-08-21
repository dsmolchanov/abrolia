---
date: 2026-08-18T23:28:21+0200
researcher: Claude Opus 5
git_commit: 1f2397556db0c9dd1b5e898fb90c4469b6eac11e
branch: codex/phase-A-controller-identity
repository: abrolia
topic: "Canon status, stalled PR queue, and the Nerve detour — Implementation Strategy"
tags: [canon, phase-a, phase-c, phase-e, codex-review-window, nerve-cloud, nerve-oss]
status: complete
last_updated: 2026-08-18
last_updated_by: Claude Opus 5
type: implementation_strategy
---

# Handoff — abrolia canon status and the Nerve detour

## Task(s)

| Work | Phase | Status |
|---|---|---|
| Canon status review | — | **done**; findings below |
| C2/C3 hermetic rescue from stale branches | C | **done**, PRs open as drafts |
| Pre-onboarding dry-run rehearsal | E1 | **done**, PR #53 open |
| Backup before migrate on start | E9 (code half) | **done**, PR #55 open |
| Controller identity, Art 9(2)(a) condition and consent | A | **done**, PR #56 open |
| `cloud-e2e-smoke` repair (nerve-cloud) | — | **done and merged**, green |
| Go 1.25.13 toolchain | — | **done and merged**, both repos |
| nerve-oss#43 adapter blockers | — | **handed back to the owner**, see warning |

**Nothing this session produced in abrolia has merged.** `origin/main` is still
`31a8481`, exactly where the session started.

## Critical References

- `thoughts/shared/plans/2026-08-06-canon-execution-plan.md` — blockers B-01..B-12, phases A–F
- `thoughts/shared/plans/2026-08-06-phase-A-gate1-legal.md` — the only phase whose remainder is not code
- `docs/privacy/lawful-bases.md` §3 — the Art 9(2)(a) decision recorded this session

## Recent changes

Open PRs authored this session, all with green functional checks:

- **#56** Phase A: controller identity, Art 9(2)(a) condition and consent
  - `docs/privacy/lawful-bases.md:120-190` — condition selected, Art 9(4) scan for DE/IT/NL/ES
  - `docs/privacy/dpia.md:57` — R7 moved from `высокий, блокирующий` to `средний, принят`
  - `control_plane/privacy/consent.py:16-31` — new `special_category_household_content` purpose
  - `control_plane/onboarding/service.go` equivalent: `control_plane/onboarding/service.py:262-300` — fail-closed in a real-email rollout only
- **#55** Phase E9 — `control_plane/db.py:218-270`, `control_plane/backup.py:30-49`, `deploy/control-plane/Dockerfile:44`
- **#53** Phase E1 — `control_plane/onboarding/provision.py` (new), `tests/control_plane/test_provision_dry_run.py` (new)
- **#51 / #54** — C2/C3 hermetic work replayed onto current `main`; drafts on purpose

## Learnings

1. **Every abrolia PR is blocked by one thing only: `codex-review-window`.** All
   three ready PRs failed with `No Codex review arrived for <sha>` on
   2026-08-12 — a timeout, not a finding. Functional checks pass on all of them.
   A rerun is one command; the gate is fail-closed by design and must not be
   bypassed with `--admin`.
2. **PR #57 changes that gate.** It is a policy refresh authored by the owner,
   not by this session. It debounces pushes so a burst of commits costs one
   review generation, removes two open-ended P1 classes from `AGENTS.md`, and
   asks for one complete inventory up front. It is plausibly the fix for the
   timeout loop above, so **inspect and merge it before rerunning the others**.
   After merge it requires two manual steps named in its body: the Auto-fix
   smoke test, and appending `dsmolchanov/abrolia` to
   `/data/registry/claude_app_enabled.json` on plaintalk-dev-agent.
3. **The dev-agent merges out of order.** In nerve-cloud it squash-merged a
   stacked PR ahead of its parents twice, which silently carried later layers
   into `main` and left earlier PRs reverting them if merged. Stacked branches in
   these repos need their merge order enforced by hand, or the earlier PRs
   closed as superseded once verified — see nerve-cloud #87/#89/#91/#92/#93.
4. **Review findings are cumulative per commit, not per gate.** The gate counts
   only comments whose `pull_request_review_id` matches the review for the
   current head. A raw count by `commit_id` overstates the live set — on
   nerve-oss#43 that was 13 comments versus 3 live findings. Always resolve the
   review id first.
5. **Two rounds of "fresh evidence after your fix" mean the fix was too narrow.**
   On nerve-oss the pattern only broke when the evaluator's inputs were
   enumerated and closed as a set instead of patching each reported instance.

## Artifacts

Created or updated in abrolia:

- `control_plane/onboarding/provision.py`, `tests/control_plane/test_provision_dry_run.py`
- `control_plane/privacy/consent.py`, `control_plane/models.py`, `control_plane/provisioning/planner.py`, `control_plane/provisioning/manifest.py`
- `control_plane/db.py`, `control_plane/backup.py`, `control_plane/cli.py`, `deploy/control-plane/Dockerfile`
- `docs/privacy/{lawful-bases,dpia,processors,privacy-notice-ru,privacy-notice-en}.md`, `README.md`, `.check-fixtures-allow`
- `docs/control-plane-restore.md`, `docs/onboarding-runbook.md`
- `tests/control_plane/email/{byo_support,test_byo_reload_resume,test_byo_dns_advance,test_byo_domain_race}.py`

In nerve-cloud (merged): `thoughts/shared/plans/2026-08-12-cloud-e2e-smoke-repair.md`,
`thoughts/shared/plans/2026-08-17-go-1-25-13-toolchain.md`,
`.github/workflows/cloud-e2e-smoke.yml`, `scripts/ci/{gen_m2m_signing_keys.go,post_stripe_webhook.sh}`.

In nerve-oss (branch `codex/mcp2026-phase2-adapters`, **owner-driven now**):
`internal/tools/{outbound_policy,visible_text}.go`, `internal/store/feature_flags.go`,
plan §7.2a threat model.

## Action Items & Next Steps

1. **Inspect and merge #57 first.** It changes gate behaviour; merging the
   others first wastes reruns. Then perform the two manual post-merge steps in
   its body.
2. **Rerun the three timed-out gates**, then merge in any order — they are
   independent:
   ```bash
   cd ~/Programs/abrolia
   for p in 56 53 55; do gh pr checks $p | grep codex; done   # confirm still timeout, not findings
   gh run rerun <run-id>                                       # per PR
   ```
3. **Leave #51 and #54 as drafts.** They are hermetic-only and must not merge
   before the operator records the live batteries: real DNS cases 1–8 for C2,
   and the allowlisted Gmail battery for C3. Both are on `abrolia-synthetic`.
4. **Phase A is the pilot's real blocker, and it is not code.** The processors
   registry is still `⏳` for P1 Anthropic, P2 Fly, P4 Resend and P11; those are
   DPAs, SCC module 2 annexes and TIAs to execute with the counterparties. The
   Art 27 Union representative is undesignated — `processors.md` §2 p.4a. Until
   both are done, no real family data may be processed, and marking the registry
   `✅` without signatures would be a false compliance record.
5. **Decide the `help@` vs `support@` question if it resurfaces.** The owner
   chose `help@abrolia.com` everywhere, which avoided bumping the hashed consent
   copy to `-v2` and invalidating issued receipts.
6. **Old PRs #13 and #29** (2026-08-05/06) are untouched by this session and
   need a keep-or-close decision.

## Other Notes

- **Do not push to nerve-oss#43.** The owner took it over directly with
  `06d3040` and `9297c20`; this session had a near-miss where both sides were
  about to fix the same finding. Any further work there needs an explicit
  handover.
- **The Nerve detour was owner-directed** and each step confirmed, but it ran
  long: `cloud-e2e-smoke` in nerve-cloud had never passed in 176 scheduled runs
  since 2026-02-18 and took eight layered fixes to go green. It is green now,
  including under the `schedule` trigger (run `31686818213`).
- MCP 2026 §2.4's 2026 M2M leg is still blocked on `deploy/cloud/runtime.lock`,
  which pins a runtime built before the dual-protocol router. The 2025 legacy
  leg shipped in nerve-cloud#98.
- Local checkouts used: `~/Programs/abrolia`, `~/Programs/nerve-cloud`,
  `~/Programs/nerve-oss`. The abrolia working tree carries untracked
  `.phase-b-*.py` operator scripts and `landing/` that predate this session.

# Validation Report: Canon Execution Plan

**Plan**: `thoughts/shared/plans/2026-08-06-canon-execution-plan.md`
(companion for the current phase: `thoughts/shared/plans/2026-08-22-phase-F-closure-design-pass.md`)
**Validated**: 2026-08-23, at `1aa2461` on `codex/phase-F-allowlist-at-dispatch`.
**Merge state**: PR #70 is already merged — origin/main carries it as squash
`c7e1d36`, and that squash contains ALL of this branch's content (verified by
content markers: M6/M7 mutations and both late-round regression tests are
present on origin/main). The local branch is simply 1 commit behind its own
merge; nothing is outstanding.

## Implementation Status

- ✓ Phase A — Legal & Residency (B-07, B-08): merged (`9a02880`, PR #56).
  eu-strict fail-closed tests exist exactly as demanded and pass
  (`tests/test_config_and_cli.py::test_eu_strict_fails_closed_without_vertex:294`,
  plus manifest/boot variants). Plan acceptance checkboxes left unticked — see
  Deviations.
- ✓ Phase B — Foundation & Org Migration (B-09, B-11): closed in-plan
  ([x] boxes), evidence addendum in
  `thoughts/shared/implementations/2026-08-04-onboarding-foundation-validation.md`;
  commits #41/#49/#50 on main.
- ~ Phase C — Email Identity (B-01, B-02, B-05, B-06): C1/C2 evidenced in the
  two email-identity validation docs. Two items remain open by design:
  **B-06** (dedicated Gmail live/CASA) explicitly deferred per the plan's own
  Phase D closure note, and **B-01**'s upstream Nerve PR evidence cannot be
  confirmed from this repository.
- ✓ Phase D — Real Actions sign-off (B-10): closed with dedicated validation
  doc `thoughts/shared/implementations/2026-08-06-phase-D-real-actions-validation.md`.
- ~ Phase E — Pilotization (B-12): governed by
  `thoughts/shared/plans/2026-08-06-phase-DE-pilot.md` (own plan/validation
  flow); several slices merged (#53 E1 dry-run, #55 E9 backup-before-migrate,
  #62/#63 Web/PWA packaging, #67 options cut). Not re-validated here — outside
  this branch's diff.
- ✓ Phase F — Release Gating & Rollout (this branch): fully implemented,
  reviewed through twelve Codex rounds, merged via PR #70. Deep-validated
  below.

## Automated Verification Results

| Check | Command | Result |
|---|---|---|
| Full non-live suite | `python3 -m pytest -p no:cacheprovider -m "not live"` | ✓ **1472 passed**, 2 deselected, exit 0 |
| Whitespace gate | `git diff --check` | ✓ clean |
| Lint | `ruff check .` | ✓ all checks passed |
| Secret scan | `gitleaks detect --source . --config .gitleaks.toml --log-opts="--all" --redact` | ✓ no leaks across 344 commits |
| Sanitizer | `python3 scripts/check_fixtures.py --all --require-deny` | ⚠ scan itself clean ("чисто"); `--require-deny` not fully verifiable locally — private deny-patterns file (`~/.config/hermes-cloud/deny-patterns.txt`) absent on this machine; CI supplies it |
| Mutation harness | `python3 .phase-f-mutations.py` | ✓ **M1–M7 all KILLED** at this head |
| Erasure/regression suites | three named tests run explicitly | ✓ **17 cases green** (nine-case sequence + four-origin synthetic derivation + four-contract late-waiting = 9+4+4, exactly as claimed) |

## Plan Conformance Findings

### Matches plan

- **Consolidated flag set** exactly as canon Round 1 sanctioned:
  `ABROLIA_REAL_EMAIL_ENABLED` (managed+BYO incident brake + household
  allowlist), `ABROLIA_BYO_EMAIL_ENABLED`, `ABROLIA_GMAIL_ENABLED`,
  `ABROLIA_WHATSAPP_SHARED_ENABLED`
  (`control_plane/feature_flags.py:37`). The three retired switches are gone;
  `check_provider_enabled` REFUSES them rather than silently allowing
  (`tests/test_feature_flags.py::test_a_retired_switch_is_gone_rather_than_silently_permissive`).
- **Default off, fail-closed, call-time read**: proven by
  `test_all_flags_default_off`, `test_flags_independently_togglable`, and
  `test_flag_toggle_mid_run_blocks_next_call` over the real gateway webhook path.
- **Enforcement at both layers** (Round 2 invariant): selection-time
  `_assert_email_rollout` runs in both `select` and `retry`
  (`control_plane/onboarding/service.py:536,849`); worker-time
  `_blocked_by_email_kill_switch` reads flags where the provider is called
  (`control_plane/provisioning/worker.py:1518`) with shutdown/teardown exempt.
- **Flag matrix doctested**: `docs/onboarding-runbook.md` §4 and the
  `phase-DE-pilot.md` matrix rewritten to match implemented reality, including
  an explicit record of what the retired flags were and why they gated nothing;
  behaviour pinned by `tests/test_feature_flags.py`.
- **Reconcile-contract hoist** (closure design pass Item 2) verified in code:
  `Provisioner` protocol declares `reconcile`
  (`control_plane/provisioning/contracts.py:121`); deterministic fake
  implements it (`control_plane/provisioning/fakes.py:116`); shutdown routing
  hoisted above every adapter-shaped check
  (`control_plane/provisioning/worker.py:1888-1924`); forward reconcile
  requires the method and fails closed `failed/provider_cannot_reconcile`
  (worker.py:2039-2047); the unguarded inspect tail is replaced by an explicit
  refusal.
- **Erasure sequence** (Item 1): `DeletionService.delete` marks identities
  `disconnecting` inside the deletion transaction
  (`control_plane/privacy/delete.py:210`); the nine-case sequence test drives
  reconcile → derived-ref cleanup → parent settlement → resume → household row
  gone with real worker + real DeletionService.
- **Round 11**: synthetic teardown reference derived from the adapter's own
  declaration, not a registry-name list; four-origin regression present and
  green.
- **Round 12**: late-`ProviderWaiting` ownership asked BEFORE the status
  restriction; four-contract parameterized regression over REAL waiting shapes
  green; `.check-fixtures-allow` gained exactly the specified single
  exact-value entry for the managed contract's production inbox domain
  (`<local_part>@abrolia.com` is what the worker's validator compares against).
- **Invariant recorded**: "Reconcile dispatches exhaustively; adapter shape
  gates nothing about teardown" at `AGENTS.repo-invariants.md:282`.
- **Mutation harness committed** as the plan demands ("an uncommitted proof
  proves nothing") — and independently re-run by this validation: all seven
  mutations killed.

### Deviations from plan

No functional deviations found. Non-functional notes:

1. **Plan checkboxes unticked** — Phase A and Phase F acceptance boxes remain
   `[ ]` in the canon file despite their criteria now being met and evidenced.
   The narrative rounds (1–6) were dutifully appended, but nobody closed the
   loop on the checklist itself. Documentation drift, not a code defect.
2. **gitleaks invocation** — the plan writes `gitleaks --all`; the canonical
   command (CI + CONTRIBUTING) is `gitleaks detect … --log-opts="--all"`. Ran
   the canonical form. Cosmetic wording only.
3. **Branch/merge bookkeeping** — local `main` ref is stale behind
   origin/main, and this branch is 1 commit behind its own merge (the #70
   squash). Matches the known "green gate but BEHIND" pattern; housekeeping
   only.

### Potential Issues

- **Untracked session debris on the branch** (untracked files do not ship, but
  they will confuse the next reader of `git status`):
  `.phase-f-handoff.md` (session handoff — belongs in
  `thoughts/shared/handoffs/` per repo convention),
  `.phase-f-reply.md` (text already posted to the PR thread), and
  `.phase-b-delete-operator.py` / `.phase-b-runtime-operator.py` /
  `.phase-b-tamper.py` (Phase B operator leftovers).
- **B-06 open by design** — dedicated Gmail live/CASA validation remains the
  largest open canon blocker; deliberately deferred, tracked in the plan.
- **B-01 upstream evidence unverifiable here** — canon acceptance asks for the
  Nerve cross-org PR + `go test` proof; that lives in the upstream
  nerve-cloud repository and should be linked once confirmed.

## Next Step: Diff Quality Review

The diff under review is already merged (PR #70 after twelve review rounds),
so a fresh `/code-review` is optional retrospect rather than a gate. For the
next branch, `/code-review` remains the recommended bug-focused follow-up —
plan conformance (this report) does not substitute for it.

## Manual Testing Required

Nothing new is owed by THIS branch (synthetic-only scope, data policy held).
Canon-level operator drills still standing:

1. Flag drill on staging: flip `ABROLIA_REAL_EMAIL_ENABLED` 1→0 mid-queue and
   confirm the next Nerve call stops without restart while teardown still
   works; restore and confirm resume.
2. Rollout-order transitions per provider (synthetic → operator accounts →
   pilot families), each requiring the Phase A legal pack, C1 receipt proof,
   `go test`, non-live suite green, and one manual live gate — before any
   `ABROLIA_REAL_*` flag is enabled anywhere.
3. B-06 live battery (Gmail connect/receive/approve-send/revoke) when taken up.

## Recommendations

- Tick the Phase A/F acceptance boxes (or append closure dates) so the plan's
  checklist matches its narrative.
- Land or delete the five untracked session artifacts listed above.
- Fast-forward/delete local refs to reflect the #70 merge.
- Link the upstream Nerve B-01 PR in the canon references when confirmed.

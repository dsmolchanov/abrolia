# Validation Report: Canon Execution Plan

**Plan**: `thoughts/shared/plans/2026-08-06-canon-execution-plan.md`
(operational child for the current phase:
`thoughts/shared/plans/2026-08-23-go-live-checklist.md`)
**Validated**: 2026-08-30, at `c72ff4c` on `main`, working tree clean.
**Delta scope**: this is an incremental validation over
`thoughts/shared/implementations/2026-08-23-canon-execution-plan-validation.md`,
which validated at `c7e1d36` (PR #70). Nineteen commits have landed since
(#71–#97), 110 files, +19955/−423. All of them serve canon **Phase E (B-12)**
through the go-live checklist's Track C slices.

## Implementation Status

| Phase | Blockers | Verdict at this head |
|---|---|---|
| A — Legal & Residency | B-07, B-08 | ~ Code done and proven; legal pack **partially** executed — see below |
| B — Foundation & Org Migration | B-09, B-11 | ✓ Closed (unchanged) |
| C — Email Identity | B-01, B-02, B-05, B-06 | ~ C1/C2 evidenced; **B-06 open by design**, B-01 upstream unverifiable here |
| D — Real Actions | B-10 | ✓ Closed (unchanged) |
| E — Pilotization | B-12 | ✓ **Substantially closed this window** — all 19 commits; detail below |
| F — Release Gating | — | ✓ Closed, and still protected — M1–M7 re-killed at this head |

### Phase A moved, and not only on paper

The last validation recorded "no DPA signed". `docs/privacy/processors.md:3-12`
now states that **DPA + SCC are in effect for P1 (Anthropic) and P4 (Resend)** —
both incorporated by reference into the customer agreement, with retrieved
copies committed at `docs/privacy/vendor-dpas/2026-08-23-anthropic-dpa.html`
and `2026-08-23-resend-dpa.md`.

**Still open, and still the gate on everything real:** P2 (Fly.io) DPA not
executed, TIAs not conducted, Art. 27 representative not appointed. B-07 is
therefore open and the synthetic-only data policy holds correctly. The document
is honest about this in exactly the way Gate −1 demanded — ⏳ marks work, not
achievement.

`pytest -k eu_strict` exits 0, so canon Phase A acceptance line 73 is
factually met (the box is still unticked — see Deviations).

### Phase E — what the nineteen commits actually delivered

Every go-live Track C slice is present in code and independently exercised:

- **C1 cost caps** — `budget_exceeded` is emitted at all three previously
  uncapped model call sites: `runner/pipeline.py:456` (WhatsApp dialogue),
  `:600` (Telegram dialogue), `channels/web.py:62`. The audit's "dialogue+web
  uncapped" verdict is closed at the call site, per the repo invariant.
- **C2 web chat** — `/api/web/message` (`control_plane/api/web.py:290`) now
  carries the same CSRF gate as every sibling mutation (`web.py:81`), and the
  model path resolves to the household runtime's own loop
  (`hermes_cloud/runtime/service.py:669 web_chat_turn`), which owns the budget
  counter — so the cap is enforced where the provider is called rather than
  proxied around. See Potential Issues #1 for what this path still does not read.
- **C3/C3a** — `channel_bindings` gained the sender/chat split: migration
  `0010_channel_binding_chat_id.sql`, with all three consumers moved together
  (gateway lookup, planner projection, runtime manifest). The second adult is
  now genuinely representable — `planner.py:211` derives `family` from the
  table with explicit duplicate-actor handling, replacing the hardcoded
  `family=(owner,)`.
- **C3b/C3c/C3e** — staged/published binding lifecycle
  (`repositories/bindings.py:151 published_revision`, `:462
  retire_staged_members`, migration `0012_channel_binding_published.sql`), and
  the rollout is no longer terminal at launch: `AWAITING_ACTIVATION` /
  `ACTIVATION_DEADLINE_PASSED` (`provisioning/worker.py:142,147`) keep the job
  alive until the revision activates.
- **C3d** — the two vacuous regressions were narrowed by measurement rather
  than argument and now reach the paths they name (#82).
- **C4a/C4b** — `channel_preferences` finally has a writer:
  `container.py:138` builds the repository, `planner.py:195` seeds it with
  `fallback_account_id` as a **reference** (migration `0011`). The dead
  `_validate_no_self_ingestion` is gone, replaced by an HMAC comparison.
  `primary_unavailable` is emitted for the first time
  (`hermes_cloud/channels/fallback.py:178`).
- **C5a–C5e** — one signature between both ends (`gateway/whatsapp_router.py:65`
  signs `body|timestamp`; the runtime reads `X-Relay-Timestamp` it had always
  been sent and never read); the ingress WAL is read back by a real redeliver
  worker (`gateway/app.py:404`); the relay key is generated, installed and
  `external_id_hmac` backfilled (`repositories/bindings.py:208-212`); an HTTP
  entrypoint and a narrow deploy unit exist (`gateway/app.py`,
  `deploy/gateway/{Dockerfile,fly.toml}`); and the gateway resolves bindings
  over an authenticated lookup that holds nothing
  (`control_plane/api/internal_bindings.py:78`).
- **C6a/C6b** — reconcile no longer reads a successful activation as a stale
  projection (#96), and retry paths catch only the failures they are prepared
  for rather than converting `NameError`/`TypeError` into silent permanent
  retries (#97).

## Automated Verification Results

| Check | Command | Result |
|---|---|---|
| Full non-live suite | `pytest -p no:cacheprovider -m "not live"` | ✓ **exit 0**, 1686 tests across 91 files (was 1472 — **+214**) |
| Whitespace | `git diff --check` | ✓ clean |
| Lint | `ruff check .` | ✓ all checks passed |
| Secret scan | `gitleaks detect --config .gitleaks.toml --log-opts="--all" --redact` | ✓ no leaks, 170 commits scanned |
| Sanitizer | `python3 scripts/check_fixtures.py --all` | ✓ clean (exit 0) |
| Sanitizer (gate form) | `… --all --require-deny` | ✗ **exit 2** — refuses without the private deny-patterns file; see Potential Issues #2 |
| Phase F mutations | `python3 .phase-f-mutations.py` | ✓ **M1–M7 all KILLED** at this head |
| eu-strict fail-closed | `pytest -k eu_strict` | ✓ exit 0 |
| Phase E suites (12, run individually) | see below | ✓ **all exit 0** |

**One caveat on how these were run**, and it turned out to be a real defect
rather than an artifact of my invocation — see the collection-fragility finding
in Post-Validation Corrections.

Phase E suites each verified separately: `test_onboarding_state.py`,
`email/test_google_oauth.py`, `test_channel_preferences.py`,
`test_channel_bindings.py`, `test_binding_api.py`, `test_web_channel.py`,
`test_runtime_web_chat.py`, `test_cost_caps.py`, `test_gateway_app.py`,
`test_gateway_routing.py`, `test_primary_unavailable.py`,
`test_provision_dry_run.py`.

**Worth recording:** the Phase F mutation harness still kills all seven
mutations after nineteen commits reworked provisioning, bindings and reconcile
around it. The erasure/teardown invariants Phase F established survived a large
refactor without anyone re-deriving them.

## Plan Conformance Findings

### Matches plan

- Canon Phase E's nine numbered changes are each represented in code (durable
  machine, OAuth prod path gated, preferences/routing, bindings, shared gateway,
  minimal Web, observability, cost caps, release plumbing).
- The synthetic-only data policy is intact: every product flag defaults off, and
  no `ABROLIA_REAL_*` flag is enabled anywhere in the tree.
- The plan's own discipline held under review pressure. The execution log
  records a fix being **narrowed** after review found it could make things worse
  ("a household stuck in `provisioning` is visible and fixable; one that is
  falsely `active` is neither"), and the author stopping rather than opening a
  fourth round. C3d exists because a slice reported its own tests as reaching a
  path they did not. That is the "fix the invariant, not the instance" rule
  working, including against the author.

### Deviations from plan

No functional deviations. Four bookkeeping ones, and the first would bite:

1. **Canon Phase E acceptance names test files that do not exist at those
   paths** (`canon-execution-plan.md:189`). `tests/test_onboarding.py`,
   `tests/test_google_oauth.py`, `tests/test_channel_preferences.py`,
   `tests/test_channel_bindings.py` are all absent; the real files live under
   `tests/control_plane/` (`test_onboarding_state.py`,
   `email/test_google_oauth.py`, `test_channel_preferences.py`,
   `test_channel_bindings.py`). Only `tests/test_web_channel.py` matches. The
   acceptance command as written fails on a green tree — the same class of
   defect the checklist's own C6 box was opened for.
   **Fixed 2026-08-30, after this validation** (see Post-Validation
   Corrections). Canon's remaining acceptance paths were swept at the same
   time and all resolve; this box was the only broken one.
2. **Checkbox drift, now in both plans, and this is the second validation to
   say so.** Canon A/E/F acceptance boxes remain `[ ]` though met. In the
   go-live checklist, C1, C2, C3, C3a, C3b, C3c, C3d, C3e, C4, C4a, C4b, C5a
   and C5b are all `[ ]` while merged — several literally read "*Done — see the
   inventory below*" next to an unticked box. Only C5c/C5d/C5e were ticked.
   **Fixed 2026-08-30** — see Post-Validation Corrections.
3. **C6a and C6b have inventories but no boxes.** Both merged (#96, #97) and
   appear only as `#### Inventory —` headings; Track C's ordered list never
   gained an entry for either, so the checklist cannot show them as done.
   **Fixed 2026-08-30** — both now carry ticked boxes.
4. **`gitleaks --all`** in canon Phase F acceptance is still not the canonical
   invocation (`gitleaks detect … --log-opts="--all"`). Cosmetic; flagged in
   the previous report and unchanged. **Fixed 2026-08-30** in both canon and
   `phase-DE-pilot.md`.

### Potential Issues

1. **A stale deferral: the runtime ignores web channel bindings.**
   **Escalated 2026-08-31 by review on #101, and the escalation was right.**
   This entry closed by calling the gap "not a security hole — it fails closed",
   and then the same session wrote "Track C is closed" into the go-live
   checklist. Those two statements cannot both stand: a promotion gated on the
   second would ship with the canon-promised second-adult Web flow unusable.
   The gap is also one step worse than described here — `api/web.py:322-329`
   resolves the caller's REAL membership role and forwards it, so an `adult`
   reaches `_web_chat` and is refused by role, rather than Web simply being
   owner-only by omission. Now tracked as an OPEN slice, **C3f**, and the
   closure claim is narrowed accordingly. Original text follows. `web` is a
   first-class binding channel (`repositories/bindings.py:94`,
   `models.py:260`, `provisioning/manifest.py:25`) and the manifest carries
   verified web bindings. But `hermes_cloud/runtime/service.py:669
   web_chat_turn` synthesizes `allowed_chats=frozenset({"web-chat"})` and
   `_web_chat` refuses every role but `owner`, with a comment deferring to
   "C3 moves web into the manifest when bindings get a lifecycle". **C3a and
   C3c have since given bindings exactly that lifecycle**, so the condition the
   comment waits on is met and the deferral now reads as current when it is
   not. Consequence: a second adult holding a *verified web binding* still
   cannot chat. Canon Phase E item 4 names Web among the channels whose binding
   derives `RunContext`. Not a security hole — it fails closed — but it is
   unfinished canon scope wearing a resolved excuse.
2. **The `check_fixtures --require-deny` gate cannot be evidenced locally.** It
   exits **2**, not 0 — a refusal, not a warning (the previous report read it
   as the softer thing). Canon Phase F acceptance lists it as a green gate, so
   that box can only ever be closed by CI, which supplies
   `HERMES_EXTRA_DENY_FILE`. Worth stating in the plan rather than rediscovering
   each validation.
3. **Zero git tags (checklist O2).** Unchanged. The release tag and the isolated
   restore drill against the *Phase E schema* are outstanding — and that schema
   has moved a long way this window: control-plane migrations now run through
   `0012`, adding `channel_preferences` fallback refs, binding chat IDs and the
   published-revision lifecycle. The drill is more valuable now than when it was
   written, and staler.
4. **`scripts/rehearse_0012_routing.py` is a one-shot gate that is easy to
   lose.** Track R1 requires running it before the first promotion, because
   `0012` decides which existing bindings the gateway still routes and a
   household it misses **goes silent rather than failing a test**. Correctly
   not applicable yet (no data to lose), but it is a single sentence inside a
   plan between here and production.
5. **B-06 and B-01 unchanged.** Dedicated Gmail live/CASA remains the largest
   open canon blocker (the config gate exists and fails closed —
   `config.py:240-251` requires verified OAuth, scope, CASA and Limited Use
   evidence). B-01's upstream Nerve cross-org proof still cannot be confirmed
   from this repository and is still unlinked in canon's references.

## Next Step: Diff Quality Review

This validation checked plan conformance, not diff correctness, and the diff
under review is large (+19955 lines across 19 merged PRs). Each PR passed the
Codex review gate individually. `/code-review` on any follow-up branch remains
the bug-focused complement — this report does not substitute for it.

## Manual Testing Required

Ordered by what blocks what. Nothing here is owed by the code; all of it is
owed by an operator.

1. **O1 — legal (blocks all of Track R).** Fly.io DPA execution, TIAs for
   P1/P2/P4, Art. 27 representative appointment. P1 and P4 are done.
2. **O2 — release tag + restore drill** on the current schema (through `0012`):
   `integrity_check`, `foreign_key_check`, 0600 pause marker, smoke without
   leasing, resume-jobs, new onboarding through rev 1, destroy temp app.
3. **R1 prerequisite — `python3 scripts/rehearse_0012_routing.py`** against a
   copy of the production database before the first promotion.
4. **Flag drill** on staging: `ABROLIA_REAL_EMAIL_ENABLED` 1→0 mid-queue stops
   the next Nerve call without a restart while teardown still reaches Nerve;
   restore and confirm resume.
5. **O3 — per-transition live battery** for each promotion: receive, approved
   send, restart/cursor resume, reconnect, export, revoke, delete — PII-safe.
6. **B-06 Gmail battery** (connect → receive → approve/send → revoke/delete)
   when that blocker is taken up.

## Recommendations

1. ~~**Fix canon Phase E acceptance line 189 to name the files that exist.**~~
   **Done 2026-08-30** — see Post-Validation Corrections.
2. ~~**Close the boxes.**~~ **Done 2026-08-30** — 45 open boxes across the three
   plans reduced to 18, every remaining one genuinely blocked. See
   Post-Validation Corrections.
3. **Resolve the web-binding deferral one way or the other** — either wire
   `web_chat_turn` to the manifest's verified pairs, or rewrite the comment to
   say web is deliberately owner-only and why. A comment naming a prerequisite
   that has already shipped is worse than no comment.
4. ~~**Record in canon Phase F that `--require-deny` is a CI-only gate.**~~
   **Done 2026-08-30** — recorded in canon Phase F and its `phase-DE-pilot.md`
   twin.
5. **Link the upstream Nerve B-01 PR** in canon's references once confirmed.

## Post-Validation Corrections

**2026-08-30 — canon Phase E acceptance paths (Deviation 1).** Line 189 now
names the files as they exist:

| Was | Is |
|---|---|
| `tests/test_onboarding.py` | `tests/control_plane/test_onboarding_state.py` |
| `test_google_oauth.py` | `tests/control_plane/email/test_google_oauth.py` |
| `test_channel_preferences.py` | `tests/control_plane/test_channel_preferences.py` |
| `test_channel_bindings.py` | `tests/control_plane/test_channel_bindings.py` |
| `test_web_channel.py` | unchanged — the only one ever correct |

Verified as a command rather than as a list: all five paths resolve, and one
pytest invocation over exactly those five exits 0. The plan carries a dated
note recording why the paths were wrong, so the next reader does not re-derive
it. **Correction, same day:** this section first said the box was deliberately left
`[ ]` because its sibling criteria were operator work. That reasoning was wrong
— the staging dry-run and the manual pilot onboarding are their OWN boxes, and
line 189 gates the test criterion alone. It is now ticked; the other two remain
open.

Canon's other acceptance references were swept in the same pass
(`tests/test_gmail_api_*`, `tests/control_plane/email`,
`test_phase1_chaos_matrix.py`, `test_config_and_cli.py`,
`test_feature_flags.py`) and all exist. Recommendations 2–5 remain open.

**2026-08-30 — checkbox closure (Deviations 2–4, Recommendations 2 and 4).**
Adjudicated every open box in the three plans against evidence rather than
sweeping them. **45 open → 18 open**, and each of the 18 is genuinely blocked.

| Plan | Was | Now | Closed |
|---|---|---|---|
| `canon-execution-plan.md` | 11 open | 8 open | A×2, E×1 |
| `2026-08-23-go-live-checklist.md` | 22 open | 7 open | C1, C2, C3, C3a–C3e, C4, C4a, C4b, C5, C5a, C5b, C6 (+C6a/C6b boxes created, ticked) |
| `2026-08-06-phase-DE-pilot.md` | 12 open | 3 open | Phase E×6, Phase F×3 |

Beyond the ticks, four stale statements were corrected because they asserted
things that stopped being true:

- the checklist's opening paragraph still described the three half-built Phase E
  surfaces as current — all three shipped;
- its true-state table now carries a "closed by" map above the preserved audit
  rows, so a reader cannot mistake a dated audit for present state;
- O1 said "every row ⏳ today", which the Anthropic and Resend DPAs falsified;
- `phase-DE-pilot.md` said "six per-provider flags" (four survived #69/#70) and
  quoted "expect 626+ / 215+" suite counts (measured: 1686).

Seven acceptance commands across the two child plans named test paths that do
not exist; all now resolve.

**Three boxes were deliberately NOT ticked despite being close:**

1. **Canon Phase A line 71** (`processors.md` ✅ for P1/P2/P4) — P1 and P4 are
   ✅, but P2 Fly.io has no DPA, all three TIAs are open, and no Art. 27
   representative is appointed. Recorded as partially met.
2. **Canon Phase F / `phase-DE-pilot.md` gate box** — `check_fixtures --all
   --require-deny` exits 2 locally; only CI can close it. Both boxes now say so.
3. **Canon Phase C1 line 123** — see the new finding below.

### New finding from the closure pass: B-02 has a mechanism without a proof

Canon's C1 acceptance asks for two proofs. The second is present by name
(`test_unknown_secret_handoff_never_verifies_from_secretless_inspect`,
`tests/control_plane/email/test_identity.py:478`). The first — crash after
`SecretSink.write` converges without an operator — has a **working mechanism and
no test that drives it**: `control_plane/provisioning/worker.py:1056-1075`
creates the `email_secret_installs` receipt on reclaim when the sink already
contains the generation, exactly as the plan specified, but that table appears
in tests only in `test_db.py` (schema) and `test_provision_dry_run.py`
(audit/export). The suite is green and this path is not what makes it green.

This is the same shape as C3d — a mechanism whose test does not reach it — and
B-02 is a P0 blocker on real `@abrolia.com`/BYO rollout, so it is worth one
regression before that gate opens rather than after. Recorded in the plan at the
box rather than only here.

**Adjacent observation, not a blocker:** the receipt lookup at `worker.py:1061`
wraps its read in a bare `except Exception: pass`. It fails safe here (control
falls through to the sink check), but it is the exact shape C6b was opened to
remove — a broad except around an operation with a retry policy. Worth a look
when that regression is written.

### Second finding from the closure pass: interleaved test roots silently ERROR

While re-running every acceptance command I had rewritten, the combination that
failed during validation reproduced — and it is not an artifact of an ad-hoc
invocation, which is how the main report first read it.

Passing explicit file paths that INTERLEAVE `tests/` and
`tests/control_plane/` in one pytest invocation loses
`tests/control_plane/conftest.py`'s fixtures for every control_plane file
collected after the switch. Reproduced minimally with the same three files in
two orders:

```
control_plane → tests/ → control_plane   exit=1  (7 × "fixture 'api_harness' not found")
tests/ → control_plane → control_plane   exit=0
```

It surfaces as a collection **ERROR**, not a failure, which is the part that
matters for a checklist: a box whose command interleaves the roots could look
"run" while nothing in it actually executed. Contributing conditions: no
`__init__.py` in either test directory, `--import-mode=importlib`, and four
duplicate basenames across the two roots (`test_backup.py`, `test_db.py`,
`test_runtime_dsar.py`, `chaos_child.py`).

Directory-based runs are unaffected — the full non-live suite is green at 1686 —
and all nine rewritten acceptance commands are grouped by root and were re-run
as written. A caution is recorded in `phase-DE-pilot.md` above the Cross-Phase
Verification Commands. Worth fixing properly at the harness level rather than
by convention, since the failure mode is silence.

**2026-08-31 — review on #101 found two [BLOCKER]s, both mine.**

The PR merged before the verdict landed (this repository carries the
`review-lane-fast` topic, so green CI auto-merges and the Codex check posts
findings without holding the merge). Both findings were correct and are fixed in
a follow-up.

1. **Stale verification targets survived the commit that claimed to fix them.**
   I corrected the acceptance commands under boxes and then wrote a caution note
   saying "every command in this plan ... was re-run as written", which was
   false: `phase-DE-pilot.md`'s Cross-Phase block still named four nonexistent
   Phase E files and a nonexistent `tests/test_config.py`, and Step F4 invoked
   `python -m check_fixtures`, which has no module entry point. Two sibling
   plans carried the same defect (`phase-A` the sanitizer line, `phase-C` three
   BYO test files that were planned and never written — their coverage having
   consolidated into `test_nerve_byo_domain.py`). Worth stating plainly: my own
   scan missed these because it paired code fences with a non-greedy regex that
   silently skipped regions, and I trusted its empty result over reading the
   file.
2. **"Track C is closed" contradicted this report's own Potential Issue 1.**
   See the amended entry above.

**The invariant, not the instances.** This defect class has now cost three
passes — the checklist's `C6` box, canon's four wrong paths, and these. Per
`AGENTS.md`, a class that keeps returning is a missing rule, so
`tests/test_plan_commands.py` now asserts that every `tests/**`/`scripts/**`
path and every `python -m` module inside a fenced plan block resolves. It was
checked in both directions: reintroducing each of Codex's two defects makes it
fail, naming the plan and line. The failure being silent is what earns it a
test — `pytest missing_file.py` exits 4 and prints no test failures, so a gate
that proves nothing still looks like it ran.

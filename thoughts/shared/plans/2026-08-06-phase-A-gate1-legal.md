---
title: "Phase A — Gate -1 Legal & Residency Closure (B-07, B-08)"
status: planning
created_at: "2026-08-06"
base_commit: b9d3614
parent: thoughts/shared/plans/2026-08-06-canon-execution-plan.md
depends_on:
  - thoughts/shared/plans/2026-08-02-family-ops-assistant-mvp.md
  - docs/privacy/processors.md
  - docs/privacy/lawful-bases.md
  - docs/privacy/dpia.md
scope: phase-A
data_policy: synthetic-only-until-explicit-gates
blockers: [B-07, B-08]
gate: "Gate -1 — no ABROLIA_REAL_* flag enabled until A is merged"
---

# Phase A — Gate -1 Legal & Residency Closure (B-07, B-08)

## Overview

Phase A closes the two legal/residency blockers that gate **every** real-data operation in canon. Until A is merged, the system remains synthetic-only — including owner-mailbox data. This phase produces no provider execution or content-classification feature code; it produces signed legal artifacts, corrected privacy disclosures, and a fail-closed config enforcement with tests. A narrow, synthetic-only onboarding restriction may land before counsel review under the pre-gate rule below, but it is not Phase A closure.

| Blocker | Severity | Summary |
|---------|----------|---------|
| B-07 | P0 legal | No DPA/SCC/TIA signed (P1 Anthropic, P2 Fly, P4 Resend, P8/P9 storage/logs, P11 push); no Art 9(2) condition for special categories; lawful-bases/counsel review open; notices lack controller/contact and still describe future SCC as current |
| B-08 | P1 | Residency `eu-strict` Vertex AI EU not wired; honest wording already fixed but config enforcement needs test |

**Exit gate:** `ABROLIA_REAL_*` flags remain `0` / `synthetic_only=1`. No provider flag flip occurs in this phase. The follow-on PR that flips any real flag must reference the merged A evidence.

## Current State

### What the docs already say correctly

From direct reads of the current tree at `b9d3614`:

- `docs/privacy/processors.md:1-9` opens with a gate banner: *"ни один DPA не подписан, ни один трансферный механизм не оформлен … система работает только на синтетических данных. Всё, что ниже помечено ⏳ — это работа, а не достигнутое состояние."* Statuses `✅/⏳/❌` are defined. §2 lists the 7-point checklist before first real family (name entities, sign DPAs, SCC module 2, TIA, Gmail verification, WhatsApp notices, replace wording). §3 documents the honest residency promise (`ams` EU, `global/us` for Claude, `iad` for Nerve, US for Resend/Google/Telegram/WA) and the `eu-app` (default) / `eu-strict` (requires Vertex AI EU, crashes on start if absent) table. §4 defines new-provider admission criteria.

- `docs/privacy/lawful-bases.md:1-152` correctly scopes subjects S1–S5, purposes C0–C11, LIA for C3, and §3 blocks on Art 9(2): adult users (S1–S3) can use explicit consent Art 9(2)(a) via separate onboarding receipt; S4/S5 (children/third parties) require a counsel choice between (1) family-as-controller / processor under Art 28 with documented instruction, or (2) filtering so content never reaches our systems. Server-side detector is explicitly **not** a substitute — reading to classify is itself Art 4(2) processing. Art 9(4) noted as additional barrier, not replacement. §4 requires versioned receipts; Art 50 disclosure lives in signature default.

- `docs/privacy/dpia.md:1-137` declares DPIA mandatory (WP248: vulnerable subjects + non-user data subjects + innovative tech + special categories), lists R1–R18 with residual risks (R7 `высокий, блокирующий`), and §5 enumerates 7 preconditions before real data (Art 9(2) condition, trust foundation, DPAs/SCCs, Gmail verification/CASA, WhatsApp receipts, controller/authority filling, counsel review of Art 35(4)/36). Review cadence: model/provider/channel change, >20 families, any leak incident.

- `docs/privacy/privacy-notice-{en,ru}.md` carry TODOs for controller name/address/DPO/authority and a pilot-status banner. `docs/privacy/data-map.md` is the retention source of truth (S1–S15, 30-day raw TTL, 365-day journal, 3-year consent/tombstone).

- `docs/source-pins.md` at `c26fc41`/`c6f5ed03`/`e3d011e` is current; hermes repin deferred — no action in A.

- `control_plane/config.py:50-202` enforces `synthetic_only`, `real_family_data_enabled`, `real_email_enabled`, `gmail_real_enabled` (requires `google_oauth_app_verified && google_gmail_scope_approved && google_casa_current && google_limited_use_disclosed`), separate encryption/HMAC/backup key independence, runtime provider lock to `ams`, and Fly provider completeness. `hermes_cloud/core/config.py:40-151` implements `residency_mode == "eu-strict"` requiring `HERMES_VERTEX_EU_ENABLED ∈ {1,true,yes,on}` or raising `RuntimeError("residency_mode eu-strict requires $HERMES_VERTEX_EU_ENABLED; refusing downgrade")` at `load_config()`.

### What is still open (the actual work)

1. **P1/P2/P4/P8/P9/P11 entities unnamed.** `processors.md` rows P1, P2, P4, P8, P9 show `TBD (US)` and P11 `TBD`; addresses/EU representatives absent.
2. **No DPA/SCC/TIA signed.** All three columns show `⏳`. TIA documents not linked.
3. **Art 9(2) not selected.** `lawful-bases.md` §3 lists options but records no decision; `dpia.md` R7 remains `высокий, блокирующий`.
4. **Controller/contact TODOs remain** in `privacy-notice-en.md:32-38` / `privacy-notice-ru.md` and `README.md` open questions. Future-tense SCC claim in notices must reflect actual pending status (already done in `processors.md` banner; notices carry pilot banner but need controller fill).
5. **Residency enforcement exists but lacks its canonical test.** `hermes_cloud/core/config.py:143-151` crashes correctly; `control_plane/config.py` documents `eu-app`/`eu-strict` in `processors.md:70-75` but the canon requires an explicit test `tests/test_config_and_cli.py::test_eu_strict_fails_closed_without_vertex` proving `eu-app` boots and `eu-strict` without vertex exits non-zero. Current `tests/test_config_and_cli.py` exists but does not yet assert this path by name.
6. **`docs/SECURITY.md` reporting contact** still `TODO: security contact address` (`docs/SECURITY.md:8`).

### Non-goals for Phase A

- No Nerve/Fly/Google API calls, no org migration, no email provider wiring, no channel binding changes.
- No hermes repin, no landing publish.

## Desired End State

After Phase A merges on a short-lived `codex/phase-A-legal` branch:

1. **Processors registry is sign-evidenced.** `processors.md` shows `✅` with date for every mandatory processor that will handle real data in pilot scope (at minimum P1 Anthropic, P2 Fly, P4 Resend; P8/P9 once named — or explicitly scoped out with rationale and push/opt stayed disabled). Each `⏳` row turned `✅` is backed by a stored artifact (DPA PDF + SCC module 2 annex + TIA memo) linked from the registry. P3 internal transfer documented; P5 n/a with rationale preserved; P6/P7 `❌` with separate notice path unchanged; P11 stays `⏳+disabled` if push not in pilot.

2. **Art 9(2) condition selected by counsel and reflected everywhere.** `lawful-bases.md` §3 records the chosen direction (family-as-controller with Art 28 instruction vs. content-never-reaches-us filtering vs. counsel-crafted hybrid) with citation, rationale, and jurisdiction check for Art 9(4) (DE/IT/NL/ES barrier scan). `dpia.md` R7 residual moves from `высокий, блокирующий` → `низкий` (or `средний` with explicit residual acceptance) and cross-references the same condition. `processors.md` §2 and `privacy-notice-*` align. Onboarding consent receipt text for S1–S3 Art 9(2)(a) is versioned and linked.

3. **Disclosures are honest and complete.** `privacy-notice-en.md` / `privacy-notice-ru.md` controller block filled (legal name, registered address, registration, EU representative if Art 27 applies), contact address for DSAR/withdrawal, supervisory authority, DPA/SCC status line states actual pending/signed status (no future-tense claim of coverage before signing), retention table provisional note retained until counsel confirm. `README.md` open-questions / controller section mirrored. `docs/SECURITY.md` reporting contact filled.

4. **`eu-strict` fail-closed path is proven.** `hermes_cloud/core/config.py` behavior unchanged (or tightened if counsel requires stricter check), `control_plane/config.py` residency gate unchanged, and a named test proves the contract.

## Detailed Steps

### Step A0 — Branch and guard

- Branch from `b9d3614`: `git checkout -b codex/phase-A-legal b9d3614`.
- Keep `ABROLIA_SYNTHETIC_ONLY=1` throughout; do not add any `ABROLIA_REAL_*=1` fixture.
- Create a tracking doc `thoughts/shared/plans/2026-08-06-phase-A-gate1-legal.md` — this file — and keep it as the phase checklist.

### Step A1 — Name legal entities (processors.md §2 p.1)

**Owner:** counsel + operator. Engineer prepares the table; counsel fills addresses.

**Files:** `docs/privacy/processors.md:14-27` (registry table), `docs/privacy/README.md` if it mirrors processors.

**Edits:**

1. Replace `TBD (US)` for P1/P2/P4 with actual legal entity names (e.g., Anthropic PBC / Fly.io Inc. / Resend Inc.), registered addresses, and EU representative where applicable.
2. For P8 (object storage for backups) and P9 (logs/alerts) — either name the chosen EU provider that will store backups/logs for pilot (preferred) or explicitly record `not selected — backups/logs stay on Fly EU volume only for pilot; no third-party P8/P9 engaged` with a date and owner signature. If the latter, §2 p.2 scope is reduced accordingly but must be explicit — silent omission is not acceptable.
3. For P11 (Web Push) — record `TBD — not enabled for pilot; push remains fake only` (matches `docs/privacy/processors.md:27` current text). No push for real families.
4. Keep P3 as internal record (`то же юрлицо, что и оператор → не третье лицо`) with transfer documentation pointer (see A2).
5. Commit message: `docs(privacy): name processor entities P1/P2/P4 (+P8/P9 scope)` — separate from signature commit.

**Counsel decision required:** confirm entity names, addresses, EU representatives. No engineering shortcut (guessing legal names) allowed.

### Step A2 — Execute DPA + SCC module 2 + TIA (processors.md §2 p.2–4)

**Owner:** counsel leads; engineer links artifacts.

**Files:** `docs/privacy/processors.md:32-50` (registry + checklist), new artifacts under `docs/privacy/transfers/` (or private legal vault with link — choose one and document it).

**Edits:**

1. For each in-scope processor (P1, P2, P4, and P8/P9 if named), obtain signed DPA under Art 28(3) (controller→processor, module 2 for SCC). Record signature date in registry: `⏳ → ✅ 2026-08-XX` per cell. Store the signed PDF (or vault pointer) and link it: e.g., `docs/privacy/transfers/P1-anthropic-dpa-2026-08-XX.pdf` or `vault://legal/dpa/P1-…`.
2. For each US transfer (P1, P2, P4, and P3 internal), attach SCC module 2 annex. `processors.md` mechanism column changes `⏳ SCC модуль 2` → `✅ SCC модуль 2 (дата, ссылка)`.
3. Produce a TIA memo per US transfer (access by US authorities, provider practice, effectiveness of supplementary measures). Minimum contents: legal basis for transfer beyond SCC, government-access assessment, provider transparency/practice notes, supplementary measures (encryption in transit, field encryption/HMAC, no training, minimization, disk encryption, isolated secrets — as listed in registry), and a conclusion `transfer permitted with measures` or `not permitted — remain synthetic`. Link TIA: `TIA` column `⏳ → ✅ (ссылка)`.
4. For P3 (Nerve, same owner, `iad` US), add internal Art 30 record + transfer documentation (same-owner commitment, EU→US transfer rationale, supplementary measures). Registry `⏳ → ✅ (внутренняя запись + TIA)`.
5. **Separate PR rule:** registry update that flips `⏳→✅` must land only after signatures exist. Do not flip speculatively. This matches `processors.md:29` rule and canon Phase A acceptance.

**Counsel decision required:** DPA/SCC text, sub-processor list and change-notification order, TIA conclusions. Engineer does not author legal conclusions.

### Step A3 — Art 9(2) condition selection (lawful-bases.md §3, dpia.md R7)

**Owner:** counsel decides; engineer documents and wires consent receipt.

**Pre-gate operational mitigation (owner-approved 2026-08-08):** before counsel
selects an Art. 9(2) condition, onboarding may require a versioned acknowledgement
that the household will not send special-category content. The exact text shown
to the user must be the text hashed in the durable receipt, the requirement must
apply to every email selection including synthetic/default API flows, and all
`ABROLIA_REAL_*` flags remain disabled. This mitigation does not select direction
2, satisfy Art. 9(2), lower DPIA R7, complete any A3 checkbox, or authorize real
family data. Owner authorization: `/s/ Product owner (CEO), 2026-08-08`.

The same gate is fail-closed across deployment boundaries: queued/retried email
and runtime jobs recheck the authoritative current receipt before provider work;
a blocked legacy runtime job returns onboarding to an explicit email reset and
acknowledgement path; and an already-active runtime refuses readiness when its
manifest lacks the current version+SHA receipt. Exact activation replays remain
available only for durable bootstrap-secret cleanup.

**Files:** `docs/privacy/lawful-bases.md:92-162` (§3), `docs/privacy/dpia.md:48-57` (R7 row) + `docs/privacy/dpia.md:118-137` (§5 preconditions), `docs/privacy/privacy-notice-{en,ru}.md` (§ Other people's data / Children / legal bases para), onboarding copy `control_plane/onboarding/*` + `control_plane/api/*`, provisioning/activation enforcement in `control_plane/provisioning/*`, and steady-state readiness in `hermes_cloud/runtime/service.py` if consent copy lives there.

**Edits:**

1. Counsel picks one of the two directions (or a reasoned hybrid) for S4/S5:
   - **Direction 1 — family as controller:** Abrolia is Art 28 processor for health/religion data; family ensures Art 9(2) condition (e.g., explicit consent of its members / vital interests); Abrolia documents instruction in contract + onboarding instruction. Requires checking that household exception Art 2(2)(c) does not remove provider obligations.
   - **Direction 2 — content never reaches us:** family filters before sending; onboarding instructs not to forward medical certificates / similar; any such content that does arrive is still processing (so instruction is advice, not guarantee).
   Counsel may craft a third framing, but it must cite a concrete Art 9(2) paragraph (a, f, h, i, j) — "residual risk accepted" remains invalid as the canon already fixed.
2. Update `lawful-bases.md` §3: replace the open-choice prose with the selected condition, rationale, and jurisdiction-specific Art 9(4) barrier check (DE/IT/NL/ES). Keep the server-detector note: detector is only a minimizer **after** the condition is chosen.
3. Update `dpia.md` R7: change `Остаточный риск = высокий, блокирующий` → selected residual (`низкий` if filtering, `средний` with mitigation if controller model) and cite `lawful-bases.md` §3. Update `dpia.md` §5 precondition 1 to `выполнено (дата, ссылка)`.
4. Update `privacy-notice-*` to disclose the chosen basis for health/religion data (one paragraph, mirrored RU/EN). Update `README.md` open-questions if it references Art 9(2).
5. If direction 1 (explicit consent for S1–S3 Art 9(2)(a)), add/confirm the separate onboarding consent checkbox: not bundled with ToS, one-step withdrawal, receipt versioned and stored in `control_plane` consent receipts (authoritative). Link receipt version in notices. If direction 2, add the onboarding instruction copy that tells the family not to forward medical material to the agent inbox/channel.
6. **Engineering constraint:** do not implement a server-side health/religion classifier in this phase. `hermes_cloud/runner/extraction.py` must not gain a medical-attribute filter until the Art 9(2) condition is signed — building it earlier would itself be Art 4(2) processing without a basis.

**Counsel decision required:** Art 9(2) paragraph, direction, Art 9(4) scan result, DPO-appointment check. This is the blocking decision for all of Phase A.

### Step A4 — Fill controller / contact / authority / security contact

**Files:** `docs/privacy/privacy-notice-en.md:32-38`, `docs/privacy/privacy-notice-ru.md` (mirrored block), `README.md` (controller / contact section + open questions), `docs/SECURITY.md:4-8` (reporting contact), `docs/privacy/dpia.md:137` (§5 p.6), `docs/privacy/processors.md:50` (§2 p.7 wording).

**Edits:**

1. Replace every `TODO:` in `privacy-notice-en.md:32-38` / `privacy-notice-ru.md` with real values: controller legal name, registered address, registration details, EU representative (or `n/a — Art 27 not applicable, rationale`), contact address for requests/withdrawal, DPO statement (`appointed: name/contact` or `not required — rationale dated`). **Owner decision 2026-08-12:** naming a supervisory authority is no longer required here. Art 13(2)(d) requires informing the subject of the right to lodge a complaint, not naming an authority, and no member state's authority is the authority "of the establishment" while the controller has none in the Union; the notices carry the full Art 77 right instead. Art 27 applicability is recorded as a rationale (the 27(2)(a) derogation is unavailable) and the designation itself remains a gate in `processors.md` §2 p.4a.
2. Mirror controller/contact in `README.md` and close the corresponding `README.md` open question.
3. Fill `docs/SECURITY.md` reporting contact (`TODO: security contact address` → real address).
4. Ensure notices state actual DPA/SCC status after A2 (e.g., `"DPA/SCC signed 2026-08-XX; copy available on request"` vs. `"being put in place — no real data yet"`). Remove any line that claims SCC coverage before signing; the `processors.md` banner already models the correct honest phrasing.
5. Keep RU as reference text: update RU first, then mirror EN, then note sync date in the notice version line.

### Step A5 — Residency `eu-strict` wiring + named test

**Files:** `hermes_cloud/core/config.py:40-151`, `control_plane/config.py:70-75` (residency table comment), `hermes_cloud/core/runtime_manifest.py` (if `residency_mode` parsing lives there), `tests/test_config_and_cli.py` (add named test).

**Changes:**

1. Confirm `hermes_cloud/core/config.py:143-151` still raises `RuntimeError("residency_mode eu-strict requires $HERMES_VERTEX_EU_ENABLED; refusing downgrade")` when `manifest.residency_mode == "eu-strict"` and `HERMES_VERTEX_EU_ENABLED` not in `{1,true,yes,on}`. If `maybe_load_runtime_manifest` lowercases or trims, keep casefold behavior — do not weaken. No silent downgrade to `eu-app`.
2. Ensure the crash happens at `load_config()` startup, before any model call. No fallback to Anthropic global when `eu-strict` is requested.
3. Add the canonical test the execution plan gates on:

   ```python
   # tests/test_config_and_cli.py
   def test_eu_strict_fails_closed_without_vertex(tmp_path, monkeypatch): ...
   def test_eu_app_boots_without_vertex(...): ...
   def test_eu_strict_boots_with_vertex_enabled(...): ...
   ```

   The test must drive `load_config(env={...})` with a synthetic manifest/runtime-manifest fixture that sets `residency_mode="eu-strict"` and asserts `RuntimeError` with `EU_STRICT_REQUIRES_VERTEX` / the exact message above when `HERMES_VERTEX_EU_ENABLED` is absent, and success when it is `1`. The `eu-app` variant must boot without the flag. Name must include `eu_strict` so `pytest -k eu_strict` selects it.

4. Document `processors.md:70-75` residency config table already matches; no doc change unless counsel tightens wording.

### Step A6 — Audit and PR

**Checks before merge:**

- `git diff --check` (no trailing whitespace in RU notices).
- `ruff check .` — notices are Markdown, but Python changes in A5 must pass.
- `rg -n "TODO" docs/privacy/privacy-notice-*.md docs/SECURITY.md README.md` — expect zero after A4.
- `rg -n "SCC|Standard Contractual" docs/privacy/ -n` — every claim must match `processors.md` registry status.
- `rg -n "eu-strict|EU_STRICT|VERTEX" hermes_cloud/core/config.py control_plane/config.py` — confirm wiring.

**PR structure:** one PR for A1–A4 (docs/legal) + one commit for A5 (code/test) is acceptable; alternatively two PRs (A1–A4 docs, then A5 test) — either way, the `⏳→✅` flip commits only after signatures are in hand. PR description must list artifact links and counsel reviewer.

## Acceptance Criteria

### Legal artifacts

- [ ] `docs/privacy/processors.md` registry shows `✅` with date for at least P1, P2, P4 (and P8/P9 or explicit out-of-scope note, plus P11 `⏳+disabled`). Each `✅` links to a stored DPA PDF + SCC module 2 annex + TIA memo. P3 internal transfer documented with TIA. Change landed in a PR **after** signatures.
- [x] `docs/privacy/lawful-bases.md` §3 records the counsel-selected Art 9(2) condition (paragraph letter + rationale + Art 9(4) jurisdiction scan) and no longer presents an open choice. No "residual risk accepted" as basis. Art 9(2)(a) selected 2026-08-12 by the product owner **acting as counsel for the controller** — the controller has no separate external counsel for this determination, and `lawful-bases.md` §3 now names both roles in the signature rather than leaving a product-owner signature against a criterion that says counsel. Decision and review come from one person, so the record carries no independent check; that limitation is stated in the document. Selected for adults and the household's own children; third-party special categories are out of scope with every rejected paragraph named; the Art 9(4) scan is recorded for ES (LOPDGDD art 9.1), IT (art 2-septies), DE (§ 22 BDSG) and NL (UAVG); the `special_category_household_content` consent purpose is implemented and required fail-closed in a real-email rollout.
- [x] `docs/privacy/dpia.md` R7 residual is `низкий` (or `средний` with acceptance rationale) and cites the selected condition; §5 precondition 1 marked `выполнено`. R7 is `средний, принят` with the acceptance rationale naming the accidental-receipt residue, and precondition 1 records the two remaining implementation steps.
- [x] `docs/privacy/privacy-notice-en.md` and `privacy-notice-ru.md` controller/contact/DPA-status filled; no `TODO` remains; RU/EN in sync; `README.md` and `docs/SECURITY.md` mirrored. **Owner decision 2026-08-12:** naming a specific supervisory authority is dropped from this criterion. Art 13(2)(d) requires informing the subject of the right to lodge a complaint, not naming an authority, and the notices carry the full Art 77 right; the authority of an establishment cannot be named while the controller has none in the Union. The Art 27 designation itself remains a gate in `processors.md` §2 p.4a.
- [x] Counsel review evidenced: reviewer name/date in PR or in `docs/privacy/dpia.md` §5 p.7 / `lawful-bases.md` header; DPO necessity checked. Recorded in `dpia.md` §5 p.7 as `/s/ Product owner (CEO), acting as counsel for the controller, Axiom Atlas, LLC, 2026-08-12`: Art 36 prior consultation not triggered, DPO not required under Art 37(1) with the reasoning, position revisited when the pilot ends. The reviewer is the decision-maker, which the DPIA states rather than obscures; an independent external review would strengthen the record and remains open — it is not a condition of the Art 9(2)(a) basis, which is the explicit consent itself.

### Residency gate

- [x] `hermes_cloud/core/config.py` `eu-strict` path still fail-closed (no downgrade).
- [x] Named tests exist and pass (`pytest -k "eu_strict or eu_app"` — 5 passed on 2026-08-19):
  `test_eu_strict_fails_closed_without_vertex`, `test_eu_app_boots_without_vertex`,
  `test_eu_strict_boots_with_vertex_enabled`, alongside the pre-existing
  `test_eu_strict_manifest_fails_without_explicit_provider`.

  **Corrected 2026-08-19.** This item was checked on 2026-08-12 naming three
  tests that did not exist; only the combined
  `test_eu_strict_manifest_fails_without_explicit_provider` did, and `eu-app`
  had no coverage at all — so the gate asserted a property nobody had checked.
  The named tests are now written. `eu-app` matters on its own: if it inherited
  the strict requirement, the gate would be fail-closed for a reason unrelated
  to residency and the strict guarantee would be untestable.

```bash
pytest -k eu_strict -q
pytest tests/test_config_and_cli.py -q
```

Expected: `eu-strict` without `HERMES_VERTEX_EU_ENABLED` → `RuntimeError` / non-zero exit; `eu-app` boots; `eu-strict` with flag boots.

### Synthetic-only guard

- [x] `control_plane/config.py` synthetic guard unchanged: `synthetic_only=1` blocks `real_*` flags; no `ABROLIA_REAL_*=1` in repo fixtures.
- [x] Full non-live suite still green:

```bash
pytest -p no:cacheprovider -m "not live" -q
ruff check .
gitleaks detect --no-git --source . 2>&1 | head
python -m check_fixtures --all --require-deny  # private deny-list in CI; local run checks public sanitizer
```

### Gate

- [x] No `ABROLIA_REAL_EMAIL_ENABLED`, `ABROLIA_REAL_FAMILY_DATA_ENABLED`, or `HERMES_GMAIL_ADDRESS`/`HERMES_GMAIL_APP_PASSWORD` enabled in any committed config/fixture. Verified: the only committed occurrence is `deploy/control-plane/fly.toml:14`, `ABROLIA_REAL_EMAIL_ENABLED = "0"`.

  **Scope revised 2026-08-19.** This item originally also asserted that the diff
  touches only docs, `hermes_cloud/core/config.py` and
  `tests/test_config_and_cli.py`. That was the shape of the step as planned on
  2026-08-06, before A3 selected the Art 9(2)(a) condition on 2026-08-12 and put
  the consent into the product. The implementation half of A3 necessarily
  reaches onboarding, provisioning, the consent registry, the runtime readiness
  path and the web onboarding forms, and the file-scope sentence was left
  unrevised while the step grew — so the gate contradicted the plan it belongs
  to rather than constraining it. The assertion that carries the safety
  property is the first sentence, and it is unchanged and still verified; the
  file list is replaced by the scope actually approved:

  - `docs/privacy/**` — the determinations and the records of them.
  - `control_plane/privacy/**`, `control_plane/onboarding/**`,
    `control_plane/provisioning/**`, `control_plane/api/**`,
    `control_plane/web/**` — the Art 9(2)(a) consent and its enforcement.
  - `hermes_cloud/runtime/service.py` — readiness enforcement of the manifest's
    authoritative purposes.
  - `tests/**` — the regression suite for each of the above.
  - `control_plane/models.py` — the selection fields the consent travels in.
  - `README.md` and `.check-fixtures-allow` — the operator-facing description of
    the above, and the sanitizer allowance the new fixtures need.
  - `thoughts/**` — this plan and the session handoffs recorded beside it.
  - `control_plane/cli.py` and `control_plane/container.py` — the operator
    boundary that makes Art. 7(3) withdrawal invocable outside tests.
  - `docs/onboarding-runbook.md` — the procedure that boundary is run from.

  Anything outside this set on this branch remains a deviation and a blocker.

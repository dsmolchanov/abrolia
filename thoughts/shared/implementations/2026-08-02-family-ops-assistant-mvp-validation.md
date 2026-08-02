# Validation Report: Family Ops Assistant MVP — Phase 0 (Gate −1)

**Plan:** `thoughts/shared/plans/2026-08-02-family-ops-assistant-mvp.md` (v2)  
**Implementation commit:** `13c4e6a2b33d1948b6cde99356fde447d7ef78d3`  
**Validated:** 2026-08-02  
**Scope:** Phase 0 only; read-only review of implementation and donor repositories. This report is the only file added by validation.

## Verdict

**Gate −1 is not complete and its two manual checkboxes must remain open.** The commit provides a strong structural foundation and the reported local test suite is reproducible, but the fixture/secret gate has concrete false negatives and bypasses, the privacy package has blockers for any real data, and the selected Hermes donor baseline is older than six material P0 fixes.

- Privacy package: structurally complete for synthetic-only development; not approved for real data.
- Donor pin: the recorded SHA is published and reproducible, but should be replaced by the current fixed donor HEAD after that HEAD is pushed.
- Phase 1 may proceed only on synthetic data after the engineering gate defects below are fixed. No real owner mailbox and no real family data are authorised before Phase 2 and legal approval.

## Phase 0 status by planned item

| Item | Status | Evidence / decision |
|---|---|---|
| 1. Donor pins and Nerve API snapshot | **Partial** | Three recorded SHAs exist and are reachable, but Hermes `c26fc41` predates six material fixes; the API snapshot names nonexistent `draft_reply` instead of `draft_reply_with_policy`. |
| 2. Fixture sanitisation and gitleaks | **Blocked** | Checks are green on the current tree, but allowlist, key-format and structured/binary-input false negatives let prohibited data pass. |
| 3. Privacy package | **Partial** | All required artifacts exist, but Article 9, Articles 13/14, processor/transfer status, lawful-basis and DPIA issues block real data. |
| 4. Threat model | **Partial** | Required actors, boundaries, 18 threats, WhatsApp consent text and non-goals exist; WhatsApp approval semantics contradict the locked decision. |
| 5. Retention matrix | **Partial** | All six planned TTLs exist; the added DLQ row extends raw payload retention from 30 to 90 days and other rows need reconciliation. |
| 6. Honest residency wording | **Partial** | EU-hosted/international-transfer wording is present, but README/notices state DPA/SCC as current fact while the register says pending. |

## Blocking findings

### 1. Nerve API keys are invisible to both automated gates

`.gitleaks.toml:22-25` accepts `nrv_` followed only by alphanumerics. The pinned Nerve generator emits `nrv_live_` followed by 64 hexadecimal characters (`~/Programs/nerve-cloud/internal/cloudapi/handler_keys.go:231-236`), so the second underscore breaks the regex. `scripts/check_fixtures.py:99-108` has no Nerve rule. A probe using the real format produced no finding in either checker.

Required: match the actual key grammar in both tools and add positive and negative tests derived from the generator contract.

### 2. A single allowlisted fragment disables every rule on that line

`scripts/check_fixtures.py:250-262` performs a line-wide early `continue` before built-in and private-deny checks. `.check-fixtures-allow:10-23` includes both a broad address fragment and the marker `check-fixtures: allow`. Confirmed probes showed no findings for:

- a secret followed by the marker;
- a real-format phone on a line containing the allowed school address;
- a Telegram ID whose context is on the preceding JSON line.

The blanket gitleaks path exclusions at `.gitleaks.toml:33-39` compound the issue: a secret on a marker-bearing line in an excluded test file is invisible to both gates. Missing `HERMES_EXTRA_DENY_FILE` only warns and exits successfully (`scripts/check_fixtures.py:305-312`), CI does not supply it, and the documented local file is absent in the validation environment.

Required: make exceptions `path + rule + exact value`, never suppress private-deny checks, avoid whole-file gitleaks exclusions, and provide a fail-closed trusted check for donor-specific values (for example, CI secret material or non-reversible exact-value fingerprints).

### 3. The scanner does not establish “fixtures only synthetic”

CI runs the default paths rather than `--all` (`.github/workflows/ci.yml:22-23`). The checker skips media, PDF, databases, the whole `data` directory, files over 2 MB, non-UTF-8 text, and encoded MIME bodies (`scripts/check_fixtures.py:58-67,218-237,274-278`). Confirmed misses include spaced/lowercase IBANs, `00`-prefixed phones, multiline Telegram JSON and encoded `.eml` content.

Required: scan all relevant repository paths, MIME-decode `.eml`, fail closed or require provenance for unscannable fixtures, and add tests for skip/encoding/size paths.

### 4. WhatsApp approval semantics contradict the locked decision

The locked decision requires all outgoing WhatsApp actions to use staged approval (`plan:56`). `docs/SECURITY.md:109-112` and the later Phase 4 text (`plan:212`) exempt family recipients by using tracked-send. That conflicts with the root README and the security invariant that nothing outward-facing happens without explicit confirmation.

Required product decision: preserve the safer locked rule—every outgoing WhatsApp action is staged—or explicitly revise the locked decision, README, threat model and success criteria together.

### 5. Special-category data lacks an Article 9 condition

`docs/privacy/lawful-bases.md:61-78`, `docs/privacy/minors.md:28-31` and `docs/privacy/dpia.md:46` treat “not extracting attributes”, short TTL and residual-risk acceptance as sufficient. Storage, reading and transmission to the model are already processing; Article 9 generally prohibits this unless a specific Article 9(2) condition applies. Family consent cannot cover health or religion data belonging to teachers, other parents or their children. [GDPR Articles 4(2) and 9](https://eur-lex.europa.eu/eli/reg/2016/679/oj)

Required: counsel-approved routing or blocking policy for special-category content, or a documented applicable Article 9(2) condition for each affected data-subject class. This cannot be accepted merely as residual risk.

### 6. Transfer and notice claims do not match the processor register

`docs/privacy/processors.md:3-19` leaves required DPA/SCC/TIA items pending, WhatsApp marked absent, Google’s role unresolved, and backup/log providers unnamed. Nevertheless, `README.md:9` and both notices (`privacy-notice-ru.md:61-63`, `privacy-notice-en.md:63-65`) state that transfers are already covered by SCC. The notices also retain controller/contact TODOs and omit parts of the Article 13/14 information set. [GDPR Articles 13, 14, 28 and 44–49](https://eur-lex.europa.eu/eli/reg/2016/679/oj)

Required: identify legal entities and roles per operation, record Article 28 arrangements, transfer mechanism/SCC module, TIA and supplementary measures, then describe only the resulting current state in the notices.

### 7. Current status text authorises too much

`README.md:11` declares Gate −1 closed while the canonical plan leaves both manual criteria unchecked. `docs/privacy/README.md:8-10` says the package closes the gate for owner-test data, but the plan permits real owner-test data only after Phase 2 (`plan:75,170`).

Required: describe the current state as “engineering/privacy draft ready for review; synthetic only; Gate open”.

### 8. Hermes should be repinned after publication of the fixed branch

Actual donor state at validation time:

- branch `agent/foundation-activation` is clean at `a02ab33c5c287b0d6ccecd112042991d27c12203`, not `1633ffe`;
- it is seven commits ahead of the remote, whose HEAD is still the recorded `c26fc41`;
- `c26fc41..a02ab33` changes 23 files (+1695/−195), including crash-safety, scoped reads, barriers, jobs, backups and fail-closed approval-limiter fixes;
- donor tests at current HEAD: 473 passed;
- `claim_pending(code, chat, thread, actor)` remains compatible.

Decision: keep `c26fc41` in the public document only until the donor branch is pushed, then repin to `a02ab33` in a dedicated commit. Do not pin `1633ffe`; it is only the first commit in the remediation series. The Nerve snapshot at `docs/source-pins.md:20` must also use `draft_reply_with_policy` and state the HTTP methods for domain DNS/verify routes.

## Other material privacy findings

- `docs/privacy/dpia.md:6-11` calls the DPIA voluntary despite acknowledging multiple EDPB high-risk criteria. Treat it as required until counsel checks the competent authority’s national list. [GDPR Articles 35–36](https://eur-lex.europa.eu/eli/reg/2016/679/oj)
- `docs/privacy/lawful-bases.md:13-22` does not cleanly separate parties to the contract from other adults, guests, children and third parties; C5 stacks consent and contract without mapping distinct operations. [EDPB Guidelines 2/2019](https://www.edpb.europa.eu/documents/guideline/guidelines-22019-on-the-processing-of-personal-data-under-article-61b-gdpr-in_en)
- `docs/privacy/data-map.md:36,49` promises 30-day raw-event retention but keeps DLQ payload for 90 days. Store raw payload for 30 days and error metadata/status for 90, or explicitly revise the promise.
- `docs/privacy/data-map.md:50` assumes logs containing linkable event IDs are not personal data; consent receipts, DSAR/breach records and the private ops store are absent from the map.
- `docs/privacy/incident-response.md:8-13` sends only confidentiality events in class B through the GDPR breach path, although integrity and availability incidents can also be personal-data breaches. [EDPB data-breach guidance](https://www.edpb.europa.eu/sme/assess-the-risks/data-breaches_en)
- The additional 30-day log and backup TTLs are reasonable proposals, but require explicit owner/counsel acceptance. The 90-day DLQ payload TTL conflicts with the existing promise and should not be accepted as written.

## Automated verification reproduced

| Check | Result |
|---|---|
| `python3 scripts/check_fixtures.py` | pass |
| `python3 scripts/check_fixtures.py --all` | pass |
| Literal `python scripts/check_fixtures.py` in this macOS shell | unavailable (`python` command not installed); documentation should name the supported environment or use `python3` |
| `ruff check .` | pass |
| `pytest -m "not live"` | 25 passed |
| `gitleaks detect --source . --config .gitleaks.toml --log-opts=--all --redact --verbose` | 7 commits, no leaks |
| `gitleaks protect --staged` | no leaks; no staged content existed |
| `actionlint .github/workflows/ci.yml` | pass |
| Hermes donor full suite at `a02ab33` | 473 passed |

These green results establish only that the current samples satisfy the current rules; the confirmed false negatives mean they do not establish the claimed no-PII/no-secret invariant.

## Manual verification outcome

- [ ] **Privacy package reviewed/approved:** not signable as real-data ready. Product owner may acknowledge that the required document skeleton exists for synthetic-only work; GDPR counsel must resolve the listed blockers before any real data.
- [ ] **Donor branch fixed and SHA recorded:** old published snapshot is recorded, but the chosen current fixed baseline is not yet pushed or pinned.

## Closure sequence

1. Harden the sanitiser/gitleaks rules and add adversarial regression tests, including the actual Nerve key format.
2. Resolve the WhatsApp approval contradiction in the canonical plan and security docs.
3. Reconcile privacy TTLs/status claims and obtain counsel decisions on Article 9, lawful bases, Articles 13/14, DPIA and transfers.
4. Push `agent/foundation-activation`, repin Hermes to `a02ab33`, and correct the Nerve API snapshot.
5. Change README/privacy status to Gate open, rerun all checks, and repeat manual sign-off.


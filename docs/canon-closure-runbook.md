# Canon closure runbook — the seven boxes no code can close

Every remaining open box in
`thoughts/shared/plans/2026-08-06-canon-execution-plan.md` needs a human:
counsel, an operator with live credentials, a different repository, or CI.
This file is the sequenced list, with the exact acceptance artifact or command
each one is measured by.

It is not a summary of the plans. Every command below was run against this
checkout before it was written down, and where a box's acceptance criterion is
easy to misread, the misreading is called out — see **O4**, where the obvious
`rg` check reports a failure in the correct state.

**Status at 2026-09-01.** The last box that was code — canon C1's
generation-scoped convergence — closed in #119. Nothing below is blocked on
engineering.

---

## Dependency order

```
O1 (legal)  ─────────────┬──────────────► O3 (BYO live)
                         └──────────────► O4 (Gmail live)
O2 (upstream Nerve) ─────────────────────► O3, O4   [real Nerve rollout only]
O5 (dry-run) ──► O6 (pilot onboarding)
O7 (CI deny-patterns) — independent, any time
```

**O1 gates O3 and O4 absolutely.** Canon Phase A's gate is "no `ABROLIA_REAL_*`
flag enabled until A is merged", and the data policy is synthetic-only until
counsel signs. Running the BYO or Gmail live batteries against real DNS or a
real Google account before O1 is closed would breach that gate — the batteries
themselves are what turns real data on.

O5 and O7 are independent of the legal pack and can be done today.

---

## O1 — The legal pack

**Owner:** counsel + the controller. **Blocks:** every real-data operation.

Three things are outstanding. P1 (Anthropic) and P4 (Resend) are already done
— DPA + SCC in effect, copies committed under `docs/privacy/vendor-dpas/` — so
this is the remainder, not the whole pack.

| # | Item | Status |
|---|---|---|
| 1 | **P2 Fly.io** — DPA 28(3) executed, SCC module 2, transfer assessment | ⏳ none of the three |
| 2 | **TIAs** — all three vendors (Anthropic, Fly.io, Resend) | ⏳ none conducted |
| 3 | **Art. 27 representative** — appointed and named in the notices | ⏳ not appointed |

**Steps**

1. Execute the Fly.io DPA and SCC module 2. Fly publishes a DPA; the
   controller signs it, not an engineer.
2. Conduct a transfer impact assessment per vendor. Each must cite the
   specific transfer, the destination, and the supplementary measures relied
   on. `docs/privacy/processors.md` §2 states the required form.
3. Appoint an Art. 27 representative in the EU and record the entity, address
   and contact in `privacy-notice-en.md` and `privacy-notice-ru.md`.
4. Commit the retrieved copies under `docs/privacy/vendor-dpas/` with the
   retrieval date in the filename, matching the existing two.
5. **Only then** flip the registry rows in `docs/privacy/processors.md` from ⏳
   to ✅ with dates.

> **The rule that matters:** the registry update lands in a *separate PR, after
> signatures exist*. Never flip speculatively. This is canon Phase A rule 2 and
> `processors.md:29`. A ✅ that precedes a signature is the one failure mode
> this whole gate exists to prevent.

**Acceptance artifact**
- `docs/privacy/processors.md` shows ✅ for P1, P2, P4 with dates, and links
  each TIA.
- Art. 27 representative named in both notices.
- Counsel has reviewed the `privacy-notice-*` diff.

Closes: canon Phase A acceptance box 1, go-live checklist **O1**, blocker
**B-07**.

---

## O2 — The upstream Nerve cross-org test

**Owner:** whoever holds commit rights on `nerve-cloud`. **Repository:** not
this one.

**Steps**

1. In `nerve-cloud`, resolve the requested `org_id` against the authenticated
   tenant in `internal/cloudapi/handler_keys.go` (and `internal/store/*` as
   needed).
2. Reject an A→B service-token request with **403**.
3. Add the negative HTTP test `TestServiceToken_CrossOrgRejected`: tenant A's
   billing token requests tenant B's org.
4. Open the PR and merge it.

**Acceptance command** (in `nerve-cloud`)

```bash
go test ./... -count=1
```

green, with `TestServiceToken_CrossOrgRejected` present and passing.

On the Abrolia side the adapter assertion that a tenant never calls
`/v1/service-tokens` is already in place; nothing here changes in this repo.

Closes: canon C1 acceptance box 1, blocker **B-01**.

---

## O3 — BYO domain live battery

**Owner:** operator with live DNS control. **Requires:** O1 closed.

Run against the staging synthetic org, on a domain you control. Bounded
backoff (30/60/120/300/600) and manual CHECK are already implemented; this is
execution, not development.

**The four scenarios**

1. **Resume.** Reload and re-login mid-flow — the same DNS records and state
   come back.
2. **Wrong/partial DNS** stays `waiting_user`; corrected, verified DNS advances
   exactly once.
3. **Writer race.** Two connections claim the same canonical domain — exactly
   one wins, by HMAC uniqueness.
4. **Delete/reconnect**, exercising each failure shape: DNS present, provider
   unavailable, lost response. Follow the webhook → key → inbox → domain → org
   order, confirm `outcome_unknown` is explicit and never auto-retried, and
   exercise the Nerve bootstrap hard-delete path (PR #64).

**Acceptance command**

```bash
pytest tests/control_plane/email -k byo
```

**Acceptance artifact** — an addendum to
`thoughts/shared/implementations/2026-08-05-abrolia-phase-2-email-identity-validation.md`
recording the operator-observed DNS record IDs and the `nerve domain` HMACs.
PII-safe: identifiers only, no message content.

Closes: canon C2 acceptance box, blocker **B-05**.

---

## O4 — Dedicated Gmail live path

**Owner:** operator with a Google test account. **Requires:** O1 closed, and
the account on the allowlist.

**Steps** — each performed manually, once, and recorded:

1. **Connect** — OAuth with PKCE, `prompt=select_account`, exact scopes
   (`openid email gmail.readonly gmail.send`), address confirmation.
2. **Receive** — a message arrives and is ingested through the History cursor.
3. **Approve and send** — the outbound goes only after ✅.
4. **Revoke and delete** — disconnect revokes the grant at Google and deletes
   the stored refresh token. Verify the revocation from the Google account's
   own permissions page, not only from our side.

**Acceptance commands**

```bash
pytest tests/test_gmail_api_ingest.py tests/test_gmail_api_oauth_grant.py \
       tests/test_gmail_api_send.py
```

and the prod-path check. Note that a bare `rg` for the names is **not** the
check, and reading the canon box that way gives a false failure: the names
legitimately appear in two places today, and both are correct.

```bash
rg -n 'HERMES_GMAIL_ADDRESS|HERMES_GMAIL_APP_PASSWORD' -- \
   control_plane hermes_cloud deploy gateway
```

Expected — and only — hits:

| Location | Why it is correct |
|---|---|
| `hermes_cloud/core/config.py` | defines the env var NAMES for the legacy test seam |
| `hermes_cloud/cli.py` | the refusal message stating a provisioned runtime does **not** support them |

The box is satisfied when every hit is one of those two: a definition and a
refusal. A hit anywhere under `control_plane/`, `deploy/` or `gateway/`, or any
hit that *reads* the values on a provisioned runtime path, fails it. The legacy
IMAP poller stays an internal test seam and must not be reachable from
onboarding.

Closes: canon C3 acceptance box, blocker **B-06**.

---

## O5 — Provisioning dry-run on staging

**Owner:** operator. **Independent of O1** — it performs no writes.

**Acceptance command** (against the staging control plane). The plan's
`provision.py --dry-run` is exact; the module has no console-script entry in
`pyproject.toml`, so invoke it with `-m`:

```bash
python -m control_plane.onboarding.provision --dry-run --household <household-id>
```

`--household` is required. The script **refuses to run without `--dry-run`**
(`provision refuses to run without --dry-run`), and it takes no process lock,
so it is safe to run against a control plane that is serving.

Do not confuse it with `abrolia-control-plane dry-run`, which is a different
thing — that drains fake providers; this rehearses one household's onboarding.

**Acceptance criterion** — it lists the three onboarding steps
(email → WhatsApp → primary) and reports `"committed": false` with no writes.
Confirm afterwards that nothing was created:

```bash
flyctl apps list
flyctl volumes list -a <staging-app>
```

Closes: canon Phase E acceptance box 2.

---

## O6 — Manual pilot onboarding, under 60 minutes

**Owner:** operator, following `docs/onboarding-runbook.md`. **Requires:** O5.

One complete pass, timed, on the staging synthetic org:

1. Step 1, 2 and 3 each pass their own checks.
2. Switch the primary channel — and confirm **no history is lost** across the
   switch. That is the substantive assertion; the rest is the runbook.
3. Record the wall-clock time. The box is ≤ 60 minutes; if it takes longer, the
   number to record is the real one, and the runbook is what needs work.

**Acceptance artifact** — a PII-safe transcript with the approval, event,
effect and message IDs, and the elapsed time, appended to the Phase E
validation document.

Closes: canon Phase E acceptance box 3, blocker **B-12**'s last manual gate.

---

## O7 — `check_fixtures --require-deny` in CI

**Owner:** whoever holds the CI secret. **Independent of everything else.**

This box can only ever be closed by CI, and that is not a workaround — it is
the design. The private deny-patterns file exists only there.

Locally the command **exits 2 — a refusal, not a warning**:

```
$ python3 scripts/check_fixtures.py --all --require-deny
warning: приватные deny-паттерны не загружены (HERMES_EXTRA_DENY_FILE не задан)
```

**Steps**

1. Confirm `HERMES_EXTRA_DENY_FILE` is set in the CI environment and points at
   the private patterns file.
2. Confirm the `fixtures & lint & tests` job runs
   `check_fixtures --all --require-deny`.
3. The box closes when that job is green on `main`.

Everything else in this box is already green locally and stays that way:
`git diff --check`, `ruff`, `gitleaks detect --log-opts="--all"`,
`check_fixtures --all`.

Closes: canon Phase F acceptance box.

---

## What is *not* on this list

- **Deploying to production.** Already automated and working: merge to `main` →
  CI → `deploy-production` ships the landing page and the control plane. The
  first green run after nine days of failures was `33430439419` on 2026-08-31.
- **The backup durability gap.** Fixed in code, not by an operator — the boot
  path now writes the archive `/readyz` reads. See
  `docs/control-plane-restore.md`, "The boot archive".
- **Generation-scoped convergence.** Closed in #119.

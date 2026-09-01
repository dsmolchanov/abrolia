# Canon closure runbook — the boxes no code can close

Every remaining open box across
`thoughts/shared/plans/2026-08-06-canon-execution-plan.md`,
`2026-08-23-go-live-checklist.md` (Track O) and `2026-08-06-phase-DE-pilot.md`
needs a human: counsel, an operator with live credentials, a different
repository, or CI. This is the sequenced list, with the acceptance artifact or
command each one is measured by.

Every command below was run against this checkout before it was written down.
Where a box's acceptance criterion is easy to misread, the misreading is called
out — see **O9**, where the obvious `rg` check reports a failure in the correct
state.

**Status at 2026-09-01.** The last box that was *code* — canon C1's
generation-scoped convergence — closed in #119. Nothing below is blocked on
engineering, with one exception: **O4** is waiting on a diagnosis
(issue #121), not on a decision.

---

## The rollout gate set

O7, O8 and O9 each turn a real provider path on. None of them is gated by the
legal pack alone. `phase-DE-pilot.md:723` and canon Phase F state one set that
**every** transition requires, and it is repeated here once rather than
partially restated three times:

1. **Phase A legal merged** — O1 below.
2. **Phase C1 receipt green** — the generation-scoped convergence (#119) and
   the upstream cross-org test (O2).
3. `go test ./... -count=1` green in **nerve-cloud**.
4. `pytest -p no:cacheprovider -m "not live" -q` green in **abrolia**.
5. **One manual live gate** on `abrolia-synthetic` for that provider.

Gmail carries one more, and it is a launch gate rather than a debt: **Google
OAuth verification and the CASA assessment** must be complete before the
dedicated-Gmail card is enabled for any real family
(`2026-08-02-family-ops-assistant-mvp.md:105`).

Reading the dependency graph as "O1, then go" is the error this section exists
to prevent.

---

## Dependency order

```
O1 legal ────────────────┐
O2 nerve cross-org ──────┼──► [rollout gate set] ──► O7 BYO ──┐
                         │                          O8 Gmail ─┼──► O9 per-transition
O3 release tag + drill ──┘                                    │
O4 backup independence   (issue #121 — diagnosis first)       │
O5 dry-run ──► O6 pilot onboarding ───────────────────────────┘
O10 CI deny-patterns — independent, any time
```

O5 and O10 need nothing and can be done today. O3 needs nothing either, and is
the cheapest way to reduce real risk.

---

## O1 — The legal pack

**Owner:** counsel + the controller. **Blocks:** every real-data operation.

P1 (Anthropic) and P4 (Resend) are done — DPA + SCC in effect, copies committed
under `docs/privacy/vendor-dpas/`. This is the remainder.

| # | Item | Status |
|---|---|---|
| 1 | **P2 Fly.io** — DPA 28(3), SCC module 2, transfer assessment | ⏳ none of the three |
| 2 | **TIAs** — Anthropic, Fly.io, Resend | ⏳ none conducted |
| 3 | **Art. 27 representative** — appointed and named in the notices | ⏳ not appointed |

**Steps**

1. Execute the Fly.io DPA and SCC module 2. The controller signs, not an
   engineer.
2. Conduct a transfer impact assessment per vendor, each citing the specific
   transfer, the destination, and the supplementary measures relied on.
   `docs/privacy/processors.md` §2 states the required form.
3. Appoint an EU Art. 27 representative; record entity, address and contact in
   `privacy-notice-en.md` and `privacy-notice-ru.md`.
4. Commit retrieved copies under `docs/privacy/vendor-dpas/` with the retrieval
   date in the filename, matching the existing two.
5. **Only then** flip the registry rows in `docs/privacy/processors.md` ⏳ → ✅
   with dates.

> **The rule that matters:** the registry update lands in a *separate PR, after
> signatures exist*. Never flip speculatively — canon Phase A rule 2 and
> `processors.md:29`. A ✅ that precedes a signature is the failure mode this
> whole gate exists to prevent.

**Acceptance artifact** — `processors.md` shows ✅ for P1, P2, P4 with dates and
links each TIA; the Art. 27 representative is named in both notices; counsel has
reviewed the `privacy-notice-*` diff.

Closes canon Phase A box 1, go-live **O1**, blocker **B-07**.

---

## O2 — The upstream Nerve cross-org test

**Owner:** whoever holds commit rights on `nerve-cloud`. **Repository:** not
this one.

1. Resolve the requested `org_id` against the authenticated tenant in
   `internal/cloudapi/handler_keys.go` (and `internal/store/*` as needed).
2. Reject an A→B service-token request with **403**.
3. Add `TestServiceToken_CrossOrgRejected`: tenant A's billing token requests
   tenant B's org.
4. Merge.

**Acceptance command** (in `nerve-cloud`)

```bash
go test ./... -count=1
```

green, with `TestServiceToken_CrossOrgRejected` present and passing.

The Abrolia-side assertion that a tenant never calls `/v1/service-tokens` is
already in place; nothing changes in this repository.

Closes canon C1 box 1, blocker **B-01**.

---

## O3 — Release tag and restore drill

**Owner:** operator. **Needs nothing first.** **Zero git tags exist today.**

This is the cheapest open box with real downside if skipped: nothing has yet
demonstrated that the *current* Phase E schema can actually be restored.

1. Tag the release.
2. Repeat the Phase B isolated backup/restore procedure against the Phase E
   schema — now including `channel_preferences`, `channel_bindings` and the
   usage tables — per `docs/control-plane-restore.md`:
   - `integrity_check` clean,
   - `foreign_key_check` zero violations,
   - pause marker mode `0600`,
   - smoke without leasing,
   - `resume-jobs`,
   - a new onboarding through revision 1,
   - destroy the temporary app.

Backup-before-migrate itself already landed and fails closed
(`tests/control_plane/test_migrate_on_start.py`).

**Acceptance artifact** — the drill transcript with the tag name, appended to
the Phase E validation document.

Closes go-live **O2**, `phase-DE-pilot.md:646`, canon Phase E change 9.

---

## O4 — Backups must not depend on deploys

**Owner:** operator, then possibly engineering. **Status: diagnosis first —
see issue #121.**

#118 gave the readiness signal the writer it never had: the boot path now
writes `/data/backups/boot-<epoch>.cpb`, the archive `/readyz` actually reads.
It deployed successfully on 2026-09-01 and **no archive appeared** — production
is still ageing off an operator's manual archive from ~2026-08-22.

The boot archive is non-fatal by design, so the reason is in the container's
stderr and nowhere else:

```bash
flyctl logs -a abrolia-control-plane-synthetic | grep 'boot archive skipped'
```

Leading hypothesis is directory ownership on `/data/backups`; issue #121 has
the full candidate list and the check commands.

Beyond that diagnosis, go-live **O0a** remains open on its own terms: the only
backup path still runs at container start, so the system can only back up by
restarting. Closing O0a properly means an in-process periodic backup (the
serving process already holds the lock) or a scheduled restart. Both are real
work; neither is a workflow file.

---

## O5 — Provisioning dry-run on staging

**Owner:** operator. **Independent of O1** — it performs no writes.

The plan's `provision.py --dry-run` is exact, but the module has no
console-script entry in `pyproject.toml`, so invoke it with `-m`:

```bash
python -m control_plane.onboarding.provision --dry-run --household <household-id>
```

`--household` is required, and the script **refuses to run without
`--dry-run`** (`provision refuses to run without --dry-run`). It takes no
process lock, so it is safe against a control plane that is serving.

Not to be confused with `abrolia-control-plane dry-run`, which drains fake
providers — a different thing that is easy to reach for by name.

**Acceptance criterion** — it lists the three onboarding steps
(email → WhatsApp → primary), reports `"committed": false`, and writes nothing:

```bash
flyctl apps list
flyctl volumes list -a <staging-app>
```

Closes canon Phase E box 2.

---

## O6 — Manual pilot onboarding, under 60 minutes

**Owner:** operator, following `docs/onboarding-runbook.md`. **Requires:** O5.

One complete timed pass on the staging synthetic org:

1. Steps 1, 2 and 3 each pass their own checks.
2. Switch the primary channel and confirm **no history is lost** across the
   switch. That is the substantive assertion; the rest is the runbook.
3. Record the wall-clock time. The box is ≤ 60 minutes. If it takes longer,
   record the real number — the runbook is then what needs work, not the
   record.

**Acceptance artifact** — a PII-safe transcript with approval, event, effect
and message IDs plus elapsed time, appended to the Phase E validation document.

Closes canon Phase E box 3.

---

## O7 — BYO domain live battery

**Owner:** operator with live DNS control.
**Requires: the full rollout gate set above** — not O1 alone.

Bounded backoff (30/60/120/300/600) and manual CHECK are already implemented;
this is execution, not development.

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
recording operator-observed DNS record IDs and the `nerve domain` HMACs.
Identifiers only, no message content.

Closes canon C2 box, blocker **B-05**.

---

## O8 — Dedicated Gmail live path

**Owner:** operator with a Google test account.
**Requires: the full rollout gate set, plus OAuth verification and CASA.**
The account must also be on the allowlist.

Each step performed manually, once, and recorded:

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

---

## O9 — The `rg` check, and how to read it

Part of O8, but stated separately because reading it naively produces a false
failure.

```bash
rg -n 'HERMES_GMAIL_ADDRESS|HERMES_GMAIL_APP_PASSWORD' -- \
   control_plane hermes_cloud deploy gateway
```

Both names legitimately appear today, and both hits are correct:

| Location | Why it is correct |
|---|---|
| `hermes_cloud/core/config.py` | defines the env var NAMES for the legacy test seam |
| `hermes_cloud/cli.py` | the message refusing them on a provisioned runtime |

The box is satisfied when every hit is one of those two — a definition and a
refusal. A hit anywhere under `control_plane/`, `deploy/` or `gateway/`, or any
hit that *reads* the values on a provisioned runtime path, fails it. The legacy
IMAP poller stays an internal test seam and must not be reachable from
onboarding.

Closes canon C3 box, blocker **B-06**.

---

## O10 — Per-transition live gates

**Owner:** operator. **Requires:** O7 and/or O8 for the provider concerned.

Each promotion in Track R — synthetic → operator accounts → invited pilot
families — needs its own manual battery per `docs/onboarding-runbook.md`
§Rollout: receive, approved send, restart/cursor resume, reconnect, export,
revoke, delete. Recorded PII-safe.

The rollout gate set at the top applies to **every** transition, not only the
first, and each provider's `ABROLIA_*_ENABLED` flips independently after
operator-account soak.

Closes go-live **O3**.

---

## O11 — `check_fixtures --require-deny` in CI

**Owner:** whoever holds the CI secret. **Independent of everything else.**

Only CI can close this, and that is the design — the private deny-patterns file
exists only there. Locally the command **exits 2, a refusal, not a warning**:

```
$ python3 scripts/check_fixtures.py --all --require-deny
warning: приватные deny-паттерны не загружены (HERMES_EXTRA_DENY_FILE не задан)
```

1. Confirm `HERMES_EXTRA_DENY_FILE` is set in CI and points at the private
   patterns file.
2. Confirm the `fixtures & lint & tests` job runs
   `check_fixtures --all --require-deny`.
3. The box closes when that job is green on `main`.

Everything else in the box is already green locally and stays that way:
`git diff --check`, `ruff`, `gitleaks detect --log-opts="--all"`,
`check_fixtures --all`.

Closes canon Phase F box.

---

## What is not on this list

- **Deploying to production.** Automated and working: merge to `main` → CI →
  `deploy-production` ships the landing page and the control plane. First green
  run after nine days of failure was `33430439419` (2026-08-31).
- **Generation-scoped convergence.** Closed in #119 — the last canon box that
  was code.
- **The readiness-signal writer.** Landed in #118. Whether it actually produces
  an archive in production is **O4**, above.

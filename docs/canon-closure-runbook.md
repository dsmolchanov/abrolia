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

**Status at 2026-09-02.** The last box that was *code* — canon C1's
generation-scoped convergence — closed in #119. Since then: **O4** is closed
(issue #121 — a root-owned `/data/backups`, fixed with a `chown` on the
volume, not a deploy), go-live **O0a** is closed (#131, the serving process
keeps its own archive fresh), **O11** is closed by CI evidence (see below),
and **O3** is half done — `v0.1.0` exists and the schema drill passed, the
staging drill has not run. Nothing below is blocked on engineering. What
remains is counsel (O1), another repository (O2), and an operator with live
credentials (O3 staging half, O5–O8, O10).

---

## The rollout gate set, and which half applies when

`phase-DE-pilot.md:723` and canon Phase F state one set that every real-data
transition requires. It splits in two, and conflating the halves makes the
graph circular — item 5 is the live battery itself, so demanding it *before*
the battery would make O7 and O8 prerequisites of themselves.

**Prerequisites — required BEFORE running a live battery (O7, O8):**

1. **Phase A legal merged** — O1 below.
2. **Phase C1 receipt green** — the generation-scoped convergence (#119) and
   the upstream cross-org test (O2).
3. `go test ./... -count=1` green in **nerve-cloud**.
4. `pytest -p no:cacheprovider -m "not live" -q` green in **abrolia**.

**Evidence the batteries produce — required before PROMOTION (O10):**

5. **One manual live gate** on `abrolia-synthetic` for that provider. This is
   what O7 and O8 *are*. They supply item 5; they do not consume it.

Gmail additionally needs **Google OAuth verification and the CASA
assessment** — but at the real-family boundary, not before the test battery.
The code is explicit about this: `GoogleOAuthProvisioner._allowed` permits an
account in `google_oauth_test_users` while `gmail_real_enabled` is false, and
`ControlPlaneConfig.validate` demands the verification/scope/CASA/Limited-Use
evidence **only** when `gmail_real_enabled` is on
(`control_plane/config.py:240`). So CASA gates O10 for Gmail, not O8.

Reading the dependency graph as "O1, then go" is one error this section exists
to prevent; reading it as "everything, then go" is the other.

---

## Dependency order

```
O1  legal ───────────────┐
O2  nerve cross-org ─────┼──► [prerequisites 1-4] ──► O7  BYO battery ──┐
O3  release tag + drill ─┘                            O8  Gmail battery ┼──► O10 promotion
                                                          (+O9 rg check)│    (+CASA for Gmail)
O4  backup independence  ✅ closed 2026-09-02 (#121, #131)              │
O5  dry-run ──► O6 pilot onboarding ────────────────────────────────────┘
O11 CI deny-patterns — independent, any time
```

O5 and O11 need nothing and can be done today. O3 needs nothing either, and is
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

**Owner:** operator. **Needs nothing first.**
**Status 2026-09-02: tag done, drill PARTIAL — this box is still open.**

`v0.1.0` exists (the first tag this repository has carried), and the schema
half of the drill passed 56 checks against the current Phase E schema —
evidence in
`thoughts/shared/implementations/2026-09-02-v0.1.0-restore-drill.md`.

**What is left is the staging half, and it is the half that closes the box:**
an isolated volume on a Machine with no public route, the *actual production
archive* rather than an equivalent database at the same schema, the teardown,
and step 6's job reconciliation (unexercised — the seeded jobs were
`pending`). Because this box gates the live batteries and the promotion, a
tick here reads downstream as "recovery is proven", so partial evidence stays
partial.

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

**Owner:** operator, then possibly engineering. **Status 2026-09-02: CLOSED —
issue #121 resolved, O0a closed in #131. Kept for the record.**

#118 gave the readiness signal the writer it never had: the boot path now
writes `/data/backups/boot-<epoch>.cpb`, the archive `/readyz` actually reads.
It deployed successfully on 2026-09-01 and **no archive appeared** — production
is still ageing off an operator's manual archive from ~2026-08-22.

The boot archive is non-fatal by design, so the reason is in the container's
stderr and nowhere else:

```bash
flyctl logs -a abrolia-control-plane-synthetic | grep 'boot archive skipped'
```

Leading hypothesis is directory ownership on `/data/backups`, and the
Dockerfile makes it likely: `/data` is `chown`ed to `abrolia` at **image build
time**, but the Fly volume mounts OVER that directory at runtime, so the chown
never applies to it — and `flyctl ssh console` logs in as **root**, so a
`backups/` created by an operator's manual archive is root-owned while the
service runs as uid 10001.

Confirm with (note the `sh -c`; `ssh console -C` execs directly and will not
accept `;` separators):

```bash
flyctl ssh console -a abrolia-control-plane-synthetic \
  -C "sh -c 'id; ls -ld /data /data/backups'"
```

If `/data/backups` is root-owned, the remedy is
`chown -R 10001:10001 /data/backups` on the volume — **not** a deploy, which
is why the deploy gate is right to keep shipping over it.

The boot now names this case rather than reporting it as "no room": it is a
distinct `BootArchiveDirectoryNotWritable`, whose message states the remedy,
and the readiness payload reports `backup_writer: failed` with a
`backup_writer_failed` blocker. Issue #121 has the remaining candidate list.

**Resolution (2026-09-02).** The hypothesis held: `/data/backups` was
root-owned, `EACCES` for uid 10001. The `chown -R 10001:10001 /data/backups`
landed on the volume and the next boot wrote the archive — `/readyz` went from
`backup_stale` after 258 hours without a restore point to `blockers: []`,
`backup_writer: skipped_interval`. Go-live **O0a** closed the same day in
#131: `take_periodic_archive` runs from the `serve` worker loop, so an archive
no longer needs a restart. Steady state observed at 2026-09-02 05:00 UTC:
`status: ready`, archive age 5 h, no blockers.

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
**Requires: prerequisites 1–4 above** — not O1 alone. This battery *supplies*
gate 5 for BYO; it does not need it first.

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
**Requires: prerequisites 1–4 above**, and the account's recovery address in
`google_oauth_test_users`. This battery *supplies* gate 5 for Gmail.

**Not** OAuth verification or CASA. Those gate the real-family promotion
(O10), and requiring them here would block the very battery that is meant to
run before them: `_allowed` admits an allowlisted test user while
`gmail_real_enabled` is false, which is the path this box exercises.

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

Prerequisites 1–4 apply to **every** transition, not only the first, and gate
5 must exist for the provider being promoted — that is the battery evidence
from O7 or O8. Each provider's `ABROLIA_*_ENABLED` flips independently after
operator-account soak.

**Gmail's real-family promotion additionally requires** Google OAuth
verification, scope review, CASA and Limited Use evidence. `gmail_real_enabled`
cannot be turned on without them — `ControlPlaneConfig.validate` refuses with
"real Gmail requires verified OAuth, scope, CASA and Limited Use evidence"
(`control_plane/config.py:240`). This is the boundary those checks belong to.

Closes go-live **O3**.

---

## O11 — `check_fixtures --require-deny` in CI

**Owner:** whoever holds the CI secret. **Independent of everything else.**
**Status 2026-09-02: CLOSED.** `ci.yml` materialises `HERMES_DENY_PATTERNS`
from the repository secret into `$RUNNER_TEMP/deny-patterns.txt` (empty secret
⇒ the next step exits 2, fail-closed) and runs
`check_fixtures --all --require-deny` against it in the `fixtures & lint &
tests` job; that job is green on `main` at `97706ea`. The plan boxes were
ticked in the same PR as this note.

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

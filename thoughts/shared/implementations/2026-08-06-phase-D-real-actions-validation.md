---
title: "Phase D / MVP Phase 4 — Synthetic Live Validation"
status: passed
validated_at: "2026-08-06"
base_commit: a4c4467
environment: abrolia-phase4-live-synthetic
data_policy: synthetic-content-only
deferred_gate: Gmail OAuth / History production path moves to Phase 5
---

# Phase D / MVP Phase 4 — Synthetic Live Validation

## Decision

Phase 4 is closed for the operator-approved live scope. The dedicated Gmail OAuth / History
production path is explicitly deferred to the start of Phase 5; legacy SMTP/IMAP remains a
test-only seam and is not accepted as the Gmail production path.

## Fly deployment

- Source baseline: `main@a4c4467`, built from an isolated detached worktree.
- App: `abrolia-phase4-live-synthetic` (`personal`, because no separate Fly org named
  `abrolia-synthetic` exists in the current account).
- Machine: `d8d4524b619748`, `ams`, `shared-cpu-1x`, 512 MB.
- Encrypted volume: `vol_vgn1wpnll31yme14`, 1 GB.
- Final image: `deployment-01KZB8KPHJZFKRF3K6Y2X2V802`.
- Final digest: `sha256:d3385250a16c469291953c53b24a16e6908121300d7933afeca4764b42ec16db`.

## Live evidence

| Gate | Result | Evidence |
|---|---|---|
| Staged email + PDF | Passed | Approval `54d5af681ee442c49985653a4daa35f1`; RFC Message-ID `<hermes-5a03b81af9df4151a5b596fc2138ee53@hermes-cloud.invalid>`; IMAP read-back confirmed `application/pdf`, filename `phase4-synthetic.pdf`, and `%PDF` payload after confirmation. |
| Email kill-switch | Passed | Approval `bfce8fecd1c24035895f46d5c35fd3bf`; `HERMES_EMAIL_SEND=0` produced `EgressBlocked`, approval became `failed`, and the SMTP backend was not called. |
| Calendar create → move | Passed | Google event `hc1likjv7brt4qe77jie3d181kkcgsq2`; create on 2026-09-12, update to 2026-09-19 with the same ID, provider listing contained exactly one matching event; event deleted after verification. |
| Calendar timezone idempotency | Passed after fix | Live Google response normalized `07:45+00:00` to `09:45+02:00`; final image canonicalizes both to UTC for comparison and reports `differs=false`, avoiding a redundant PATCH. Diagnostic event `hc1nmtqsplcpkstq4mj7pk0vmp2h5954` was deleted. |
| WhatsApp inbound/HMAC | Passed | Wrong signature returned 401 and wrote no event; correct signature created event `5507b076cccb422f9833f65a955e5eff`; replay deduplicated it. |
| WhatsApp outbound after confirmation | Passed | Approval `93316fd6697246fca1046ca4e8dca3a1` was `staged` before callback and `done` afterward; effect `00ab36d6655d445099a9075b27ebe810` is `done` with a provider receipt. No recipient or credential is recorded here. |

All provider payloads were explicitly marked synthetic. Operator credentials were used only
after explicit approval, inside the isolated Machine; secret values and recipient identifiers
were not written to this evidence file.

## Code defects found and remediated

1. The runtime wheel omitted `hermes_cloud/core/migrations/*.sql`; a clean database therefore
   failed with `no such table: approvals`.
2. Google dependencies existed in `requirements.txt` but not in wheel metadata, so the Docker
   runtime failed with `ModuleNotFoundError: google`.
3. Calendar equality compared ISO strings rather than instants, causing redundant updates after
   Google changed the timezone offset representation.

The final image contains all seven runtime migrations, declares both Google dependencies, creates
the `approvals` table from a clean DB, imports `google.oauth2`, and treats equivalent instants as
unchanged.

## Verification

- Isolated `a4c4467` + fixes: `pytest -m "not live" -q -p no:cacheprovider` — passed.
- `ruff check .` — passed.
- `git diff --check` — passed.
- Main worktree targeted tests: `tests/test_gcal.py`, `tests/test_packaging.py`, and runtime DB tests — passed.
- The main worktree's full suite has two unrelated fixture-scanner failures caused by the
  pre-existing untracked canon handoff containing the real security contact; the isolated source
  tree used for the image is green.

## Deferred to Phase 5

- Gmail OAuth connect/receive/send/revoke and Gmail History cursor validation.
- Gmail verification/CASA and legal gates remain independent prerequisites for any real-family
  enablement.

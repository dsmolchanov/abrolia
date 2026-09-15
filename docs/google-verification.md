# Google OAuth verification for the agent Gmail option

The separate agent Gmail option requests `openid`, `email`,
`https://www.googleapis.com/auth/gmail.readonly` (**restricted**) and
`https://www.googleapis.com/auth/gmail.send` (**sensitive**). Opening it to any
Google account — not only `ABROLIA_GOOGLE_OAUTH_TEST_USERS` — needs Google's
restricted-scope verification and a CASA security assessment. Only after both
may `ABROLIA_GMAIL_REAL_ENABLED=1` be deployed, together with the four evidence
flags `control_plane/config.py` demands.

Requirements below were read from Google's official pages on 2026-09-13:
[restricted scope verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification),
[verification FAQ](https://support.google.com/cloud/answer/13463817),
[User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
[Workspace user data policy](https://developers.google.com/workspace/workspace-api-user-data-developer-policy),
[security assessment](https://support.google.com/cloud/answer/13465431),
[CASA assurance levels](https://appdefensealliance.dev/casa/casa-tiering).
Re-read them before submitting; they change.

## Status

| Item | Owner | Status |
|---|---|---|
| Public homepage `https://abrolia.com` linking Privacy Policy and Terms | engineering | done in this change |
| Privacy Policy `https://abrolia.com/privacy.html` with the Limited Use statement (§ "If you chose a separate agent Gmail", anchor `#google`) | engineering + owner review | done in this change |
| Terms of Service `https://abrolia.com/terms.html` | engineering draft + owner review | done in this change |
| In-product disclosure before OAuth, linking the policy | engineering | done in this change |
| `abrolia.com` verified in Google Search Console | owner | ⏳ |
| OAuth consent screen branding complete; brand verification | owner | ⏳ |
| Publishing status switched from **Testing** to **In production** | owner | ⏳ |
| Verification submitted with scope justifications and demo video | owner | ⏳ |
| CASA assessment with an authorized lab; Letter of Assessment | owner + lab | ⏳ |
| Dependency vulnerability scan and `cryptography` ≥ 50 in the control plane | engineering | done (#166); household runtimes need the next image + `roll-runtime` |
| Evidence flags + `ABROLIA_GMAIL_REAL_ENABLED=1` deployed | engineering | ⏳ after approval |

**While the app is in Testing**, Google expires refresh tokens after 7 days for
any scope beyond name/email/profile: a connected agent Gmail stops working
weekly and the runtime reports it as needing attention. Switching to In
production removes that expiry but shows the unverified-app screen and counts
toward a lifetime cap of 100 new users until verification completes.

## 1. Owner checklist in Google Cloud Console

1. **Search Console** — verify ownership of `abrolia.com` (DNS TXT record) with
   the same Google account that owns the Cloud project.
2. **Google Auth Platform → Branding**
   - App name: `Abrolia` — must match the homepage.
   - User support email and developer contact: an address at `abrolia.com`.
   - App logo: the Abrolia mark (`landing/favicon.svg` rendered as 120×120 PNG).
   - Application home page: `https://abrolia.com`
   - Privacy policy: `https://abrolia.com/privacy.html`
   - Terms of service: `https://abrolia.com/terms.html`
   - Authorized domains: `abrolia.com`
3. **Clients** — the web client's authorized redirect URI is exactly
   `https://app.abrolia.com/api/v1/email/google/callback`; no other clients.
4. **Data access** — exactly these scopes: `openid`, `.../auth/userinfo.email`,
   `.../auth/gmail.readonly`, `.../auth/gmail.send`. Remove anything else.
5. **Audience** — publishing status **In production**, user type External.
6. **Verification Center** — submit brand verification (2–3 business days),
   then the restricted-scope request with §2 and the §3 video (about 6 weeks).
7. When Google requests it, choose an authorized CASA lab and share §4.

## 2. Scope justifications (paste into the form)

**`gmail.readonly`** — Abrolia is a family operations assistant. A household
creates a *separate* Google account that serves as the assistant's mailbox;
schools, clubs and family members send letters there, or the family forwards
them. Abrolia reads only messages that arrive in that mailbox's INBOX after it
is connected (Gmail History API, `historyTypes=messageAdded`, `labelId=INBOX`;
no backlog is read), extracts dates, amounts and required actions, and shows the
family a proposal card. Narrower scopes are insufficient: `gmail.metadata`
exposes no message body, and the obligations Abrolia explains are in the body
and attachments; `gmail.labels` and `gmail.send` do not read mail.

**`gmail.send`** — When the family confirms a proposed reply on a card (for
example accepting a parent meeting), Abrolia sends that single email from the
assistant's mailbox via `users.messages.send`. Nothing is sent without an
explicit confirmation by an authorised adult. `gmail.compose` and full-access
scopes are broader than needed; Abrolia never creates drafts or modifies mail.

**Data handling summary** — Gmail content is processed only inside the
household's dedicated EU-hosted runtime, kept 30 days, never used for
advertising or to train generalized models, and transferred only to the model
provider named in the Privacy Policy to produce the proposal. Refresh tokens are
stored encrypted. Access is removed in the user's Google Account, on request,
or by deleting the Abrolia account, which revokes the grant with Google.

## 3. Demo video script (English, ~3 minutes, unlisted YouTube link)

Google requires: the OAuth consent flow in English, the app name on the consent
screen, the OAuth **client ID visible in the browser address bar** on the
consent page, and how each requested scope is used.

1. Show `https://abrolia.com`, then the footer links to Privacy Policy and Terms.
2. Sign in at `https://app.abrolia.com`, complete the household profile.
3. On the email step choose **Separate agent Gmail**; show the disclosure list
   and the Privacy Policy link.
4. Click **Continue with Google**. On Google's consent screen, pause on the
   address bar and zoom into the `client_id=` parameter; show the app name and
   the two Gmail permissions.
5. Choose the *separate* agent account; confirm it is the dedicated mailbox.
6. **gmail.readonly**: from another account send a school-style letter to the
   agent mailbox; show the proposal card Abrolia creates from it.
7. **gmail.send**: confirm a reply on a card; show the sent message arriving in
   the other account's inbox.
8. Open `myaccount.google.com/permissions` for the agent account, show Abrolia
   listed with its Gmail access, and remove it.

Use synthetic letters and accounts only; no real family data on screen.

## 4. CASA preparation

- **Architecture**: control plane (`app.abrolia.com`, Fly.io `ams`) stores only
  account/onboarding metadata and never receives mail content; each household
  has a dedicated runtime (Fly.io `ams`) that fetches Gmail, stores content in
  its own SQLite volume, and deletes it after 30 days.
- **Tokens**: the authorization code is exchanged server-side with PKCE (S256);
  the refresh token is written to the household's Fly secret namespace and kept
  AES-256-GCM encrypted in the runtime database, bound to identity and revision
  (`hermes_cloud/email/google_grant.py`). It never reaches the browser, the
  control-plane database, job records, the model, or logs.
- **Revocation and deletion**: resetting the email step or an operator
  disconnect calls Google's revoke endpoint and wipes token material; account deletion revokes and removes the secret
  (`docs/privacy/delete-runbook.md`).
- **Transport and headers**: HTTPS only; HSTS on `app.abrolia.com`
  (`max-age=31536000; includeSubDomains`) and on `abrolia.com`; strict CSP,
  `X-Frame-Options: DENY`, `nosniff` (`control_plane/api/app.py`,
  `landing/vercel.json`).
- **Logging**: identifiers and statuses only, never message content
  (`docs/SECURITY.md`).
- **Secrets scanning**: gitleaks over full history and the fixture sanitizer on
  every PR (`.github/workflows/ci.yml`).
- **Dependencies**: `pip-audit` runs on every PR and weekly
  (`.github/workflows/dependency-audit.yml`). Before the lab scan, roll
  household runtimes onto an image built with `cryptography` ≥ 50.
- **Known gap**: there is no in-app "Disconnect Gmail" control once setup is
  complete; users revoke in their Google Account, on request, or by deleting
  the account. An in-app control is worth adding before submission.
- Level (AL1/AL2) is assigned by Google; revalidation is annual.

## 5. Opening Gmail to everyone

Only after Google's approval email and the lab's Letter of Assessment exist:

1. Record both (dates, reference numbers) in this file's status table and in
   `docs/privacy/processors.md` row P5.
2. In `deploy/control-plane/fly.toml` set `ABROLIA_GOOGLE_OAUTH_APP_VERIFIED`,
   `ABROLIA_GOOGLE_GMAIL_SCOPE_APPROVED`, `ABROLIA_GOOGLE_CASA_CURRENT`,
   `ABROLIA_GOOGLE_LIMITED_USE_DISCLOSED` and `ABROLIA_GMAIL_REAL_ENABLED` to
   `"1"`. The boot refuses the last one without the other four.
3. Update the Privacy Policy sentence about test users, in both notices and
   `landing/privacy.html`.
4. Put a calendar reminder 11 months after the Letter of Assessment for CASA
   revalidation; set `ABROLIA_GOOGLE_CASA_CURRENT=0` if it lapses.

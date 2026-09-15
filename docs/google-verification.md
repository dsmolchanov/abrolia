# Google OAuth verification for the agent Gmail option

The separate agent Gmail option requests `openid`, `email` and
`https://www.googleapis.com/auth/gmail.send` (**sensitive**) — and nothing
else. Owner decision 2026-09-13: no read scope. Abrolia never reads the agent
mailbox; incoming mail reaches the household because the family turns on
Gmail's automatic forwarding to a hidden relay address Abrolia created for
them (`thoughts/shared/plans/2026-09-13-gmail-send-only-forwarding.md`).

Because no **restricted** scope is requested, opening the option to any Google
account — not only `ABROLIA_GOOGLE_OAUTH_TEST_USERS` — needs Google's
**sensitive-scope verification** only. There is no CASA security assessment
and no annual revalidation. Only after Google's approval may
`ABROLIA_GMAIL_REAL_ENABLED=1` be deployed, together with the three evidence
flags `control_plane/config.py` demands.

Requirements below were read from Google's official pages on 2026-09-13 and
2026-09-15:
[sensitive scope verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/sensitive-scope-verification),
[verification FAQ](https://support.google.com/cloud/answer/13463817),
[User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
[Gmail API scopes](https://developers.google.com/workspace/gmail/api/auth/scopes),
[Gmail forwarding help](https://support.google.com/mail/answer/10957).
Re-read them before submitting; they change.

## Status

| Item | Owner | Status |
|---|---|---|
| Public homepage `https://abrolia.com` linking Privacy Policy and Terms | engineering | done (#165) |
| Privacy Policy `https://abrolia.com/privacy.html` with the Limited Use statement (§ "Google user data", anchor `#google`), send-only wording | engineering + owner review | done (#165, Phase 5) |
| Terms of Service `https://abrolia.com/terms.html` | engineering draft + owner review | done (#165, Phase 5) |
| In-product disclosure before OAuth, linking the policy | engineering | done (#165, Phase 5) |
| Gmail API enabled in the Cloud project `abrolia-508516` (spike S4: `messages.send` is 403 without it) | owner | ⏳ |
| `abrolia.com` verified in Google Search Console | owner | ⏳ |
| OAuth consent screen branding complete; brand verification | owner | ⏳ |
| Publishing status switched from **Testing** to **In production** | owner | ⏳ |
| Verification submitted with the scope justification and demo video | owner | ⏳ |
| Dependency vulnerability scan and `cryptography` ≥ 50 in the control plane | engineering | done (#166); household runtimes need the next image + `roll-runtime` |
| Evidence flags + `ABROLIA_GMAIL_REAL_ENABLED=1` deployed | engineering | ⏳ after approval |

**While the app is in Testing**, Google expires refresh tokens after 7 days for
any scope beyond name/email/profile: a connected agent Gmail stops sending
weekly and the runtime's readiness fails closed until the family reconnects.
Switching to In production removes that expiry but shows the unverified-app
screen and counts toward a lifetime cap of 100 new users until verification
completes.

## 1. Owner checklist in Google Cloud Console

1. **APIs & Services → Library** — enable the **Gmail API** for the project.
2. **Search Console** — verify ownership of `abrolia.com` (DNS TXT record) with
   the same Google account that owns the Cloud project.
3. **Google Auth Platform → Branding**
   - App name: `Abrolia` — must match the homepage.
   - User support email and developer contact: an address at `abrolia.com`.
   - App logo: the Abrolia mark (`landing/favicon.svg` rendered as 120×120 PNG).
   - Application home page: `https://abrolia.com`
   - Privacy policy: `https://abrolia.com/privacy.html`
   - Terms of service: `https://abrolia.com/terms.html`
   - Authorized domains: `abrolia.com`
4. **Clients** — the web client's authorized redirect URI is exactly
   `https://app.abrolia.com/api/v1/email/google/callback`; no other clients
   (delete the Desktop client created for the 2026-09-14 spike).
5. **Data access** — exactly these scopes: `openid`,
   `.../auth/userinfo.email`, `.../auth/gmail.send`. Remove anything else —
   the Gmail read scope in particular, which would turn this into a
   restricted-scope request.
6. **Audience** — publishing status **In production**, user type External.
7. **Verification Center** — submit brand verification (2–3 business days),
   then the sensitive-scope request with §2 and the §3 video.

## 2. Scope justification (paste into the form)

**`gmail.send`** — Abrolia is a family operations assistant. A household
creates a *separate* Google account that serves as the assistant's mailbox.
When an authorised adult confirms a proposed reply on a card (for example
accepting a parent meeting), Abrolia sends that single email from the
assistant's mailbox via `users.messages.send`. Nothing is sent without an
explicit confirmation. `gmail.compose` and full-access scopes are broader than
needed: Abrolia never creates drafts, reads, labels or modifies mail. Abrolia
requests **no read scope**: incoming mail reaches it only through Gmail's
automatic forwarding, which the family turns on in Gmail's own settings to a
relay address Abrolia created for their household.

**`openid`, `email`** — to show the family which address was connected and to
check that it is the dedicated agent account, not a personal mailbox.

**Data handling summary** — Emails Abrolia sends are composed inside the
household's dedicated EU-hosted runtime from content the family confirmed and
journaled for 365 days. Refresh tokens are stored encrypted. Access is removed
in the user's Google Account, on request, or by deleting the Abrolia account,
which revokes the grant with Google. Google user data is never used for
advertising or to train generalized models.

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
   the single Gmail permission, **Send email on your behalf**, and tick it.
5. Choose the *separate* agent account; confirm it is the dedicated mailbox.
6. **Forwarding (no read scope)**: in Gmail on a computer open Settings →
   Forwarding and POP/IMAP → Add a forwarding address; enter the relay address
   the web chat showed. Open the web chat, show Abrolia's message with Gmail's
   confirmation link, open it, press Confirm; back in Gmail choose "Forward a
   copy of incoming mail to …" and Save Changes. From another account send a
   school-style letter to the agent mailbox; show the proposal card Abrolia
   creates from the forwarded copy.
7. **gmail.send**: confirm a reply on that card; show the sent message arriving
   in the other account's inbox, sent from the agent address.
8. Open `myaccount.google.com/permissions` for the agent account, show Abrolia
   listed with **Send email on your behalf** only, and remove it.

Use synthetic letters and accounts only; no real family data on screen.

## 4. Security posture (for the verification form's questions)

- **Architecture**: control plane (`app.abrolia.com`, Fly.io `ams`) stores only
  account/onboarding metadata and never receives mail content; each household
  has a dedicated runtime (Fly.io `ams`) that receives forwarded mail through
  its relay, stores content in its own SQLite volume, and deletes it after 30
  days.
- **Tokens**: the authorization code is exchanged server-side with PKCE (S256);
  the refresh token is written to the household's Fly secret namespace and kept
  AES-256-GCM encrypted in the runtime database, bound to identity and revision
  (`hermes_cloud/email/google_grant.py`). It never reaches the browser, the
  control-plane database, job records, the model, or logs.
- **Revocation and deletion**: resetting the email step or an operator
  disconnect calls Google's revoke endpoint and wipes token material; a grant
  Google reports revoked is zeroed by the runtime the moment it is observed
  and readiness fails closed; account deletion revokes and removes the secret
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
  (`.github/workflows/dependency-audit.yml`).
- **Known gap**: there is no in-app "Disconnect Gmail" control once setup is
  complete; users revoke in their Google Account, on request, or by deleting
  the account.

## 5. Opening Gmail to everyone

Only after Google's approval email exists:

1. Record it (date, reference number) in this file's status table and in
   `docs/privacy/processors.md` row P5.
2. In `deploy/control-plane/fly.toml` set `ABROLIA_GOOGLE_OAUTH_APP_VERIFIED`,
   `ABROLIA_GOOGLE_GMAIL_SCOPE_APPROVED`, `ABROLIA_GOOGLE_LIMITED_USE_DISCLOSED`
   and `ABROLIA_GMAIL_REAL_ENABLED` to `"1"`. The boot refuses the last one
   without the other three.
3. Update the Privacy Policy sentence about test users, in both notices and
   `landing/privacy.html`.

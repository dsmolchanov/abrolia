# Abrolia landing page

Static, dependency-free landing page for `abrolia.com`.

## Preview locally

```bash
python3 -m http.server 4173 --directory landing
```

Open `http://localhost:4173`.

## Deploy

Production is deployed by `.github/workflows/deploy-production.yml`. A first-party push to
`main` must finish the complete `ci` workflow successfully; the deploy workflow then builds this
directory with the pinned Vercel CLI, publishes the prebuilt output, and verifies both the unique
deployment URL and `https://abrolia.com` before reporting success.

Re-run a failed GitHub deployment job for a transient provider error only while its commit remains
the tip of `main`; otherwise let the newer green run deploy. Do not deploy this folder directly
during the normal release path: a direct Vercel deployment bypasses the green-CI and current-commit
gates.

The page intentionally makes no third-party requests and uses no cookies, analytics, or externally hosted fonts.

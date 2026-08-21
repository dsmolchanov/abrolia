# Abrolia landing page

Static, dependency-free landing page for `abrolia.com`.

## Preview locally

```bash
python3 -m http.server 4173 --directory landing
```

Open `http://localhost:4173`.

## Deploy

The Vercel project is linked from inside this directory. Deploy only this folder:

```bash
vercel deploy --cwd landing --prod
```

The page intentionally makes no third-party requests and uses no cookies, analytics, or externally hosted fonts.

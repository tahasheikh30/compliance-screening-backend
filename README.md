# Account Screening — Backend

FastAPI service that screens applicant names against the UNSC sanctions
list, a manually-uploaded FIA Red Book PDF, and (optionally) an
adverse-media vendor API. Produces a downloadable evidence PDF for any hit.

## Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in API_KEY at minimum
export $(cat .env | xargs)   # or use python-dotenv / your own process manager
uvicorn app.main:app --reload --port 8000
```

Populate the UNSC cache once (free, no key needed):

```bash
curl -X POST http://localhost:8000/api/admin/refresh-unsc -H "X-API-Key: $API_KEY"
```

Upload the FIA Red Book PDF manually — download the latest edition from
fia.gov.pk yourself, glance over it, then upload it:

```bash
curl -X POST http://localhost:8000/api/admin/fia-redbook/upload \
  -H "X-API-Key: $API_KEY" \
  -F "file=@/path/to/redbook.pdf"
```

Re-run that upload whenever a new edition is published (roughly annual).

## Security

This handles applicant PII (names, CNICs) and produces evidence used in
adverse-action decisions, so it's locked down by default rather than
open-by-default:

- **API key required on every endpoint except `/api/health`.** Set `API_KEY`
  in the environment; requests must send it as `X-API-Key: <key>`. If
  `API_KEY` isn't set, the app still starts (so local dev doesn't need
  ceremony) but every protected endpoint returns `503` until it's
  configured — **fails closed, not open.** Generate one with:
  ```bash
  python3 -c "import secrets; print(secrets.token_urlsafe(32))"
  ```
  This is a shared-secret gate, appropriate for a small internal pilot. For
  anything beyond that, replace it with real per-user auth (your org's
  SSO/Azure AD) so actions are attributable to a specific compliance
  analyst, not just "someone with the key."
- **Rate limiting** — `/api/screen` is capped at 10/minute per IP (it's the
  expensive path if you're paying per-search for adverse media), admin
  endpoints at 5–10/hour. Tune the `@limiter.limit(...)` values in
  `app/main.py` to your actual usage pattern.
- **Upload validation** — the Red Book upload checks actual file bytes
  (`%PDF-` magic header), not just the filename or declared content-type
  (both are trivially spoofable), and caps size at 20MB.
- **No silent false-clears** — if the UNSC or FIA cache isn't populated (or
  an adverse-media check errors out), that source reports `NOT_CONFIGURED`,
  not `CLEAR`, and the applicant is routed to manual review rather than
  auto-cleared. A check that didn't run must never look identical to a
  check that came back clean.
- **Security headers + no caching of PII** — responses set
  `X-Content-Type-Options`, `X-Frame-Options: DENY`, and `Cache-Control:
  no-store` on all `/api/*` responses.
- **Generic error responses** — unhandled exceptions return a plain 500 to
  the client; the real error (stack trace, file paths, library internals)
  goes to server-side logs only.
- **CORS locked to explicit origins** — no wildcard; set `ALLOWED_ORIGINS`
  to your actual frontend URL(s).
- **All DB queries are parameterized** (stdlib `sqlite3` with `?`
  placeholders) — no string-built SQL anywhere, so no SQL injection surface.
- **Secrets never touch the repo** — everything sensitive is read from
  environment variables (`API_KEY`, `ADVERSE_MEDIA_API_KEY`,
  `ANTHROPIC_API_KEY`); `.env` is gitignored, `.env.example` documents the
  shape without real values.

### Before you consider this production-ready

- PII sits in a plain SQLite file (or your Postgres, if you migrate) with no
  encryption at rest. If Render's disk-level encryption isn't sufficient for
  your compliance requirements, add application-level encryption for the
  `cnic` column and evidence PDFs, and define a data-retention/deletion
  policy with compliance.
- The shared API key means every request looks the same in the logs — there's
  no per-analyst audit trail of who ran which screening. Fine for a pilot
  with a small trusted team; not fine once more than a couple of people use
  this or it needs to survive a regulator asking "who screened this
  applicant."
- No automated tests are wired into CI here — I ran manual smoke tests
  (auth enforcement, rate limiting, upload validation, the NOT_CONFIGURED
  status fix) during development, but there's no regression suite. Worth
  adding before this becomes load-bearing.

## Do you need an API key?

- **UNSC** — no, free public feed.
- **FIA Red Book** — no, you're uploading it yourself.
- **Adverse media** — the check tries three modes, in this order:
  1. **Vendor API** (`ADVERSE_MEDIA_API_KEY` set) — a dedicated AML vendor
     with a curated compliance database. Most reliable, but a paid contract.
  2. **Claude web search** (`ANTHROPIC_API_KEY` set) — uses Claude's
     web search tool to search for adverse media and returns a structured
     verdict with real cited sources (screenshotted as evidence). Roughly
     $10 per 1,000 searches plus token costs (check
     [docs.claude.com](https://docs.claude.com) for current pricing) —
     cheaper than most vendors, and a legitimate use of a search API rather
     than scraping search-result pages. It's live web search though, not a
     curated PEP/crime database, so treat it as a solid pilot-stage option
     rather than a permanent replacement for a compliance-grade vendor.
     Get a key at [console.anthropic.com](https://console.anthropic.com) and
     `pip install anthropic`.
  3. **Raw search + screenshot** (no keys set) — last-resort fallback,
     screenshots a search-results page directly. Noisiest option, always
     routes to manual review.

  Gemini's grounding tool is a similar option (~$14/1,000 requests, 5,000
  free/month) if you'd rather use that — the code currently wires up Claude,
  but the same pattern (call the model with its search tool, parse a
  structured JSON verdict, screenshot the cited sources) applies to Gemini
  too if you'd prefer it.

## Deploying to Render (Starter + persistent disk)

`render.yaml` is set up for Render's **Starter plan (~$7/month) plus a 1GB
persistent disk (~$0.25/month)** — always-on (no cold starts), and the
SQLite database, cached watchlists, and evidence PDFs all survive
spin-downs and redeploys, since they live on the attached disk rather than
the container's ephemeral local storage.

### Step-by-step

1. Push this `backend/` folder as its own Git repo (GitHub/GitLab/Bitbucket).
2. In the Render dashboard: **New +** → **Blueprint** → connect that repo.
   Render reads `render.yaml` directly — plan, disk, build/start commands
   are already set.
   (Alternatively: **New +** → **Web Service**, and fill in the fields
   manually — instance type **Starter**, add a 1GB disk mounted at
   `/opt/render/project/src/data`, build command
   `pip install -r requirements.txt`, start command
   `uvicorn app.main:app --host 0.0.0.0 --port $PORT`, and set the
   `STORAGE_DIR` env var to that same mount path — see the warning below
   about why this last step isn't optional.)
3. Under **Environment**, confirm/add:
   - `API_KEY` — `render.yaml` auto-generates one; copy it from the
     dashboard once deployed (or set your own, see Security section above)
   - `STORAGE_DIR` — must exactly match the disk's mount path
     (`/opt/render/project/src/data` if you used the Blueprint/`render.yaml`
     route). **Attaching a disk alone does nothing** — this env var is what
     actually tells the app to write there instead of to the container's
     ephemeral local storage. Get this wrong (or forget it) and the app
     will run fine, evidence downloads will work fine, everything will look
     fine — right up until the next spin-down silently wipes it all, with
     no error to warn you.
   - `ALLOWED_ORIGINS` — your Vercel frontend URL, e.g.
     `https://your-app.vercel.app` (update and redeploy once you know it)
   - `ANTHROPIC_API_KEY` or `ADVERSE_MEDIA_API_KEY` if using either for
     adverse media
4. Deploy. Note the `.onrender.com` URL — you'll need it for the
   frontend's `VITE_API_BASE_URL`.
5. Once live, initialize the caches (replace both placeholders):
   ```bash
   curl -X POST https://your-backend.onrender.com/api/admin/refresh-unsc \
     -H "X-API-Key: your-api-key"
   curl -X POST https://your-backend.onrender.com/api/admin/fia-redbook/upload \
     -H "X-API-Key: your-api-key" \
     -F "file=@/path/to/redbook.pdf"
   ```
   Because the disk persists, you only need to do this once — not after
   every redeploy.

### Deploying on the free tier instead

If you'd rather not pay the ~$7.25/month, you can run this on Render's free
tier — set `plan: free` in `render.yaml` and delete the `disk:` block
entirely (free tier doesn't support disks). The trade-off: the service
sleeps after 15 minutes idle (~30–60s cold start on the next request), and
— the one that actually matters — **local files, including the SQLite
database and every evidence PDF, are wiped on every spin-down, not just on
redeploys.** Fine for demoing to compliance; not fine for evidence you need
to still be there next week. See the "Do you need an API key?" section
above and the earlier conversation in this project for the full trade-off
writeup, including the free Neon+R2 alternative if you want to stay at $0
without losing persistence (not yet built into this code — ask if you want
it).

## Playwright (only needed for the adverse-media fallback)

If you're not using a vendor API or `ANTHROPIC_API_KEY`, and want the
raw search+screenshot fallback to work on Render, add this to your build
command:

```
pip install -r requirements.txt && playwright install --with-deps chromium
```

This adds real weight to the build and Render's free tier (512MB RAM) may
struggle to run a real Chromium instance reliably — another reason to
prefer `ANTHROPIC_API_KEY` or a vendor for adverse media once you're past
pure local testing.

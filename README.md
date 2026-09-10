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

## Deploying to Render

1. Push this folder as its own repo, connect it to Render as a new Web
   Service (Python runtime).
2. Render will pick up `render.yaml` automatically if you use "Blueprint"
   deploy, or set manually:
   - Build command: `pip install -r requirements.txt`
   - Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
3. Set environment variables in the Render dashboard:
   - `ALLOWED_ORIGINS` = your Vercel frontend URL(s), comma-separated
   - `ADVERSE_MEDIA_API_KEY` / `ADVERSE_MEDIA_ENDPOINT` if using a vendor

### Important: persistent storage

Render's default filesystem is **ephemeral** — it resets on every deploy
and restart. That means your SQLite database, cached watchlists, and
**evidence PDFs will be lost** unless you attach a persistent disk.

`render.yaml` already requests a 1GB persistent disk mounted at
`/opt/render/project/src/data`. All storage paths in this app read from
`app/config.py`, which respects a `STORAGE_DIR` env var — Render sets this
automatically when you use the disk config in `render.yaml`. If you're
setting things up manually in the dashboard instead, add a disk and set
`STORAGE_DIR` to its mount path yourself.

For anything beyond a pilot, consider moving to Render's managed Postgres
(free tier available) for the database and S3-compatible object storage
(Cloudflare R2, AWS S3) for evidence PDFs — more durable than a single disk,
and works across multiple instances if you ever scale up.

## Playwright (only needed for the adverse-media fallback)

If you're not using a vendor API and want the search+screenshot fallback to
work on Render, add this to your build command:

```
pip install -r requirements.txt && playwright install --with-deps chromium
```

This adds real weight to the build and Render's free tier may struggle with
it — another reason to prefer a vendor API for adverse media once you're
past the pilot stage.

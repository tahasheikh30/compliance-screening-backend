# Account Screening — Backend

FastAPI service that screens applicant names against the UNSC sanctions
list, a manually-uploaded FIA Red Book PDF, and (optionally) an
adverse-media vendor API. Produces a downloadable evidence PDF for any hit.

## Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Populate the UNSC cache once (free, no key needed):

```bash
curl -X POST http://localhost:8000/api/admin/refresh-unsc
```

Upload the FIA Red Book PDF manually — download the latest edition from
fia.gov.pk yourself, glance over it, then upload it:

```bash
curl -X POST http://localhost:8000/api/admin/fia-redbook/upload \
  -F "file=@/path/to/redbook.pdf"
```

Re-run that upload whenever a new edition is published (roughly annual).

## Do you need an API key?

- **UNSC** — no, free public feed.
- **FIA Red Book** — no, you're uploading it yourself.
- **Adverse media** — optional but recommended for production. Without a
  key, it falls back to a raw search + screenshot, which is noisier and
  always routes to manual review, and scraping search engines directly is
  generally against their ToS — fine for a pilot, not something to rely on
  long-term. Get a quote from a vendor (Sanction Scanner, WorldAML,
  ComplyAdvantage) and set:

  ```
  ADVERSE_MEDIA_API_KEY=your-key
  ADVERSE_MEDIA_ENDPOINT=https://api.yourvendor.com/v1/adverse-media
  ```

  then adapt `check_via_vendor()` in `app/screening/adverse_media.py` to
  their actual response schema.

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

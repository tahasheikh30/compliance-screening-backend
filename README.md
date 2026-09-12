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

Run the test suite (matching accuracy is the part most worth continuously
checking as you tune thresholds or extend the transliteration list):

```bash
pytest tests/ -v
```

## Matching accuracy

Name screening is fuzzy by nature — this section explains how matching
works now, what it fixes over a naive single-ratio approach, and what it
still doesn't guarantee. All of this lives in `app/screening/matching.py`.

**Normalization before comparison.** Both the applicant name and every
watchlist entry are lowercased, stripped of diacritics/punctuation, and
had honorifics removed (Syed, Haji, Sheikh, Dr., etc.) before scoring —
otherwise formatting noise alone can push a real match below threshold.
A conservative, hand-maintained list also maps common transliteration
variants to one canonical form (Mohammad/Mohammed/Muhammed → muhammad,
Yousaf/Yusuf/Yousif → yousuf, and similar) — this is not a general
transliteration engine, and it's meant to be extended over time as real
mismatches surface, not treated as complete.

**Multiple algorithms, combined deliberately.** Rather than a single
`fuzz.token_sort_ratio()` call, each comparison runs `token_sort_ratio`,
`token_set_ratio`, `WRatio`, and (guarded — see below) `partial_ratio` and
a phonetic score, and takes the maximum. Each algorithm has a different
blind spot: `token_sort_ratio` misses cases where the applicant supplied
fewer name parts than the watchlist entry; `token_set_ratio` handles that
but can over-match on very short tokens in isolation; `partial_ratio`
catches truncated/abbreviated entries but will treat any short name as
"matched" if it happens to appear as a substring of a longer unrelated
name (e.g. "Ali" inside "Alistair") — so it's only trusted once the
shorter side of the comparison has at least two tokens or is reasonably
long on its own. This combination is covered by `tests/test_matching.py`,
including a regression test for that exact substring false-positive.

**Phonetic matching, as a supplement to the variant table, not a
replacement for it.** The hand-curated `VARIANT_MAP` only catches spelling
pairs someone has already written down — real applicant traffic will
surface transliteration pairs nobody thought to add. Each comparison now
also computes a Metaphone-coded token-overlap score (`jellyfish.metaphone`
per token), which catches phonetically-identical names that are literally
quite different strings (e.g. "Zulfiqar" vs "Zulfikar" — not in the
variant table, but phonetically the same name). It uses exact phonetic-
code equality per token, not substring containment, and the same
short-token guard as `partial_ratio`, for the same reason: a phonetic
match is a stronger claim than "one string contains another," but a very
short token can still coincidentally share a phonetic code with an
unrelated name. This is a coarser, noisier signal than the variant table
by nature — treat a HIT/REVIEW that only cleared threshold via the
phonetic score with the same "route to a human, don't auto-decide"
posture as everything else here, not extra confidence.

Taking the **maximum** across algorithms, rather than an average, is a
deliberate choice: for a compliance screen, a missed true match is the
worse failure mode, and every result at REVIEW or above still routes to a
human analyst, so a slightly noisier top score is an acceptable trade for
not averaging a real match down below threshold.

**CNIC is a separate, stronger signal.** If the applicant supplied a CNIC
and it exactly matches an entry's CNIC (currently only the FIA Red Book
source captures CNICs, where the PDF edition includes them), that result
is escalated to `HIT` *regardless of the name fuzzy score* — a shared
13-digit national ID number is direct identity evidence, not a fuzzy
inference, and the API surfaces this distinctly via a `cnic_match` field
on every result plus a loud banner on the evidence PDF.

**Near misses are logged, not just cleared.** Any score that lands within
`NEAR_MISS_MARGIN` points (default 10) below `REVIEW_THRESHOLD` without
crossing it is written to an append-only audit table
(`GET /api/admin/near-misses`). This doesn't change the applicant's
status — it exists so a compliance analyst can periodically check where
real traffic is actually landing relative to the thresholds, instead of
setting `MATCH_THRESHOLD`/`REVIEW_THRESHOLD` once and never revisiting them.
A CNIC that differs from a Red Book entry by exactly one digit (a
plausible OCR/typo slip) is logged to the same table for the same reason —
it is never auto-escalated to a HIT (see `matching.cnic_near_match`), only
flagged for a human to glance at.

**What this does NOT solve, and shouldn't be sold as solving:**
- Fuzzy name matching is inherently probabilistic — on this codebase or
  any other vendor's. This is a meaningfully stronger implementation than
  a single fuzzy ratio, not a guarantee of zero false negatives or false
  positives, and not a benchmarked claim of being more accurate than any
  specific commercial product (no such benchmark has been run).
- The transliteration variant list is hand-curated and will miss names it
  hasn't seen. Treat it as a living document — extend it from real
  mismatches, don't assume it's exhaustive. The added phonetic (Metaphone)
  layer covers some of that gap automatically, but Metaphone was designed
  for English phonetics and is an approximation for Arabic/Urdu-derived
  names, not a purpose-built solution for them — it catches some real
  variants the variant table misses, and will also miss some, and very
  occasionally over-match short names that happen to share a phonetic
  code. It's a net improvement in coverage, not a solved problem.
- FIA Red Book extraction (`app/screening/fia_redbook.py`) is still
  best-effort PDF parsing — this version tries real table structure first
  and falls back to a text-scan heuristic, and now also attempts to pull
  a CNIC column when present, but Red Book table layouts change across
  editions. A compliance analyst spot-checking a sample of parsed
  names/CNICs against the source PDF after every new edition is a
  required step, not an optional nicety — `get_status()` /
  `ingest_uploaded_pdf()` responses say this explicitly.
- `MATCH_THRESHOLD` / `REVIEW_THRESHOLD` (env-configurable, defaults
  85/60) are reasonable starting points, not values validated against
  your actual applicant population. Tune them against labeled data and
  the near-miss log, with compliance sign-off, before treating them as
  final.
- Every HIT/REVIEW result routes to a human compliance analyst by design
  (see "No silent false-clears" below). That routing is load-bearing, not
  a formality — this tool is a screening aid, not an automated
  adjudicator.

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
- **CNIC overrides a weak name score, never the reverse** — an exact CNIC
  match always escalates to `HIT`, but a *lack* of a CNIC match never
  downgrades a name-based HIT/REVIEW. CNIC is additive evidence only.
- **Near-miss audit trail** — see "Matching accuracy" above.
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
- Automated tests now cover the matching engine specifically
  (`tests/test_matching.py` — normalization, multi-algorithm scoring, the
  short-name/substring false-positive guard, CNIC matching, near-miss
  flagging), but there's still no CI wiring and no coverage of the API
  layer, upload validation, or rate limiting beyond the manual smoke
  testing done during development. Worth adding before this becomes
  load-bearing, and worth extending `test_matching.py` with real
  borderline cases pulled from production near-miss logs once you have
  them.
- The `MATCH_THRESHOLD` / `REVIEW_THRESHOLD` defaults (85/60) and the
  transliteration variant list in `app/screening/matching.py` are
  reasonable starting points, not compliance-validated final values — see
  "Matching accuracy" above.

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
   - `MATCH_THRESHOLD` / `REVIEW_THRESHOLD` / `NEAR_MISS_MARGIN` — optional,
     override the defaults (85/60/10) documented in the "Matching accuracy"
     section above once you've validated your own values.
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

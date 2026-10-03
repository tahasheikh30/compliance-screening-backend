# Applicant Screening Backend

FastAPI backend that screens an applicant's name against international and Pakistani watch lists and an open news search, and produces an evidence PDF when something is found.

The screening logic is a Python port of the n8n **Applicant Screening Engine** and **Applicant Sanctions Screening** workflows. It needs **no API keys and no third party services**: every list is downloaded live from its public publisher.

## What it screens

| Source key | List | Downloaded from |
|---|---|---|
| `UNSC` | UN Security Council Consolidated List | UN legacy XML |
| `OFAC` | OFAC SDN and Consolidated (Non-SDN), with aliases | US Treasury CSV exports |
| `UKSL` | UK Sanctions List (FCDO) | UK XML |
| `FIA_REDBOOK` | FIA Red Books (Pakistan) | PDF editions discovered on fia.gov.pk |
| `NACTA` | NACTA Proscribed Persons, Fourth Schedule (Pakistan) | A CSV or JSON export you upload (see below) |
| `ADVERSE_MEDIA` | Open news search | Google News RSS |

Lists are downloaded in parallel on a screening and kept in memory for `LIST_CACHE_TTL_SECONDS` (default 1 hour; `0` downloads fresh every time, like the workflow). Nothing about the lists is stored on disk.

## NACTA Proscribed Persons

NACTA publishes the Fourth Schedule list (about 5,300 people) at `nfs.nacta.gov.pk`, which has Excel,
JSON and XML export buttons. The site is a **Blazor Server** app: the page talks to the server over a
private SignalR connection and the export buttons build the file inside that session, so **there is no
web address that returns the list**. The list therefore reaches the screening as a file. Three ways:

1. **By hand.** Click **JSON** on the NACTA site, then upload the file on the Lists page.
2. **Scheduled, with a browser (recommended).** `scripts/fetch_nacta.py` opens the site in a headless
   browser, clicks the JSON button, checks the file (at least 1,000 people, within 10 percent of the count
   the page shows, readable by the same parser the backend uses), and uploads it. A refused or partial
   download never replaces a good list. `.github/workflows/refresh-nacta.yml` runs it twice a week on GitHub
   Actions with two repository secrets, `SCREENING_API_URL` and `SCREENING_API_KEY`. If NACTA does not answer
   GitHub's servers (government sites sometimes only answer inside Pakistan), run the same script on a
   computer in Pakistan on a schedule instead.

   ```bash
   pip install -r scripts/requirements-nacta.txt
   python -m playwright install chromium
   python scripts/fetch_nacta.py --show              # watch it work, saves the file
   python scripts/fetch_nacta.py --upload            # also uploads (needs SCREENING_API_URL and SCREENING_API_KEY)
   ```
   The browser is not part of the web service, so the backend stays small.
3. **A plain address.** If the list is ever served as a file at some address (your own copy, a proxy),
   set `NACTA_PERSONS_URL` and it is downloaded whenever the lists load, saved as the **last good copy**,
   and that copy is used (with a note on the Lists page) if the address later fails, as long as it is no older
   than `NACTA_MAX_AGE_DAYS`. NACTA's own site cannot be used this way.

To upload yourself:

```bash
curl -X POST "https://your-backend/api/admin/nacta?filename=nacta.json" \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" --data-binary @nacta.json
```

The Lists page in the frontend does the same with a file picker. The body is the raw file, not a
multipart form. CSV (comma, semicolon or tab separated), JSON (an array of objects, or an object
holding one) and XML are accepted. XML files with entity declarations are refused. Columns are recognised by name, so `Primary Title / Name`, `Father Name`,
`CNIC / ID Number`, `District` and `Province` all work. A file that cannot be read is rejected and
the previous list stays in use.

- **Freshness.** The list changes every few weeks. A copy older than `NACTA_MAX_AGE_DAYS` (30) is
  reported as out of date and sends the applicant to manual review, so a stale list never looks clean.
- **No upload yet** is reported as not screened. Set `NACTA_REQUIRED=false` to let that through.
- **Not included:** NACTA's list of Proscribed Organizations. It is a PDF whose address changes with
  each update, so it is not read.

NACTA records have name, father's name, CNIC, district and province, and **no date of birth**.

## Seeing which list has a problem

`GET /api/admin/lists` (shown on the Lists page) reports every list behind each source on its own: its
record count, whether it could be read and why not, and the address it came from. The FIA Red Books are
listed one by one, so it is clear which book failed. For a PDF that downloaded but gave no people (a
layout the reader does not understand), it also returns a sample of the text read from the PDF, so the
layout can be diagnosed from the screen without server access. The server log carries the same detail,
one line per Red Book.

## CNIC and father's name

The FIA Red Book and NACTA both publish a CNIC and a father's or husband's name, so the form can take
both (optional):

- A **CNIC equal to a listed CNIC is reported as a match whatever the name looks like**, and ranks first.
  The CNIC must be 13 digits; anything else is ignored. Placeholder numbers such as `1111111111166` in
  the published data are never used.
- A **matching father's name** is shown as supporting evidence. A different father never removes a match.
- Without a CNIC, matching is by name as before. Common names produce many false matches on a list this
  size, so enter the CNIC whenever you have it.

## How matching works

Identical to the workflow:

1. Names are upper-cased, stripped of accents and punctuation, and stripped of titles and particles (Dr, Haji, Al, Bin, ...). Common surnames such as Sheikh and Syed are kept.
2. Every applicant token is compared with every candidate token using Jaro-Winkler. A token pair below **0.88** counts as no match.
3. The score is symmetric: unmatched tokens on either side lower it. A score at or above the **threshold** (default 85, per request 50 to 100) is a potential match.
4. Every primary name and alias is scored; the best one is reported.
5. The applicant's birth year is compared with the listed record and reported as supporting evidence. **Date of birth and nationality never filter matches** (the one exception is a CNIC match, above, which adds a match). A match is not a confirmed identity: a person must verify it.
6. News: an article counts when its title or summary contains the applicant's surname plus at least one more name part, and an adverse keyword (arrested, fraud, laundering, terror, ...). News hits are unverified leads.

## API

All endpoints except `/api/health` need an `X-API-Key` header.

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/screen` | Screen one applicant (10/min) |
| GET | `/api/applicants` | Past screenings |
| GET | `/api/applicants/{id}` | One screening with all results |
| GET | `/api/applicants/{id}/evidence` | Evidence PDF of that screening |
| GET | `/api/evidence/{result_id}` | Same PDF, by result row |
| GET | `/api/admin/lists` | What is cached in memory |
| POST | `/api/admin/refresh` | Clear the cache and reload every list (5/hour) |
| GET | `/api/admin/nacta` | Which NACTA file is loaded and how old it is |
| POST | `/api/admin/nacta` | Upload the NACTA CSV or JSON export (10/hour) |
| GET | `/api/health` | Liveness |

### `POST /api/screen`

```json
{ "full_name": "Muhammad Ali Khan", "dob": "1975-03-04", "nationality": "Pakistan", "threshold": 85 }
```

Only `full_name` is required. `cnic` and `father_name` are optional and are used for the FIA Red Book and NACTA (see above).

The response has one result row per source (`UNSC`, `OFAC`, `UKSL`, `FIA_REDBOOK`, `ADVERSE_MEDIA`), each with `status`, `score`, `matched_entry`, `detail`, `list_version`, `records_screened`, and the full `matches` or `articles`. It also carries `case_ref`, `threshold`, `records_screened`, `sanctions_hit_count`, `media_hit_count`.

| Row `status` | Meaning |
|---|---|
| `HIT` | One or more watch-list matches at or above the threshold |
| `REVIEW` | Adverse news found (unverified lead) |
| `CLEAR` | Screened, nothing found |
| `ERROR` | The source could not be downloaded or read |
| `NOT_CONFIGURED` | The FIA Red Book could not be loaded, or no NACTA list has been uploaded |
| `PARTIAL` | Nothing found, but part of the source was not fully screened (an unreadable Red Book, or an out of date NACTA list) |

| `overall_status` | When |
|---|---|
| `ESCALATE_TO_COMPLIANCE` | Any `HIT` |
| `MANUAL_REVIEW` | Any `REVIEW`, `ERROR` or `NOT_CONFIGURED` |
| `AUTO_CLEAR` | Every source screened and nothing found |

**A source that did not run never counts as clear.** One list failing does not stop the others from being screened.

### Evidence PDF

One PDF per screening, generated when there is any hit (watch list or news). It has the same sections as the workflow's report: result banner, summary cards, applicant, screening details, per-list status and result, method note, one card per match, adverse media, and a reviewer decision block. It is attached to every `HIT` and `REVIEW` row of that screening.

## Configuration

See `.env.example`. Required: `API_KEY`. Common: `ALLOWED_ORIGINS`, `STORAGE_DIR` (persistent disk), `MATCH_THRESHOLD`, `LIST_CACHE_TTL_SECONDS`, `FIA_REQUIRED`, `NACTA_REQUIRED`, `NACTA_MAX_AGE_DAYS`, `NACTA_PERSONS_URL`. The uploaded NACTA file lives under `STORAGE_DIR/lists`, so it needs the persistent disk.

## Run and test

```bash
pip install -r requirements.txt
API_KEY=dev uvicorn app.main:app --reload --port 8000
python -m pytest
```

Tests use synthetic copies of every feed and never touch the network.

## Differences from the n8n workflow

- **Unavailable sources.** The workflow shows the FIA Red Book and news as "unavailable" but still gives a clearance. Here an unavailable source routes to `MANUAL_REVIEW`. Set `FIA_REQUIRED=false` to let an unreachable FIA Red Book through, as the workflow does (news and the sanctions lists always block).
- **Evidence in the API.** The workflow returned the PDF through its form. Here it is a download endpoint, and each screening is saved in SQLite.
- **Speed.** Sources download in parallel and can be cached in memory; matching 60,000+ names takes under a second.
- **Jaro-Winkler.** The workflow's exact algorithm is used (rapidfuzz only pre-filters pairs that cannot reach the cutoff).
- **News wording.** The workflow's PDF said articles must contain every part of the name; the code actually requires the surname plus one more part. The report now describes what the code does.

## Known limits

- Matching is name based and will produce false positives for common names. Every hit needs a human decision.
- With the 0.88 token cutoff, some spelling variants are not matched (for example MUHAMMAD vs MOHAMMED scores 0.85). MUHAMMAD vs MOHAMMAD and MUHAMMED do match. This is inherited from the workflow.
- The FIA Red Book is read by text extraction from PDFs found on fia.gov.pk, using the workflow's patterns. If FIA changes the page or PDF layout, that source reports as not screened rather than clear.
- Lists are public downloads, so a publisher outage or a block on the server's IP shows up as `ERROR` for that source.

## Deploying

`render.yaml` is included. Attach the persistent disk and keep `STORAGE_DIR` equal to its mount path, otherwise screening history and evidence PDFs are lost on redeploy.

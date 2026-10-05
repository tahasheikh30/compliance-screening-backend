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

Lists are downloaded in parallel and kept in memory for `LIST_CACHE_TTL_SECONDS` (default 1 hour; `0` downloads fresh every time, like the workflow). They are loaded when the server starts and reloaded in the background once they are three quarters of the way to expiring, so a screening normally never waits for a download (`PRELOAD_LISTS=false` turns this off). If a background reload fails the previous copy is kept until it expires; after that the source is reported as unavailable, never as clear. Nothing about the lists is stored on disk.

## Users, history and storage (Supabase)

Everything the service remembers lives in a Supabase Postgres database, so the web server itself keeps nothing on disk and can be restarted, redeployed or put to sleep (Render's free plan does this) without losing anything:

- **Users.** People sign up and sign in with email and password through Supabase Auth. A new account is **pending** until an admin approves it; a pending user can sign in but cannot screen anyone or see any data.
- **History.** Every screening is saved against the person who ran it. A user sees **only their own** screenings and evidence PDFs (someone else's looks exactly like one that does not exist, a 404). **Admins see everyone's**, each with who ran it.
- **Evidence PDFs** are stored in the database with the screening.
- **The NACTA list** is stored in the database. Each upload replaces the previous list in one statement.

The tables are in `app/schema.sql` (the backend applies it on start, it is safe to run again) and `supabase/setup.sql` (Supabase only: the link to Supabase Auth, the trigger that creates a pending profile at sign up, and revoking the public REST API's access). Row Level Security is on for every table with no policies, so the public anon key reaches nothing; only the backend, which connects with the database owner role, reads and writes.

### Roles

| | pending | approved | admin |
|---|---|---|---|
| `GET /api/me` | yes | yes | yes |
| Screen, own history and evidence | no | yes | yes |
| Everyone's history, user approval, list refresh, NACTA upload | no | no | yes |

Approval, rejection and role changes take effect on the next request. The last admin cannot be demoted or rejected.

### Setting it up

1. **Backend settings** (Render environment, see `.env.example`): `DATABASE_URL` (Supabase dashboard, **Connect**, **Session pooler**; URL-encode special characters in the password), `SUPABASE_URL` (`https://<project>.supabase.co`), and `ALLOWED_ORIGINS`. `SUPABASE_JWT_SECRET` is needed only if the project signs tokens with the legacy shared secret (Project Settings, JWT Keys); projects with asymmetric signing keys do not need it.
2. **Supabase Auth settings.** Keep **Confirm email** on, so an address is verified before it can sign in. Supabase's built in mail sender is heavily rate limited: set up your own SMTP before real users sign up.
3. **The first admin.** Sign up once through the frontend and confirm the email, then run this in the Supabase SQL editor:
   ```sql
   UPDATE public.profiles SET status = 'approved', role = 'admin' WHERE email = 'you@example.com';
   ```
   After that, approve everyone else with `GET /api/admin/users?status=pending` and `POST /api/admin/users/{id}/status` with `{"status": "approved"}`.
4. **The frontend** signs people up and in with `@supabase/supabase-js` and sends the access token on every call:
   ```js
   const supabase = createClient(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY);   // the publishable (anon) key is meant to be public
   await supabase.auth.signUp({ email, password });
   await supabase.auth.signInWithPassword({ email, password });
   const { data: { session } } = await supabase.auth.getSession();         // refreshes an expired token
   fetch(`${API}/api/me`, { headers: { Authorization: `Bearer ${session.access_token}` } });
   ```
   Call `/api/me` after sign in: `status` is `pending`, `approved` or `rejected` and `role` is `user` or `admin`, which is what the screen should show. Never put the database password or the service role key in the frontend.
5. **`API_KEY`** is now only a machine credential for the scheduled NACTA upload (`X-API-Key`). It cannot read applicant data. `ALLOW_API_KEY_FULL_ACCESS=true` gives an older frontend full admin access again while it is being replaced; it logs a warning on start and should be turned off as soon as sign in works.

Supabase free projects are paused after a week of inactivity, and the free plan has no automatic backups: for real compliance records, export the tables now and then, or move to a paid plan. Applicant names and CNICs are personal data held by a third party cloud service, so check that this is allowed where you work and pick the region on purpose.

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
# or, as a signed in admin: -H "Authorization: Bearer $ACCESS_TOKEN"
```

The Lists page in the frontend does the same with a file picker (admins only). The body is the raw file, not a
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

All endpoints except `/api/health` need `Authorization: Bearer <access token>` (see above). Rate limits are per signed in person.

| Method | Path | Who | Purpose |
|---|---|---|---|
| GET | `/api/me` | any signed in user | Your email, `role` and approval `status` |
| POST | `/api/screen` | approved | Screen one applicant (10/min) |
| GET | `/api/applicants` | approved | Your past screenings (admins: everyone's, `?mine=true` for their own) |
| GET | `/api/applicants/{id}` | approved | One screening with all results |
| GET | `/api/applicants/{id}/evidence` | approved | Evidence PDF of that screening |
| GET | `/api/evidence/{result_id}` | approved | Same PDF, by result row |
| GET | `/api/admin/lists` | approved | What is cached in memory |
| GET | `/api/admin/nacta` | approved | Which NACTA file is loaded and how old it is |
| POST | `/api/admin/refresh` | admin | Clear the cache and reload every list (5/hour) |
| POST | `/api/admin/nacta` | admin, or `X-API-Key` | Upload the NACTA CSV or JSON export (10/hour) |
| GET | `/api/admin/users` | admin | People who signed up (`?status=pending`) |
| POST | `/api/admin/users/{id}/status` | admin | `{"status": "approved" or "rejected"}` |
| POST | `/api/admin/users/{id}/role` | admin | `{"role": "admin" or "user"}` |
| GET | `/api/health` | anyone | Liveness (`?deep=true` also checks the database) |

Sign in problems come back with a stable `error.code`: `AUTH_REQUIRED`, `AUTH_INVALID_TOKEN`, `AUTH_TOKEN_EXPIRED` (sign in again), `ACCOUNT_PENDING`, `ACCOUNT_REJECTED`, `ADMIN_ONLY`. A database outage is `DATABASE_UNAVAILABLE` (503).

### `POST /api/screen`

```json
{ "full_name": "Muhammad Ali Khan", "dob": "1975-03-04", "nationality": "Pakistan", "threshold": 85 }
```

Only `full_name` is required. `cnic` and `father_name` are optional and are used for the FIA Red Book and NACTA (see above).

The response has one result row per source (`UNSC`, `OFAC`, `UKSL`, `FIA_REDBOOK`, `NACTA`, `ADVERSE_MEDIA`), each with `status`, `score`, `matched_entry`, `detail`, `list_version`, `records_screened`, and the full `matches` or `articles`. It also carries `case_ref`, `threshold`, `records_screened`, `sanctions_hit_count`, `media_hit_count`.

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

See `.env.example`. Required: `DATABASE_URL`, `SUPABASE_URL`. Common: `ALLOWED_ORIGINS`, `API_KEY` (for the scheduled NACTA upload), `SUPABASE_JWT_SECRET` (legacy token signing only), `DB_POOL_MAX`, `MATCH_THRESHOLD`, `LIST_CACHE_TTL_SECONDS`, `PRELOAD_LISTS`, `FIA_REQUIRED`, `NACTA_REQUIRED`, `NACTA_MAX_AGE_DAYS`, `NACTA_PERSONS_URL`. Without `DATABASE_URL` every request is refused with a 503 that says so; without `SUPABASE_URL` (or the legacy secret) no sign in can be verified, and requests are refused rather than let through.

## Run and test

```bash
pip install -r requirements-dev.txt   # requirements.txt alone is what production installs
DATABASE_URL=... SUPABASE_URL=https://<project>.supabase.co uvicorn app.main:app --reload --port 8000
```

The tests need a throwaway PostgreSQL database, because that is what the app uses. They refuse to run against anything that is not clearly a test database (the name must contain `test`, and Supabase is never allowed), since every test empties the tables:

```bash
docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=screening_test postgres:16
python -m pytest        # TEST_DATABASE_URL overrides postgresql://postgres:postgres@localhost:5432/screening_test
```

Tests use synthetic copies of every feed and never touch the network. Sign in is tested with real signed tokens (shared secret and public key), including expired, forged and unsigned ones. GitHub Actions runs the suite on every push (`.github/workflows/tests.yml`).

## Differences from the n8n workflow

- **Unavailable sources.** The workflow shows the FIA Red Book and news as "unavailable" but still gives a clearance. Here an unavailable source routes to `MANUAL_REVIEW`. Set `FIA_REQUIRED=false` to let an unreachable FIA Red Book through, as the workflow does (news and the sanctions lists always block).
- **Evidence in the API.** The workflow returned the PDF through its form. Here it is a download endpoint, and each screening is saved in the database with the person who ran it.
- **Speed.** Sources download in parallel, are cached in memory and refreshed in the background; matching 60,000+ names takes under a second. Result rows and the evidence PDF are saved in one transaction, through a pool of database connections.
- **Jaro-Winkler.** The workflow's exact algorithm is used (rapidfuzz only pre-filters pairs that cannot reach the cutoff).
- **News wording.** The workflow's PDF said articles must contain every part of the name; the code actually requires the surname plus one more part. The report now describes what the code does.

## Known limits

- Matching is name based and will produce false positives for common names. Every hit needs a human decision.
- With the 0.88 token cutoff, some spelling variants are not matched (for example MUHAMMAD vs MOHAMMED scores 0.85). MUHAMMAD vs MOHAMMAD and MUHAMMED do match. This is inherited from the workflow.
- The FIA Red Book is read by text extraction from PDFs found on fia.gov.pk, using the workflow's patterns. If FIA changes the page or PDF layout, that source reports as not screened rather than clear.
- Lists are public downloads, so a publisher outage or a block on the server's IP shows up as `ERROR` for that source.

## Deploying

`render.yaml` is included. It needs no disk: all state is in Supabase. Set `DATABASE_URL` (and `SUPABASE_JWT_SECRET` if your project uses the legacy secret) in the Render dashboard, since those are secrets. Pick the Render region closest to the Supabase project, because every request talks to the database. The free plan sleeps when idle, so the first request after a sleep is slow while the lists load again.

Behind Render's proxy, `render.yaml` starts uvicorn with `--proxy-headers`, so the real client address is available. Rate limits count per signed in person.

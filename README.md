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
4. **The frontend** signs people up and in with `@supabase/supabase-js` and sends **two things on every call**: the app's key and the person's access token:
   ```js
   const supabase = createClient(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY);   // the publishable (anon) key is meant to be public
   await supabase.auth.signUp({ email, password });
   await supabase.auth.signInWithPassword({ email, password });
   const { data: { session } } = await supabase.auth.getSession();         // refreshes an expired token
   fetch(`${API}/api/me`, { headers: {
     "X-API-Key": APP_API_KEY,                                              // which app is calling
     Authorization: `Bearer ${session.access_token}`,                       // who is using it
   } });
   ```
   Call `/api/me` after sign in: `status` is `pending`, `approved` or `rejected` and `role` is `user` or `admin`, which is what the screen should show. Never put the database password, the service role key or `API_KEY` in the frontend.
5. **Two different keys.**
   - **`APP_API_KEY`** identifies the frontend. Set it on the backend, and the same value as `VITE_API_KEY` on the frontend. Every route except `/api/health` refuses a request without it (`AUTH_MISSING_KEY`, `AUTH_INVALID_KEY`), before it even looks at the sign in. It is built into the frontend, so anyone who opens the app can read it: it is an app identifier, not a secret, and on its own it opens nothing (a request with only the app key gets `AUTH_REQUIRED`).
   - **`API_KEY`** is a secret machine credential for the scheduled NACTA upload (`X-API-Key`, no sign in). It works on that one route only, cannot read applicant data, and must **never** be given to the frontend or set to the same value as `APP_API_KEY` (the backend warns at start if you do, because then anyone who opens the app could replace the NACTA list).
   - The old `ALLOW_API_KEY_FULL_ACCESS` switch is gone: a key by itself no longer opens anything.

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

## CNIC, father's name and province

The FIA Red Book and NACTA both publish a CNIC and a father's or husband's name, so the form can take
both (optional):

- A **CNIC equal to a listed CNIC is reported as a match whatever the name looks like**, and ranks first.
  The CNIC must be 13 digits; anything else is ignored. Placeholder numbers such as `1111111111166` in
  the published data are never used.
- A **matching father's name** is shown as supporting evidence. A different father never removes a match.
- A **matching province** is shown the same way (`province`, `province_match` on each match, and in the evidence PDF). Only NACTA publishes one, so a match from another list shows none. Spellings are normalised on both sides (`KPK`, `NWFP` and `Khyber Pakhtunkhwa` are one province; `Baluchistan` is `Balochistan`; `FATA` counts as Khyber Pakhtunkhwa since the 2018 merger). A province is a coarse clue shared by millions of people and people move, so it never adds or removes a match; it helps the reviewer tell two people with the same name apart. A province that is not recognised is compared as written.
- Without a CNIC, matching is by name as before. Common names produce many false matches on a list this
  size, so enter the CNIC whenever you have it.

## How matching works

Identical to the workflow:

1. Names are upper-cased, stripped of accents and punctuation, and stripped of titles and particles (Dr, Haji, Al, Bin, ...). Common surnames such as Sheikh and Syed are kept. Letters that do not decompose (Ł, Ø, Đ, Ð, Þ, Æ, Œ) are folded to Latin rather than dropped.
   **Spelling variants are folded** before scoring, on both sides: MOHAMMED, MOHAMMAD, MUHAMMED, MOHD and MD all become MUHAMMAD; SYED, SAYYID, SAYED become one spelling; CHAUDHRY, CHOWDHURY, CHOUDHARY likewise (about 45 families, listed in `app/screening/names.py`). Fused and split forms meet too (ABDULRAHMAN = ABDUL RAHMAN), but ABDULLAH is never cut. Plain Jaro-Winkler scored MOHAMMED against MUHAMMAD at 0.85, under the 0.88 token cutoff, so that listed person was missed. `NAME_VARIANTS=false` turns this off and gives the original workflow's raw-spelling scores.
2. Every applicant token is compared with every candidate token using Jaro-Winkler. A token pair below **0.88** counts as no match.
3. The score is symmetric: unmatched tokens on either side lower it. A score at or above the **threshold** (default 85, per request 50 to 100) is a potential match.
4. Every primary name and alias is scored; the best one is reported.
5. The applicant's birth year is compared with the listed record and reported as supporting evidence. **Date of birth and nationality never filter matches** (the one exception is a CNIC match, above, which adds a match). A match is not a confirmed identity: a person must verify it.
6. News: an article counts when its title or summary contains the applicant's surname plus at least one more name part, and an adverse keyword (arrested, fraud, laundering, terror, ...). News hits are unverified leads.

### Speed

Each list is indexed by name token when it loads. A screening scores the applicant's tokens against the list's unique tokens, then visits only records that could still reach the threshold (a record that holds one of three names cannot score 85). The result is **identical** to scanning every record (`tests/test_matching_quality.py` compares the two on random lists, with repeated tokens and at thresholds from 50 to 100) but far cheaper. Measured on a synthetic 65,000-name list (not the real lists), against 200 to 305 ms for the full scan:

| Name | First look at its words | Repeated |
|---|---|---|
| MUHAMMAD ALI KHAN (the heaviest case) | about 36 ms | about 15 ms |
| MOHAMMED HUSSAIN SHAH | about 22 ms | about 9 ms |
| ABDUL RAHMAN MALIK | about 14 ms | about 3 ms |
| An uncommon name | about 8 ms | about 0.1 ms |

"First look" is the cost of comparing a word with every distinct word on the list; it is remembered until that list reloads. List downloads happen in the background, so a screening never waits for them.

### Names that cannot be screened

A name typed in Urdu, Arabic, Cyrillic or any non-Latin script would normalise to nothing, score 0 against every record and come back clear. It is refused with a 422 instead (`VALIDATION_ERROR`, "write the name in Latin letters"), also when only part of the name is non-Latin. Hidden control and zero-width characters are removed from names.

## API

All endpoints except `/api/health` need **both** `X-API-Key: <APP_API_KEY>` (which app) and `Authorization: Bearer <access token>` (who). The only exception is the NACTA upload, which also accepts the secret `API_KEY` on its own. Rate limits are per signed in person.

| Method | Path | Who | Purpose |
|---|---|---|---|
| GET | `/api/me` | any signed in user | Your email, `role` and approval `status` |
| POST | `/api/screen` | approved | Screen one applicant (10/min) |
| GET | `/api/applicants` | approved | Your past screenings (admins: everyone's, `?mine=true` for their own). `?limit=` (1-200), `?offset=`, `?status=`; the `X-Total-Count` header holds the total |
| GET | `/api/applicants/{id}` | approved | One screening with all results |
| GET | `/api/applicants/{id}/evidence` | approved | Evidence PDF of that screening |
| GET | `/api/evidence/{result_id}` | approved | Same PDF, by result row |
| GET | `/api/admin/lists` | approved | What is cached in memory (source addresses and PDF samples are shown to admins only) |
| GET | `/api/admin/nacta` | approved | Which NACTA file is loaded and how old it is |
| POST | `/api/admin/refresh` | admin | Clear the cache and reload every list (5/hour) |
| POST | `/api/admin/nacta` | admin, or the secret `API_KEY` | Upload the NACTA CSV or JSON export (10/hour) |
| GET | `/api/admin/users` | admin | People who signed up (`?status=pending`) |
| POST | `/api/admin/users/{id}/status` | admin | `{"status": "approved" or "rejected"}` |
| POST | `/api/admin/users/{id}/role` | admin | `{"role": "admin" or "user"}` |
| GET | `/api/admin/audit` | admin | Who did what, newest first (`?limit=`, `?offset=`, `?action=`, `?actor_id=`) |
| GET | `/api/admin/audit/verify` | admin | Re-computes the audit hash chain (6/hour) |
| | | | See **Continuous monitoring** above for the monitoring endpoints |
| GET | `/api/health` | anyone | Liveness (`?deep=true` also checks the database) |

Problems come back with a stable `error.code`: `AUTH_MISSING_KEY` and `AUTH_INVALID_KEY` (the app's key is missing or wrong: a deployment mistake, not the person's), `AUTH_NOT_CONFIGURED` (503, `APP_API_KEY` not set), `AUTH_REQUIRED`, `AUTH_INVALID_TOKEN`, `AUTH_TOKEN_EXPIRED` (sign in again), `ACCOUNT_PENDING`, `ACCOUNT_REJECTED`, `ADMIN_ONLY`. A database outage is `DATABASE_UNAVAILABLE` (503).

### `POST /api/screen`

```json
{ "full_name": "Muhammad Ali Khan", "dob": "1975-03-04", "nationality": "Pakistan", "threshold": 85 }
```

Only `full_name` is required. `cnic`, `father_name` and `province` are optional and are used for the FIA Red Book and NACTA (see above).

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

## Continuous monitoring

A screening is a photograph of one day. Someone who is clear today can be listed next week, so people can be **enrolled in monitoring** and are screened again automatically whenever a watch list changes.

**Enrol** a person when you screen them (`"monitor": true` in `POST /api/screen`) or later with `POST /api/applicants/{id}/monitoring` and `{"enabled": true}`. Enrolling later also checks them against the current lists straight away, so someone screened weeks ago is not left unchecked until the next list update. Monitoring is opt-in on purpose: it keeps personal data and re-checks it, so it is a decision for each person. `{"enabled": false}` stops it.

**How it decides to re-screen.** Every list has a fingerprint, a hash of its records' ids, names, dates of birth and CNICs. The fingerprint each list had when everyone was last checked is stored in the database. Every `MONITOR_INTERVAL_SECONDS` (default 900) the current fingerprints are compared; for each list that changed, every monitored person is screened against that list only, with the same matcher, threshold, CNIC, father's name and province as their original screening.

**What becomes an alert.** Only a **new** potential match: not one the person already had when first screened, and not one already alerted. A match is therefore raised once, however many times a list changes, and the original screening is never rewritten.

| | |
|---|---|
| `GET /api/monitoring/alerts` | New potential matches, newest first. `?status=open` (default), `confirmed` or `dismissed`; `?limit=`, `?offset=`; `X-Total-Count` has the total. Only the alerts on your own screenings, for admins too: monitoring is private to the person who ran the screening |
| `POST /api/monitoring/alerts/{id}/decision` | `{"status": "confirmed" or "dismissed" or "open", "note": "..."}`: a person's decision, with who and when |
| `GET /api/monitoring/status` | Whether monitoring is on, how many people are watched, how many alerts are open, when each list was last checked |
| `GET /api/admin/users/{id}/applicants` | Admin: one person's screening history, newest first (`?limit=`, `?offset=`, `X-Total-Count`). Used by the People tab |
| `POST /api/admin/monitoring/run` | Admin: run the check now. `?force=true` re-screens everyone against every list |

**Safe by construction.**
- A list that could not be loaded is skipped, never read as "the list became empty", and its stored fingerprint is not moved.
- The stored fingerprint moves forward only after every monitored person was checked. An interrupted run is repeated at the next pass, and the unique key on (person, list, record) makes the repeat harmless.
- A database lock means only one server instance runs the check at a time.
- A stored name that cannot be screened (for example one saved in another script) is **counted and reported** (`skipped_unscreenable`), never passed as clear.
- A list seen for the first time is only recorded; people screened before monitoring existed are checked when they are enrolled.
- Everything is in the audit trail (`monitoring.enrol`, `monitoring.alert`, `monitoring.decision`, `monitoring.run`), with ids only.

**Notification.** Set `MONITOR_WEBHOOK_URL` and it is called when new alerts appear, with a JSON body of alert and applicant **ids only** (never a name), signed with HMAC-SHA256 in `X-Signature: sha256=...` if `MONITOR_WEBHOOK_SECRET` is set. The address must be `https` and not internal. There is no email; the webhook can feed one.

**Capacity.** Re-screening is background work on one core. On a synthetic 65,000-name list it is about 10 ms per person per changed list (about 6,000 people a minute). Several lists changing in one pass multiply that, so 5,000 monitored people and three changed lists is a few minutes.

**What monitoring does not do.**
- It does not repeat the **adverse media** search. That is a per-person web search with unverified results; run it by hand when you want fresh news.
- It does not alert on a list **removal** or on a changed detail of a match that was already reported.
- A person's original screening keeps at most 50 matches per list, so for a very common name at a low threshold, matches beyond the 50th were not recorded and may alert later as if new.
- It needs the lists to be up. A list that is down for days is not checked, and the status page shows it.
- Monitored people's names and CNICs stay in the database for as long as they are monitored. Decide your retention policy before enrolling people.

## Security

What is in place:

- **Every request is authenticated twice** (app key and signed-in person), tokens are verified locally with the project's public keys, expiry, issuer and audience are checked, anything not configured fails closed, and keys are compared in constant time.
- **A user can only reach their own screenings**; someone else's is indistinguishable from one that does not exist. Admin actions are separate and rate limited.
- **Outbound requests are constrained.** The server fetches lists from public publishers and, for the FIA Red Book, follows PDF links scraped from fia.gov.pk. Only `https` is fetched; internal, loopback, link-local and cloud-metadata addresses are refused, on every redirect hop and before the hop is requested; at most 5 redirects; every download is capped (`MAX_DOWNLOAD_MB`, default 120) so a bad source cannot exhaust memory. Scraped FIA links are accepted only if they point at `fia.gov.pk` itself, so a tampered page cannot steer the server at another host (`fia.gov.pk.evil.example`, `fia.gov.pk@evil.example` and bare IPs are dropped). XML is parsed with `defusedxml`.
- **Response headers**: `Content-Security-Policy: default-src 'none'`, HSTS, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer`, a locked-down `Permissions-Policy`, and `Cache-Control: no-store` on every API response (applicant data is never cached). A wildcard in `ALLOWED_ORIGINS` is ignored. `/docs`, `/redoc` and `/openapi.json` are off unless `ENABLE_DOCS=true`.
- **No personal data in logs or errors**: request bodies and query strings are never logged, and validation errors never echo what was submitted.
- **Dependencies and code are scanned on every push** (`pip-audit` and `bandit` in `.github/workflows/tests.yml`).
- **Evidence integrity**: each evidence PDF's SHA-256 is stored when it is made and returned as `X-Content-SHA256` on download; compare it with the file you hold.

### Audit trail

`GET /api/admin/audit` lists who ran a screening, viewed one, downloaded evidence, approved or changed a user, uploaded the NACTA list or refreshed the lists. Entries hold ids, outcomes and the evidence hash, **never an applicant's name or CNIC**. The log is append only (the database rejects UPDATE and DELETE) and **tamper evident**: every entry stores the hash of the one before it, so `GET /api/admin/audit/verify` reports the first entry that was altered, removed or inserted. To also catch someone who rewrites the whole table, chain included, copy the `head` hash it returns to somewhere outside the database now and then. A failure to write an entry is logged as `AUDIT WRITE FAILED` but does not fail the screening.

### What this does not cover

Be clear-eyed about these before relying on the service for regulated records:

- **No rate limit before sign-in.** Limits are per signed-in person, so a flood of unauthenticated requests is limited only by the host. Put Cloudflare or another WAF in front for that.
- **Limits are held in the process.** With more than one instance they are per instance. Use a shared store (Redis) if you scale out.
- **The client address in the audit log is whatever the proxy reports.** `--forwarded-allow-ips='*'` makes uvicorn believe the first `X-Forwarded-For` value, which a client can set. Restrict it to your proxy's address where you can, and do not treat that field as proof of origin.
- **A host name that later resolves to an internal address (DNS rebinding) is not checked**; only literal addresses and redirect targets are.
- **Personal data is stored unencrypted at the application level** (it relies on the database's encryption at rest) and there is **no retention or deletion policy**. AML record-keeping rules usually set a minimum, so decide yours.
- **Name matching is not identity verification.** Every hit needs a person's decision.

## Configuration

See `.env.example`. Required: `DATABASE_URL`, `SUPABASE_URL`. `APP_API_KEY` (the frontend's key; without it every request from the app is refused). Common: `ALLOWED_ORIGINS`, `API_KEY` (the secret for the scheduled NACTA upload), `SUPABASE_JWT_SECRET` (legacy token signing only), `DB_POOL_MAX`, `MATCH_THRESHOLD`, `LIST_CACHE_TTL_SECONDS`, `PRELOAD_LISTS`, `FIA_REQUIRED`, `NACTA_REQUIRED`, `NACTA_MAX_AGE_DAYS`, `NACTA_PERSONS_URL`, `NAME_VARIANTS`, `MAX_DOWNLOAD_MB`, `ENABLE_DOCS`. Without `DATABASE_URL` every request is refused with a 503 that says so; without `SUPABASE_URL` (or the legacy secret) no sign in can be verified, and requests are refused rather than let through.

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
- Spelling variants are matched only if they are in the variant table or close enough under Jaro-Winkler (0.88 per token). The table covers the common Arabic, Urdu and Persian name families, not every transliteration. Jaro-Winkler's prefix bonus also scores short and long forms of a name closely (ABDUL vs ABDULLAH is 0.93), which errs toward flagging.
- Names must be typed in Latin letters. There is no automatic Urdu or Arabic transliteration; such names are refused rather than screened badly.
- The FIA Red Book is read by text extraction from PDFs found on fia.gov.pk, using the workflow's patterns. If FIA changes the page or PDF layout, that source reports as not screened rather than clear.
- Lists are public downloads, so a publisher outage or a block on the server's IP shows up as `ERROR` for that source.

## Docker

```bash
docker build -t screening-backend .
docker run -p 8000:8000 --env-file .env screening-backend      # runs as a non-root user, with a health check
```

## Deploying

`render.yaml` is included. It needs no disk: all state is in Supabase. Set `DATABASE_URL` (and `SUPABASE_JWT_SECRET` if your project uses the legacy secret) in the Render dashboard, since those are secrets. Pick the Render region closest to the Supabase project, because every request talks to the database. The free plan sleeps when idle, so the first request after a sleep is slow while the lists load again.

Behind Render's proxy, `render.yaml` starts uvicorn with `--proxy-headers`, so the real client address is available. Rate limits count per signed in person.

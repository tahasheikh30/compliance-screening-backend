"""
Central configuration: where the data lives and how screening behaves.

All state (users, screening history, evidence PDFs and the NACTA list) is in the Supabase
Postgres database named by DATABASE_URL, so the web server itself keeps nothing on disk and
can be restarted, redeployed or put to sleep freely.

The sanctions lists themselves are NOT stored anywhere: they are downloaded live from the
official publishers (see app/screening/loader.py) and kept in memory for LIST_CACHE_TTL_SECONDS.
"""

import os


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


# --- Database and sign in ------------------------------------------------
# The Supabase connection string. Use the "Session pooler" one from the dashboard's Connect dialog
# (postgresql://postgres.<ref>:<password>@<region>.pooler.supabase.com:5432/postgres). URL-encode any
# special characters in the password.
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DB_POOL_MIN = int(_float_env("DB_POOL_MIN", 1))
DB_POOL_MAX = max(DB_POOL_MIN, int(_float_env("DB_POOL_MAX", 8)))

# Users sign in with Supabase Auth and send the access token as "Authorization: Bearer <token>".
# SUPABASE_URL (https://<ref>.supabase.co) is where the public signing keys are fetched from and what
# the token's issuer must be. SUPABASE_JWT_SECRET is only needed if the project still signs tokens
# with the legacy shared secret (HS256); projects with asymmetric signing keys do not need it.
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET", "").strip()

# Two different keys, because they have different jobs and different exposure:
#
# APP_API_KEY identifies the FRONTEND. The browser app sends it as X-API-Key on every request, next to
# the signed in person's token. It is built into the frontend, so treat it as an app identifier (anyone
# who can open the app can read it), not a secret: it only proves a request comes from the app, and the
# person's own sign in is what protects the data. Set REQUIRE_APP_KEY=false only for local development.
#
# API_KEY is a machine credential for the scheduled NACTA upload (X-API-Key, no sign in). It stays
# secret (a GitHub Actions secret), is never given to the frontend, and works on that one route only.
APP_API_KEY = os.environ.get("APP_API_KEY", "").strip()
REQUIRE_APP_KEY = os.environ.get("REQUIRE_APP_KEY", "").strip().lower() not in ("0", "false", "no")

# --- Matching ------------------------------------------------------------
# A name scoring at or above the threshold is reported as a potential match.
# The threshold can be overridden per request (50..100); anything outside
# that range, or missing, falls back to this default. Same rule as the
# original n8n workflow.
MATCH_THRESHOLD = _float_env("MATCH_THRESHOLD", 85)
MIN_THRESHOLD = 50.0
MAX_THRESHOLD = 100.0

# Keep at most this many watch-list matches per screening (best score first).
MAX_MATCHES = 50

# --- Live list loading ---------------------------------------------------
# The n8n workflow downloaded every list on every run (20-40 s). To keep
# repeated screenings fast, parsed lists are kept in memory for this long.
# Set to 0 to download fresh on every screening, exactly like the workflow.
LIST_CACHE_TTL_SECONDS = _float_env("LIST_CACHE_TTL_SECONDS", 3600)

# Load every list when the server starts and reload each one shortly before it expires, so a
# screening does not wait for a 20-40 s download. Needs LIST_CACHE_TTL_SECONDS > 0.
PRELOAD_LISTS = os.environ.get("PRELOAD_LISTS", "true").strip().lower() not in ("0", "false", "no")

HTTP_USER_AGENT = os.environ.get(
    "HTTP_USER_AGENT", "Mozilla/5.0 (compatible; ComplianceScreening/1.0)"
)

# The workflow treats the FIA Red Book as optional: if the FIA website cannot be
# read, the screening still completes and that list is shown as unavailable.
# Here an unavailable list routes the applicant to manual review by default, so a
# check that did not run can never look like a clean result. Set FIA_REQUIRED=false
# to get the workflow's behaviour instead.
FIA_REQUIRED = os.environ.get("FIA_REQUIRED", "true").strip().lower() not in ("0", "false", "no")

# --- NACTA Proscribed Persons (Fourth Schedule) ---------------------------
# NACTA publishes this list only through a web app (nfs.nacta.gov.pk), with no
# stable file download, so it is loaded from a CSV or JSON file uploaded through
# POST /api/admin/nacta. If you find a stable URL that returns the list as CSV or
# JSON, set NACTA_PERSONS_URL and it is downloaded live instead.
NACTA_PERSONS_URL = os.environ.get("NACTA_PERSONS_URL", "").strip()

# The list changes every few weeks. A copy older than this is reported as
# out of date, which routes the applicant to manual review instead of looking clear.
NACTA_MAX_AGE_DAYS = _float_env("NACTA_MAX_AGE_DAYS", 30)

# Like the FIA Red Book: when true (the default), a NACTA list that is missing or out
# of date sends an otherwise clear applicant to manual review.
NACTA_REQUIRED = os.environ.get("NACTA_REQUIRED", "true").strip().lower() not in ("0", "false", "no")


# --- Network safety ------------------------------------------------------
# Largest single download accepted from a list publisher. The biggest real files are a few tens of megabytes;
# the cap stops a compromised or misconfigured source from exhausting the server's memory.
MAX_DOWNLOAD_BYTES = int(_float_env("MAX_DOWNLOAD_MB", 120) * 1024 * 1024)

# --- API surface ---------------------------------------------------------
# The interactive docs (/docs, /redoc, /openapi.json) describe every route to anyone who asks, so they are off
# unless you turn them on (local development).
ENABLE_DOCS = os.environ.get("ENABLE_DOCS", "").strip().lower() in ("1", "true", "yes")


# --- Continuous monitoring -----------------------------------------------
# Applicants who are enrolled in monitoring are re-screened whenever a watch list changes. The check runs in the
# background every MONITOR_INTERVAL_SECONDS (at least 60). Set MONITORING=false to switch the background check off
# (an admin can still run it by hand).
MONITORING_ENABLED = os.environ.get("MONITORING", "true").strip().lower() not in ("0", "false", "no")
MONITOR_INTERVAL_SECONDS = max(60.0, _float_env("MONITOR_INTERVAL_SECONDS", 900))
# Optional: told when new alerts appear. The message holds alert and applicant ids only, never a name, and is signed
# with HMAC-SHA256 (X-Signature: sha256=...) when MONITOR_WEBHOOK_SECRET is set.
MONITOR_WEBHOOK_URL = os.environ.get("MONITOR_WEBHOOK_URL", "").strip()
MONITOR_WEBHOOK_SECRET = os.environ.get("MONITOR_WEBHOOK_SECRET", "").strip()

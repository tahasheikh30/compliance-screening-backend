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

# API_KEY (an X-API-Key header) is a machine credential: it is accepted only to upload the NACTA list,
# which is what the scheduled GitHub workflow uses. Set this to true ONLY while an older frontend that
# still sends the key is being replaced: it then gets full admin access again, without any user attribution.
ALLOW_API_KEY_FULL_ACCESS = os.environ.get("ALLOW_API_KEY_FULL_ACCESS", "").strip().lower() in ("1", "true", "yes")

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

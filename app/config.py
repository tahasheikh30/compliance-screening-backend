"""
Central configuration: where data lives on disk and how screening behaves.

Locally, everything defaults to folders inside this repo (fine for dev).

On Render, set STORAGE_DIR to the persistent disk's mount path (see
render.yaml) so the SQLite DB and evidence PDFs survive redeploys. Without
this, Render's filesystem is ephemeral and evidence PDFs and screening
history are lost on every deploy.

The sanctions lists themselves are NOT stored on disk: every screening
downloads them live from the official publishers (see app/screening/loader.py),
optionally reusing an in-memory copy for LIST_CACHE_TTL_SECONDS.
"""

import os
from pathlib import Path

_backend_root = Path(__file__).resolve().parent.parent
STORAGE_DIR = Path(os.environ.get("STORAGE_DIR", str(_backend_root)))

DB_PATH = STORAGE_DIR / "screening.db"
EVIDENCE_DIR = STORAGE_DIR / "evidence"
LISTS_DIR = STORAGE_DIR / "lists"   # the NACTA list file uploaded through the API

for d in (STORAGE_DIR, EVIDENCE_DIR, LISTS_DIR):
    d.mkdir(parents=True, exist_ok=True)


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


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

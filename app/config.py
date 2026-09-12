"""
Central place for where data lives on disk, plus the screening thresholds
that decide HIT / REVIEW / CLEAR.

Locally, everything defaults to folders inside backend/ (fine for dev).

On Render, set STORAGE_DIR to the persistent disk's mount path (see
render.yaml — it mounts a disk at /opt/render/project/src/data) so the
SQLite DB, evidence PDFs, and cached watchlists survive redeploys and
restarts. Without this, Render's filesystem is ephemeral and you WILL
lose evidence PDFs and screening history on every deploy.
"""

import os
from pathlib import Path

_backend_root = Path(__file__).resolve().parent.parent
STORAGE_DIR = Path(os.environ.get("STORAGE_DIR", str(_backend_root)))

DB_PATH = STORAGE_DIR / "screening.db"
CACHE_DIR = STORAGE_DIR / "cache"
EVIDENCE_DIR = STORAGE_DIR / "evidence"
SCREENSHOT_DIR = EVIDENCE_DIR / "screenshots"

# Every FIA Red Book PDF that's ever been loaded (via upload or scrape) gets
# copied here, timestamped, before it's replaced by a newer edition. This is
# what makes "swap in the new edition" safe: nothing is destroyed, so if a
# past screening HIT is ever questioned you can go back and see exactly
# which edition was active when it ran. See app/screening/fia_redbook.py.
FIA_REDBOOK_ARCHIVE_DIR = CACHE_DIR / "fia_redbook_archive"

for d in (CACHE_DIR, EVIDENCE_DIR, SCREENSHOT_DIR, FIA_REDBOOK_ARCHIVE_DIR):
    d.mkdir(parents=True, exist_ok=True)


# --- Matching thresholds -----------------------------------------------
# Score is 0-100 from app/screening/matching.py's combined algorithm.
# These are configurable via env vars so they can be tuned per deployment
# without a code change — but changing them should still go through the
# same review a compliance policy change would, not be done casually.
# There is no universally "correct" threshold: raising it reduces false
# positives (fewer innocent applicants flagged) at the cost of missing
# more true matches, and lowering it does the reverse. Tune against a
# labeled test set (see tests/test_matching.py for a starting point) and
# get sign-off from compliance before changing these in production.
MATCH_THRESHOLD = float(os.environ.get("MATCH_THRESHOLD", "85"))   # score >= this => HIT
REVIEW_THRESHOLD = float(os.environ.get("REVIEW_THRESHOLD", "60"))  # score >= this => REVIEW

# How many points below REVIEW_THRESHOLD a score can fall and still get
# logged to the near-miss audit trail (see app/screening/matching.py and
# database.insert_near_miss). This does NOT change any applicant's status —
# it only controls what gets written to the audit log for periodic
# compliance review.
NEAR_MISS_MARGIN = float(os.environ.get("NEAR_MISS_MARGIN", "10"))

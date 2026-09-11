"""
Central place for where data lives on disk.

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

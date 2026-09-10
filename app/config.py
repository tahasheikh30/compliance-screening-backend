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

for d in (CACHE_DIR, EVIDENCE_DIR, SCREENSHOT_DIR):
    d.mkdir(parents=True, exist_ok=True)

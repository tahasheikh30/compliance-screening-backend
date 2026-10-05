"""
Storage for the NACTA Proscribed Persons file.

NACTA publishes this list only through a web app, so the list is uploaded as a CSV or JSON file
through the API (by an admin, or by the scheduled GitHub workflow) and kept in the database.
Only the most recent upload is kept. The file is stored as uploaded; it is parsed each time the
list is loaded, so a parser improvement applies to the existing file too.
"""

import os
from datetime import datetime, timezone

from app import database as db


def save(data: bytes, filename: str, records: int, live: bool = False) -> dict:
    # an uploaded file keeps only its file name (never a client side path); a live copy keeps its address
    name = (filename if live else os.path.basename(filename or "nacta.csv"))[:200] or "nacta.csv"
    return db.nacta_put(data, name, records, live)


def meta() -> dict | None:
    return db.nacta_meta()


def load() -> tuple | None:
    """(file bytes, metadata) of the stored list, or None when nothing was uploaded."""
    return db.nacta_get()


def age_days(m: dict | None) -> float | None:
    if not m or not m.get("uploaded_at"):
        return None
    try:
        when = datetime.fromisoformat(m["uploaded_at"])
    except ValueError:
        return None
    return max(0.0, (datetime.now(timezone.utc) - when).total_seconds() / 86400)

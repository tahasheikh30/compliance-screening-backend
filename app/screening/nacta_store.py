"""
Storage for the NACTA Proscribed Persons file.

NACTA publishes this list only through a web app, so the list is uploaded as a CSV or
JSON file through the API and kept on the persistent disk (STORAGE_DIR/lists). Only
the most recent upload is kept. The file is stored as uploaded; it is parsed each
time the list is loaded, so a parser improvement applies to the existing file too.
"""

import hashlib
import json
import os
from datetime import datetime, timezone

from app import config


def _paths():
    d = config.LISTS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d / "nacta_persons.dat", d / "nacta_persons.json"


def _atomic_write(path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)  # never leaves a half written list behind


def save(data: bytes, filename: str, records: int) -> dict:
    blob, meta_path = _paths()
    meta = {
        "filename": os.path.basename(filename or "nacta.csv")[:200],
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "records": int(records),
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    _atomic_write(blob, data)
    _atomic_write(meta_path, json.dumps(meta).encode())
    return meta


def meta() -> dict | None:
    _, meta_path = _paths()
    try:
        return json.loads(meta_path.read_text())
    except (OSError, ValueError):
        return None


def load() -> tuple | None:
    """(file bytes, metadata) of the stored list, or None when nothing was uploaded."""
    blob, _ = _paths()
    m = meta()
    if not m or not blob.exists():
        return None
    return blob.read_bytes(), m


def age_days(m: dict | None) -> float | None:
    if not m or not m.get("uploaded_at"):
        return None
    try:
        when = datetime.fromisoformat(m["uploaded_at"])
    except ValueError:
        return None
    return max(0.0, (datetime.now(timezone.utc) - when).total_seconds() / 86400)

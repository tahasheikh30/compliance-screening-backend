"""
FIA Red Book (Pakistan) — Most Wanted Human Traffickers/Smugglers &
Terrorism Suspects.

Unlike UNSC, this has NO api/feed — it's a PDF published irregularly
(roughly annually) on fia.gov.pk. There's no reliable way to screen this
in true real time; the practical approach is:

  1. A new Red Book PDF arrives — uploaded by an analyst (preferred), or
     found by the scraper on the FIA publications page.
  2. It is validated and parsed into a name list (and CNIC / source page,
     where the edition's layout allows) and stored as a STAGED edition.
     Nothing changes for live screening yet.
  3. A compliance analyst reviews the parsed entries against the PDF pages
     (table layouts have changed across editions, so blind trust in the
     parser is not safe — this code does not claim to solve that; it makes
     the review easy and makes activation a deliberate act).
  4. The analyst ACTIVATES the edition. Applicant screening then runs
     against the active edition, using the shared matching engine in
     app.screening.matching (normalization + multi-algorithm scoring) plus
     an exact-CNIC check where both sides have one.
  5. Superseded editions are never deleted. Rolling back is just activating
     an older edition again. Every screening result records which edition
     it was checked against (see `list_version`).

STORAGE LAYOUT (all under CACHE_DIR, i.e. on the persistent disk):

  fia_redbook_editions/<edition_id>/redbook.pdf   the original file, untouched
  fia_redbook_editions/<edition_id>/entries.txt   parsed "Name|CNIC|page" lines
  fia_redbook_editions/<edition_id>/meta.json     hash, counts, warnings, history
  fia_redbook_active.json                         pointer to the active edition
  fia_redbook_latest.pdf / fia_redbook_names.txt / fia_redbook_meta.json
                                                  the LIVE copy that check()
                                                  and render_matched_page()
                                                  read (copied on activation)

edition_id looks like 20260922T101500Z_ab12cd34 (UTC timestamp + first 8 hex
chars of the PDF's SHA-256) and is validated against that exact pattern
everywhere it is used as a path component.

CACHE FILE FORMAT: entries are "Name|CNIC|page_index" per line. CNIC is empty
when the edition's table had none for that row; page_index is the 0-based PDF
page (blank if unknown). Older caches with "Name|CNIC" or plain one-name-per-
line are still read correctly.

CONCURRENCY: registry writes are serialised with an in-process lock (the app
runs as a single uvicorn process on Render). Files are written to a temp name
and os.replace()d, so a screening that happens to read during an activation
sees either the old or the new file, never a half-written one.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pdfplumber
import pymupdf as fitz  # PyMuPDF, for validation and rendering pages as images
import requests

from app.config import (
    CACHE_DIR, FIA_REDBOOK_ARCHIVE_DIR, FIA_REDBOOK_EDITIONS_DIR, FIA_AUTO_ACTIVATE_MIN_RATIO,
)
from app.errors import AppError
from app.screening import matching

logger = logging.getLogger("screening")

FIA_PUBLICATIONS_PAGE = "https://www.fia.gov.pk/press-pub"
FIA_BASE = "https://www.fia.gov.pk"

PDF_CACHE = CACHE_DIR / "fia_redbook_latest.pdf"
NAMES_CACHE = CACHE_DIR / "fia_redbook_names.txt"
# Small JSON sidecar describing whatever edition is *currently* loaded.
META_CACHE = CACHE_DIR / "fia_redbook_meta.json"
ACTIVE_POINTER = CACHE_DIR / "fia_redbook_active.json"

EDITIONS_DIR = FIA_REDBOOK_EDITIONS_DIR
_MIGRATION_MARKER_NAME = ".migrated"

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # Red Book PDFs are small; this is a generous ceiling
MAX_PDF_PAGES = 1500                 # sanity cap so a hostile/huge file can't tie up the parser
MAX_NOTES_LEN = 500

_NAME_LINE_RE = re.compile(r"^[A-Za-z][A-Za-z .'-]{2,}$")
_EDITION_ID_RE = re.compile(r"^\d{8}T\d{6}Z_[0-9a-f]{8}$")
# Column header labels that happen to be name-shaped text ("Name", "Full Name")
# and would otherwise be parsed as a data row — repeated table headers (one per
# page, via repeatRows) are a common Red Book layout.
_HEADER_LABELS = {
    "name", "full name", "applicant name", "s no", "sno", "sr no", "serial no",
    "father name", "husband name", "cnic", "cnic no", "remarks", "alias",
}

_lock = threading.RLock()

Entry = tuple  # (name, cnic or None, 0-based page index or None)


class DuplicateEdition(AppError):
    def __init__(self, existing_id: str, status: str):
        super().__init__(
            409, "FIA_DUPLICATE_EDITION",
            f"This exact PDF has already been uploaded (edition {existing_id}, currently {status}).",
            "Open that edition from the list below. If you want it live again, use Activate on it.",
        )
        self.edition_id = existing_id


# ---------------------------------------------------------------------------
# small file helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _atomic_write_text(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _clean_filename(name: str | None) -> str | None:
    if not name:
        return None
    base = Path(name.replace("\\", "/")).name
    base = re.sub(r"[\x00-\x1f\x7f]", "", base).strip()
    return base[:200] or None


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

@dataclass
class ParseResult:
    entries: list
    page_count: int = 0
    table_pages: int = 0     # pages where a real table structure was used
    text_pages: int = 0      # pages that fell back to the text heuristic
    empty_pages: int = 0     # pages with no extractable text at all (scans/images)
    error_pages: int = 0     # pages that raised while being parsed
    text_chars: int = 0


def _clean_cell(cell) -> str:
    """Collapse whitespace/newlines inside a table cell ('Muhammad\\nAli' -> 'Muhammad Ali')."""
    return re.sub(r"\s+", " ", str(cell)).strip() if cell else ""


def _parse_pdf(pdf_path: Path) -> ParseResult:
    """
    Extracts (name, cnic_or_None, page_index) entries from the Red Book PDF.

    Red Book table layouts vary by edition, so this tries strategies in
    order of reliability and falls back gracefully:

      1. Real table structure (page.extract_table()) — if a row has a
         column that looks like a name and (optionally) a column that
         contains a CNIC pattern, use both. Most reliable when the PDF
         actually has table structure.
      2. Plain text fallback — if no usable table is detected on a page,
         scan its text line by line: a CNIC pattern found near a name-shaped
         line is associated with that line; a name-shaped line with no
         nearby CNIC is still kept (name-only entry).

    A page that raises while parsing is counted in `error_pages` and skipped
    rather than aborting the whole edition. Counts of each path taken are
    returned so the review screen can warn when the heuristic path did a lot
    of the work. This is still a best-effort heuristic, not a guaranteed-
    correct parser: an analyst reviewing the parsed entries against the PDF
    after every new edition remains a required step.
    """
    res = ParseResult(entries=[])
    seen: set[str] = set()

    def maybe_add(name: str, cnic: str | None, page_idx: int):
        name = name.strip()
        if not name or not _NAME_LINE_RE.match(name):
            return
        key = name.lower()
        if key in seen or key in _HEADER_LABELS:
            return
        seen.add(key)
        res.entries.append((name, cnic, page_idx))

    with pdfplumber.open(pdf_path) as pdf:
        res.page_count = len(pdf.pages)
        for idx, page in enumerate(pdf.pages):
            try:
                nchars = len(page.chars)
                res.text_chars += nchars
                if nchars == 0:
                    res.empty_pages += 1
                    continue

                rows = [r for r in (page.extract_table() or []) if r]
                ncols = max((len(r) for r in rows), default=0)
                name_col = 0
                if ncols:
                    col_hits = [0] * ncols
                    for row in rows:
                        for i, cell in enumerate(row):
                            if cell and _NAME_LINE_RE.match(_clean_cell(cell)):
                                col_hits[i] += 1
                    if max(col_hits) > 0:
                        name_col = col_hits.index(max(col_hits))
                    else:
                        ncols = 0  # table has no name-shaped column: use the text path

                if ncols:
                    res.table_pages += 1
                    for row in rows:
                        if name_col >= len(row) or not row[name_col]:
                            continue
                        cnic = None
                        for cell in row:
                            if cell:
                                found = matching.extract_cnic(str(cell))
                                if found:
                                    cnic = found
                                    break
                        maybe_add(_clean_cell(row[name_col]), cnic, idx)
                else:
                    res.text_pages += 1
                    lines = (page.extract_text() or "").splitlines()
                    for i, line in enumerate(lines):
                        for candidate in re.findall(r"\b([A-Z][a-z]+(?:\s[A-Z][a-z]+){1,3})\b", line):
                            # Look at this line and the next for a CNIC that
                            # likely belongs to the same record.
                            window = line + (" " + lines[i + 1] if i + 1 < len(lines) else "")
                            maybe_add(candidate, matching.extract_cnic(window), idx)
            except Exception:
                res.error_pages += 1
                logger.exception("FIA parse: page %d failed and was skipped", idx + 1)

    return res


def _extract_entries_from_pdf(pdf_path: Path) -> list[tuple[str, str | None]]:
    """Back-compat wrapper: (name, cnic) pairs only."""
    return [(n, c) for n, c, _ in _parse_pdf(pdf_path).entries]


def _serialize_entries(entries) -> str:
    lines = []
    for e in entries:
        name, cnic = e[0], e[1]
        page = e[2] if len(e) > 2 else None
        lines.append(f"{name}|{cnic or ''}|{'' if page is None else page}")
    return "\n".join(lines)


def _parse_entries_text(text: str) -> list[Entry]:
    out: list[Entry] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("|")
        name = parts[0].strip()
        if not name:
            continue
        cnic = (parts[1].strip() or None) if len(parts) > 1 else None
        page = int(parts[2]) if len(parts) > 2 and parts[2].strip().isdigit() else None
        out.append((name, cnic, page))
    return out


def _save_entries(entries) -> None:
    """Writes the LIVE names cache (kept for callers of the pre-registry API)."""
    _atomic_write_text(NAMES_CACHE, _serialize_entries(entries))


# ---------------------------------------------------------------------------
# PDF validation
# ---------------------------------------------------------------------------

def validate_pdf_bytes(pdf_bytes: bytes) -> int:
    """
    Trust the file's contents, not its name or declared content-type (both are
    trivially spoofed). Returns the page count, or raises an AppError that says
    exactly what is wrong with the file.
    """
    if not pdf_bytes.startswith(b"%PDF-"):
        raise AppError(400, "FIA_NOT_A_PDF", "This file is not a PDF (it does not start with a PDF header).",
                       "Upload the Red Book PDF itself — not a screenshot, Word file, or web page saved as .pdf.")
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        raise AppError(422, "FIA_PDF_CORRUPT", "The PDF is damaged and could not be opened.",
                       "Download it again from fia.gov.pk. If it opens in your PDF viewer, re-save it (Print -> Save as PDF) and retry.")
    try:
        if doc.needs_pass:
            raise AppError(422, "FIA_PDF_ENCRYPTED", "The PDF is password-protected.",
                           "Remove the password in a PDF viewer, save a copy, and upload that copy.")
        n = doc.page_count
    finally:
        doc.close()
    if n < 1:
        raise AppError(422, "FIA_PDF_EMPTY", "The PDF has no pages.", "Check you picked the right file.")
    if n > MAX_PDF_PAGES:
        raise AppError(422, "FIA_PDF_TOO_LONG", f"The PDF has {n} pages, which is far more than a Red Book.",
                       "Check you picked the right file.")
    return n


# ---------------------------------------------------------------------------
# registry: reading
# ---------------------------------------------------------------------------

def _edition_dir(edition_id: str) -> Path:
    if not isinstance(edition_id, str) or not _EDITION_ID_RE.match(edition_id):
        raise AppError(404, "FIA_EDITION_NOT_FOUND", "No such Red Book edition.",
                       "Reload the page — the edition list may be out of date.")
    d = EDITIONS_DIR / edition_id
    if not d.is_dir():
        raise AppError(404, "FIA_EDITION_NOT_FOUND", "No such Red Book edition.",
                       "Reload the page — the edition list may be out of date.")
    return d


def _read_meta(edition_id: str) -> dict:
    d = _edition_dir(edition_id)
    try:
        return json.loads((d / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise AppError(500, "FIA_EDITION_META_UNREADABLE",
                       "This edition's metadata file is missing or damaged.",
                       "The PDF is still on disk. Contact whoever maintains the server.")


def _write_meta(meta: dict) -> None:
    _atomic_write_text(EDITIONS_DIR / meta["id"] / "meta.json", json.dumps(meta, indent=2))


def _all_meta() -> list[dict]:
    out = []
    if not EDITIONS_DIR.exists():
        return out
    for d in EDITIONS_DIR.iterdir():
        if d.is_dir() and _EDITION_ID_RE.match(d.name):
            try:
                out.append(json.loads((d / "meta.json").read_text(encoding="utf-8")))
            except (OSError, ValueError):
                logger.warning("FIA registry: skipping edition %s with unreadable meta.json", d.name)
    return out


def _active_id() -> str | None:
    try:
        aid = json.loads(ACTIVE_POINTER.read_text(encoding="utf-8")).get("edition_id")
    except (OSError, ValueError):
        return None
    return aid if isinstance(aid, str) and _EDITION_ID_RE.match(aid) else None


def _public(meta: dict, active_id: str | None) -> dict:
    activations = meta.get("activations") or []
    if meta["id"] == active_id:
        status = "active"
    elif activations:
        status = "archived"
    else:
        status = "staged"
    out = {k: meta.get(k) for k in (
        "id", "original_filename", "source", "source_url", "sha256", "size_bytes", "page_count",
        "names_found", "cnics_found", "parse", "warnings", "notes", "created_at", "legacy",
    )}
    out["status"] = status
    out["activation_count"] = len(activations)
    out["last_activated_at"] = activations[-1]["at"] if activations else None
    out["activations"] = activations
    return out


def _find_by_sha(sha: str) -> dict | None:
    for m in _all_meta():
        if m.get("sha256") == sha:
            return m
    return None


def list_editions() -> dict:
    with _lock:
        _ensure_migrated()
        active = _active_id()
        metas = sorted(_all_meta(), key=lambda m: m.get("created_at", ""), reverse=True)
        return {
            "active_edition_id": active,
            "editions": [_public(m, active) for m in metas],
            "limits": {"max_upload_bytes": MAX_UPLOAD_BYTES},
        }


def get_edition(edition_id: str) -> dict:
    with _lock:
        _ensure_migrated()
        return _public(_read_meta(edition_id), _active_id())


def get_status() -> dict:
    """
    What edition is currently loaded, and when/how it got there — so you
    can confirm a swap worked (or check staleness) without downloading
    anything. Backs GET /api/admin/fia-redbook/status (kept for back-compat;
    the Red Book tab uses list_editions()).
    """
    with _lock:
        _ensure_migrated()
        active = _active_id()
        metas = _all_meta()
        if not active:
            return {
                "configured": False,
                "staged_editions": sum(1 for m in metas if not m.get("activations")),
                "detail": "No Red Book edition is live — upload one on the FIA Red Book tab "
                          "(or POST /api/admin/fia-redbook/upload).",
            }
        meta = _read_meta(active)
        activations = meta.get("activations") or []
        loaded_at = activations[-1]["at"] if activations else meta.get("created_at")
        try:
            age_days = (_now() - datetime.fromisoformat(loaded_at)).total_seconds() / 86400
        except (TypeError, ValueError):
            age_days = None
        return {
            "configured": True,
            "edition_id": active,
            "source": meta.get("source"),
            "original_filename": meta.get("original_filename"),
            "names_found": meta.get("names_found"),
            "cnics_found": meta.get("cnics_found"),
            "page_count": meta.get("page_count"),
            "sha256": meta.get("sha256"),
            "loaded_at": loaded_at,
            "age_days": None if age_days is None else round(age_days, 1),
            "previous_editions_archived": sum(
                1 for m in metas if m.get("activations") and m["id"] != active),
            "staged_editions": sum(1 for m in metas if not m.get("activations")),
        }


def active_edition_id() -> str | None:
    return _active_id()


# ---------------------------------------------------------------------------
# registry: writing
# ---------------------------------------------------------------------------

def _store_edition(pdf_bytes: bytes, entries, meta: dict) -> dict:
    """Writes one edition folder. Caller holds _lock. Cleans up after itself on failure."""
    d = EDITIONS_DIR / meta["id"]
    d.mkdir(parents=True, exist_ok=False)
    try:
        _atomic_write_bytes(d / "redbook.pdf", pdf_bytes)
        _atomic_write_text(d / "entries.txt", _serialize_entries(entries))
        _write_meta(meta)
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        raise
    return meta


def _build_warnings(parse: ParseResult, names_found: int, cnics_found: int,
                    active_meta: dict | None) -> list[dict]:
    w: list[dict] = []
    if parse.empty_pages:
        w.append({"code": "FIA_PAGES_NO_TEXT", "severity": "high",
                  "message": f"{parse.empty_pages} of {parse.page_count} pages contain no extractable text "
                             "(probably scanned images). Names on those pages were NOT captured — "
                             "check them by eye, or obtain a text-based copy of the PDF."})
    if parse.error_pages:
        w.append({"code": "FIA_PAGE_PARSE_ERRORS", "severity": "high",
                  "message": f"{parse.error_pages} page(s) raised errors while parsing and were skipped. "
                             "Names on them were NOT captured."})
    if parse.text_pages:
        w.append({"code": "FIA_TEXT_FALLBACK_USED", "severity": "medium",
                  "message": f"{parse.text_pages} of {parse.page_count} pages had no usable table, so a text "
                             "heuristic was used instead. It can pick up headings that are not names and can "
                             "miss names printed in ALL CAPS. Review those pages carefully."})
    if names_found < 20:
        w.append({"code": "FIA_FEW_NAMES", "severity": "medium",
                  "message": f"Only {names_found} name(s) were found, which is unusually few for a Red Book."})
    if cnics_found == 0:
        w.append({"code": "FIA_NO_CNICS", "severity": "info",
                  "message": "No CNIC numbers were detected. Screening against this edition will use name "
                             "matching only (exact-CNIC matching will not be possible)."})
    if active_meta and active_meta.get("names_found"):
        ratio = names_found / active_meta["names_found"]
        if ratio < 0.6:
            w.append({"code": "FIA_SHRUNK", "severity": "high",
                      "message": f"This edition has {names_found} names versus {active_meta['names_found']} "
                                 f"in the live edition ({(ratio - 1) * 100:+.0f}%). A big drop usually means "
                                 "the parser missed entries, not that FIA removed them."})
        elif ratio > 2.0:
            w.append({"code": "FIA_GREW", "severity": "medium",
                      "message": f"This edition has {names_found} names versus {active_meta['names_found']} "
                                 f"in the live edition ({(ratio - 1) * 100:+.0f}%). Check the text heuristic "
                                 "isn't picking up non-names."})
    return w


def create_edition(pdf_bytes: bytes, original_filename: str | None = None, source: str = "upload",
                   source_url: str | None = None, notes: str | None = None) -> dict:
    """
    Validates, parses and stores a new STAGED edition. Does not touch the live
    edition. Raises AppError with a specific code for every way this can fail.
    """
    page_count = validate_pdf_bytes(pdf_bytes)
    sha = _sha256(pdf_bytes)

    with _lock:
        _ensure_migrated()
        dup = _find_by_sha(sha)
        if dup:
            raise DuplicateEdition(dup["id"], _public(dup, _active_id())["status"])

    # Parse outside the lock: it can take several seconds on a big PDF and
    # must not block other registry calls (status checks, screening).
    tmp = EDITIONS_DIR / f".incoming_{uuid.uuid4().hex}.pdf"
    try:
        tmp.write_bytes(pdf_bytes)
        try:
            parse = _parse_pdf(tmp)
        except Exception as e:
            logger.exception("FIA parse failed for upload %s", _clean_filename(original_filename))
            raise AppError(422, "FIA_PDF_UNREADABLE",
                           f"The PDF opened, but its text could not be read ({type(e).__name__}).",
                           "Try re-saving the PDF (Print -> Save as PDF) and uploading that copy.")
    finally:
        tmp.unlink(missing_ok=True)

    if not parse.entries:
        if parse.text_chars == 0:
            raise AppError(422, "FIA_PDF_IS_SCAN",
                           "This PDF has no text layer (it looks like scanned images), so no names could be read.",
                           "Obtain a text-based PDF from FIA, or run OCR on this one first, then upload again.")
        raise AppError(422, "FIA_NO_ENTRIES_PARSED",
                       "The PDF has text, but no names could be recognised in it.",
                       "This layout may not be supported by the parser, or this may not be a Red Book. "
                       "If it is genuine, send the file to whoever maintains this tool.")

    names_found = len(parse.entries)
    cnics_found = sum(1 for _, c, _ in parse.entries if c)

    with _lock:
        dup = _find_by_sha(sha)  # re-check: another upload may have won the race
        if dup:
            raise DuplicateEdition(dup["id"], _public(dup, _active_id())["status"])
        active = _active_id()
        active_meta = _read_meta(active) if active else None
        created = _now()
        meta = {
            "id": f"{created.strftime('%Y%m%dT%H%M%SZ')}_{sha[:8]}",
            "original_filename": _clean_filename(original_filename),
            "source": source,
            "source_url": source_url,
            "sha256": sha,
            "size_bytes": len(pdf_bytes),
            "page_count": page_count,
            "names_found": names_found,
            "cnics_found": cnics_found,
            "parse": {
                "table_pages": parse.table_pages, "text_pages": parse.text_pages,
                "empty_pages": parse.empty_pages, "error_pages": parse.error_pages,
            },
            "warnings": _build_warnings(parse, names_found, cnics_found, active_meta),
            "notes": (notes or "").strip()[:MAX_NOTES_LEN] or None,
            "created_at": created.isoformat(),
            "activations": [],
            "legacy": False,
        }
        _store_edition(pdf_bytes, parse.entries, meta)
        logger.info("FIA edition %s staged (%d names, %d CNICs, %d warnings)",
                    meta["id"], names_found, cnics_found, len(meta["warnings"]))
        return _public(meta, active)


def activate_edition(edition_id: str, confirm_reviewed: bool = False, note: str | None = None) -> dict:
    """
    Makes an edition the one live screening uses. Also the rollback path:
    activating an older edition restores it exactly as it was.
    """
    with _lock:
        _ensure_migrated()
        meta = _read_meta(edition_id)
        active = _active_id()
        if active == edition_id:
            raise AppError(409, "FIA_ALREADY_ACTIVE", "This edition is already the live one.")
        if not meta.get("activations") and not confirm_reviewed:
            raise AppError(400, "FIA_REVIEW_CONFIRMATION_REQUIRED",
                           "Confirm you have spot-checked the parsed entries before making this edition live.",
                           "Review the entries against the PDF pages, tick the confirmation, and try again.")

        d = _edition_dir(edition_id)
        pdf_path, entries_path = d / "redbook.pdf", d / "entries.txt"
        if not pdf_path.exists() or not entries_path.exists():
            raise AppError(500, "FIA_EDITION_FILES_MISSING",
                           "This edition's files are missing on the server, so it can't be made live.",
                           "The persistent disk may have been reset. Upload the PDF again.")
        pdf_bytes = pdf_path.read_bytes()
        if _sha256(pdf_bytes) != meta.get("sha256"):
            raise AppError(500, "FIA_EDITION_INTEGRITY_FAILED",
                           "The stored PDF no longer matches its recorded SHA-256, so it will not be made live.",
                           "The file was modified or corrupted on disk. Upload the original PDF again.")

        now = _now().isoformat()
        # PDF first, then names: a screening that reads in between at worst
        # pairs new names with the old PDF for the page-image lookup.
        _atomic_write_bytes(PDF_CACHE, pdf_bytes)
        _atomic_write_bytes(NAMES_CACHE, entries_path.read_bytes())
        _atomic_write_text(META_CACHE, json.dumps({
            "edition_id": edition_id, "source": meta.get("source"),
            "original_filename": meta.get("original_filename"),
            "names_found": meta.get("names_found"), "cnics_found": meta.get("cnics_found"),
            "sha256": meta.get("sha256"), "loaded_at": now,
        }, indent=2))
        _atomic_write_text(ACTIVE_POINTER, json.dumps({"edition_id": edition_id, "activated_at": now}))

        meta.setdefault("activations", []).append({
            "at": now, "action": "activated",
            "note": (note or "").strip()[:MAX_NOTES_LEN] or None,
            "previous_edition_id": active,
        })
        _write_meta(meta)
        logger.info("FIA edition %s is now LIVE (previous: %s)", edition_id, active)
        return _public(meta, edition_id)


def delete_edition(edition_id: str) -> dict:
    """
    Discards a STAGED edition (an upload that was never made live). Editions
    that have ever been live are retained: a past screening result may need to
    be traced back to the exact list it was checked against.
    """
    with _lock:
        _ensure_migrated()
        meta = _read_meta(edition_id)
        if _active_id() == edition_id:
            raise AppError(409, "FIA_EDITION_ACTIVE", "The live edition can't be deleted.",
                           "Activate a different edition first.")
        if meta.get("activations"):
            raise AppError(409, "FIA_EDITION_RETAINED",
                           "This edition has been live before, so it is kept for audit purposes.",
                           "Past screening results refer to it. You can still download it or re-activate it.")
        shutil.rmtree(_edition_dir(edition_id))
        logger.info("FIA staged edition %s discarded", edition_id)
        return {"deleted": edition_id}


def edition_pdf_path(edition_id: str) -> Path:
    with _lock:
        _ensure_migrated()
        p = _edition_dir(edition_id) / "redbook.pdf"
    if not p.exists():
        raise AppError(404, "FIA_EDITION_FILES_MISSING", "The PDF for this edition is missing on the server.")
    return p


def _load_edition_entries(edition_id: str) -> list[Entry]:
    p = _edition_dir(edition_id) / "entries.txt"
    if not p.exists():
        raise AppError(404, "FIA_EDITION_FILES_MISSING", "The parsed entries for this edition are missing.")
    return _parse_entries_text(p.read_text(encoding="utf-8"))


def browse_entries(edition_id: str, q: str | None = None, cnic_only: bool = False,
                   offset: int = 0, limit: int = 50) -> dict:
    """Paginated, searchable view of what the parser extracted — the analyst's spot-check surface."""
    with _lock:
        _ensure_migrated()
        entries = _load_edition_entries(edition_id)
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    if cnic_only:
        entries = [e for e in entries if e[1]]
    if q:
        ql = q.strip().lower()
        qdigits = re.sub(r"\D", "", ql)
        entries = [
            e for e in entries
            if ql in e[0].lower() or (len(qdigits) >= 3 and e[1] and qdigits in re.sub(r"\D", "", e[1]))
        ]
    total = len(entries)
    items = [{"name": n, "cnic": c, "page": (p + 1) if p is not None else None}
             for n, c, p in entries[offset:offset + limit]]
    return {"total": total, "offset": offset, "limit": limit, "items": items}


def diff_against_active(edition_id: str, sample: int = 50) -> dict:
    """How does this edition differ from the live one? Names compared case-insensitively."""
    with _lock:
        _ensure_migrated()
        new = {e[0].lower(): e[0] for e in _load_edition_entries(edition_id)}
        active = _active_id()
        old = {e[0].lower(): e[0] for e in _load_edition_entries(active)} if active else {}
    added = sorted(v for k, v in new.items() if k not in old)
    removed = sorted(v for k, v in old.items() if k not in new)
    return {
        "active_edition_id": active,
        "is_active": active == edition_id,
        "added": len(added), "removed": len(removed),
        "unchanged": len(new) - len(added),
        "added_sample": added[:sample], "removed_sample": removed[:sample],
    }


def render_edition_page(edition_id: str, page_number: int, zoom: float = 1.5) -> bytes:
    """PNG of one page (1-based) of an edition's PDF, for side-by-side spot-checking."""
    path = edition_pdf_path(edition_id)
    try:
        with fitz.open(path) as doc:
            if page_number < 1 or page_number > doc.page_count:
                raise AppError(404, "FIA_PAGE_OUT_OF_RANGE",
                               f"This PDF has {doc.page_count} pages; page {page_number} doesn't exist.")
            pix = doc[page_number - 1].get_pixmap(matrix=fitz.Matrix(zoom, zoom))
            return pix.tobytes("png")
    except AppError:
        raise
    except Exception:
        logger.exception("FIA page render failed (edition %s, page %s)", edition_id, page_number)
        raise AppError(500, "FIA_PAGE_RENDER_FAILED", "Could not render that page.",
                       "The PDF may be damaged. Download it and check it opens.")


# ---------------------------------------------------------------------------
# migration from the pre-registry layout
# ---------------------------------------------------------------------------

def init_registry() -> None:
    """Called once at startup. Never raises: a migration problem must not stop the API booting."""
    try:
        with _lock:
            _ensure_migrated()
    except Exception:
        logger.exception("FIA registry initialisation failed; screening will use the live cache as-is")


def _ensure_migrated() -> None:
    marker = EDITIONS_DIR / _MIGRATION_MARKER_NAME
    if marker.exists():
        return
    with _lock:
        if marker.exists():
            return
        try:
            _migrate_legacy()
        except Exception:
            logger.exception("FIA legacy migration failed part-way; it will be retried on next start")
            return  # no marker => retried; sha-based de-duplication makes a retry safe
        _atomic_write_text(marker, _now().isoformat())


def _pdf_page_count(data: bytes) -> int | None:
    try:
        with fitz.open(stream=data, filetype="pdf") as doc:
            return doc.page_count
    except Exception:
        return None


def _migrate_legacy() -> None:
    """
    Imports the pre-registry files into the registry, once:
      * every <ts>_redbook.pdf in the old archive folder -> an ARCHIVED edition
      * the live fia_redbook_latest.pdf                   -> the ACTIVE edition
    Nothing in the old locations is modified or deleted.
    """
    known = {m.get("sha256") for m in _all_meta()}

    if FIA_REDBOOK_ARCHIVE_DIR.exists():
        for pdf in sorted(FIA_REDBOOK_ARCHIVE_DIR.glob("*_redbook.pdf")):
            try:
                data = pdf.read_bytes()
                sha = _sha256(data)
                if sha in known:
                    continue
                ts = pdf.name.split("_")[0]
                try:
                    created = datetime.strptime(ts, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                except ValueError:
                    created = _now()
                names_file = pdf.with_name(f"{ts}_names.txt")
                if names_file.exists():
                    entries = _parse_entries_text(names_file.read_text(encoding="utf-8"))
                else:
                    entries = _parse_pdf(pdf).entries
                meta = _legacy_meta(data, entries, created, pdf.name, "legacy-archive")
                meta["activations"] = [{
                    "at": created.isoformat(),
                    "action": "activated (before the edition registry existed; time approximate)",
                    "note": None, "previous_edition_id": None,
                }]
                _store_edition(data, entries, meta)
                known.add(sha)
            except Exception:
                logger.exception("FIA migration: skipping unreadable archive %s", pdf.name)

    if PDF_CACHE.exists():
        data = PDF_CACHE.read_bytes()
        sha = _sha256(data)
        try:
            legacy = json.loads(META_CACHE.read_text(encoding="utf-8")) if META_CACHE.exists() else {}
        except ValueError:
            legacy = {}
        try:
            loaded = datetime.fromisoformat(legacy["loaded_at"])
        except (KeyError, ValueError, TypeError):
            loaded = datetime.fromtimestamp(PDF_CACHE.stat().st_mtime, tz=timezone.utc)
        existing = _find_by_sha(sha)
        if existing:
            meta = existing
        else:
            entries = (_parse_entries_text(NAMES_CACHE.read_text(encoding="utf-8"))
                       if NAMES_CACHE.exists() else _parse_pdf(PDF_CACHE).entries)
            meta = _legacy_meta(data, entries, loaded, legacy.get("original_filename"),
                                legacy.get("source") or "legacy")
            _store_edition(data, entries, meta)
        meta.setdefault("activations", []).append({
            "at": loaded.isoformat(), "action": "activated (before the edition registry existed)",
            "note": None, "previous_edition_id": None,
        })
        _write_meta(meta)
        _atomic_write_text(ACTIVE_POINTER, json.dumps({"edition_id": meta["id"], "activated_at": loaded.isoformat()}))
        logger.info("FIA migration: live Red Book imported as edition %s", meta["id"])


def _legacy_meta(data: bytes, entries, created: datetime, filename: str | None, source: str) -> dict:
    sha = _sha256(data)
    return {
        "id": f"{created.strftime('%Y%m%dT%H%M%SZ')}_{sha[:8]}",
        "original_filename": _clean_filename(filename),
        "source": source, "source_url": None, "sha256": sha, "size_bytes": len(data),
        "page_count": _pdf_page_count(data),
        "names_found": len(entries), "cnics_found": sum(1 for e in entries if e[1]),
        "parse": None,
        "warnings": [{"code": "FIA_LEGACY_IMPORT", "severity": "info",
                      "message": "Imported from the pre-registry cache; parse statistics were not recorded at the time."}],
        "notes": None, "created_at": created.isoformat(), "activations": [], "legacy": True,
    }


# ---------------------------------------------------------------------------
# scraper (optional) and legacy upload entry point
# ---------------------------------------------------------------------------

def find_latest_pdf_url() -> str | None:
    resp = requests.get(FIA_PUBLICATIONS_PAGE, timeout=30)
    resp.raise_for_status()
    pdf_links = re.findall(r'href="([^"]+\.pdf)"', resp.text)
    redbook_links = [
        link for link in pdf_links
        if any(k in link.lower() for k in ("redbook", "red-book", "red_book"))
    ]
    if not redbook_links:
        return None
    link = redbook_links[0]
    return link if link.startswith("http") else FIA_BASE + link


def refresh_cache(auto_activate: bool = True) -> dict:
    """
    Looks for a Red Book on the FIA site and stages it as a new edition.

    With auto_activate=True (the scheduled/combined-refresh behaviour, as
    before) a NEW edition goes live automatically ONLY if it looks sane:
    no high-severity parse warnings, and at least FIA_AUTO_ACTIVATE_MIN_RATIO
    of the live edition's name count. Otherwise it is left staged for a human.
    The Red Book tab's "check FIA website" button passes auto_activate=False.
    """
    url = find_latest_pdf_url()
    checked_at = _now().isoformat()
    if not url:
        return {"status": "NO_PDF_FOUND", "checked_at": checked_at}

    resp = requests.get(url, timeout=60)
    resp.raise_for_status()

    try:
        ed = create_edition(resp.content, original_filename=url, source="scrape", source_url=url)
    except DuplicateEdition as dup:
        return {"status": "UNCHANGED", "source_url": url, "edition_id": dup.edition_id,
                "detail": dup.message, "checked_at": checked_at}

    active = active_edition_id()
    live = _read_meta(active) if active else None
    sane = (not any(w["severity"] == "high" for w in ed["warnings"])
            and (live is None or ed["names_found"] >= FIA_AUTO_ACTIVATE_MIN_RATIO * (live.get("names_found") or 0)))
    if auto_activate and sane:
        activate_edition(ed["id"], confirm_reviewed=True,
                         note="Auto-activated after scrape (passed sanity checks; not individually reviewed).")
        status = "REFRESHED"
        note = "Have a compliance analyst spot-check the parsed names on the FIA Red Book tab."
    else:
        status = "STAGED_NEEDS_REVIEW"
        note = "A new edition was downloaded and staged but NOT made live. Review and activate it on the FIA Red Book tab."
    return {"status": status, "source_url": url, "edition_id": ed["id"],
            "names_found": ed["names_found"], "cnics_found": ed["cnics_found"],
            "warnings": ed["warnings"], "refreshed_at": checked_at, "note": note}


def ingest_uploaded_pdf(pdf_bytes: bytes, original_filename: str | None = None) -> dict:
    """
    Legacy one-call swap (POST /api/admin/fia-redbook/upload): stage the PDF and
    make it live immediately. Prefer the staged flow on the FIA Red Book tab.
    The previous live edition is retained, not deleted.
    """
    ed = create_edition(pdf_bytes, original_filename=original_filename, source="upload")
    ed = activate_edition(ed["id"], confirm_reviewed=True,
                          note="Activated immediately via the legacy upload endpoint.")
    return {
        "status": "INGESTED",
        "edition_id": ed["id"],
        "names_found": ed["names_found"],
        "cnics_found": ed["cnics_found"],
        "ingested_at": ed["last_activated_at"],
        "warnings": ed["warnings"],
        "note": "Spot-check a sample of these names (and CNICs, if this edition's "
                "table includes them) against the PDF before relying on this "
                "edition — table layout varies by year.",
    }


# ---------------------------------------------------------------------------
# screening
# ---------------------------------------------------------------------------

def _load_entries() -> list[tuple[str, str | None]]:
    """Live entries as (name, cnic) pairs (back-compat signature)."""
    return [(n, c) for n, c, _ in _load_entries_full()]


def _load_entries_full() -> list[Entry]:
    if not NAMES_CACHE.exists():
        return []
    return _parse_entries_text(NAMES_CACHE.read_text(encoding="utf-8"))


def check(applicant_name: str, applicant_cnic: str | None = None, threshold: float = 60) -> dict:
    entries = _load_entries_full()
    if not entries:
        return {
            "matched_entry": None,
            "score": None,
            "detail": "FIA Red Book cache not populated — upload a PDF on the FIA Red Book tab first.",
            "source_url": FIA_PUBLICATIONS_PAGE,
            "page_number": None,
            "available": False,
            "near_miss": False,
            "cnic_match": False,
            "list_version": None,
        }

    names = [n for n, _, _ in entries]
    best = matching.find_best_match(applicant_name, names, threshold)

    # CNIC is checked independently of the name score — a shared CNIC is
    # direct identity evidence and should surface even if the name score
    # (e.g. due to a nickname or transliteration this module doesn't know
    # about) happens to land below threshold.
    cnic_hit_name = None
    cnic_near_hit_name = None
    if applicant_cnic:
        for name, cnic, _ in entries:
            if matching.cnic_exact_match(applicant_cnic, cnic):
                cnic_hit_name = name
                break
        if not cnic_hit_name:
            # Only look for a near-match (single-digit typo/OCR slip) once
            # there's no exact hit — a weaker, audit-only signal, never an
            # escalation path of its own. See matching.cnic_near_match.
            for name, cnic, _ in entries:
                if matching.cnic_near_match(applicant_cnic, cnic):
                    cnic_near_hit_name = name
                    break

    page_number = None
    matched_for_page = best.matched_entry or cnic_hit_name
    if matched_for_page and PDF_CACHE.exists():
        pages = {n: p for n, _, p in entries if p is not None}
        page_number = pages.get(matched_for_page)
        if page_number is None:  # pre-registry caches didn't record pages
            page_number = _find_page_for_name(matched_for_page)

    detail = f"Checked against {len(entries)} names in cached Red Book edition. {best.detail}"
    if cnic_hit_name:
        detail += f" CNIC exact match found against entry '{cnic_hit_name}' — treat as confirmed identity evidence."
    elif cnic_near_hit_name:
        detail += (
            f" CNIC differs by a single digit from entry '{cnic_near_hit_name}' — not treated as a match, "
            "but flagged for the near-miss audit log in case it's a transcription error worth a human look."
        )

    return {
        "matched_entry": best.matched_entry or cnic_hit_name,
        "score": best.score,
        "detail": detail,
        "source_url": FIA_PUBLICATIONS_PAGE,
        "page_number": page_number,
        "available": True,
        "near_miss": best.near_miss or bool(cnic_near_hit_name),
        "cnic_match": bool(cnic_hit_name),
        "breakdown": best.breakdown,
        "list_version": _active_id(),
    }


def _find_page_for_name(name: str) -> int | None:
    """Finds which cached PDF page contains the matched name, for evidence rendering."""
    if not PDF_CACHE.exists():
        return None
    with pdfplumber.open(PDF_CACHE) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            if name.lower() in text.lower():
                return i
    return None


def render_matched_page(page_number: int, out_path: Path, zoom: float = 2.0) -> Path | None:
    """
    Renders a specific (0-based) page of the LIVE PDF as a PNG — the 'proof'
    artifact embedded into the evidence PDF for a Red Book hit, since there's
    no live webpage to screenshot the way there is for adverse media.
    """
    if not PDF_CACHE.exists():
        return None
    with fitz.open(PDF_CACHE) as doc:
        if page_number < 0 or page_number >= doc.page_count:
            return None
        pix = doc[page_number].get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        pix.save(out_path)
    return out_path

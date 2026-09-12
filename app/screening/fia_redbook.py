"""
FIA Red Book (Pakistan) — Most Wanted Human Traffickers/Smugglers &
Terrorism Suspects.

Unlike UNSC, this has NO api/feed — it's a PDF published irregularly
(roughly annually) on fia.gov.pk. There's no reliable way to screen this
in true real time; the practical approach is:

  1. A scheduled job (weekly) checks the FIA publications page for a new
     Red Book PDF link and downloads it if the link has changed.
  2. The PDF's text/tables are parsed into a name list (and CNIC, where the
     edition's table includes one) and cached.
  3. A compliance analyst spot-checks the parsed list once per new edition
     — table layouts have changed across editions, so blind trust in the
     parser is not safe, and this code does not claim to solve that; it
     only makes the extraction more resilient than a single regex pass.
  4. Applicant screening then runs against the cached, analyst-approved
     name list, using the shared matching engine in
     app.screening.matching (normalization + multi-algorithm scoring) plus
     an exact-CNIC check where both sides have one.

The page-image rendering (render_matched_page) is what produces the
"proof" artifact for a Red Book hit, since there's no webpage URL to
screenshot — the evidence is a rendered image of the actual PDF page
containing the matched entry.

CACHE FILE FORMAT: NAMES_CACHE stores one entry per line as
"Name|CNIC" — CNIC is empty when the edition's table didn't have one or
extraction couldn't find it for that row. Older caches with plain
one-name-per-line (no "|") are still read correctly (CNIC treated as
absent) for backward compatibility.
"""

import hashlib
import json
import re
from pathlib import Path
from datetime import datetime, timezone
import requests
import pdfplumber
import pymupdf as fitz  # PyMuPDF, for rendering a specific page as an image
from app.config import CACHE_DIR, FIA_REDBOOK_ARCHIVE_DIR
from app.screening import matching

FIA_PUBLICATIONS_PAGE = "https://www.fia.gov.pk/press-pub"
FIA_BASE = "https://www.fia.gov.pk"

PDF_CACHE = CACHE_DIR / "fia_redbook_latest.pdf"
NAMES_CACHE = CACHE_DIR / "fia_redbook_names.txt"
# Small JSON sidecar describing whatever edition is *currently* loaded —
# so a compliance analyst can check what's active (GET /api/admin/fia-redbook/status)
# without having to re-upload anything just to find out.
META_CACHE = CACHE_DIR / "fia_redbook_meta.json"

_NAME_LINE_RE = re.compile(r"^[A-Za-z][A-Za-z .'-]{2,}$")


def _archive_current_edition() -> Path | None:
    """
    Copies the PDF (and parsed names) that are about to be replaced into
    FIA_REDBOOK_ARCHIVE_DIR, timestamped, before overwriting the live cache.

    This is what makes "just upload the new one" safe to do carelessly: the
    old edition is never silently deleted, only superseded. If a past
    screening HIT is ever questioned, you can pull the exact edition that
    was active at the time from the archive rather than reconstructing it.
    """
    if not PDF_CACHE.exists():
        return None
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_pdf = FIA_REDBOOK_ARCHIVE_DIR / f"{ts}_redbook.pdf"
    archive_pdf.write_bytes(PDF_CACHE.read_bytes())
    if NAMES_CACHE.exists():
        (FIA_REDBOOK_ARCHIVE_DIR / f"{ts}_names.txt").write_text(
            NAMES_CACHE.read_text(encoding="utf-8"), encoding="utf-8"
        )
    return archive_pdf


def _write_metadata(source: str, names_found: int, cnics_found: int,
                     original_filename: str | None = None) -> dict:
    meta = {
        "source": source,  # "upload" or "scrape"
        "original_filename": original_filename,
        "names_found": names_found,
        "cnics_found": cnics_found,
        "sha256": hashlib.sha256(PDF_CACHE.read_bytes()).hexdigest(),
        "loaded_at": datetime.now(timezone.utc).isoformat(),
    }
    META_CACHE.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def get_status() -> dict:
    """
    What edition is currently loaded, and when/how it got there — so you
    can confirm a swap worked (or check staleness) without downloading
    anything. Backs GET /api/admin/fia-redbook/status.
    """
    if not META_CACHE.exists():
        return {
            "configured": False,
            "detail": "No Red Book edition cached yet — upload one via "
                       "POST /api/admin/fia-redbook/upload.",
        }
    meta = json.loads(META_CACHE.read_text(encoding="utf-8"))
    meta["configured"] = True
    archived = sorted(FIA_REDBOOK_ARCHIVE_DIR.glob("*_redbook.pdf"))
    meta["previous_editions_archived"] = len(archived)
    return meta


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


def refresh_cache() -> dict:
    """Downloads the latest Red Book PDF and re-parses names. Run weekly."""
    url = find_latest_pdf_url()
    if not url:
        return {"status": "NO_PDF_FOUND", "checked_at": datetime.now(timezone.utc).isoformat()}

    resp = requests.get(url, timeout=60)
    resp.raise_for_status()

    _archive_current_edition()
    PDF_CACHE.write_bytes(resp.content)

    entries = _extract_entries_from_pdf(PDF_CACHE)
    _save_entries(entries)
    cnics_found = sum(1 for _, c in entries if c)
    meta = _write_metadata("scrape", len(entries), cnics_found, original_filename=url)

    return {
        "status": "REFRESHED",
        "source_url": url,
        "names_found": len(entries),
        "cnics_found": cnics_found,
        "refreshed_at": meta["loaded_at"],
        "note": "Have a compliance analyst spot-check parsed names (and CNICs, "
                "if present in this edition) before trusting this edition in production.",
    }


def _extract_entries_from_pdf(pdf_path: Path) -> list[tuple[str, str | None]]:
    """
    Extracts (name, cnic_or_None) pairs from the Red Book PDF.

    Red Book table layouts vary by edition, so this tries strategies in
    order of reliability and falls back gracefully:

      1. Real table structure (page.extract_table()) — if a row has a
         column that looks like a name and (optionally) a column that
         contains a CNIC pattern, use both. This is the most reliable
         path when the PDF actually has table structure.
      2. Plain text fallback — if no table is detected on a page, scan
         its text line by line: a CNIC pattern found near a name-shaped
         line is associated with that line; a name-shaped line with no
         nearby CNIC is still kept (name-only entry).

    This is still fundamentally a best-effort heuristic extraction, not a
    guaranteed-correct parser — the README and get_status()/refresh_cache()
    responses deliberately keep saying so. A compliance analyst reviewing
    a sample of parsed entries against the source PDF after every new
    edition remains a required step, not an optional nicety.
    """
    entries: list[tuple[str, str | None]] = []
    seen_names = set()

    def _maybe_add(name: str, cnic: str | None):
        name = name.strip()
        if not name or not _NAME_LINE_RE.match(name):
            return
        key = name.lower()
        if key in seen_names:
            return
        seen_names.add(key)
        entries.append((name, cnic))

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            table = page.extract_table()
            if table:
                # Guess which column is the name column: the one whose
                # cells most often match the name-shaped regex.
                if table:
                    ncols = max(len(row) for row in table if row)
                    col_hits = [0] * ncols
                    for row in table:
                        for i, cell in enumerate(row or []):
                            if cell and _NAME_LINE_RE.match(cell.strip()):
                                col_hits[i] += 1
                    name_col = col_hits.index(max(col_hits)) if col_hits else 0

                for row in table:
                    if not row or name_col >= len(row) or not row[name_col]:
                        continue
                    name_cell = row[name_col].strip()
                    cnic = None
                    for cell in row:
                        if cell:
                            found = matching.extract_cnic(cell)
                            if found:
                                cnic = found
                                break
                    _maybe_add(name_cell, cnic)
            else:
                text = page.extract_text() or ""
                lines = text.splitlines()
                for i, line in enumerate(lines):
                    for candidate in re.findall(
                        r"\b([A-Z][a-z]+(?:\s[A-Z][a-z]+){1,3})\b", line
                    ):
                        # Look at this line and the next for a CNIC that
                        # likely belongs to the same record.
                        window = line
                        if i + 1 < len(lines):
                            window += " " + lines[i + 1]
                        cnic = matching.extract_cnic(window)
                        _maybe_add(candidate, cnic)

    return entries


def _save_entries(entries: list[tuple[str, str | None]]) -> None:
    lines = [f"{name}|{cnic or ''}" for name, cnic in entries]
    NAMES_CACHE.write_text("\n".join(lines), encoding="utf-8")


def ingest_uploaded_pdf(pdf_bytes: bytes, original_filename: str | None = None) -> dict:
    """
    Use this instead of refresh_cache() when you're uploading the Red Book
    PDF yourself (e.g. downloaded manually from fia.gov.pk and reviewed by
    a compliance analyst first). Skips the scraper entirely.

    This is a straight swap: whatever edition was previously live gets
    archived (see _archive_current_edition) and the upload becomes the new
    active edition — one call, no separate "delete the old one" step.
    """
    _archive_current_edition()
    PDF_CACHE.write_bytes(pdf_bytes)
    entries = _extract_entries_from_pdf(PDF_CACHE)
    _save_entries(entries)
    cnics_found = sum(1 for _, c in entries if c)
    meta = _write_metadata("upload", len(entries), cnics_found, original_filename=original_filename)
    return {
        "status": "INGESTED",
        "names_found": len(entries),
        "cnics_found": cnics_found,
        "ingested_at": meta["loaded_at"],
        "note": "Spot-check a sample of these names (and CNICs, if this edition's "
                "table includes them) against the PDF before relying on this "
                "edition — table layout varies by year.",
    }


def _load_entries() -> list[tuple[str, str | None]]:
    if not NAMES_CACHE.exists():
        return []
    out = []
    for line in NAMES_CACHE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        if "|" in line:
            name, cnic = line.split("|", 1)
            out.append((name, cnic or None))
        else:
            out.append((line, None))  # backward-compat with old cache format
    return out


def check(applicant_name: str, applicant_cnic: str | None = None, threshold: float = 60) -> dict:
    entries = _load_entries()
    if not entries:
        return {
            "matched_entry": None,
            "score": None,
            "detail": "FIA Red Book cache not populated — upload a PDF via /api/admin/fia-redbook/upload first.",
            "source_url": FIA_PUBLICATIONS_PAGE,
            "page_number": None,
            "available": False,
            "near_miss": False,
            "cnic_match": False,
        }

    names = [n for n, _ in entries]
    best = matching.find_best_match(applicant_name, names, threshold)

    # CNIC is checked independently of the name score — a shared CNIC is
    # direct identity evidence and should surface even if the name score
    # (e.g. due to a nickname or transliteration this module doesn't know
    # about) happens to land below threshold.
    cnic_hit_name = None
    if applicant_cnic:
        for name, cnic in entries:
            if matching.cnic_exact_match(applicant_cnic, cnic):
                cnic_hit_name = name
                break

    page_number = None
    matched_for_page = best.matched_entry or cnic_hit_name
    if matched_for_page and PDF_CACHE.exists():
        page_number = _find_page_for_name(matched_for_page)

    detail = f"Checked against {len(entries)} names in cached Red Book edition. {best.detail}"
    if cnic_hit_name:
        detail += f" CNIC exact match found against entry '{cnic_hit_name}' — treat as confirmed identity evidence."

    return {
        "matched_entry": best.matched_entry or cnic_hit_name,
        "score": best.score,
        "detail": detail,
        "source_url": FIA_PUBLICATIONS_PAGE,
        "page_number": page_number,
        "available": True,
        "near_miss": best.near_miss,
        "cnic_match": bool(cnic_hit_name),
        "breakdown": best.breakdown,
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
    Renders a specific PDF page as a PNG image — this is the 'proof' artifact
    embedded into the evidence PDF for a Red Book hit, since there's no live
    webpage to screenshot the way there is for adverse media.
    """
    if not PDF_CACHE.exists():
        return None
    doc = fitz.open(PDF_CACHE)
    if page_number >= len(doc):
        return None
    page = doc[page_number]
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    pix.save(out_path)
    doc.close()
    return out_path

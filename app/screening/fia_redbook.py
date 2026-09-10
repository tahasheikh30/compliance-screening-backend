"""
FIA Red Book (Pakistan) — Most Wanted Human Traffickers/Smugglers &
Terrorism Suspects.

Unlike UNSC, this has NO api/feed — it's a PDF published irregularly
(roughly annually) on fia.gov.pk. There's no reliable way to screen this
in true real time; the practical approach is:

  1. A scheduled job (weekly) checks the FIA publications page for a new
     Red Book PDF link and downloads it if the link has changed.
  2. The PDF's text/tables are parsed into a name list and cached.
  3. A compliance analyst spot-checks the parsed list once per new edition
     (table layouts have changed across editions, so blind trust in the
     parser is not safe).
  4. Applicant screening then runs against the cached, analyst-approved
     name list — same fuzzy-match approach as UNSC.

The page-image rendering (render_matched_page) is what produces the
"proof" artifact for a Red Book hit, since there's no webpage URL to
screenshot — the evidence is a rendered image of the actual PDF page
containing the matched entry.
"""

import re
from pathlib import Path
from datetime import datetime, timezone
import requests
from rapidfuzz import fuzz
import pdfplumber
import pymupdf as fitz  # PyMuPDF, for rendering a specific page as an image
from app.config import CACHE_DIR

FIA_PUBLICATIONS_PAGE = "https://www.fia.gov.pk/press-pub"
FIA_BASE = "https://www.fia.gov.pk"

PDF_CACHE = CACHE_DIR / "fia_redbook_latest.pdf"
NAMES_CACHE = CACHE_DIR / "fia_redbook_names.txt"


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
    PDF_CACHE.write_bytes(resp.content)

    names = _extract_names_from_pdf(PDF_CACHE)
    NAMES_CACHE.write_text("\n".join(names), encoding="utf-8")

    return {
        "status": "REFRESHED",
        "source_url": url,
        "names_found": len(names),
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
        "note": "Have a compliance analyst spot-check parsed names before "
                "trusting this edition in production.",
    }


def _extract_names_from_pdf(pdf_path: Path) -> list[str]:
    """
    Placeholder extraction — Red Book tables vary by edition. Prefer
    page.extract_table() where the PDF has real table structure; fall
    back to this regex heuristic otherwise. Replace/tune once you have
    a sample PDF from the current edition to test against.
    """
    names = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            table = page.extract_table()
            if table:
                for row in table:
                    if row and row[0] and re.match(r"^[A-Za-z ]{4,}$", row[0].strip()):
                        names.append(row[0].strip())
            else:
                text = page.extract_text() or ""
                names.extend(re.findall(r"\b([A-Z][a-z]+(?:\s[A-Z][a-z]+){1,3})\b", text))
    return list(dict.fromkeys(names))  # de-dupe, preserve order


def ingest_uploaded_pdf(pdf_bytes: bytes) -> dict:
    """
    Use this instead of refresh_cache() when you're uploading the Red Book
    PDF yourself (e.g. downloaded manually from fia.gov.pk and reviewed by
    a compliance analyst first). Skips the scraper entirely.
    """
    PDF_CACHE.write_bytes(pdf_bytes)
    names = _extract_names_from_pdf(PDF_CACHE)
    NAMES_CACHE.write_text("\n".join(names), encoding="utf-8")
    return {
        "status": "INGESTED",
        "names_found": len(names),
        "ingested_at": datetime.now(timezone.utc).isoformat(),
        "note": "Spot-check a sample of these names against the PDF before "
                "relying on this edition — table layout varies by year.",
    }


def _load_names() -> list[str]:
    if not NAMES_CACHE.exists():
        return []
    return [n for n in NAMES_CACHE.read_text(encoding="utf-8").splitlines() if n.strip()]


def check(applicant_name: str, threshold: int = 60) -> dict:
    names = _load_names()
    if not names:
        return {
            "matched_entry": None,
            "score": None,
            "detail": "FIA Red Book cache not populated — run refresh_cache() first.",
            "source_url": FIA_PUBLICATIONS_PAGE,
            "page_number": None,
        }

    best_name, best_score = None, 0
    for entry in names:
        score = fuzz.token_sort_ratio(applicant_name.lower(), entry.lower())
        if score > best_score:
            best_name, best_score = entry, score

    page_number = None
    if best_score >= threshold and PDF_CACHE.exists():
        page_number = _find_page_for_name(best_name)

    return {
        "matched_entry": best_name if best_score >= threshold else None,
        "score": best_score,
        "detail": f"Checked against {len(names)} names in cached Red Book edition.",
        "source_url": FIA_PUBLICATIONS_PAGE,
        "page_number": page_number,
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

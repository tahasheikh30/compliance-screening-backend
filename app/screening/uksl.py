"""
UK Sanctions List (UKSL) — Foreign, Commonwealth & Development Office.

Free, official, no-login data — but unlike UNSC/OFAC, there is no fixed
"always this URL" file to download. The list is published on a gov.uk
page as CSV/XML/XLSX/HTML/PDF/TXT, and every time FCDO updates the list
they upload a NEW file to assets.publishing.service.gov.uk with a fresh,
hash-like path — the CSV link on the page today will not be the CSV link
after the next update. So refresh_cache() here does two hops instead of
one: fetch the publication page, find the current CSV asset link, then
download that.

Publication page:
  https://www.gov.uk/government/publications/the-uk-sanctions-list

Background: as of 28 January 2026, the UKSL replaced the old OFSI
"Consolidated List of Asset Freeze Targets" as the UK's single official
sanctions list — see app.screening's write-up sources for details. If
you were pointed at the old OFSI Consolidated List, use this module
instead; the old list is frozen and no longer updated.

Before relying on this in production:
  - Confirm the CSV column headers against a freshly downloaded copy —
    the field layout below (Name 1..Name 6, "Group Type", "Regime Name")
    follows OFSI's last-published Consolidated List Format Guide, which
    UKSL's format guide is expected to closely follow, but has NOT been
    verified against an actual current UKSL CSV export.
  - The link-discovery regex below is a best-effort HTML scrape of a
    government page that could change layout at any time — treat a
    failure to find a CSV link as a hard error requiring investigation,
    not a silent "list unavailable".

Matching is delegated to app.screening.matching, same as UNSC/OFAC/FIA
Red Book. The UKSL CSV does not carry a Pakistani CNIC, so cnic_match is
always False here.
"""

import csv
import io
import re
from datetime import datetime, timezone
import requests
from app.config import CACHE_DIR
from app.screening import matching

PUBLICATION_PAGE_URL = "https://www.gov.uk/government/publications/the-uk-sanctions-list"
CACHE_FILE = CACHE_DIR / "uksl_consolidated.csv"

# Matches an absolute link to a .csv asset hosted on the government's
# asset CDN, e.g. https://assets.publishing.service.gov.uk/media/<id>/UK_Sanctions_List.csv
_CSV_LINK_RE = re.compile(
    r'https://assets\.publishing\.service\.gov\.uk/[^"\']+?\.csv', re.IGNORECASE
)


def _find_current_csv_url() -> str:
    resp = requests.get(PUBLICATION_PAGE_URL, timeout=30)
    resp.raise_for_status()
    match = _CSV_LINK_RE.search(resp.text)
    if not match:
        raise RuntimeError(
            "Could not find a .csv asset link on the UKSL publication page — "
            "the page layout may have changed. Inspect "
            f"{PUBLICATION_PAGE_URL} manually and update _CSV_LINK_RE."
        )
    return match.group(0)


def refresh_cache() -> dict:
    """
    Two-hop refresh: resolve today's CSV asset URL from the publication
    page, then download it. Call from the same daily scheduled job as
    unsc.refresh_cache() / ofac.refresh_cache().
    """
    csv_url = _find_current_csv_url()
    resp = requests.get(csv_url, timeout=30)
    resp.raise_for_status()
    CACHE_FILE.write_bytes(resp.content)
    return {
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
        "bytes": len(resp.content),
        "resolved_csv_url": csv_url,
    }


def _load_names() -> list[str]:
    """
    Parses the cached CSV into a flat list of names. Each row is one
    name variant (primary name or alias) rather than one person — the
    UKSL/OFSI format lists Name 1..Name 6 as separate ordered name-part
    columns per row, with AKAs as their own rows, so this builds one
    space-joined name per row rather than trying to merge rows into
    "one record per person" the way unsc.py does for UN's nested XML.
    """
    if not CACHE_FILE.exists():
        return []

    names = []
    with CACHE_FILE.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        name_columns = [c for c in (reader.fieldnames or []) if re.match(r"Name\s*\d+$", c or "")]
        for row in reader:
            parts = [row.get(col, "") for col in name_columns]
            full_name = " ".join(p.strip() for p in parts if p and p.strip())
            if full_name:
                names.append(full_name)

    return names


def check(applicant_name: str, threshold: float = 60) -> dict:
    """Same contract as app.screening.unsc.check() / ofac.check()."""
    names = _load_names()
    if not names:
        return {
            "matched_entry": None,
            "score": None,
            "detail": "UKSL cache not populated — run refresh_cache() first.",
            "source_url": PUBLICATION_PAGE_URL,
            "available": False,
            "near_miss": False,
            "cnic_match": False,
        }

    best = matching.find_best_match(applicant_name, names, threshold)

    return {
        "matched_entry": best.matched_entry,
        "score": best.score,
        "detail": f"Checked against {len(names)} UK Sanctions List name rows. {best.detail}",
        "source_url": PUBLICATION_PAGE_URL,
        "available": True,
        "near_miss": best.near_miss,
        "cnic_match": False,  # UKSL has no Pakistani-CNIC field to compare against
        "breakdown": best.breakdown,
    }

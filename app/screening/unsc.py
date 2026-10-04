"""
UN Security Council Consolidated Sanctions List.

Free, official, live XML feed — no API key needed. We cache it locally and
refresh on a schedule (see scheduler.py) rather than hitting the UN server
on every single screening request.

Matching is delegated to app.screening.matching, which normalizes names
(honorifics, punctuation, common transliteration variants) and combines
several fuzzy algorithms rather than relying on a single ratio — see that
module's docstring for the reasoning. UNSC entries don't carry a Pakistani
CNIC, so this source never contributes a cnic_match signal; that only
comes from FIA Red Book / internal applicant data.
"""

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import requests
from app.config import CACHE_DIR
from app.screening import matching

UNSC_XML_URL = "https://scsanctions.un.org/resources/xml/en/consolidated.xml"
CACHE_FILE = CACHE_DIR / "unsc_consolidated.xml"


def refresh_cache() -> dict:
    """Downloads the latest UNSC list. Call this from a daily scheduled job."""
    resp = requests.get(UNSC_XML_URL, timeout=30)
    resp.raise_for_status()
    CACHE_FILE.write_bytes(resp.content)
    return {"refreshed_at": datetime.now(timezone.utc).isoformat(), "bytes": len(resp.content)}


def _load_names() -> list[str]:
    """
    Parses cached XML into a flat list of full names (+ known aliases).
    NOTE: the real UN schema nests INDIVIDUAL and ENTITY records with
    FIRST_NAME / SECOND_NAME / THIRD_NAME / FOURTH_NAME and an ALIAS_LIST.
    This covers individuals; extend similarly for ENTITIES if you also
    need to screen corporate applicants.
    """
    if not CACHE_FILE.exists():
        return []

    tree = ET.parse(CACHE_FILE)
    root = tree.getroot()
    names = []

    for individual in root.iter("INDIVIDUAL"):
        parts = [
            individual.findtext("FIRST_NAME", ""),
            individual.findtext("SECOND_NAME", ""),
            individual.findtext("THIRD_NAME", ""),
            individual.findtext("FOURTH_NAME", ""),
        ]
        full_name = " ".join(p for p in parts if p).strip()
        if full_name:
            names.append(full_name)

        for alias in individual.iter("INDIVIDUAL_ALIAS"):
            alias_name = alias.findtext("ALIAS_NAME", "")
            if alias_name:
                names.append(alias_name.strip())

    return names


def check(applicant_name: str, threshold: float = 60) -> dict:
    """
    Returns the best match (if any) above `threshold`, plus the combined
    score, a near_miss flag for audit logging, and a breakdown of the
    individual algorithm scores behind that number. Caller (app/main.py)
    decides HIT / REVIEW / CLEAR cutoffs and whether to log the near miss.
    """
    names = _load_names()
    if not names:
        return {
            "matched_entry": None,
            "score": None,
            "detail": "UNSC cache not populated — run refresh_cache() first.",
            "source_url": UNSC_XML_URL,
            "available": False,
            "near_miss": False,
            "cnic_match": False,
        }

    best = matching.find_best_match(applicant_name, names, threshold)

    return {
        "matched_entry": best.matched_entry,
        "score": best.score,
        "detail": f"Checked against {len(names)} UNSC names/aliases. {best.detail}",
        "source_url": UNSC_XML_URL,
        "available": True,
        "near_miss": best.near_miss,
        "cnic_match": False,  # UNSC has no Pakistani-CNIC field to compare against
        "breakdown": best.breakdown,
    }

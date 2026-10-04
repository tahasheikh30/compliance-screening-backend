"""
OFAC Sanctions Lists (US Treasury) — SDN + Consolidated Non-SDN.

Two free, official, no-login XML feeds — no API key, no scraping:
  - SDN List (Specially Designated Nationals):
      https://www.treasury.gov/ofac/downloads/sdn.xml
  - Consolidated Non-SDN List (sectoral / other-program targets):
      https://www.treasury.gov/ofac/downloads/consolidated/consolidated.xml

Both use OFAC's classic <sdnEntry> schema, where each entry can have a
nested <akaList> of aliases — structurally similar to UNSC's
<INDIVIDUAL>/<INDIVIDUAL_ALIAS> shape, so the parsing approach mirrors
app.screening.unsc closely. One real difference: OFAC's XML declares a
default namespace, and that namespace URI has changed across schema
revisions over the years. Rather than hardcode one, _strip_namespace()
removes it from every tag before lookup, so plain names ("sdnEntry",
"firstName", "aka") work regardless of which URI the file currently
declares.

Before relying on this in production: download a fresh copy of each file,
open it, and confirm the entry/alias field names below still match — do
not trust this docstring or any third-party description of the schema
over an actual sample file. Also sanity-check the parsed entry count
against the totals OFAC publishes for each list (SDN is on the order of
tens of thousands of entries) as a basic parsing smoke test.

Matching is delegated to app.screening.matching, same as UNSC and FIA Red
Book. Neither OFAC list carries a Pakistani CNIC, so cnic_match is always
False here, same reasoning as unsc.py.
"""

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import requests
from app.config import CACHE_DIR
from app.screening import matching

SDN_XML_URL = "https://www.treasury.gov/ofac/downloads/sdn.xml"
CONSOLIDATED_XML_URL = "https://www.treasury.gov/ofac/downloads/consolidated/consolidated.xml"

SDN_CACHE_FILE = CACHE_DIR / "ofac_sdn.xml"
CONSOLIDATED_CACHE_FILE = CACHE_DIR / "ofac_consolidated.xml"

_SOURCES = (
    ("SDN", SDN_XML_URL, SDN_CACHE_FILE),
    ("Consolidated Non-SDN", CONSOLIDATED_XML_URL, CONSOLIDATED_CACHE_FILE),
)


def refresh_cache() -> dict:
    """Downloads both OFAC feeds. Call from the same daily scheduled job as unsc.refresh_cache()."""
    results = {}
    for label, url, cache_file in _SOURCES:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        cache_file.write_bytes(resp.content)
        results[label] = {
            "refreshed_at": datetime.now(timezone.utc).isoformat(),
            "bytes": len(resp.content),
        }
    return results


def _strip_namespace(root):
    for elem in root.iter():
        if "}" in elem.tag:
            elem.tag = elem.tag.split("}", 1)[1]
    return root


def _load_names(cache_file) -> list[str]:
    """Parses one cached OFAC file into a flat list of primary names + aliases."""
    if not cache_file.exists():
        return []

    tree = ET.parse(cache_file)
    root = _strip_namespace(tree.getroot())
    names = []

    for entry in root.iter("sdnEntry"):
        first = entry.findtext("firstName", "")
        last = entry.findtext("lastName", "")
        full_name = " ".join(p for p in (first, last) if p).strip()
        if full_name:
            names.append(full_name)

        for aka in entry.iter("aka"):
            aka_first = aka.findtext("firstName", "")
            aka_last = aka.findtext("lastName", "")
            aka_name = " ".join(p for p in (aka_first, aka_last) if p).strip()
            if aka_name:
                names.append(aka_name)

    return names


def check(applicant_name: str, threshold: float = 60) -> dict:
    """
    Same contract as app.screening.unsc.check(): best match above
    `threshold`, near_miss flag, per-algorithm breakdown. Checks SDN and
    Consolidated separately (rather than merging the name lists) purely
    so the response can tell the caller which of the two lists actually
    matched — the higher-scoring of the two wins.
    """
    per_source = []
    for label, url, cache_file in _SOURCES:
        names = _load_names(cache_file)
        if not names:
            continue
        best = matching.find_best_match(applicant_name, names, threshold)
        per_source.append((best, label, url, len(names)))

    if not per_source:
        return {
            "matched_entry": None,
            "score": None,
            "detail": "OFAC cache not populated — run refresh_cache() first.",
            "source_url": SDN_XML_URL,
            "available": False,
            "near_miss": False,
            "cnic_match": False,
        }

    best, label, url, checked_count = max(per_source, key=lambda t: t[0].score)

    return {
        "matched_entry": best.matched_entry,
        "score": best.score,
        "detail": f"Checked against {checked_count} OFAC {label} names/aliases. {best.detail}",
        "source_url": url,
        "available": True,
        "near_miss": best.near_miss,
        "cnic_match": False,  # OFAC has no Pakistani-CNIC field to compare against
        "breakdown": best.breakdown,
    }

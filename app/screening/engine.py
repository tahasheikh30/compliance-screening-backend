"""
Applicant screening engine.

Port of the n8n "Applicant Screening Engine" workflow: take a name (plus
optional date of birth and nationality), score it against the UN, OFAC, UK and
FIA Red Book records and an open news search, and return every potential match.

Screening is name based. Date of birth and nationality do not filter matches;
the birth year is compared and reported as supporting evidence only, exactly as
in the workflow. Every potential match needs a human to verify it.
"""

import re
from datetime import datetime, timezone

from app.config import MATCH_THRESHOLD, MAX_MATCHES, MAX_THRESHOLD, MIN_THRESHOLD
from app.errors import logger
from app.screening import loader, parsers
from app.screening.names import NameScorer
from concurrent.futures import ThreadPoolExecutor

SOURCE_LABELS = {
    "UNSC": "UN Security Council Consolidated List",
    "OFAC": "OFAC Sanctions Lists (SDN and Consolidated Non-SDN)",
    "UKSL": "UK Sanctions List (FCDO)",
    "FIA_REDBOOK": "FIA Red Book",
    "ADVERSE_MEDIA": "Adverse media (open news search)",
}
SOURCE_ORDER = ("UNSC", "OFAC", "UKSL", "FIA_REDBOOK", "ADVERSE_MEDIA")


def resolve_threshold(value) -> float:
    """A missing, non numeric or out of range (50..100) threshold falls back to the default."""
    try:
        t = float(re.sub(r"[^0-9.]", "", str(value))) if value not in (None, "") else 0.0
    except ValueError:
        t = 0.0
    if not t or t < MIN_THRESHOLD or t > MAX_THRESHOLD:
        return MATCH_THRESHOLD
    return t


def _match_dict(r, best: float, best_name: str, dob_year: str) -> dict:
    if dob_year and r.dob:
        year_match = "Yes" if dob_year in r.dob else "No"
    else:
        year_match = "n/a"
    return {
        "source": r.source_key,
        "list": r.list,
        "id": r.id,
        "score": best,
        "matched_name": best_name,
        "primary_name": r.primary,
        "type": r.type,
        "programs": r.programs,
        "dob": r.dob,
        "dob_year_match": year_match,
        "nationality": r.nationality,
        "listed_on": r.listed_on,
        "remarks": r.remarks[:1200],
        "aliases": [n for n in r.names if n != r.primary][:15],
    }


def match_records(scorer: NameScorer, records: list, threshold: float, dob_year: str) -> list:
    out = []
    for r in records:
        best, best_i = 0.0, 0
        for i, tk in enumerate(r.toks):
            s = scorer.score_tokens(tk)
            if s > best:
                best, best_i = s, i
        if best >= threshold:
            out.append(_match_dict(r, best, r.names[best_i], dob_year))
    return out


def screen(name: str, dob: str = "", nationality: str = "", threshold=None) -> dict:
    """
    Run one screening. Returns a dict with the overall result and one entry per
    source under "sources". A source that could not be loaded is reported with
    status ERROR / NOT_CONFIGURED, never as CLEAR.
    """
    name = str(name or "").strip()
    dob = str(dob or "").strip()
    nationality = str(nationality or "").strip()
    thr = resolve_threshold(threshold)
    m = re.search(r"(\d{4})", dob)
    dob_year = m.group(1) if m else ""

    # sanctions lists and the news search are independent, run them together
    with ThreadPoolExecutor(max_workers=2) as ex:
        news_f = ex.submit(_news, name)
        groups = loader.load_groups()
        news = news_f.result()

    scorer = NameScorer(name)
    all_matches: list = []
    sources: dict = {}
    lists_meta: list = []
    total_records = 0

    for key in loader.SOURCE_KEYS:
        g = groups[key]
        lists_meta.extend(g.meta)
        matches = [] if not g.available else match_records(scorer, g.records, thr, dob_year)
        matches.sort(key=lambda x: -x["score"])
        total_records += len(g.records)
        all_matches.extend(matches)
        published = "; ".join(sorted({str(x.get("published")) for x in g.meta if x.get("published") and x.get("records")}))
        sources[key] = {
            "key": key,
            "label": SOURCE_LABELS[key],
            "available": g.available,
            "error": g.error,
            "records": len(g.records),
            "list_version": published or None,
            "matches": matches,
            "articles": [],
        }

    all_matches.sort(key=lambda x: -x["score"])
    truncated = len(all_matches) > MAX_MATCHES
    kept = all_matches[:MAX_MATCHES]

    media_sources = {
        "key": "ADVERSE_MEDIA", "label": SOURCE_LABELS["ADVERSE_MEDIA"], "available": news["status"] == "OK",
        "error": None if news["status"] == "OK" else news["status"], "records": news["articles_reviewed"],
        "list_version": None, "matches": [], "articles": news["hits"],
    }
    sources["ADVERSE_MEDIA"] = media_sources

    sanctions_hits = len(all_matches)
    media_hits = len(news["hits"])
    return {
        "applicant": {"name": name, "dob": dob, "nationality": nationality},
        "threshold": thr,
        "screened_at": datetime.now(timezone.utc).isoformat(),
        "lists": lists_meta,
        "total_records": total_records,
        "sanctions_hit_count": sanctions_hits,
        "media_hit_count": media_hits,
        "hit_count": sanctions_hits + media_hits,
        "hit": sanctions_hits + media_hits > 0,
        "adverse_media": news,
        "matches": kept,
        "truncated": truncated,
        "sources": sources,
    }


def _news(name: str) -> dict:
    media = {"source": "Google News RSS (open web news search)",
             "query": '"' + name + '" + adverse keywords',
             "status": "", "articles_reviewed": 0, "hits": []}
    try:
        xml = loader.fetch_news(name)
    except Exception as exc:
        logger.warning("Adverse media search failed: %s", type(exc).__name__)
        media["status"] = f"Not available: {type(exc).__name__}: {str(exc)[:160]}"
        return media
    if not xml or "<rss" not in xml:
        media["status"] = "Not available: empty or non-RSS response"
        return media
    reviewed, hits = parsers.parse_news_hits(xml, name)
    media["articles_reviewed"] = reviewed
    media["hits"] = hits
    media["status"] = "OK"
    return media


def source_status(src: dict, fia_required: bool = True) -> str:
    """HIT / REVIEW / CLEAR / NOT_CONFIGURED / ERROR for one source."""
    if not src["available"]:
        if src["key"] == "FIA_REDBOOK":
            return "NOT_CONFIGURED"
        return "ERROR"
    if src["matches"]:
        return "HIT"
    if src["articles"]:
        return "REVIEW"  # news hits are unverified leads
    return "CLEAR"


def overall_status(statuses: dict, fia_required: bool = True) -> str:
    """
    A source that did not run must never resolve to AUTO_CLEAR. The only
    exception is the FIA Red Book when FIA_REQUIRED is false, which mirrors the
    workflow where that list is optional.
    """
    vals = list(statuses.values())
    if "HIT" in vals:
        return "ESCALATE_TO_COMPLIANCE"
    if "REVIEW" in vals:
        return "MANUAL_REVIEW"
    for k, v in statuses.items():
        if v in ("ERROR", "NOT_CONFIGURED"):
            if k == "FIA_REDBOOK" and not fia_required:
                continue
            return "MANUAL_REVIEW"
    return "AUTO_CLEAR"


def describe_source(src: dict, status: str, threshold: float) -> str:
    """One readable sentence for a source's result row."""
    if status in ("ERROR", "NOT_CONFIGURED"):
        return f"{src['label']} was NOT screened. {src['error'] or 'Unavailable.'} Treat this applicant as not yet cleared by this source."
    if src["key"] == "ADVERSE_MEDIA":
        n = len(src["articles"])
        if not n:
            return f"No adverse news found ({src['records']} articles reviewed)."
        return (f"{n} news article(s) mention the applicant alongside an adverse keyword "
                f"({src['records']} articles reviewed). Unverified leads: the person may be someone else with the same name.")
    n = len(src["matches"])
    if not n:
        return f"No match at {threshold:g}% across {src['records']:,} records."
    top = src["matches"][0]
    more = f" and {n - 1} more" if n > 1 else ""
    return (f"{n} potential match(es) at or above {threshold:g}%. Top: {top['primary_name']} "
            f"({top['score']:g}%, ref {top['id']}){more}. Verify date of birth and identifiers before any decision.")

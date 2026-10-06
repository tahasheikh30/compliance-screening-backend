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
from app.screening import loader, names, parsers
from app.screening.parsers import normalize_cnic
from app.screening.names import NameScorer
from concurrent.futures import ThreadPoolExecutor

SOURCE_LABELS = {
    "UNSC": "UN Security Council Consolidated List",
    "OFAC": "OFAC Sanctions Lists (SDN and Consolidated Non-SDN)",
    "UKSL": "UK Sanctions List (FCDO)",
    "FIA_REDBOOK": "FIA Red Book",
    "NACTA": "NACTA Proscribed Persons (Fourth Schedule)",
    "ADVERSE_MEDIA": "Adverse media (open news search)",
}
SOURCE_ORDER = ("UNSC", "OFAC", "UKSL", "FIA_REDBOOK", "NACTA", "ADVERSE_MEDIA")


class UnscreenableName(ValueError):
    """The name cannot be compared with the lists, so screening it would only ever look clear."""


UNSCREENABLE_MESSAGE = ("The name must be written in Latin letters (for example MUHAMMAD ALI KHAN). Names typed in "
                        "Urdu, Arabic or another script cannot be compared with the lists, and would wrongly look clear.")


def check_screenable(name: str) -> None:
    """Refuse a name that would produce no tokens, or that loses letters when normalised."""
    if names.has_unsupported_script(name) or not names.tokens(name):
        raise UnscreenableName(UNSCREENABLE_MESSAGE)


def resolve_threshold(value) -> float:
    """A missing, non numeric or out of range (50..100) threshold falls back to the default."""
    try:
        t = float(re.sub(r"[^0-9.]", "", str(value))) if value not in (None, "") else 0.0
    except ValueError:
        t = 0.0
    if not t or t < MIN_THRESHOLD or t > MAX_THRESHOLD:
        return MATCH_THRESHOLD
    return t


def _match_dict(r, best: float, best_name: str, dob_year: str, cnic: str, father_scorer) -> dict:
    if dob_year and r.dob:
        year_match = "Yes" if dob_year in r.dob else "No"
    else:
        year_match = "n/a"
    # An identity number is decisive when both sides have one. None means it cannot be compared.
    cnic_match = (r.cnic == cnic) if (cnic and r.cnic) else None
    father_match = None
    if father_scorer is not None and r.father:
        father_match = father_scorer.score(r.father) >= FATHER_MATCH_SCORE
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
        "cnic": r.cnic,
        "father_name": r.father,
        "cnic_match": cnic_match,
        "father_match": father_match,
    }


# A father's name counts as matching at or above this score (the same token rules as names).
FATHER_MATCH_SCORE = 80.0


def _rank(m: dict):
    """CNIC matches first, then by name score."""
    return (0 if m.get("cnic_match") else 1, -m["score"])


def match_records(scorer: NameScorer, records: list, threshold: float, dob_year: str,
                  cnic: str = "", father_scorer=None, limit: int | None = None, index=None) -> tuple:
    """
    Returns (matches, total): the best `limit` potential matches (all of them when limit is None),
    ranked CNIC matches first and then by score, and how many records matched in all.

    A record is a potential match when its best name scores at or above the threshold, or
    when the applicant's CNIC equals the record's CNIC (an identity number match is reported
    even if the name was written very differently).

    The result dicts (aliases, remarks, father's name scoring) are only built for the records that
    are kept, so a low threshold on a common name that matches thousands of records stays cheap.

    With `index` (a names.RecordIndex built from these same records) only the records that can score at
    all are visited, in list order; the outcome is identical to scanning every record.
    """
    cands = []   # (rank, record, best score, index of the best name); in record order, so ties keep it
    if index is not None and threshold > 0 and index.size == len(records):
        visit = (records[i] for i in index.candidates(scorer.q, cnic, threshold))
    else:
        visit = records
    for r in visit:
        best, best_i = 0.0, 0
        for i, tk in enumerate(r.toks):
            s = scorer.score_tokens(tk)
            if s > best:
                best, best_i = s, i
        by_cnic = bool(cnic and r.cnic and r.cnic == cnic)
        if best >= threshold or by_cnic:
            cands.append(((0 if by_cnic else 1, -best), r, best, best_i))
    cands.sort(key=lambda c: c[0])   # stable: equal ranks stay in list order
    kept = cands if limit is None else cands[:limit]
    return [_match_dict(r, best, r.names[i], dob_year, cnic, father_scorer) for _, r, best, i in kept], len(cands)


def screen(name: str, dob: str = "", nationality: str = "", threshold=None,
           cnic: str = "", father_name: str = "") -> dict:
    """
    Run one screening. Returns a dict with the overall result and one entry per
    source under "sources". A source that could not be loaded is reported with
    status ERROR / NOT_CONFIGURED, never as CLEAR.
    """
    name = str(name or "").strip()
    check_screenable(name)
    dob = str(dob or "").strip()
    nationality = str(nationality or "").strip()
    thr = resolve_threshold(threshold)
    cnic = normalize_cnic(cnic)          # an invalid CNIC is ignored, never matched
    father_name = str(father_name or "").strip()
    father_scorer = NameScorer(father_name) if father_name else None
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
    sanctions_hits = 0

    for key in loader.SOURCE_KEYS:
        g = groups[key]
        lists_meta.extend(g.meta)
        # a low threshold on a common name must not produce a huge response: keep the best MAX_MATCHES
        matches, found_count = (match_records(scorer, g.records, thr, dob_year, cnic, father_scorer, limit=MAX_MATCHES,
                                           index=g.index)
                                if g.available else ([], 0))
        total_records += len(g.records)
        sanctions_hits += found_count
        all_matches.extend(matches)
        published = "; ".join(sorted({str(x.get("published")) for x in g.meta if x.get("published") and x.get("records")}))
        lists = [loader.list_info(x) for x in g.meta]
        sources[key] = {
            "key": key,
            "label": SOURCE_LABELS[key],
            "available": g.available,
            "error": g.error,
            "records": len(g.records),
            "list_version": published or None,
            # every list behind this source, so it is visible which books were screened
            "lists": lists,
            # some lists of an otherwise available source could not be read (FIA publishes several books)
            "partial": g.available and any(li["status"] != "OK" for li in lists),
            "matches": matches,
            "match_count": found_count,  # true total; "matches" holds at most MAX_MATCHES of them
            "articles": [],
        }

    # The overall best MAX_MATCHES are always among each source's own best MAX_MATCHES.
    all_matches.sort(key=_rank)
    truncated = sanctions_hits > MAX_MATCHES
    kept = all_matches[:MAX_MATCHES]

    media_sources = {
        "key": "ADVERSE_MEDIA", "label": SOURCE_LABELS["ADVERSE_MEDIA"], "available": news["status"] == "OK",
        "error": None if news["status"] == "OK" else news["status"].removeprefix("Not available: "), "records": news["articles_reviewed"],
        "list_version": None, "lists": [], "partial": False, "matches": [], "match_count": 0, "articles": news["hits"],
    }
    sources["ADVERSE_MEDIA"] = media_sources

    media_hits = len(news["hits"])
    return {
        "applicant": {"name": name, "dob": dob, "nationality": nationality, "cnic": cnic, "father_name": father_name},
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
    """HIT / PARTIAL / REVIEW / CLEAR / NOT_CONFIGURED / ERROR for one source."""
    if not src["available"]:
        if src["key"] in ("FIA_REDBOOK", "NACTA"):
            return "NOT_CONFIGURED"
        return "ERROR"
    if src["matches"]:
        return "HIT"
    if src.get("partial"):
        return "PARTIAL"  # nothing found, but part of this source could not be read
    if src["articles"]:
        return "REVIEW"  # news hits are unverified leads
    return "CLEAR"


def overall_status(statuses: dict, fia_required: bool = True, nacta_required: bool = True) -> str:
    """
    A source that did not run must never resolve to AUTO_CLEAR. The only
    exceptions are the FIA Red Book (FIA_REQUIRED=false, which mirrors the workflow where
    that list is optional) and NACTA (NACTA_REQUIRED=false).
    """
    vals = list(statuses.values())
    if "HIT" in vals:
        return "ESCALATE_TO_COMPLIANCE"
    if "REVIEW" in vals:
        return "MANUAL_REVIEW"
    for k, v in statuses.items():
        if v in ("ERROR", "NOT_CONFIGURED", "PARTIAL"):
            if k == "FIA_REDBOOK" and not fia_required:
                continue
            if k == "NACTA" and not nacta_required:
                continue
            return "MANUAL_REVIEW"
    return "AUTO_CLEAR"


def describe_source(src: dict, status: str, threshold: float) -> str:
    """One readable sentence for a source's result row."""
    if status in ("ERROR", "NOT_CONFIGURED"):
        reason = (src["error"] or "The source was unavailable").strip().rstrip(".")
        return f"Not screened. {reason}. Treat this applicant as not yet cleared by this source."
    if src["key"] == "ADVERSE_MEDIA":
        n = len(src["articles"])
        if not n:
            return f"No adverse news found ({src['records']} {'article' if src['records'] == 1 else 'articles'} reviewed)."
        return (f"{n} news article(s) mention the applicant alongside an adverse keyword "
                f"({src['records']} articles reviewed). Unverified leads: the person may be someone else with the same name.")
    unread = [f"{li['list']}: {li['status']}" for li in src.get("lists", []) if li["status"] != "OK"]
    if status == "PARTIAL":
        return ("Incomplete screening. Not fully screened: " + "; ".join(unread)
                + ". Treat this applicant as not yet cleared by this source.")
    n = src.get("match_count", len(src["matches"]))
    if n and unread:
        extra = " Note: some lists of this source were not fully screened. " + "; ".join(unread) + "."
    else:
        extra = ""
    if not n:
        return f"No match at {threshold:g}% across {src['records']:,} {'record' if src['records'] == 1 else 'records'}."
    top = src["matches"][0]
    more = f" and {n - 1} more" if n > 1 else ""
    return (f"{n} potential match(es) at or above {threshold:g}%. Top: {top['primary_name']} "
            f"({top['score']:g}%, ref {top['id']}){more}. Verify date of birth and identifiers before any decision.{extra}")

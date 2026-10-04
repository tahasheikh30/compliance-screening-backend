"""
Live list loading.

The n8n workflow downloaded every list on every run. This module does the same
(same URLs, same order of precedence) but

  * downloads the sources in parallel instead of one after another,
  * isolates failures: one list that cannot be fetched or parsed becomes an
    error on that source only, it never takes down the other sources,
  * optionally keeps the parsed list in memory for LIST_CACHE_TTL_SECONDS so a
    burst of screenings does not re-download tens of megabytes each time.
    Failures are never cached. Set the TTL to 0 for a fresh download every time.

Nothing is written to disk and no API key is needed.
"""

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import requests

from app import config
from app.config import HTTP_USER_AGENT, LIST_CACHE_TTL_SECONDS
from app.errors import logger
from app.screening import nacta_store, parsers

# Same endpoints as the workflow's "List Sources" node.
UN_URL = "https://unsolprodfiles.blob.core.windows.net/publiclegacyxmlfiles/EN/consolidatedLegacyByNAME.xml"
OFAC_BASE = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/"
OFAC_SDN_URL = OFAC_BASE + "SDN.CSV"
OFAC_SDN_ALT_URL = OFAC_BASE + "ALT.CSV"
OFAC_CONS_URL = OFAC_BASE + "CONS_PRIM.CSV"
OFAC_CONS_ALT_URL = OFAC_BASE + "CONS_ALT.CSV"
UK_URL = "https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.xml"
NEWS_URL = "https://news.google.com/rss/search"

NEWS_KEYWORDS = ("arrested OR charged OR convicted OR fraud OR laundering OR terror OR terrorist OR "
                 "sanctions OR smuggling OR trafficking OR corruption OR wanted OR scam")

CONNECT_TIMEOUT = 15
READ_TIMEOUT = 180  # the workflow allowed 180 s per list download
PAGE_READ_TIMEOUT = 60

SOURCE_KEYS = ("UNSC", "OFAC", "UKSL", "FIA_REDBOOK", "NACTA")


class SourceUnavailable(Exception):
    """A list could not be loaded. The message is safe to show to the user."""


@dataclass
class GroupData:
    key: str
    records: list = field(default_factory=list)
    meta: list = field(default_factory=list)      # one dict per list, as in the workflow's listMeta
    error: str | None = None                      # set when the whole source could not be screened
    loaded_at: float = field(default_factory=time.time)

    @property
    def available(self) -> bool:
        return self.error is None


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _get(url: str, read_timeout: float = READ_TIMEOUT, **kwargs) -> requests.Response:
    headers = {"User-Agent": HTTP_USER_AGENT, **kwargs.pop("headers", {})}
    resp = requests.get(url, headers=headers, timeout=(CONNECT_TIMEOUT, read_timeout), **kwargs)
    resp.raise_for_status()
    return resp


def decode_bytes(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def fetch_text(url: str, read_timeout: float = READ_TIMEOUT) -> str:
    return decode_bytes(_get(url, read_timeout).content)


def _short(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:160]}"


def list_info(meta: dict, debug: bool = False) -> dict:
    """
    What the API reports about one list: name, size, date and whether it could be read.
    `debug` adds the address it came from and, for a PDF that was downloaded but gave no
    records, a sample of the text that was read from it. That is only for the admin Lists
    page, so a layout the parser does not understand can be diagnosed from the screen.
    """
    status = str(meta.get("status") or "OK")
    if status.startswith("Not available: "):
        status = status[len("Not available: "):]
    out = {"list": meta.get("list", ""), "records": int(meta.get("records") or 0),
           "published": meta.get("published"), "status": status}
    if meta.get("note"):
        out["note"] = meta["note"]
    if debug:
        out["source"] = meta.get("source") or None
        if meta.get("sample"):
            out["sample"] = meta["sample"]
    return out


# --------------------------------------------------------------------------
# One loader per source
# --------------------------------------------------------------------------

def _load_unsc() -> GroupData:
    xml = fetch_text(UN_URL)
    records, published = parsers.parse_un(xml)
    if not records:
        raise SourceUnavailable("No records could be read from the UN list. The file format may have changed.")
    meta = [{"list": parsers.UN_LABEL, "source": UN_URL, "published": published, "records": len(records)}]
    return GroupData("UNSC", parsers.prepare(records), meta)


def _load_ofac() -> GroupData:
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(fetch_text, u) for u in (OFAC_SDN_URL, OFAC_SDN_ALT_URL, OFAC_CONS_URL, OFAC_CONS_ALT_URL)]
        sdn_csv, sdn_alt, cons_csv, cons_alt = [f.result() for f in futs]
    sdn = parsers.parse_ofac(sdn_csv, sdn_alt, parsers.OFAC_SDN_LABEL)
    cons = parsers.parse_ofac(cons_csv, cons_alt, parsers.OFAC_CONS_LABEL)
    empty = [label for label, recs in ((parsers.OFAC_SDN_LABEL, sdn), (parsers.OFAC_CONS_LABEL, cons)) if not recs]
    if empty:
        raise SourceUnavailable("No records could be read from: " + ", ".join(empty) + ". The file format may have changed.")
    meta = [
        {"list": parsers.OFAC_SDN_LABEL, "source": OFAC_SDN_URL + " (+ aliases)", "published": "Retrieved live", "records": len(sdn)},
        {"list": parsers.OFAC_CONS_LABEL, "source": OFAC_CONS_URL + " (+ aliases)", "published": "Retrieved live", "records": len(cons)},
    ]
    return GroupData("OFAC", parsers.prepare(sdn + cons), meta)


def _load_uksl() -> GroupData:
    xml = fetch_text(UK_URL)
    records, published = parsers.parse_uk(xml)
    if not records:
        raise SourceUnavailable("No records could be read from the UK list. The file format may have changed.")
    meta = [{"list": parsers.UK_LABEL, "source": UK_URL, "published": published, "records": len(records)}]
    return GroupData("UKSL", parsers.prepare(records), meta)


def _fetch_fia_page(url: str) -> dict:
    try:
        return {"data": fetch_text(url, PAGE_READ_TIMEOUT), "error": None}
    except Exception as exc:  # the workflow also continued past a failed FIA page
        return {"data": None, "error": _short(exc)}


def _load_fia_edition(ed: dict) -> tuple:
    """Returns (records, meta) for one Red Book edition. Never raises."""
    url = ed.get("url", "")
    if not url:
        return [], {"list": "FIA Red Book", "source": parsers.FIA_PAGES[0], "published": "n/a", "records": 0,
                    "optional": True, "status": "Not available: " + (ed.get("note") or "no Red Book link found on the FIA website")}
    label = parsers.fia_label(ed.get("title"))
    try:
        data = _get(url, READ_TIMEOUT).content
        text = parsers.pdf_to_text(data)
        if not text.strip():
            raise ValueError("the PDF contains no extractable text (it may be a scanned image)")
    except Exception as exc:
        logger.warning("FIA edition %r (%s) could not be read: %s", label, url, _short(exc))
        return [], {"list": label, "source": url, "published": ed.get("updated") or "n/a", "records": 0,
                    "optional": True, "status": "Not available: " + _short(exc)[:200]}
    records = parsers.parse_redbook(text, ed.get("title") or "Red Book")
    meta = {"list": label, "source": url, "published": ed.get("updated") or "n/a", "records": len(records),
            "optional": True, "status": "OK"}
    if not records:
        meta["status"] = "Downloaded, but no records could be read (the layout is not one the reader understands)"
        meta["sample"] = re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()[:1200]
        logger.warning("FIA edition %r (%s): downloaded %d characters of text but no records could be read",
                       label, url, len(text))
    else:
        logger.info("FIA edition %r (%s): %d records", label, url, len(records))
    return records, meta


def _load_fia() -> GroupData:
    with ThreadPoolExecutor(max_workers=2) as ex:
        pages = list(ex.map(_fetch_fia_page, parsers.FIA_PAGES))
    editions = parsers.find_redbook_editions(pages)
    with ThreadPoolExecutor(max_workers=3) as ex:
        loaded = list(ex.map(_load_fia_edition, editions))
    records = [r for recs, _ in loaded for r in recs]
    meta = [m for _, m in loaded]
    if not records:
        reasons = "; ".join(str(m.get("status", "")).removeprefix("Not available: ") for m in meta)[:300]
        # not an exception: the FIA site is optional in the workflow, the list is simply reported as not screened
        return GroupData("FIA_REDBOOK", [], meta, error="The FIA Red Book could not be loaded. " + reasons)
    return GroupData("FIA_REDBOOK", parsers.prepare(records), meta)


def _read_nacta() -> tuple:
    """
    (text, metadata) of the NACTA list.

    With NACTA_PERSONS_URL set it is downloaded from NACTA and, once it has been read successfully,
    saved as the last good copy. If that download fails, the last good copy is used as long as it is
    no older than NACTA_MAX_AGE_DAYS, with a note saying so. Without a URL, the uploaded file is used.
    """
    url = config.NACTA_PERSONS_URL
    stored = nacta_store.load()
    if url:
        try:
            raw = _get(url, READ_TIMEOUT, headers={"Accept": "application/json, text/csv, application/xml, */*"}).content
            text = decode_bytes(raw)
            records, _info = parsers.parse_nacta_persons(text)
            if not records:
                raise ValueError("the response contained no usable records")
        except Exception as exc:
            reason = _short(exc)
            logger.warning("NACTA download from %s failed: %s", url, reason)
            if stored:
                raw_s, meta_s = stored
                age = nacta_store.age_days(meta_s)
                if age is not None and age <= config.NACTA_MAX_AGE_DAYS:
                    note = f"The live download from NACTA failed ({reason}). Using the last good copy, {int(age)} days old."
                    return decode_bytes(raw_s), {**meta_s, "note": note}
            raise SourceUnavailable(f"The NACTA list could not be downloaded or read from the configured address ({reason}).") from None
        meta = nacta_store.save(raw, "live: " + url, len(records), live=True)
        return text, {**meta, "live": True}
    if not stored:
        raise SourceUnavailable(
            "No NACTA list has been loaded yet. Export the Fourth Schedule list as JSON or CSV from nfs.nacta.gov.pk "
            "and upload it on the Lists page, or set NACTA_PERSONS_URL to download it automatically.")
    raw, meta = stored
    return decode_bytes(raw), meta


def _load_nacta() -> GroupData:
    text, meta = _read_nacta()
    try:
        records, _info = parsers.parse_nacta_persons(text)
    except ValueError as exc:
        raise SourceUnavailable(f"The NACTA file could not be read: {exc}") from None
    if not records:
        raise SourceUnavailable("The NACTA file contains no usable records (no names found).")
    entry = {"list": parsers.NACTA_LABEL, "source": config.NACTA_PERSONS_URL or meta.get("filename") or "uploaded file",
             "published": "Retrieved live" if meta.get("live") else (meta.get("uploaded_at") or "n/a")[:10],
             "records": len(records)}
    if meta.get("note"):
        entry["note"] = meta["note"]
    age = nacta_store.age_days(meta)
    if age is not None and age > config.NACTA_MAX_AGE_DAYS:
        entry["status"] = f"Out of date: this copy was loaded {int(age)} days ago (limit {config.NACTA_MAX_AGE_DAYS:g}). Upload a fresh export"
    return GroupData("NACTA", parsers.prepare(records), [entry])


_LOADERS = {"UNSC": _load_unsc, "OFAC": _load_ofac, "UKSL": _load_uksl, "FIA_REDBOOK": _load_fia, "NACTA": _load_nacta}


# --------------------------------------------------------------------------
# Cache + public API
# --------------------------------------------------------------------------

_cache: dict = {}
_last_attempt: dict = {}   # the most recent load of each source, including failures, for the Lists page
_locks = {k: threading.Lock() for k in SOURCE_KEYS}


def _fresh(entry) -> bool:
    return bool(entry) and LIST_CACHE_TTL_SECONDS > 0 and (time.time() - entry.loaded_at) < LIST_CACHE_TTL_SECONDS


def load_group(key: str) -> GroupData:
    """Load one source. Always returns a GroupData; failures come back as .error."""
    entry = _cache.get(key)
    if _fresh(entry):
        return entry
    with _locks[key]:
        entry = _cache.get(key)
        if _fresh(entry):
            return entry
        try:
            data = _LOADERS[key]()
        except SourceUnavailable as exc:
            logger.warning("%s unavailable: %s", key, exc)
            data = GroupData(key, error=str(exc), meta=[{"list": key, "source": "", "published": "n/a", "records": 0,
                                                       "status": "Not available: " + str(exc)}])
        except Exception as exc:
            logger.exception("%s could not be loaded", key)
            msg = f"The list could not be downloaded or read ({_short(exc)})."
            data = GroupData(key, error=msg, meta=[{"list": key, "source": "", "published": "n/a", "records": 0,
                                                   "status": "Not available: " + msg}])
        _last_attempt[key] = data
        if data.available:
            _cache[key] = data
        return data


def load_groups() -> dict:
    """Load every source in parallel."""
    with ThreadPoolExecutor(max_workers=len(SOURCE_KEYS)) as ex:
        futs = {k: ex.submit(load_group, k) for k in SOURCE_KEYS}
        return {k: f.result() for k, f in futs.items()}


def clear_cache(key: str | None = None) -> None:
    if key is None:
        _cache.clear()
        _last_attempt.clear()
    else:
        _cache.pop(key, None)
        _last_attempt.pop(key, None)


def cache_status() -> dict:
    """
    For the Lists page: per source, whether it is held in memory, and every list behind it with its
    own record count and status, taken from the most recent load (successful or not). A source that
    has not been loaded since the server started has no lists yet.
    """
    out = {}
    for k in SOURCE_KEYS:
        cached = _cache.get(k)
        last = _last_attempt.get(k)
        entry = {"cached": bool(cached), "records": len(cached.records) if cached else 0,
                 "age_seconds": round(time.time() - cached.loaded_at) if cached else None,
                 "error": last.error if last else None,
                 "lists": [list_info(m, debug=True) for m in last.meta] if last else []}
        out[k] = entry
    return out


def build_news_query(name: str) -> str:
    return '"' + str(name or "").strip() + '" (' + NEWS_KEYWORDS + ")"


def fetch_news(name: str) -> str:
    """Google News RSS for the applicant plus adverse keywords. Raises on failure."""
    resp = _get(NEWS_URL, PAGE_READ_TIMEOUT,
                params={"q": build_news_query(name), "hl": "en-US", "gl": "US", "ceid": "US:en"})
    return decode_bytes(resp.content)

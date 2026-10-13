"""
Members of Pakistan's assemblies, read from the public member lists (Wikipedia, which keeps one table per assembly,
and the National Assembly's own member table).

Wikidata alone misses many sitting members, so these tables fill the gap. Nothing here is specific to one page:
`members_from_html` finds every table that has a name column, works out which columns are the name, constituency
and party from the header text, and takes the rows. A page that changes its layout may yield fewer rows (or none),
which is reported per source on the Lists page instead of failing the whole PEP source.
"""

import json
import re
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from app import config

NATIONAL, PROVINCIAL = "National", "Provincial"

# Each assembly: the page titles to try (newest term first; an older term is still within the look-back for
# people who have just left), the level, and the province for provincial ones.
WIKI_SOURCES = [
    {"key": "na16", "label": "National Assembly (16th)", "level": NATIONAL, "province": "",
     "titles": ["List of members of the 16th National Assembly of Pakistan"]},
    {"key": "na15", "label": "National Assembly (15th)", "level": NATIONAL, "province": "",
     "titles": ["List of members of the 15th National Assembly of Pakistan"]},
    {"key": "senate", "label": "Senate", "level": NATIONAL, "province": "",
     "titles": ["List of members of the Senate of Pakistan", "List of current senators of Pakistan",
                "List of senators of Pakistan"]},
    {"key": "punjab16", "label": "Punjab Assembly (16th)", "level": PROVINCIAL, "province": "Punjab",
     "titles": ["List of members of the 16th Provincial Assembly of the Punjab",
                "List of members of the 16th Provincial Assembly of Punjab"]},
    {"key": "punjab15", "label": "Punjab Assembly (15th)", "level": PROVINCIAL, "province": "Punjab",
     "titles": ["List of members of the 15th Provincial Assembly of the Punjab",
                "List of members of the 15th Provincial Assembly of Punjab",
                "15th Provincial Assembly of the Punjab", "15th Provincial Assembly of Punjab"]},
    {"key": "sindh16", "label": "Sindh Assembly (16th)", "level": PROVINCIAL, "province": "Sindh",
     "titles": ["List of members of the 16th Provincial Assembly of Sindh"]},
    {"key": "sindh15", "label": "Sindh Assembly (15th)", "level": PROVINCIAL, "province": "Sindh",
     "titles": ["List of members of the 15th Provincial Assembly of Sindh"]},
    {"key": "kp12", "label": "Khyber Pakhtunkhwa Assembly (12th)", "level": PROVINCIAL, "province": "Khyber Pakhtunkhwa",
     "titles": ["List of members of the 12th Provincial Assembly of Khyber Pakhtunkhwa"]},
    {"key": "kp11", "label": "Khyber Pakhtunkhwa Assembly (11th)", "level": PROVINCIAL, "province": "Khyber Pakhtunkhwa",
     "titles": ["List of members of the 11th Provincial Assembly of Khyber Pakhtunkhwa"]},
    {"key": "balochistan12", "label": "Balochistan Assembly (12th)", "level": PROVINCIAL, "province": "Balochistan",
     "titles": ["List of members of the 12th Provincial Assembly of Balochistan"]},
    {"key": "balochistan11", "label": "Balochistan Assembly (11th)", "level": PROVINCIAL, "province": "Balochistan",
     "titles": ["List of members of the 11th Provincial Assembly of Balochistan"]},
    {"key": "gb", "label": "Gilgit-Baltistan Assembly", "level": PROVINCIAL, "province": "Gilgit-Baltistan",
     "titles": ["List of members of the Gilgit-Baltistan Assembly", "Gilgit-Baltistan Assembly",
                "Gilgit-Baltistan Legislative Assembly"]},
    {"key": "ajk", "label": "Azad Jammu and Kashmir Assembly", "level": PROVINCIAL, "province": "Azad Jammu and Kashmir",
     "titles": ["List of members of the Azad Jammu and Kashmir Legislative Assembly",
                "Azad Jammu and Kashmir Legislative Assembly"]},
]

# The assemblies' own websites. parser "table": the page has a member table (found by its header text);
# parser "cards": the page has one link per member reading "Name PK-12 Constituency" (the KP Assembly's layout);
# parser "pairs": a member's name is followed by a line with the seat, "PS-124 Karachi Central-III" (Sindh) or
# "PP-262 (Layyah-I) | Layyah" (Punjab, where the name line may read "Name | email");
# parser "profiles": one link per member to a /member-profile/ page whose text starts with the name (Balochistan).
OFFICIAL_SOURCES = [
    {"key": "na_official", "label": "National Assembly website (na.gov.pk)", "level": NATIONAL, "province": "",
     "url": "https://na.gov.pk/en/all_members.php", "parser": "table"},
    {"key": "senate_official", "label": "Senate website (senate.gov.pk)", "level": NATIONAL, "province": "",
     "url": "https://senate.gov.pk/en/current_members.php", "parser": "table"},
    {"key": "kp_official", "label": "Khyber Pakhtunkhwa Assembly website (pakp.gov.pk)", "level": PROVINCIAL,
     "province": "Khyber Pakhtunkhwa", "url": "https://www.pakp.gov.pk/members/", "parser": "cards"},
    # the 33 is the assembly term on that site (2024 - till date); a new term gets a new number
    {"key": "sindh_official", "label": "Sindh Assembly website (pas.gov.pk)", "level": PROVINCIAL,
     "province": "Sindh", "url": "https://www.pas.gov.pk/members/byname/33", "parser": "pairs"},
    # the 22 is the assembly term on that site; the page may refuse automated access in its robots.txt, in which
    # case the Lists page says so and the last saved members stay
    {"key": "punjab_official", "label": "Punjab Assembly website (pap.gov.pk)", "level": PROVINCIAL,
     "province": "Punjab", "url": "https://www.pap.gov.pk/members/listing/en/22?byname=true&limit=all", "parser": "pairs"},
    {"key": "balochistan_official", "label": "Balochistan Assembly website (pabalochistan.gov.pk)",
     "level": PROVINCIAL, "province": "Balochistan", "url": "https://pabalochistan.gov.pk/list-members",
     "parser": "profiles"},
    # columns: Tenure, Member Name, Political Party, Constituency, Seat Type, District. The Tenure column ("2023-2025")
    # can list members of earlier terms, so rows whose tenure ended long ago are dropped (see _tenure_is_recent)
    {"key": "gb_official", "label": "Gilgit-Baltistan Assembly website (gba.gov.pk)", "level": PROVINCIAL,
     "province": "Gilgit-Baltistan", "url": "https://gba.gov.pk/members/", "parser": "table"},
]

# Assemblies whose official member list is not set up yet. They are shown on the Lists page as such (instead of
# being silently missing) and keep their last saved members. Add each with PEP_EXTRA_SOURCES once its page is known.
PENDING_OFFICIAL = [
    {"key": "ajk_official", "label": "Azad Jammu and Kashmir Assembly website"},
]


def official_sources() -> tuple:
    """(sources, pending): the built-in official sources with PEP_EXTRA_SOURCES applied on top."""
    by_key = {src["key"]: dict(src) for src in OFFICIAL_SOURCES}
    if config.PEP_EXTRA_SOURCES:
        try:
            extra = json.loads(config.PEP_EXTRA_SOURCES)
        except ValueError:
            extra = []
        for src in extra if isinstance(extra, list) else []:
            if isinstance(src, dict) and src.get("key") and str(src.get("url", "")).startswith("https://"):
                by_key[src["key"]] = {"level": PROVINCIAL, "province": "", "parser": "table", "label": src["key"], **src}
    pending = [p for p in PENDING_OFFICIAL if p["key"] not in by_key]
    return list(by_key.values()), pending


def source_signature() -> str:
    """Which assembly sources this server reads. A saved copy made from a different set is refetched after a deploy."""
    sources = list(WIKI_SOURCES) if config.PEP_WIKIPEDIA_FALLBACK else []
    official, pending = official_sources()
    return ",".join(sorted(f'{s["key"]}={s.get("url", "")}' for s in sources + official + pending))


# a member list with fewer rows than this is a layout we did not understand, not a real assembly
MIN_MEMBERS = 10


# --------------------------------------------------------------------------
# HTML tables
# --------------------------------------------------------------------------

class _Tables(HTMLParser):
    """Every <table> as a grid of (text, is_header) cells, with rowspan and colspan expanded."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: list = []
        self._stack: list = []      # open tables: {"rows": [], "row": None, "cell": None, "pending": {}}
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "sup"):
            self._skip += 1
        elif tag == "table":
            self._stack.append({"rows": [], "row": None, "cell": None, "pending": {}})
        elif not self._stack:
            return
        elif tag == "tr":
            t = self._stack[-1]
            t["row"] = []
            t["rows"].append(t["row"])
            self._fill(t)
        elif tag in ("td", "th") and self._stack[-1]["row"] is not None:
            t = self._stack[-1]
            self._fill(t)
            t["cell"] = {"text": [], "th": tag == "th", "rs": _int(a.get("rowspan")), "cs": _int(a.get("colspan"))}
        elif tag == "br" and self._stack[-1]["cell"] is not None:
            self._stack[-1]["cell"]["text"].append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "sup"):
            self._skip = max(0, self._skip - 1)
        elif tag in ("td", "th") and self._stack:
            self._close_cell(self._stack[-1])
        elif tag == "tr" and self._stack:
            t = self._stack[-1]
            self._close_cell(t)
            self._fill(t)
            t["row"] = None
        elif tag == "table" and self._stack:
            t = self._stack.pop()
            self._close_cell(t)
            self.tables.append(t["rows"])

    def handle_data(self, data):
        if self._stack and self._stack[-1]["cell"] is not None and not self._skip:
            self._stack[-1]["cell"]["text"].append(data)

    @staticmethod
    def _fill(t):
        """Cells carried down from a rowspan above sit at their column before new cells are added."""
        row = t["row"]
        if row is None:
            return
        while len(row) in t["pending"]:
            col = len(row)
            text, th, left = t["pending"][col]
            row.append((text, th))
            if left <= 1:
                del t["pending"][col]
            else:
                t["pending"][col] = (text, th, left - 1)

    def _close_cell(self, t):
        c = t["cell"]
        if c is None or t["row"] is None:
            return
        t["cell"] = None
        text = re.sub(r"\s+", " ", "".join(c["text"])).strip()
        for _ in range(min(c["cs"], 20)):
            col = len(t["row"])
            t["row"].append((text, c["th"]))
            if c["rs"] > 1:
                t["pending"][col] = (text, c["th"], min(c["rs"], 200) - 1)
            self._fill(t)


def _int(v) -> int:
    try:
        return max(1, int(str(v).strip()))
    except (TypeError, ValueError):
        return 1


def tables(html: str) -> list:
    p = _Tables()
    p.feed(html)
    p.close()
    return p.tables


# --------------------------------------------------------------------------
# Rows to members
# --------------------------------------------------------------------------

_NAME_HEAD = re.compile(r"\b(member|members|name|mna|mpa|senator|elected|representative)\b", re.I)
_NOT_NAME = re.compile(r"\b(constituency|party|province|district|seat|no\b|number|image|photo|picture|colou?r|notes?|"
                       r"reason|date|remarks|assumed|term|since|status|gender|reserved|type)\b", re.I)
_SEAT_HEAD = re.compile(r"\b(constituency|seat|district|region|province)\b", re.I)
_TENURE_HEAD = re.compile(r"\b(tenure|period)\b", re.I)
_STILL_SERVING = re.compile(r"\b(present|till date|to date|current|incumbent|ongoing)\b", re.I)
_PARTY_HEAD = re.compile(r"\b(party|affiliation|parliamentary|bloc|alliance)\b", re.I)

_VACANT = re.compile(r"^(vacant|none|n/?a|tbd|tba|-+|—|–|\?)$|\b(seat vacant|vacant seat|disqualified|"
                     r"deceased|died|resigned|de-?notified|unseated)\b", re.I)
_REF = re.compile(r"\[[^\]]{0,20}\]")
_TITLES = re.compile(r"^((dr|prof|professor|engr|engineer|mr|mrs|ms|miss|maj|major|gen|general|col|colonel|capt|captain|"
                     r"lt|brig|retd|rtd|r|haji|hafiz|qari|maulana|mufti|justice|barrister|advocate|begum)\.?\s+)+", re.I)
_HONOR = re.compile(r"^((mian|sardar|malik|chaudhry|choudhry|chaudhary|ch|khawaja|sahibzada|nawab|raja|rana|pir|"
                    r"makhdoom|syed|sayyid|sheikh|shaikh|khan bahadur|mir|sardarzada|nawabzada|makhdoomzada)\.?\s+)+", re.I)


def _clean_name(raw: str) -> list:
    """The member's name as printed, plus the form without titles (a customer may give either)."""
    s = _REF.sub("", raw or "").strip()
    if _VACANT.search(s):
        return []                                   # "Vacant", "X (died)", "X (disqualified)"
    s = re.sub(r"\([^)]*\)", " ", s)              # (died), (PTI), (Nawab) etc.
    s = re.sub(r"[†‡*# ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" ,;:-")
    if not s or _VACANT.search(s) or len(s) < 4 or len(s) > 80 or not re.search(r"[A-Za-z]{2}", s):
        return []
    if sum(ch.isdigit() for ch in s) > 2:
        return []
    out = [s]
    stripped = _TITLES.sub("", s)
    for cand in (stripped, _HONOR.sub("", stripped)):
        if cand != s and len(cand.split()) >= 2 and cand not in out:
            out.append(cand)
    return out


def _header_row(rows: list):
    """(index, labels) of the header: the first of the top rows that names a member column."""
    for i, row in enumerate(rows[:4]):
        labels = [t for t, _th in row]
        if any(_NAME_HEAD.search(t) and not _NOT_NAME.search(t) for t in labels):
            return i, labels
    return None


def _tenure_is_recent(text: str, today: date | None = None) -> bool:
    """
    False when a Tenure cell ("2015-2020") shows the member left office more than PEP_LOOKBACK_YEARS ago. A cell that
    says "present" or "till date", or that names no year, is kept: a member is never dropped on a guess.
    """
    if not text or _STILL_SERVING.search(text):
        return True
    years = [int(y) for y in re.findall(r"\b(?:19|20)\d{2}\b", text)]
    if not years:
        return True
    today = today or datetime.now(timezone.utc).date()
    return max(years) >= today.year - config.PEP_LOOKBACK_YEARS


def members_from_html(html: str, today: date | None = None) -> list:
    """[{name, names, constituency, party}] from every table with a member-name column."""
    out = []
    for rows in tables(html):
        head = _header_row(rows)
        if not head:
            continue
        hi, labels = head
        name_col = next((c for c, t in enumerate(labels) if _NAME_HEAD.search(t) and not _NOT_NAME.search(t)), None)
        seat_col = next((c for c, t in enumerate(labels) if c != name_col and _SEAT_HEAD.search(t)), None)
        party_col = next((c for c, t in enumerate(labels) if c != name_col and _PARTY_HEAD.search(t)), None)
        tenure_col = next((c for c, t in enumerate(labels) if c != name_col and _TENURE_HEAD.search(t)), None)
        for row in rows[hi + 1:]:
            if len(row) <= name_col or all(th for _t, th in row if _t) and len(row) <= 2:
                continue            # a section heading row ("Reserved seats for women"), not a member
            names = _clean_name(row[name_col][0])
            if not names:
                continue
            if tenure_col is not None and len(row) > tenure_col and not _tenure_is_recent(row[tenure_col][0], today):
                continue            # sat in an earlier term and left office long ago
            out.append({"name": names[0], "names": names,
                        "constituency": row[seat_col][0] if seat_col is not None and len(row) > seat_col else "",
                        "party": row[party_col][0] if party_col is not None and len(row) > party_col else ""})
    # one entry per name within a page
    seen, uniq = set(), []
    for m in out:
        k = m["name"].lower()
        if k not in seen:
            seen.add(k)
            uniq.append(m)
    return uniq


class _Links(HTMLParser):
    """Every <a href> as (href, text)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list = []
        self._href = None
        self._text: list = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href") or ""
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None


_CARD = re.compile(r"^(?P<name>.+?)\s+(?P<seat>(?:PK|PP|PS|PB|NA|WR|MR|RW|GB|LA)[-\s]?\d+[A-Za-z]?)\b[-\s]*(?P<place>.*)$")


def members_from_cards(html: str) -> list:
    """[{name, names, constituency, party}] from links reading "Name PK-12 Constituency" under a /members/ address."""
    parser = _Links()
    parser.feed(html)
    out, seen = [], set()
    for href, text in parser.links:
        if "/members/" not in href:
            continue
        m = _CARD.match(text)
        if not m:
            continue
        names = _clean_name(m.group("name"))
        if not names or names[0].lower() in seen:
            continue
        seen.add(names[0].lower())
        out.append({"name": names[0], "names": names,
                    "constituency": (m.group("seat") + " " + m.group("place")).strip(), "party": ""})
    return out


class _TextNodes(HTMLParser):
    """Every non-empty text node of a page, in order (scripts and styles skipped)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.nodes: list = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        text = " ".join(data.split())
        if text and not self._skip:
            self.nodes.append(text)


_SEAT_LINE = re.compile(r"^(?:PS|PP|PK|PB|NA|RSW|RSM|NM|WR|MR|W)[-\s]?\d+\b")


def members_from_pairs(html: str) -> list:
    """Members from a page where each name is directly followed by its seat line (PS-124 Karachi Central-III)."""
    parser = _TextNodes()
    parser.feed(html)
    nodes, out, seen = parser.nodes, [], set()
    for i, node in enumerate(nodes):
        if i == 0 or not _SEAT_LINE.match(node) or _SEAT_LINE.match(nodes[i - 1]):
            continue
        raw = nodes[i - 1].split(" | ")[0]            # Punjab prints "Name | email address"
        names = _clean_name(raw)
        if not names or len(raw) < 3:
            continue
        key = (names[0].lower(), node.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": names[0], "names": names, "constituency": node, "party": ""})
    return out


class _LinkNodes(HTMLParser):
    """Every <a href> with the text nodes inside it."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list = []
        self._href = None
        self._nodes: list = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href") or ""
            self._nodes = []

    def handle_data(self, data):
        text = " ".join(data.split())
        if self._href is not None and text:
            self._nodes.append(text)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, self._nodes))
            self._href = None


def members_from_profiles(html: str) -> list:
    """Members from links to /member-profile/ pages: the name is the first text after the "Member Name" label."""
    parser = _LinkNodes()
    parser.feed(html)
    out, seen = [], set()
    for href, nodes in parser.links:
        if "member-profile" not in href or not nodes:
            continue
        first = re.sub(r"^\s*Member\s*Name\s*:?\s*", "", nodes[0], flags=re.I)
        if not first and len(nodes) > 1:
            first = nodes[1]
        first = re.sub(r"\s+N-?A\s*$", "", first).strip()          # "N-A" is the empty portfolio field
        names = _clean_name(first)
        if not names or names[0].lower() in seen:
            continue
        seen.add(names[0].lower())
        text = " ".join(nodes)
        seat = re.search(r"Constituency:\s*(\S+)", text)
        out.append({"name": names[0], "names": names, "constituency": seat.group(1) if seat else "", "party": ""})
    return out


_SHOWING = re.compile(r"Showing\s*(?:<[^>]+>)?\s*\d+\s*(?:<[^>]+>)?\s*-\s*(?:<[^>]+>)?\s*\d+\s*(?:<[^>]+>)?\s*of\s*(?:<[^>]+>)?\s*(\d+)", re.I)


def expected_total(html: str):
    """The record count a paged site announces ("Showing 1 - 10 of 369 Records"), or None when it announces none."""
    m = _SHOWING.search(html or "")
    return int(m.group(1)) if m else None


def check_complete(html: str, members: list) -> None:
    """Raise when the page says it has more members than were read (the site showed only one page of them)."""
    total = expected_total(html)
    if total and len(members) < total * 0.98:
        raise ValueError(f"only {len(members)} of the {total} members the page lists were read (it shows them in pages)")


_PARSERS = {"table": members_from_html, "cards": members_from_cards,
            "pairs": members_from_pairs, "profiles": members_from_profiles}


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def _wiki_html(get, api_url: str, title: str) -> str:
    raw = get(api_url, {"action": "parse", "page": title, "prop": "text", "format": "json", "formatversion": "2",
                        "redirects": "1", "disabletoc": "1"})
    parsed = json.loads(raw).get("parse")
    if not parsed:
        raise ValueError("page not found")
    text = parsed.get("text")
    return text.get("*", "") if isinstance(text, dict) else (text or "")


def fetch_assemblies(get, wiki_api_url: str, sources=None, official=None) -> tuple:
    """
    (assemblies, report). `get(url, params)` returns response text. Each source is tried on its own: one page that
    is gone or looks different costs only that assembly. `assemblies` is [{key, label, level, province, source,
    members}]; `report` is [{key, label, source, count, error}] for the Lists page.
    """
    assemblies, report = [], []
    if sources is None:
        sources = WIKI_SOURCES if config.PEP_WIKIPEDIA_FALLBACK else []
    pending = []
    if official is None:
        official, pending = official_sources()
    for src in sources:
        err, got, used = "page not found", [], ""
        for title in src["titles"]:
            try:
                got = members_from_html(_wiki_html(get, wiki_api_url, title))
            except Exception as exc:          # noqa: BLE001  (network, HTTP status, bad JSON, missing page)
                err = _why(exc)
                continue
            used = title
            if len(got) >= MIN_MEMBERS:
                break
            err = f"only {len(got)} member rows could be read"
        if len(got) >= MIN_MEMBERS:
            assemblies.append({"key": src["key"], "label": src["label"], "level": src["level"],
                               "province": src["province"], "source": "en.wikipedia.org: " + used, "members": got})
            report.append({"key": src["key"], "label": src["label"], "source": used, "count": len(got), "error": "",
                           "province": src["province"]})
        else:
            report.append({"key": src["key"], "label": src["label"], "source": used or src["titles"][0],
                           "count": 0, "error": err, "province": src["province"]})
    for src in official:
        try:
            page = get(src["url"], None)
            got = _PARSERS.get(src.get("parser", "table"), members_from_html)(page)
            check_complete(page, got)
            if len(got) < MIN_MEMBERS:
                raise ValueError(f"only {len(got)} member rows could be read")
            assemblies.append({"key": src["key"], "label": src["label"], "level": src["level"],
                               "province": src["province"], "source": src["url"], "members": got})
            report.append({"key": src["key"], "label": src["label"], "source": src["url"], "count": len(got), "error": "",
                           "province": src["province"]})
        except Exception as exc:              # noqa: BLE001
            report.append({"key": src["key"], "label": src["label"], "source": src["url"], "count": 0, "error": _why(exc),
                           "province": src["province"]})
    for src in pending:
        report.append({"key": src["key"], "label": src["label"], "source": "", "count": 0,
                       "error": "official member list not set up yet"})
    return assemblies, report


def plain_reason(error: str) -> str:
    """A short, plain-language reason for the Lists page; the full error text is kept for the details view."""
    e = (error or "").lower()
    if "robots.txt" in e:
        return "the site's robots.txt does not allow automated access"
    if any(k in e for k in ("nameresolution", "name or service", "getaddrinfo", "resolve")):
        return "the site's address could not be found"
    if any(k in e for k in ("timeout", "timed out", "max retries", "connection", "connect")):
        return "the site did not answer the server"
    m = re.search(r"\b(4\d\d|5\d\d)\b", e)
    if m:
        return {"404": "the page was not found", "403": "the site refused access", "429": "the site asked for fewer requests"
                }.get(m.group(1), f"the site answered with error {m.group(1)}")
    if "not found" in e:
        return "the page was not found"
    if "member rows" in e or "of the" in e and "members" in e:
        return "the page layout was not understood or only part of it was read"
    return (error or "unknown reason")[:80]


def _why(exc: Exception) -> str:
    text = str(exc).split("\n")[0]
    text = re.sub(r"https?://\S+", "", text).strip(" :")
    return (text or exc.__class__.__name__)[:600]

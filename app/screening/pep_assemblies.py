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
from html.parser import HTMLParser

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
                "List of members of the 15th Provincial Assembly of Punjab"]},
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

OFFICIAL_SOURCES = [
    {"key": "na_official", "label": "National Assembly website (na.gov.pk)", "level": NATIONAL, "province": "",
     "url": "https://na.gov.pk/en/all_members.php"},
]

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


def members_from_html(html: str) -> list:
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
        for row in rows[hi + 1:]:
            if len(row) <= name_col or all(th for _t, th in row if _t) and len(row) <= 2:
                continue            # a section heading row ("Reserved seats for women"), not a member
            names = _clean_name(row[name_col][0])
            if not names:
                continue
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
    for src in (WIKI_SOURCES if sources is None else sources):
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
            report.append({"key": src["key"], "label": src["label"], "source": used, "count": len(got), "error": ""})
        else:
            report.append({"key": src["key"], "label": src["label"], "source": used or src["titles"][0],
                           "count": 0, "error": err})
    for src in (OFFICIAL_SOURCES if official is None else official):
        try:
            got = members_from_html(get(src["url"], None))
            if len(got) < MIN_MEMBERS:
                raise ValueError(f"only {len(got)} member rows could be read")
            assemblies.append({"key": src["key"], "label": src["label"], "level": src["level"],
                               "province": src["province"], "source": src["url"], "members": got})
            report.append({"key": src["key"], "label": src["label"], "source": src["url"], "count": len(got), "error": ""})
        except Exception as exc:              # noqa: BLE001
            report.append({"key": src["key"], "label": src["label"], "source": src["url"], "count": 0, "error": _why(exc)})
    return assemblies, report


def _why(exc: Exception) -> str:
    text = str(exc).split("\n")[0]
    text = re.sub(r"https?://\S+", "", text).strip(" :")
    return (text or exc.__class__.__name__)[:140]

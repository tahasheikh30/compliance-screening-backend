"""
Parsers for every record system the screening reads.

Literal Python ports of the parsing code in the n8n "Applicant Screening
Engine" workflow (node "Screen Applicant" and "Find Red Book Editions"). The
regular expressions are kept deliberately close to the originals, because that
workflow is known to work against the live feeds. If a publisher changes a file
layout, this is the only module that needs to change.

Each parser turns raw text into `Record` objects:
    list, id, primary, type, programs, dob, nationality, listed_on, remarks, names
"""

import csv
import io
import re
from urllib.parse import urlparse
from dataclasses import dataclass, field

from app.screening.names import tokens, jw

# --------------------------------------------------------------------------
# Record
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Record:
    list: str
    id: str
    primary: str
    type: str
    programs: str
    dob: str
    nationality: str
    listed_on: str
    remarks: str
    names: list
    source_key: str = ""
    toks: list = field(default_factory=list)  # tokenised names, filled by prepare()
    cnic: str = ""    # 13 digit national ID, digits only, when the list publishes one
    father: str = ""  # father's or husband's name, when the list publishes one


def prepare(records: list) -> list:
    """Tokenise every name once, so repeated screenings only do the scoring."""
    for r in records:
        r.toks = [tokens(n) for n in r.names]
    return records


# --------------------------------------------------------------------------
# Small text helpers (ports of tag / blocks / decode / parseCsv)
# --------------------------------------------------------------------------

_TAG_RE_CACHE: dict = {}
_STRIP_TAGS = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"\s+")


def _block_re(name: str):
    rx = _TAG_RE_CACHE.get(name)
    if rx is None:
        rx = re.compile(rf"<{name}>([\s\S]*?)</{name}>")
        _TAG_RE_CACHE[name] = rx
    return rx


def tag(block: str, name: str) -> list:
    """Text of every <name>...</name> in block (inner tags removed, spaces collapsed)."""
    out = []
    for m in _block_re(name).finditer(block):
        t = _SPACES.sub(" ", _STRIP_TAGS.sub(" ", m.group(1))).strip()
        if t:
            out.append(t)
    return out


def blocks(xml: str, name: str) -> list:
    """Raw inner content of every <name>...</name>."""
    return [m.group(1) for m in _block_re(name).finditer(xml)]


def decode(s) -> str:
    s = "" if s is None else str(s)
    return (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
             .replace("&quot;", '"').replace("&apos;", "'"))


def parse_csv(text: str) -> list:
    """CSV rows. Quoted fields may contain commas, doubled quotes and newlines."""
    cleaned = text.replace("\r", "").replace("\x1a", "")
    return list(csv.reader(io.StringIO(cleaned, newline="")))


def nul(v) -> str:
    """OFAC marks empty fields as -0-."""
    s = str(v or "").strip()
    return "" if s == "-0-" else s


# --------------------------------------------------------------------------
# UN Security Council consolidated list (legacy XML)
# --------------------------------------------------------------------------

UN_LABEL = "UN Security Council Consolidated List"


def parse_un(xml: str) -> tuple:
    """Returns (records, published). `published` is the file's dateGenerated."""
    m = re.search(r'dateGenerated="([^"]+)"', xml)
    published = m.group(1) if m else ""
    records = []
    for kind in ("INDIVIDUAL", "ENTITY"):
        for b in blocks(xml, kind):
            parts = [(tag(b, t) or [""])[0] for t in ("FIRST_NAME", "SECOND_NAME", "THIRD_NAME", "FOURTH_NAME")]
            primary = decode(" ".join(p for p in parts if p))
            aliases = [decode(a) for a in tag(b, "ALIAS_NAME")]
            dobs = []
            for d in blocks(b, "INDIVIDUAL_DATE_OF_BIRTH"):
                v = ((tag(d, "DATE") or [""])[0] or (tag(d, "YEAR") or [""])[0]
                     or "-".join(x for x in [(tag(d, "FROM_YEAR") or [""])[0], (tag(d, "TO_YEAR") or [""])[0]] if x))
                if v:
                    dobs.append(v)
            nat = []
            for n in blocks(b, "NATIONALITY"):
                nat.extend(tag(n, "VALUE"))
            records.append(Record(
                list=UN_LABEL,
                id=(tag(b, "REFERENCE_NUMBER") or tag(b, "DATAID") or [""])[0],
                primary=primary,
                type="Individual" if kind == "INDIVIDUAL" else "Entity",
                programs=", ".join(tag(b, "UN_LIST_TYPE")),
                dob="; ".join(dobs),
                nationality="; ".join(decode(x) for x in nat),
                listed_on=(tag(b, "LISTED_ON") or [""])[0],
                remarks=decode((tag(b, "COMMENTS1") or [""])[0]),
                names=[x for x in [primary, *aliases] if x],
                source_key="unsc",
            ))
    return records, published


# --------------------------------------------------------------------------
# OFAC (SDN and Consolidated Non-SDN): primary CSV + alias CSV
# --------------------------------------------------------------------------

OFAC_SDN_LABEL = "OFAC Specially Designated Nationals (SDN) List"
OFAC_CONS_LABEL = "OFAC Consolidated (Non-SDN) List"


def parse_ofac(prim_csv: str, alt_csv: str, label: str) -> list:
    prim = parse_csv(prim_csv or "")
    alt = parse_csv(alt_csv or "")
    alts: dict = {}
    for r in alt:
        if len(r) >= 4 and nul(r[3]):
            alts.setdefault(r[0], []).append(nul(r[3]))
    records = []
    for r in prim:
        if len(r) < 12 or not nul(r[1]):
            continue
        remarks = nul(r[11])
        dob = "; ".join(x.replace("DOB ", "") for x in re.findall(r"DOB [^;]+", remarks))
        nat = "; ".join(x.replace("nationality ", "") for x in re.findall(r"nationality [^;]+", remarks))
        t = nul(r[2])
        records.append(Record(
            list=label,
            id="OFAC-" + r[0],
            primary=nul(r[1]),
            type=(t[0].upper() + t[1:]) if t else "Entity",
            programs=nul(r[3]),
            dob=dob,
            nationality=nat,
            listed_on="",
            remarks=remarks,
            names=[nul(r[1]), *alts.get(r[0], [])],
            source_key="ofac",
        ))
    return records


# --------------------------------------------------------------------------
# UK Sanctions List (FCDO XML)
# --------------------------------------------------------------------------

UK_LABEL = "UK Sanctions List (FCDO)"


def parse_uk(xml: str) -> tuple:
    """Returns (records, published). `published` is the file's DateGenerated."""
    published = (tag(xml[:2000], "DateGenerated") or [""])[0]
    records = []
    for b in blocks(xml, "Designation"):
        names = []
        primary = ""
        for n in blocks(b, "Name"):
            full = decode(" ".join(
                p for p in [(tag(n, t) or [""])[0] for t in ("Name1", "Name2", "Name3", "Name4", "Name5", "Name6")] if p
            ))
            if not full:
                continue
            names.append(full)
            if not primary and re.search(r"primary name$", (tag(n, "NameType") or [""])[0], re.I):
                primary = full
        if not names:
            continue
        records.append(Record(
            list=UK_LABEL,
            id=(tag(b, "UniqueID") or [""])[0],
            primary=primary or names[0],
            type=(tag(b, "IndividualEntityShip") or [""])[0],
            programs=decode((tag(b, "RegimeName") or [""])[0]),
            dob="; ".join(tag(b, "DOB")),
            nationality="; ".join(decode(x) for x in tag(b, "Nationality")),
            listed_on=(tag(b, "DateDesignated") or [""])[0],
            remarks=decode((tag(b, "UKStatementofReasons") or tag(b, "OtherInformation") or [""])[0]),
            names=names,
            source_key="uksl",
        ))
    return records, published


# --------------------------------------------------------------------------
# FIA Red Book: find the PDF editions on the FIA website, then parse their text
# --------------------------------------------------------------------------

FIA_PAGES = ("https://www.fia.gov.pk/press-pub", "https://www.fia.gov.pk/ctw")


def _page_decode(s) -> str:
    s = "" if s is None else str(s)
    return (s.replace("&quot;", '"').replace("&amp;", "&").replace("&#039;", "'")
             .replace("&lt;", "<").replace("&gt;", ">"))


_PUB_JSON = re.compile(
    r'"pub_name"\s*:\s*"([^"]*red\s*book[^"]*)"\s*,\s*"pub_url"\s*:\s*"([^"]+)"[^}]*?"updated_at"\s*:\s*"?([^",}]*)',
    re.I,
)
_PDF_LINK = re.compile(r'<a[^>]*href="([^"]+\.pdf)"[^>]*>([\s\S]*?)</a>', re.I)
_FIA_HOSTS = frozenset({"www.fia.gov.pk", "fia.gov.pk"})
_FIA_HOST = re.compile(r"^https?://(www\.)?fia\.gov\.pk", re.I)


def find_redbook_editions(pages: list) -> list:
    """
    `pages` is a list of {"data": html or None, "error": message or None}, one
    per FIA page fetched. Returns [{"url", "title", "updated"}], de-duplicated.
    If nothing is found, returns one placeholder {"url": "", "note": reason}.
    """
    out: list = []
    seen: set = set()

    def add(url, title, updated):
        url = str(url or "").replace("\\/", "/").strip()
        if not url:
            return
        if not re.match(r"^https?:", url, re.I):
            url = "https://www.fia.gov.pk/" + url.lstrip("/")
        url = _FIA_HOST.sub("https://www.fia.gov.pk", url)
        # The link comes from a scraped web page, so it is untrusted: only the FIA's own https host is
        # fetched. Anything else (an internal address, another site, fia.gov.pk.evil.example, a user@host
        # trick) is dropped, never requested.
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in _FIA_HOSTS or parsed.username or parsed.password:
            return
        if url in seen:
            return
        seen.add(url)
        out.append({
            "url": url,
            "title": _SPACES.sub(" ", str(title or "Red Book")).strip(),
            "updated": updated or "",
        })

    for p in pages:
        html = _page_decode(p.get("data"))
        for m in _PUB_JSON.finditer(html):
            add(m.group(2), m.group(1), m.group(3))
        for m in _PDF_LINK.finditer(html):
            text = _STRIP_TAGS.sub(" ", m.group(2))
            if re.search(r"red\s*book", text, re.I):
                add(m.group(1), text, "")

    if not out:
        errs = "; ".join(str(p["error"]) for p in pages if p.get("error"))
        note = ("FIA website could not be reached: " + errs[:200]) if errs else "no Red Book link found on the FIA website"
        return [{"url": "", "title": "Red Book", "note": note}]
    return out


_ACCUSED = re.compile(r"Name\s*of\s*Accused", re.I)
_MWST = re.compile(r"\(\s*MWS\s*/\s*T\s*\)", re.I)
_ALIAS_SPLIT = re.compile(r"\s+(?:ALIAS|A\.?K\.?A\.?)\s+|\s*@\s*", re.I)
_RX_RAW1 = re.compile(r"^\s*([\s\S]*?)\s*Father\s*/?\s*Husband", re.I)
_RX_RAW2 = re.compile(r"^\s*([^\n]{3,80})")
_RX_FATHER = re.compile(r"Husband\s*Name\s*([\s\S]*?)\s*CNIC", re.I)
_RX_CNIC = re.compile(r"CNIC\s*(?:Number|No\.?)?\s*([0-9]{5}-?[0-9]{7}-?[0-9])", re.I)
_RX_DOB = re.compile(r"Date\s*of\s*Birth\s*([0-9]{1,2}[-/.][0-9]{1,2}[-/.][0-9]{2,4})", re.I)
_RX_ZONE = re.compile(r"ZONE:\s*[^)]*", re.I)
_RX_FIR = re.compile(r"FIR\s*No\s*\(s\)\s*([\s\S]*?)Section", re.I)


def _pick(src: str, rx) -> str:
    m = rx.search(src)
    return _SPACES.sub(" ", m.group(1)).strip() if m else ""


def fia_label(title: str) -> str:
    """'FIA Red Book 2026' stays as is; 'Red Book 2026' becomes 'FIA Red Book 2026'."""
    t = (title or "Red Book").strip()
    return t if t.upper().startswith("FIA") else "FIA " + t


def parse_redbook(text: str, title: str) -> list:
    """Records for every 'Name of Accused' block in one Red Book edition's text."""
    label = fia_label(title)
    starts = [(m.start(), len(m.group(0))) for m in _ACCUSED.finditer(text)]
    records = []
    count = 0
    for k, (at, ln) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else len(text)
        c = text[at + ln: min(end, at + 1500)]
        before = text[max(0, at - 700): at]

        raw = _pick(c, _RX_RAW1) or _pick(c, _RX_RAW2)
        raw = _SPACES.sub(" ", _MWST.sub("", raw)).strip()
        if not raw or len(raw) > 120:
            continue
        names = [x.strip() for x in _ALIAS_SPLIT.split(raw) if x and x.strip()]
        if not names:
            continue
        father = _pick(c, _RX_FATHER)
        cnic = _pick(c, _RX_CNIC)
        dob = _pick(c, _RX_DOB)
        zones = _RX_ZONE.findall(before)
        zone = _SPACES.sub(" ", zones[-1]).strip() if zones else ""
        firs = [m.group(0) for m in _RX_FIR.finditer(before)]
        fir = ""
        if firs:
            f = re.sub(r"^FIR\s*No\s*\(s\)", "", firs[-1], flags=re.I)
            f = re.sub(r"Section$", "", f, flags=re.I)
            fir = _SPACES.sub(" ", f).strip()
        count += 1
        zone_txt = re.sub(r"\s*CIRCLE:", ", CIRCLE:", zone, count=1, flags=re.I) if zone else ""
        remarks = "; ".join(x for x in [
            "Father/Husband: " + (father or "n/a"),
            "CNIC: " + (cnic or "n/a"),
            zone_txt,
            ("FIR: " + fir) if fir else "",
        ] if x)
        records.append(Record(
            list=label,
            id="FIA-RB-" + (cnic or str(count)),
            primary=names[0],
            type="Individual",
            programs=title or "FIA Red Book",
            dob=dob,
            nationality="",
            listed_on="",
            remarks=remarks,
            names=names,
            source_key="fia",
            cnic=normalize_cnic(cnic),
            father=father,
        ))
    return records


def pdf_to_text(data: bytes) -> str:
    """All text of a PDF, pages joined by newlines."""
    import pymupdf  # imported here so the rest of the package works without it

    if not data[:1024].lstrip().startswith(b"%PDF"):
        raise ValueError("the downloaded file is not a PDF")
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return "\n".join(page.get_text() for page in doc)


# --------------------------------------------------------------------------
# Adverse media: Google News RSS items
# --------------------------------------------------------------------------

ADVERSE = re.compile(
    r"\b(arrest\w*|charged|charges|convict\w*|sentenc\w*|fraud\w*|launder\w*|terror\w*|militant\w*|"
    r"smuggl\w*|traffick\w*|corrupt\w*|brib\w*|sanction\w*|wanted|indict\w*|scam\w*|extort\w*|kidnap\w*|"
    r"embezzl\w*|money trail|warrant\w*|militan\w*|banned)\b",
    re.I,
)
_SOURCE_TAG = re.compile(r"<source[^>]*>([\s\S]*?)</source>")


def parse_news_hits(xml: str, applicant_name: str, limit: int = 20) -> tuple:
    """
    Returns (articles_reviewed, hits). An article is a hit when its title or
    summary contains the applicant's surname plus at least one more name part
    (or the whole name when it is a single word) AND an adverse keyword.
    Headlines often shorten names, so requiring every name part would miss them.
    """
    items = blocks(xml, "item")
    need = tokens(applicant_name)
    hits = []
    for b in items:
        title = decode((tag(b, "title") or [""])[0])
        desc = _SPACES.sub(" ", _STRIP_TAGS.sub(" ", decode(decode((tag(b, "description") or [""])[0])))).strip()
        text = title + " " + desc
        have = tokens(text)
        hit_tok = [q for q in need if any(jw(q, h) >= 0.92 for h in have)]
        surname = need[-1] if need else ""
        name_found = bool(need) and surname in hit_tok and len(hit_tok) >= min(2, len(need))
        kw = ADVERSE.search(text)
        if not name_found or not kw:
            continue
        sm = _SOURCE_TAG.search(b)
        hits.append({
            "title": title,
            "link": decode((tag(b, "link") or [""])[0]),
            "published": (tag(b, "pubDate") or [""])[0],
            "source": decode(sm.group(1) if sm else ""),
            "keyword": kw.group(0),
        })
        if len(hits) >= limit:
            break
    return len(items), hits


# --------------------------------------------------------------------------
# NACTA Proscribed Persons (Fourth Schedule of the Anti-Terrorism Act, 1997)
# --------------------------------------------------------------------------
# Published by NACTA through a web app, so this parser reads an exported CSV or
# JSON file rather than a fixed feed. Column names differ between exports and
# mirrors ("Primary Title / Name", "Father Name", "CNIC / ID Number", ...), so
# columns are recognised by name, ignoring case and punctuation.

import json  # noqa: E402

NACTA_LABEL = "NACTA Proscribed Persons (Fourth Schedule)"
NACTA_PROGRAMME = "Anti-Terrorism Act 1997, Fourth Schedule"


def normalize_cnic(v) -> str:
    """13 digit CNIC with everything else removed, or '' when it is missing or not usable."""
    digits = re.sub(r"\D", "", str(v or ""))
    # placeholders such as 1111111111166 or 0000000000000 are not real identifiers
    if len(digits) != 13 or len(set(digits)) <= 2:
        return ""
    return digits


def _hkey(h) -> str:
    return re.sub(r"[^a-z0-9]", "", str(h or "").lower())


_NAME_KEYS = {"name", "primarytitlename", "primaryname", "fullname", "personname", "nameofperson",
              "nameofproscribedperson", "title", "accusedname", "nameofaccused"}
_FATHER_KEYS = {"fathername", "fatherhusbandname", "fatherorhusbandname", "fathersname", "father",
                "husbandname", "guardianname", "sonof", "swdowo", "so", "sdwo"}
_CNIC_KEYS = {"cnic", "cnicidnumber", "cnicno", "cnicnumber", "idnumber", "nic", "nicnumber",
              "nationalid", "identitynumber", "idno"}
_DISTRICT_KEYS = {"district", "city"}
_PROVINCE_KEYS = {"province", "state", "region"}
_SERIAL_KEYS = {"sno", "serialno", "serial", "srno", "sr", "id", "no"}
_ALIAS_KEYS = {"alias", "aliases", "alsoknownas", "aka", "othernames", "alternatename"}

_NACTA_PLACEHOLDERS = {"", "nill", "nil", "n/a", "na", "none", "null", "-", "--", "unknown"}


def _clean_cell(v) -> str:
    s = re.sub(r"\s+", " ", "" if v is None else str(v)).strip()
    return "" if s.lower() in _NACTA_PLACEHOLDERS else s


def _row_fields(row: dict) -> dict:
    by = {_hkey(k): v for k, v in row.items()}

    def first(keys):
        for k in keys:
            if k in by and _clean_cell(by[k]):
                return _clean_cell(by[k])
        return ""

    name = first(_NAME_KEYS)
    if not name:  # any other column that mentions "name" but is not a relative's name
        for k, v in by.items():
            if "name" in k and not re.search(r"father|husband|mother|guardian|wife", k) and _clean_cell(v):
                name = _clean_cell(v)
                break
    return {
        "name": name, "father": first(_FATHER_KEYS), "cnic_raw": first(_CNIC_KEYS),
        "district": first(_DISTRICT_KEYS), "province": first(_PROVINCE_KEYS),
        "serial": first(_SERIAL_KEYS), "alias": first(_ALIAS_KEYS),
    }


def _nacta_xml_rows(text: str) -> list:
    """
    Records from an XML export: the repeated element that holds one person, with its child
    elements and attributes as fields. Works whatever the element names are.
    """
    from defusedxml import ElementTree as ET   # refuses entity tricks and external references outright

    # entity declarations are the way XML files are made to expand into gigabytes
    if re.search(r"<!\s*(ENTITY|DOCTYPE[^>]*\[)", text, re.I):
        raise ValueError("XML files with entity declarations are not accepted.")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"The file looks like XML but could not be parsed ({exc}).") from None
    node = root
    while True:  # descend to the level that holds the repeated person elements
        kids = list(node)
        if not kids:
            raise ValueError("The XML contains no records.")
        tags = [k.tag for k in kids]
        if len(kids) > 1 and len(set(tags)) < len(tags):
            records = [k for k in kids if tags.count(k.tag) > 1 and k.tag == max(set(tags), key=tags.count)]
            break
        if len(kids) == 1:
            node = kids[0]
            continue
        # several different child tags and none repeated: the records are probably this node's children's children
        node = max(kids, key=lambda k: len(list(k)))
    rows = []
    for rec in records:
        row = dict(rec.attrib)
        for child in rec:
            tag = child.tag.split("}")[-1]
            if not list(child):
                row[tag] = (child.text or "").strip()
        if not row and (rec.text or "").strip():
            continue
        rows.append(row)
    if not rows:
        raise ValueError("The XML contains no records.")
    return rows


def _nacta_rows(text: str) -> list:
    """Rows as dicts, from a JSON array (or an object holding one) or from CSV."""
    t = text.lstrip("\ufeff").strip()
    if not t:
        raise ValueError("The file is empty.")
    if t[0] in "[{":
        try:
            data = json.loads(t)
        except ValueError as exc:
            raise ValueError(f"The file looks like JSON but could not be parsed ({exc}).") from None
        if isinstance(data, dict):
            lists = [v for v in data.values() if isinstance(v, list) and v and isinstance(v[0], dict)]
            if not lists:
                raise ValueError("The JSON has no list of records. Expected an array of objects.")
            data = max(lists, key=len)
        rows = [r for r in data if isinstance(r, dict)]
        if not rows:
            raise ValueError("The JSON contains no records.")
        return rows
    if t[0] == "<":
        return _nacta_xml_rows(t)
    first_line = t.splitlines()[0]
    delim = max((",", ";", "\t", "|"), key=first_line.count)
    grid = [r for r in csv.reader(io.StringIO(t, newline=""), delimiter=delim) if any(c.strip() for c in r)]
    # the header is the first row that has a recognisable name column
    for i, row in enumerate(grid[:15]):
        keys = {_hkey(c) for c in row}
        if keys & _NAME_KEYS or any("name" in k and "father" not in k for k in keys):
            header = row
            return [dict(zip(header, r)) for r in grid[i + 1:]]
    raise ValueError("Could not find a Name column. The first row should have headers such as Name, Father Name, CNIC.")


def parse_nacta_persons(text: str) -> tuple:
    """
    Returns (records, info). `info` has rows read, rows skipped (no name) and whether the
    file has CNIC and father's name columns, so the upload response can say what was found.
    """
    rows = _nacta_rows(text)
    records = []
    skipped = 0
    with_cnic = 0
    for idx, row in enumerate(rows, start=1):
        f = _row_fields(row)
        if not f["name"]:
            skipped += 1
            continue
        parts = [x.strip() for x in _ALIAS_SPLIT.split(f["name"]) if x and x.strip()]
        names = parts or [f["name"]]
        if f["alias"]:
            names += [x.strip() for x in re.split(r"[;,/]", f["alias"]) if x.strip()]
        cnic = normalize_cnic(f["cnic_raw"])
        with_cnic += 1 if cnic else 0
        remarks = "; ".join(x for x in [
            ("Father/Husband: " + f["father"]) if f["father"] else "",
            ("CNIC: " + (cnic or f["cnic_raw"])) if (cnic or f["cnic_raw"]) else "",
            ("District: " + f["district"]) if f["district"] else "",
            ("Province: " + f["province"]) if f["province"] else "",
        ] if x)
        records.append(Record(
            list=NACTA_LABEL,
            id="NACTA-" + (f["serial"] or str(idx)),
            primary=names[0],
            type="Individual",
            programs=NACTA_PROGRAMME,
            dob="",
            nationality="",
            listed_on="",
            remarks=remarks,
            names=names,
            source_key="nacta",
            cnic=cnic,
            father=f["father"],
        ))
    info = {"rows": len(rows), "skipped": skipped, "with_cnic": with_cnic,
            "has_father": any(r.father for r in records)}
    return records, info

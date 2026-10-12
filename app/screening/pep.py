"""
Politically exposed persons (PEPs): national and provincial office holders.

Pakistan has no official PEP list, so the records come from two places that are merged at load time:

  * Wikidata, fetched with one SPARQL query (people who hold or held a position and are Pakistani) and saved as a
    plain JSON snapshot. Which of their positions make them a PEP, and at which level, is decided HERE by
    `classify_position`, not in the query, so a rule can be improved without fetching again.
  * An admin's own CSV, JSON or XML file with columns such as Name, Position, Level, Province, CNIC.

Levels: "National" (President, Prime Minister, National Assembly and Senate members, federal ministers, the
top judiciary, the military chiefs, central bank governor, ambassadors and so on) and "Provincial" (provincial
assembly members, chief ministers, governors, provincial ministers and speakers, Gilgit-Baltistan and Azad
Jammu and Kashmir assemblies and governments). A person with offices at both levels is National. "Local" covers members and councillors of the metropolitan and
municipal corporations and district councils of the Pakistani cities listed in `_CITY_PROVINCE`; a person with a
national or provincial office as well keeps that higher level.

Offices of other countries are never PEP offices here: anything that names Bangladesh is refused (see
`mentions_excluded_country`), and no Bangladesh text reaches a PEP record or a PEP hit.
"""

import json
import re
import time
from datetime import date, datetime, timezone

from app import config
from app.screening import parsers
from app.screening.parsers import Record, _clean_cell, _hkey, normalize_cnic, normalize_province

PEP_LABEL = "Politically Exposed Persons (national and provincial)"
NATIONAL, PROVINCIAL, LOCAL = "National", "Provincial", "Local"
POLITICAL = "Political figure"     # a politician with no recorded national or provincial office

# --------------------------------------------------------------------------
# Which positions make someone a PEP
# --------------------------------------------------------------------------

_PROVINCE_NAMES = re.compile(
    r"\b(khyber[ -]pakhtunkhwa|khyber[ -]pukhtunkhwa|north[ -]west frontier province|nwfp|punjab|sindh|"
    r"balochistan|baluchistan|gilgit[ -]baltistan|azad jammu (?:and|&) kashmir|azad kashmir|azad jammu|ajk|fata)\b", re.I)

_NATIONAL_RULES = [re.compile(p, re.I) for p in (
    r"\bnational assembly\b",
    r"\bsenat(e|or)\b",
    r"\bpresident of pakistan\b",
    r"\bprime minister of pakistan\b",
    r"\b(chief justice|justice|judge) of the (supreme court|federal shariat court|islamabad high court) ?(of pakistan)?",
    r"\bchief justice of pakistan\b",
    r"\bchief of (the )?(army|air|naval) staff\b",
    r"\bchairman,? joint chiefs of staff\b",
    r"\bgovernor of the state bank of pakistan\b",
    r"\battorney[- ]general (for|of) pakistan\b",
    r"\bchief election commissioner\b",
    r"\bauditor[- ]general of pakistan\b",
    r"\bambassador of pakistan\b",
    r"\bhigh commissioner of pakistan\b",
    r"\bfederal (minister|secretary|ombudsman)\b",
    r"\bminister of state\b",
    r"\bspecial assistant to the prime minister\b",
    r"\bminister (of|for) [^()]*\(?pakistan\)?",
)]

_PROVINCIAL_RULES = [re.compile(p, re.I) for p in (
    r"\bprovincial assembly\b",
    r"\blegislative assembly\b",
    r"\bchief minister\b",
    r"\bgovernor of\b",
    r"\bprovincial minister\b",
    r"\b(speaker|deputy speaker) of the [^,;]*assembly\b",
    r"\bprime minister of azad\b",
    r"\bpresident of azad\b",
    r"\badvocate[- ]general of\b",
    r"\bminister (of|for) [^()]*",     # a minister in a provincial government: the label names the province
)]


# Local government: a member, councillor or mayor of a metropolitan or municipal corporation, city, town, tehsil or
# union council, municipal committee, cantonment board or district council. The label must also name a Pakistani
# city or district from the lists below, so "Municipal Corporation of Delhi" or a Bangladeshi city is never a PEP
# office. A place that is missing here is left out and shows in the "not counted" note on the Lists page, so it can
# be added.
_PLACES = {
    "Punjab": (
        "lahore faisalabad multan rawalpindi gujranwala sialkot sargodha bahawalpur sahiwal okara kasur sheikhupura "
        "nankana sahib jhang chiniot toba tek singh khanewal vehari lodhran pakpattan bahawalnagar rahim yar khan "
        "dera ghazi khan muzaffargarh layyah rajanpur mianwali bhakkar khushab jhelum chakwal attock gujrat "
        "mandi bahauddin hafizabad narowal murree taxila wah"),
    "Sindh": (
        "karachi sukkur larkana nawabshah shaheed benazirabad jamshoro thatta badin dadu mirpur khas umerkot "
        "tharparkar tando allahyar tando muhammad khan matiari sanghar khairpur ghotki shikarpur jacobabad kashmore "
        "kambar shahdadkot naushahro feroze sujawal korangi malir keamari"),
    "Khyber Pakhtunkhwa": (
        "peshawar mardan swat abbottabad kohat bannu nowshera charsadda swabi mansehra haripur upper dir lower dir "
        "chitral buner shangla malakand hangu karak lakki marwat dera ismail khan battagram kohistan torghar "
        "orakzai kurram mohmand bajaur waziristan khyber mingora"),
    "Balochistan": (
        "quetta gwadar turbat khuzdar sibi zhob loralai chaman pishin kalat mastung nasirabad jaffarabad kharan "
        "lasbela awaran panjgur hub"),
    "Islamabad Capital Territory": "islamabad",
    "Gilgit-Baltistan": "gilgit skardu hunza ghizer diamer astore",
    "Azad Jammu and Kashmir": "muzaffarabad kotli rawalakot bhimber neelum",
}
# multi-word names are listed with their words together above, so they are read from this explicit set
_MULTI = {"nankana sahib", "toba tek singh", "rahim yar khan", "dera ghazi khan", "mandi bahauddin", "shaheed benazirabad",
          "mirpur khas", "tando allahyar", "tando muhammad khan", "kambar shahdadkot", "naushahro feroze", "upper dir",
          "lower dir", "lakki marwat", "dera ismail khan"}


def _place_table() -> dict:
    table = {}
    for province, names in _PLACES.items():
        words = names.split()
        i = 0
        while i < len(words):
            for n in (3, 2):        # a multi-word name first
                cand = " ".join(words[i:i + n])
                if cand in _MULTI:
                    table[cand] = province
                    i += n
                    break
            else:
                table[words[i]] = province
                i += 1
    return table


_CITY_PROVINCE = _place_table()
# other spellings Wikidata uses for the same places ("councillor in the municipal corporation of Sukkar")
_CITY_PROVINCE.update({
    "sukkar": "Sindh", "mirpurkhas": "Sindh", "naushehro feroze": "Sindh", "nowshero feroze": "Sindh",
    "shaikhupura": "Punjab", "sheikhupura": "Punjab", "lyallpur": "Punjab", "d.g. khan": "Punjab", "dg khan": "Punjab",
    "d.i. khan": "Khyber Pakhtunkhwa", "di khan": "Khyber Pakhtunkhwa", "nowshehra": "Khyber Pakhtunkhwa",
    "charsada": "Khyber Pakhtunkhwa", "abbotabad": "Khyber Pakhtunkhwa",
    "jafarabad": "Balochistan", "lasbella": "Balochistan",
})
_LOCAL_BODY = re.compile(
    r"\b(metropolitan|municipal|city|town|tehsil|union|district|cantonment) (corporation|council|committee|board)\b"
    r"|\b(deputy )?mayor\b", re.I)
_CITY_NAMES = re.compile(r"\b(" + "|".join(sorted(map(re.escape, _CITY_PROVINCE), key=len, reverse=True)) + r")\b", re.I)
# names shared with places in other countries: accepted only when the label also says Sindh or Pakistan
_AMBIGUOUS = {"hyderabad": "Sindh", "mirpur": "Azad Jammu and Kashmir"}
_AMBIGUOUS_NAMES = re.compile(r"\b(" + "|".join(_AMBIGUOUS) + r")\b", re.I)
_PAKISTAN_HINT = re.compile(r"\b(pakistan|sindh|azad (jammu|kashmir)|ajk)\b", re.I)
_FOREIGN = re.compile(r"\b(india|indian|delhi|telangana|andhra|maharashtra|gujarat|uttar pradesh|afghanistan|iran|"
                      r"united kingdom|london|england)\b", re.I)

# Offices of these countries are not PEP offices for this list, even for a person who is Pakistani on Wikidata.
_EXCLUDED_COUNTRIES = re.compile(r"\bbangladesh(i)?\b", re.I)


def mentions_excluded_country(*texts) -> bool:
    """True when any of the texts names a country whose offices must not appear on the PEP list (Bangladesh)."""
    return any(_EXCLUDED_COUNTRIES.search(str(t or "")) for t in texts)


def classify_position(label: str) -> tuple | None:
    """
    (level, province) for a position label, or None when the position does not make someone a PEP.
    "Member of the Provincial Assembly of the Punjab" -> ("Provincial", "Punjab").
    """
    text = re.sub(r"\s+", " ", str(label or "")).strip()
    if not text:
        return None
    if re.search(r"\bformer\b|\bcandidate\b|\bvoter\b", text, re.I):
        return None
    if mentions_excluded_country(text):
        return None
    prov_match = _PROVINCE_NAMES.search(text)
    province = normalize_province(prov_match.group(1)) if prov_match else ""
    # the National Assembly and the Senate are national whatever province the constituency is in
    if any(r.search(text) for r in _NATIONAL_RULES[:2]):
        return NATIONAL, ""
    # a minister, governor or speaker whose label names a province belongs to that province's government
    if prov_match and any(r.search(text) for r in _PROVINCIAL_RULES):
        return PROVINCIAL, province
    # a provincial office with no province in its label ("Chief Minister of Gujarat" is not ours, and
    # "Governor of Reserve Bank of India" is not a province at all) is not accepted from a public data set
    if any(r.search(text) for r in _NATIONAL_RULES[2:]):
        return NATIONAL, ""
    # a member or councillor of a city corporation or district council of a known Pakistani city
    if _LOCAL_BODY.search(text) and not _FOREIGN.search(text):
        city = _CITY_NAMES.search(text)
        if city:
            return LOCAL, _CITY_PROVINCE[city.group(1).lower()]
        amb = _AMBIGUOUS_NAMES.search(text)
        # "Hyderabad Municipal Corporation" on a Pakistani citizen is Sindh's; "Greater Hyderabad" is India's
        if amb and (_PAKISTAN_HINT.search(text)
                    or (amb.group(1).lower() == "hyderabad" and not re.search(r"\bgreater\b", text, re.I))):
            return LOCAL, _AMBIGUOUS[amb.group(1).lower()]
    return None


def _level_from_text(v: str) -> str:
    t = str(v or "").strip().lower()
    if re.search(r"local|municipal|metropolitan|district|city|council", t):
        return LOCAL
    if re.search(r"prov|state|region|assembly of (sindh|punjab|balochistan|khyber)", t):
        return PROVINCIAL
    if re.search(r"nat|fed|central|senate", t):
        return NATIONAL
    return ""


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------

def _year(v) -> int | None:
    m = re.match(r"\s*(-?\d{4})", str(v or ""))
    return int(m.group(1)) if m else None


def _within_lookback(end, today: date, years: float) -> bool:
    """Still in office (no end date), or left no longer ago than `years` (negative: never drop anyone)."""
    if not end or years < 0:
        return True
    y = _year(end)
    if y is None:
        return True
    if years == 0:
        return False
    try:
        left = datetime.fromisoformat(str(end).replace("Z", "+00:00")).date()
    except ValueError:
        left = date(y, 12, 31)
    return (today - left).days <= years * 365.25


def _span(start, end) -> str:
    a, b = _year(start), _year(end)
    if a and b:
        return f" ({a} to {b})"
    if a:
        return f" (from {a})"
    if b:
        return f" (until {b})"
    return ""


def _make_record(rid: str, names: list, positions: list, dob: str = "", cnic: str = "", father: str = "",
                 extra: str = "", level_hint: str = "", province_hint: str = "", source: str = "") -> Record | None:
    """One PEP record from a person's names and the positions that qualify (list of (label, level, province, span))."""
    names = [n for n in dict.fromkeys(x.strip() for x in names if x and x.strip())]
    # No Bangladesh data on a PEP record: its offices are dropped, and so are the extra notes that name it
    positions = [p for p in positions if not mentions_excluded_country(p[0])]
    if mentions_excluded_country(extra, father, level_hint, province_hint):
        extra = "" if mentions_excluded_country(extra) else extra
        father = "" if mentions_excluded_country(father) else father
        level_hint = "" if mentions_excluded_country(level_hint) else level_hint
        province_hint = "" if mentions_excluded_country(province_hint) else province_hint
    if not names or not positions:
        return None
    # the highest level wins: National, then Provincial, then Local
    levels = [p[1] for p in positions]
    level = (NATIONAL if NATIONAL in levels else PROVINCIAL if PROVINCIAL in levels
             else LOCAL if LOCAL in levels else (positions[0][1] or level_hint))
    province_level = PROVINCIAL if level == PROVINCIAL else LOCAL
    provinces = list(dict.fromkeys(p[2] for p in positions if p[2] and p[1] == province_level)) or (
        [province_hint] if province_hint else [])
    shown = [f"{p[0]}{p[3]}" for p in positions[:4]]
    if len(positions) > 4:
        shown.append(f"and {len(positions) - 4} more")
    position = "; ".join(shown)
    where = f", {', '.join(provinces)}" if level in (PROVINCIAL, LOCAL) and provinces else ""
    remarks = "; ".join(x for x in [position, ("Father/Husband: " + father) if father else "",
                                    ("CNIC: " + cnic) if cnic else "", extra, ("Source: " + source) if source else ""] if x)
    return Record(
        list=PEP_LABEL, id=rid, primary=names[0], type="Individual",
        programs=f"PEP, {level}{where}" if level else "PEP",
        dob=dob, nationality="Pakistan", listed_on="", remarks=remarks, names=names, source_key="pep",
        cnic=cnic, father=father, province=provinces[0] if (level in (PROVINCIAL, LOCAL) and provinces) else "",
        pep_level=level, position=position,
    )


def records_from_snapshot(snapshot: dict, today: date | None = None,
                          lookback_years: float | None = None) -> tuple:
    """(records, info) from a Wikidata snapshot: {"people": [{id, name, aliases, dob, positions: [{label, start, end}]}]}."""
    today = today or datetime.now(timezone.utc).date()
    years = config.PEP_LOOKBACK_YEARS if lookback_years is None else lookback_years
    records, unclassified, expired = [], 0, 0
    unknown: dict = {}
    for person in snapshot.get("people", []):
        if person.get("dod"):
            continue            # died: not a living customer
        quals = []
        for pos in person.get("positions", []):
            c = classify_position(pos.get("label"))
            if not c:
                lab = str(pos.get("label") or "")
                if mentions_excluded_country(lab):
                    continue            # another country's office: left out on purpose, not reported
                unclassified += 1
                unknown[lab] = unknown.get(lab, 0) + 1
                continue
            if not _within_lookback(pos.get("end"), today, years):
                expired += 1
                continue
            quals.append((pos["label"], c[0], c[1], _span(pos.get("start"), pos.get("end"))))
        # current offices first, then the most recently ended
        quals.sort(key=lambda q: (q[3].find("to") != -1, q[3]))
        rec = _make_record("PEP-WD-" + str(person.get("id", "")), [person.get("name", "")] + list(person.get("aliases", [])),
                           quals, dob=str(person.get("dob") or "")[:10], source="Wikidata " + str(person.get("id", "")))
        if rec:
            records.append(rec)
    # people Wikidata knows as politicians but with no office on record
    have = {_key(r.primary, r.dob) for r in records}
    have_names = {_key(r.primary, "") for r in records}
    politicians, upgraded = 0, 0
    for pol in snapshot.get("politicians", []):
        k = _key(pol.get("name", ""), "")
        if k in have_names or _key(pol.get("name", ""), str(pol.get("dob") or "")) in have:
            upgraded += 1       # already a PEP through a recorded office: no longer just a "political figure"
            continue
        party = str(pol.get("party") or "")
        if mentions_excluded_country(party):
            party = ""
        rec = _make_record("PEP-WD-" + str(pol.get("id", "")), [pol.get("name", "")],
                           [("Politician" + (f" ({party})" if party else ""), POLITICAL, "", "")],
                           dob=str(pol.get("dob") or "")[:10], source="Wikidata " + str(pol.get("id", "")))
        if rec:
            records.append(rec)
            have_names.add(k)
            politicians += 1
    # members of the assemblies, read from the member lists
    members = 0
    for asm in snapshot.get("assemblies", []):
        for m in asm.get("members", []):
            names = m.get("names") or [m.get("name", "")]
            k = _key(names[0], "")
            if k in have_names:
                continue
            seat = str(m.get("constituency") or "")
            party = str(m.get("party") or "")
            if mentions_excluded_country(seat, party):
                seat, party = ("" if mentions_excluded_country(seat) else seat), ("" if mentions_excluded_country(party) else party)
            label = f"Member, {asm.get('label', 'assembly')}" + (
                f" ({', '.join(x for x in (seat, party) if x)})" if seat or party else "")
            rec = _make_record("PEP-ASM-" + asm.get("key", "") + "-" + re.sub(r"\W+", "", names[0])[:30], names,
                               [(label, asm.get("level", ""), asm.get("province", ""), "")],
                               source=str(asm.get("source", "")))
            if rec:
                records.append(rec)
                have_names.add(k)
                members += 1
    by_level = {}
    for r in records:
        by_level[r.pep_level or POLITICAL] = by_level.get(r.pep_level or POLITICAL, 0) + 1
    return records, {"people": len(snapshot.get("people", [])), "politicians": politicians, "assembly_members": members,
                     "by_level": by_level, "politicians_now_with_office": upgraded,
                     "unclassified_positions": unclassified,
                     "expired_positions": expired,
                     "top_unclassified": sorted(unknown, key=lambda k: -unknown[k])[:15]}


_POSITION_KEYS = {"position", "positionheld", "office", "officeheld", "designation", "post", "role", "currentposition"}
_LEVEL_KEYS = {"level", "peplevel", "category", "pepcategory", "type", "pepType".lower(), "scope"}
_DOB_KEYS = {"dob", "dateofbirth", "birthdate", "born", "birthday"}
_NOTES_KEYS = {"party", "politicalparty", "notes", "remarks", "constituency"}


def parse_pep_persons(text: str) -> tuple:
    """
    (records, info) from an uploaded CSV, JSON or XML file. Needs a name column; a Position column and/or a
    Level column (National or Provincial) says why the person is a PEP. Raises ValueError when unreadable.
    """
    rows = parsers._nacta_rows(text)
    records, skipped, no_level, with_cnic = [], 0, 0, 0
    for idx, row in enumerate(rows, start=1):
        by = {_hkey(k): v for k, v in row.items()}

        def first(keys):
            for k in keys:
                if k in by and _clean_cell(by[k]):
                    return _clean_cell(by[k])
            return ""

        f = parsers._row_fields(row)
        if not f["name"]:
            skipped += 1
            continue
        position = first(_POSITION_KEYS)
        level = _level_from_text(first(_LEVEL_KEYS))
        province = normalize_province(f["province"])
        classified = classify_position(position) if position else None
        if classified:
            level = level or classified[0]
            province = province or classified[1]
        if not level:
            no_level += 1
            level = ""          # still a PEP: the file says so by listing the person
        cnic = normalize_cnic(f["cnic_raw"])
        with_cnic += 1 if cnic else 0
        names = [x.strip() for x in parsers._ALIAS_SPLIT.split(f["name"]) if x and x.strip()] or [f["name"]]
        if f["alias"]:
            names += [x.strip() for x in re.split(r"[;,/|]", f["alias"]) if x.strip()]
        label = position or ("Politically exposed person" + (f" ({level.lower()})" if level else ""))
        rec = _make_record("PEP-UP-" + (f["serial"] or str(idx)), names, [(label, level, province, "")],
                           dob=first(_DOB_KEYS), cnic=cnic, father=f["father"], extra=first(_NOTES_KEYS),
                           level_hint=level, province_hint=province, source="uploaded list")
        if rec:
            records.append(rec)
    return records, {"rows": len(rows), "skipped": skipped, "with_cnic": with_cnic, "no_level": no_level,
                     "national": sum(r.pep_level == NATIONAL for r in records),
                     "provincial": sum(r.pep_level == PROVINCIAL for r in records)}


def _key(name: str, dob: str) -> tuple:
    from app.screening import names as nm
    return (" ".join(sorted(nm.tokens(name))), (dob or "")[:4])


def merge(*groups: list) -> list:
    """Records from several sources, one per person: the first source to name someone wins (put the admin's list first)."""
    from app.screening import names as nm
    seen, out = set(), []
    for records in groups:
        for r in records:
            key = (" ".join(sorted(nm.tokens(r.primary))), (r.dob or "")[:4])
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
    return out


# --------------------------------------------------------------------------
# Wikidata
# --------------------------------------------------------------------------

PAGE_PEOPLE = 1000
MAX_PAGES = 80
_QID = re.compile(r"^Q\d+$")


def _query_people(country: str, limit: int, offset: int) -> str:
    return f"""SELECT ?person ?personLabel ?dob ?dod ?posLabel ?start ?end WHERE {{
  {{ SELECT DISTINCT ?person WHERE {{ ?person wdt:P27 wd:{country} ; wdt:P39 [] . }} ORDER BY ?person LIMIT {limit} OFFSET {offset} }}
  ?person p:P39 ?st . ?st ps:P39 ?pos .
  OPTIONAL {{ ?st pq:P580 ?start }}
  OPTIONAL {{ ?st pq:P582 ?end }}
  OPTIONAL {{ ?person wdt:P569 ?dob }}
  OPTIONAL {{ ?person wdt:P570 ?dod }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}"""


def _query_politicians(country: str, limit: int, offset: int) -> str:
    """Living Pakistani politicians (occupation politician), whether or not Wikidata records an office for them.
    Party leaders and former members are often listed only this way."""
    return f"""SELECT ?person ?personLabel ?dob ?partyLabel WHERE {{
  {{ SELECT DISTINCT ?person WHERE {{ ?person wdt:P27 wd:{country} ; wdt:P106 wd:Q82955 . FILTER NOT EXISTS {{ ?person wdt:P570 [] }} }} ORDER BY ?person LIMIT {limit} OFFSET {offset} }}
  OPTIONAL {{ ?person wdt:P569 ?dob }}
  OPTIONAL {{ ?person wdt:P102 ?party }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}"""


def _query_aliases(country: str, ids: list) -> str:
    """English aliases of a small, explicit set of people. Asking for them by id is fast; asking for the aliases
    of a whole page of people made Wikidata time out."""
    values = " ".join("wd:" + i for i in ids)
    return f"""SELECT ?person ?alias WHERE {{
  VALUES ?person {{ {values} }}
  ?person skos:altLabel ?alias . FILTER(LANG(?alias) = "en")
}}"""


def fetch_politicians(get, country: str = "Q843", page: int = PAGE_PEOPLE, pause: float = 1.0) -> list:
    """Living politicians as [{id, name, dob, party}]. An extra source: the caller keeps going if it fails."""
    out: dict = {}
    for n in range(MAX_PAGES):
        rows = _ask(get, _query_politicians(country, page, n * page), pause)
        for r in rows:
            pid = _qid(r["person"]["value"])
            name = r.get("personLabel", {}).get("value", "")
            if not name or _QID.match(name):
                continue
            p = out.setdefault(pid, {"id": pid, "name": name, "dob": "", "party": ""})
            if r.get("dob") and not p["dob"]:
                p["dob"] = r["dob"]["value"][:10]
            party = r.get("partyLabel", {}).get("value", "")
            if party and not _QID.match(party) and not p["party"]:
                p["party"] = party
        if len({_qid(r["person"]["value"]) for r in rows}) < page:
            break
        time.sleep(pause)
    return sorted(out.values(), key=lambda p: p["id"])


def _qid(uri: str) -> str:
    return str(uri).rsplit("/", 1)[-1]


ALIAS_CHUNK = 100
ATTEMPTS = 3


def _ask(get, query: str, pause: float):
    """One SPARQL query, tried a few times with a growing pause: the public endpoint answers 429 and 5xx now and then."""
    last = None
    for n in range(1, ATTEMPTS + 1):
        try:
            return json.loads(get(config.PEP_WIKIDATA_URL, {"query": query, "format": "json"}))["results"]["bindings"]
        except Exception as exc:       # noqa: BLE001  (network, HTTP status, bad JSON: all worth another try)
            last = exc
            if n < ATTEMPTS:
                time.sleep(pause * n * 3)
    raise last


def fetch_snapshot(get, country: str = "Q843", page: int = PAGE_PEOPLE, pause: float = 1.0) -> dict:
    """
    Everyone Pakistani on Wikidata who holds or held a position, with their positions and English aliases.
    `get(url, params)` returns the response text of a GET (the loader's safe downloader). Raises when the people
    cannot be fetched: a partial list must never replace a good one. Aliases are an extra, so a failure to get them
    only costs the aliases (the names themselves are still screened).
    """
    if not _QID.match(country):
        raise ValueError("PEP_WIKIDATA_COUNTRY must look like Q843")
    people: dict = {}
    for n in range(MAX_PAGES):
        rows = _ask(get, _query_people(country, page, n * page), pause)
        ids = set()
        for r in rows:
            pid = _qid(r["person"]["value"])
            name = r.get("personLabel", {}).get("value", "")
            if not name or _QID.match(name):      # no English label: cannot be compared with a Latin name
                continue
            ids.add(pid)
            p = people.setdefault(pid, {"id": pid, "name": name, "aliases": [], "dob": "", "positions": []})
            if r.get("dob") and not p["dob"]:
                p["dob"] = r["dob"]["value"][:10]
            if r.get("dod") and not p.get("dod"):
                p["dod"] = r["dod"]["value"][:10]
            label = r.get("posLabel", {}).get("value", "")
            if label and not _QID.match(label):
                p["positions"].append({"label": label, "start": r.get("start", {}).get("value", "")[:10],
                                       "end": r.get("end", {}).get("value", "")[:10]})
        if len({_qid(r["person"]["value"]) for r in rows}) < page:
            break
        time.sleep(pause)
    else:
        raise ValueError(f"More than {MAX_PAGES * page} people returned; stopping")
    if not people:
        raise ValueError("Wikidata returned nobody")
    ids = sorted(people)
    for i in range(0, len(ids), ALIAS_CHUNK):
        try:
            time.sleep(pause)
            for r in _ask(get, _query_aliases(country, ids[i:i + ALIAS_CHUNK]), pause):
                p = people.get(_qid(r["person"]["value"]))
                if p and r["alias"]["value"] not in p["aliases"] and len(p["aliases"]) < 12:
                    p["aliases"].append(r["alias"]["value"])
        except Exception:      # noqa: BLE001
            break              # the rest of the aliases are skipped; every name is still there
    snap = {"fetched_at": datetime.now(timezone.utc).isoformat(), "country": country,
            "people": sorted(people.values(), key=lambda p: p["id"])}
    try:
        time.sleep(pause)
        snap["politicians"] = fetch_politicians(get, country, page, pause)
    except Exception:      # noqa: BLE001
        snap["politicians"] = []    # an extra: the office holders above are still complete
    return snap

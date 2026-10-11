"""
Group the politically exposed person (PEP) matches of one screening so that one person listed by several
sources is shown once.

Why this is done on the result and not when the lists load: the records stay exactly as the sources published
them, so nothing is lost from the audit trail, a wrong grouping hides nothing, and it can be changed or switched
off without touching the lists. Only the report and the person counts use the groups.

Two PEP matches are grouped only when every one of these holds (the rules lean towards keeping records apart):

  * both are PEP records;
  * their CNICs do not conflict (both present and different keeps them apart);
  * their provinces do not conflict, and their dates of birth do not conflict (a year present on both and
    different keeps them apart; a missing value does not);
  * something other than the name agrees: the same CNIC, the same province, or the same year of birth;
  * a name of one matches a name of the other as a whole name (spelling variants, a different spelling of one part
    is fine). The "name sits inside a longer name" rule and matches that rely on an initial are NOT enough here.

A record joins a group only when it passes these tests against every record already in the group, so a chain
A~B, B~C never joins an A and a C that conflict with each other.
"""

import re

from app.screening.names import NameScorer, tokens
from app.screening.parsers import normalize_province

NAME_GROUP_SCORE = 90.0
SOURCE_NOTE = re.compile(r"Source:\s*(.+?)\s*$", re.S)


def is_pep(m: dict) -> bool:
    return (m.get("source") or "") == "pep"


def _names(m: dict) -> list:
    out, seen = [], set()
    for n in [m.get("primary_name"), m.get("matched_name"), *(m.get("aliases") or [])]:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _year(m: dict) -> str:
    found = re.search(r"(\d{4})", m.get("dob") or "")
    return found.group(1) if found else ""


def _province(m: dict) -> str:
    return normalize_province(m.get("province") or "")


def _names_agree(a: dict, b: dict) -> bool:
    for x in _names(a):
        scorer = NameScorer(x)
        for y in _names(b):
            score, kind = scorer._detail(tokens(y))
            if kind in ("exact", "close") and score >= NAME_GROUP_SCORE:
                return True
    return False


def same_person(a: dict, b: dict) -> bool:
    ca, cb = a.get("cnic") or "", b.get("cnic") or ""
    if ca and cb and ca != cb:
        return False
    pa, pb = _province(a), _province(b)
    if pa and pb and pa != pb:
        return False
    ya, yb = _year(a), _year(b)
    if ya and yb and ya != yb:
        return False
    corroborated = bool(ca and cb and ca == cb) or bool(pa and pb) or bool(ya and yb)
    return corroborated and _names_agree(a, b)


def source_label(m: dict) -> str:
    """Where this record came from, in a few words ("uploaded list", "Wikidata Q47863721", ...)."""
    found = SOURCE_NOTE.search(m.get("remarks") or "")
    text = found.group(1) if found else (m.get("list") or "PEP list")
    return text if len(text) <= 90 else text[:87] + "..."


def _basis(members: list) -> str:
    if len(members) < 2:
        return ""
    cnics = {m.get("cnic") for m in members}
    if len(cnics) == 1 and next(iter(cnics)):
        return "same CNIC and a matching name"
    parts = ["name"]
    if all(_province(m) for m in members) and len({_province(m) for m in members}) == 1:
        parts.append("province")
    if all(_year(m) for m in members) and len({_year(m) for m in members}) == 1:
        parts.append("year of birth")
    text = ", ".join(parts[:-1]) + " and " + parts[-1] if len(parts) > 1 else parts[0]
    if "year of birth" not in parts and not any(m.get("cnic") for m in members):
        text += " only (no date of birth or CNIC on every record to confirm it)"
    return text


def group_pep_matches(matches: list) -> list:
    """
    One entry per person, best score first: {"lead", "members", "listed_in", "basis", "other_names"}.
    `matches` are match dicts from the engine (non-PEP ones are ignored). Input is not modified.
    """
    ordered = sorted((m for m in matches if is_pep(m)), key=lambda m: (-float(m["score"]), 0 if m.get("dob") else 1))
    groups: list = []
    for m in ordered:
        for g in groups:
            if all(same_person(m, x) for x in g):
                g.append(m)
                break
        else:
            groups.append([m])
    out = []
    for g in groups:
        lead = g[0]
        listed_in, seen = [], set()
        for x in g:
            lab = source_label(x)
            if lab not in seen:
                seen.add(lab)
                listed_in.append(lab)
        names, seen_n = [], {lead.get("primary_name")}
        for x in g:
            for n in _names(x):
                if n not in seen_n:
                    seen_n.add(n)
                    names.append(n)
        out.append({"lead": lead, "members": g, "listed_in": listed_in, "basis": _basis(g), "other_names": names})
    return out

"""Politically exposed persons: classification, matching, status, upload and the Wikidata fetch (no network)."""
import json
from datetime import date

import pytest

from app import config
from app.screening import engine, loader, pep
from tests import fixtures as fx
from tests.conftest import API_HEADERS, USER_HEADERS


# ---- which positions count, and at which level --------------------------------

@pytest.mark.parametrize("label,expected", [
    ("Member of the National Assembly of Pakistan", ("National", "")),
    ("Member of the 15th National Assembly of Pakistan", ("National", "")),
    ("Senator", ("National", "")),
    ("Prime Minister of Pakistan", ("National", "")),
    ("Federal Minister for Defence", ("National", "")),
    ("Minister of Finance of Pakistan", ("National", "")),
    ("Governor of the State Bank of Pakistan", ("National", "")),
    ("Ambassador of Pakistan to the United States", ("National", "")),
    ("Member of the Provincial Assembly of the Punjab", ("Provincial", "Punjab")),
    ("Chief Minister of Sindh", ("Provincial", "Sindh")),
    ("Governor of Khyber Pakhtunkhwa", ("Provincial", "Khyber Pakhtunkhwa")),
    ("Speaker of the Provincial Assembly of Balochistan", ("Provincial", "Balochistan")),
    ("Minister of Education (Punjab)", ("Provincial", "Punjab")),
    ("Member of the Legislative Assembly of Gilgit-Baltistan", ("Provincial", "Gilgit-Baltistan")),
    ("Member of the Metropolitan Corporation Lahore", ("Local", "Punjab")),
    ("Member of the Karachi Metropolitan Corporation", ("Local", "Sindh")),
    ("Member of the Faisalabad Municipal Corporation", ("Local", "Punjab")),
    ("Member of the Municipal Corporation Multan", ("Local", "Punjab")),
    ("Member of the District Council Peshawar", ("Local", "Khyber Pakhtunkhwa")),
    ("councillor in the district council of Swat", ("Local", "Khyber Pakhtunkhwa")),
    ("councillor in the district council of Mardan", ("Local", "Khyber Pakhtunkhwa")),
    ("Member of the Islamabad Metropolitan Corporation", ("Local", "Islamabad Capital Territory")),
    ("councillor in the district council of Jhang", ("Local", "Punjab")),
    ("councillor in the district council of Nawabshah", ("Local", "Sindh")),
    ("councillor in the municipal corporation of Bahawalpur", ("Local", "Punjab")),
    ("councillor in the district council of Jamshoro", ("Local", "Sindh")),
    ("councillor in the district council of Rahim Yar Khan", ("Local", "Punjab")),
    ("councillor in the district council of Sheikhupura", ("Local", "Punjab")),
    ("councillor in the district council of Kasur", ("Local", "Punjab")),
    ("Mayor of Karachi", ("Local", "Sindh")),
    ("Member of the Hyderabad Municipal Corporation, Sindh", ("Local", "Sindh")),
    ("Member of the Greater Hyderabad Municipal Corporation", None),
    ("Member of the Hyderabad Municipal Corporation", ("Local", "Sindh")),
    ("councillor in the municipal corporation of Sukkar", ("Local", "Sindh")),
    ("councillor in the municipal corporation of Sialkot", ("Local", "Punjab")),
    ("councillor in the municipal corporation of Sargodha", ("Local", "Punjab")),
    ("councillor in the municipal corporation of Sahiwal", ("Local", "Punjab")),
    ("Mayor of London", None),
    ("Member of the Parliament of Bangladesh", None),
    ("Member of the Dhaka North City Corporation, Bangladesh", None),
    ("Member of the Municipal Corporation of Delhi", None),
    ("captain of Pakistan cricket team", None),
    ("Chief Minister of Gujarat", None),
    ("Governor of Reserve Bank of India", None),
    ("", None),
])
def test_classify_position(label, expected):
    # as the Wikidata path calls it: everyone fetched there holds Pakistani citizenship
    assert pep.classify_position(label, pakistani=True) == expected


# ---- the administrator's file --------------------------------------------------

def test_upload_parser_reads_levels_and_derives_them_from_the_position():
    records, info = pep.parse_pep_persons(fx.PEP_CSV)
    by = {r.primary: r for r in records}
    assert info["national"] == 1 and info["provincial"] == 3 and info["with_cnic"] == 1
    assert by["Zorawar Khanzada Mehtab"].pep_level == "National" and by["Zorawar Khanzada Mehtab"].cnic == "3520212345679"
    assert by["Quillon Faridani"].pep_level == "Provincial" and by["Quillon Faridani"].province == "Punjab"
    assert by["Thessaly Orakzai Bunerwal"].province == "Khyber Pakhtunkhwa"       # "KP" is the same province
    assert by["Mirzada Pelethrone"].province == "Sindh"
    assert all(r.source_key == "pep" and "PEP" in r.programs for r in records)


def test_upload_without_a_name_column_is_refused():
    with pytest.raises(ValueError):
        pep.parse_pep_persons("Position,Level\nSenator,National\n")


# ---- Wikidata ------------------------------------------------------------------

def test_snapshot_keeps_current_and_recent_offices_and_drops_the_rest():
    snap = {"people": [
        {"id": "Q1", "name": "Arbuthnot Rana", "aliases": ["A Rana"], "dob": "1961-03-04", "positions": [
            {"label": "Member of the National Assembly of Pakistan", "start": "2018-08-13", "end": ""},
            {"label": "Minister of Finance of Pakistan", "start": "2019-01-01", "end": "2020-06-01"}]},
        {"id": "Q2", "name": "Wyndham Baig", "aliases": [], "positions": [
            {"label": "Member of the Provincial Assembly of Sindh", "start": "2013-06-01", "end": "2018-05-31"}]},
        {"id": "Q3", "name": "Cricket Person", "aliases": [], "positions": [{"label": "captain of Pakistan cricket team"}]},
    ]}
    today = date(2026, 10, 10)
    recs, info = pep.records_from_snapshot(snap, today, lookback_years=5)
    assert [r.primary for r in recs] == ["Arbuthnot Rana"]                 # Q2 left office more than 5 years ago
    assert recs[0].pep_level == "National" and "A Rana" in recs[0].names and recs[0].dob == "1961-03-04"
    assert info["expired_positions"] == 2 and info["unclassified_positions"] == 1   # the 2020 ministry and the 2018 assembly seat
    assert info["top_unclassified"] == ["captain of Pakistan cricket team"]
    longer, _ = pep.records_from_snapshot(snap, today, lookback_years=10)
    assert {r.primary for r in longer} == {"Arbuthnot Rana", "Wyndham Baig"}
    current, _ = pep.records_from_snapshot(snap, today, lookback_years=0)
    assert [r.primary for r in current] == ["Arbuthnot Rana"]              # only the office with no end date counts
    assert recs[0].position.startswith("Member of the National Assembly of Pakistan (from 2018)")


def test_fetch_snapshot_pages_and_builds_people(monkeypatch):
    calls = []

    def get(url, params):
        calls.append(params["query"])
        return json.dumps(fx.WIKIDATA_ALIASES if "altLabel" in params["query"] else fx.WIKIDATA_PEOPLE)

    snap = pep.fetch_snapshot(get, pause=0)
    people = {p["id"]: p for p in snap["people"]}
    assert set(people) == {"Q9000001", "Q9000002", "Q9000003"}              # Q9000004 has no English label
    assert people["Q9000001"]["aliases"] == ["A. Z. Rana"] and people["Q9000001"]["dob"] == "1961-03-04"
    assert len(people["Q9000001"]["positions"]) == 2
    assert len(calls) == 3 and "wd:Q843" in calls[0]                         # a page of people, a chunk of aliases, a page of politicians
    assert "wdt:P106 wd:Q82955" in calls[2]
    assert "VALUES ?person" in calls[1] and "wd:Q9000001" in calls[1]       # aliases are asked for by id, which Wikidata answers quickly


def test_alias_failure_keeps_the_people_and_a_flaky_page_is_retried(monkeypatch):
    monkeypatch.setattr(pep.time, "sleep", lambda s: None)
    state = {"people_calls": 0}

    def get(url, params):
        q = params["query"]
        if "altLabel" in q:
            raise ConnectionError("504 Gateway Timeout")            # the alias query always fails
        if "P106" in q:
            return json.dumps({"results": {"bindings": []}})        # the politicians page is empty
        state["people_calls"] += 1
        if state["people_calls"] == 1:
            raise ConnectionError("504 Gateway Timeout")            # the first try of the people page fails once
        return json.dumps(fx.WIKIDATA_PEOPLE)

    snap = pep.fetch_snapshot(get, pause=0)
    assert {p["id"] for p in snap["people"]} == {"Q9000001", "Q9000002", "Q9000003"}
    assert all(p["aliases"] == [] for p in snap["people"]) and state["people_calls"] == 2


def test_people_that_cannot_be_fetched_raise_so_a_good_copy_is_never_replaced(monkeypatch):
    monkeypatch.setattr(pep.time, "sleep", lambda s: None)

    def get(url, params):
        raise ConnectionError("504 Gateway Timeout")

    with pytest.raises(ConnectionError):
        pep.fetch_snapshot(get, pause=0)


def test_error_text_does_not_carry_the_request_address_or_query():
    import requests
    resp = requests.Response()
    resp.status_code = 504
    resp.url = "https://query.wikidata.org/sparql?query=SELECT+%3Fperson+WHERE+%7B"
    err = requests.HTTPError("504 Server Error: Gateway Timeout for url: " + resp.url, response=resp)
    assert loader._short(err) == "HTTP 504 from query.wikidata.org"
    assert "query=" not in loader._short(ConnectionError("failed for https://x.example/a?query=SELECT+1"))


def test_fetch_snapshot_refuses_a_bad_country_id_and_an_empty_answer():
    with pytest.raises(ValueError):
        pep.fetch_snapshot(lambda u, p: "{}", country="Q843 } UNION { ?x ?y ?z")
    empty = json.dumps({"results": {"bindings": []}})
    with pytest.raises(ValueError):
        pep.fetch_snapshot(lambda u, p: empty, pause=0)


def test_wikidata_failure_keeps_the_last_good_copy(monkeypatch):
    from datetime import datetime, timedelta, timezone
    old = {"fetched_at": "2026-01-01", "people": [{"id": "Q1", "name": "Arbuthnot Rana", "aliases": [], "positions": [
        {"label": "Member of the National Assembly of Pakistan", "start": "2018-08-13", "end": ""}]}]}
    when = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    saved = []
    monkeypatch.setattr(loader, "db_pep", lambda kind: (json.dumps(old).encode(), {"uploaded_at": when}) if kind == "wikidata" else None)
    monkeypatch.setattr(loader, "db_pep_put", lambda *a: saved.append(a))
    monkeypatch.setattr(config, "PEP_WIKIDATA_ENABLED", True)

    def boom(*a, **k):
        raise ConnectionError("blocked")

    monkeypatch.setattr(pep, "fetch_snapshot", boom)
    monkeypatch.setattr(config, "PEP_ASSEMBLIES_ENABLED", True)
    monkeypatch.setattr(loader.pep_assemblies, "fetch_assemblies", lambda get, api: (
        [], [{"key": "na", "label": "N", "source": "x", "count": 0, "error": "blocked"}]))
    with pytest.raises(ConnectionError):
        loader.refresh_wikidata()
    assert not saved                                        # nothing was overwritten
    monkeypatch.setattr(loader, "_refresh_wikidata_in_background", lambda: setattr(loader, "_wd_error", "ConnectionError: blocked"))
    loader._wd_error = None
    loader._wikidata_snapshot()                              # an old copy asks for a refresh, which fails in the background
    snap, note = loader._wikidata_snapshot()
    assert snap == old and "last saved copy" in note
    g = loader._load_pep()
    assert g.available and len(g.records) == 1 and "last saved copy" in g.meta[0]["note"]
    loader._wd_error = None


def test_a_stale_copy_never_makes_screening_wait_for_wikidata(monkeypatch):
    started = []
    monkeypatch.setattr(loader, "db_pep", lambda kind: None)
    monkeypatch.setattr(config, "PEP_WIKIDATA_ENABLED", True)
    monkeypatch.setattr(loader, "_refresh_wikidata_in_background", lambda: started.append(1))
    loader._wd_error = None
    snap, note = loader._wikidata_snapshot()
    assert snap is None and started and "being fetched" in note
    with pytest.raises(loader.SourceUnavailable):
        loader._load_pep()                                   # unavailable (not clear) until the first fetch lands


def test_no_pep_data_at_all_is_unavailable_not_clear(fake_sources):
    fake_sources["pep"] = None
    r = engine.screen("Completely Unrelated Person")
    src = r["sources"]["PEP"]
    assert src["available"] is False and engine.source_status(src) == "NOT_CONFIGURED"
    statuses = {k: ("CLEAR" if k != "PEP" else "NOT_CONFIGURED") for k in r["sources"]}
    assert engine.overall_status(statuses) == "AUTO_CLEAR"                   # PEP_REQUIRED is off by default
    assert engine.overall_status(statuses, pep_required=True) == "MANUAL_REVIEW"


# ---- screening ---------------------------------------------------------------------

def test_pep_match_is_review_and_never_a_sanctions_hit(fake_sources):
    r = engine.screen("Zorawar Khanzada Mehtab")
    src = r["sources"]["PEP"]
    assert engine.source_status(src) == "REVIEW"
    top = src["matches"][0]
    assert top["pep_level"] == "National" and "National Assembly" in top["position"]
    assert r["pep_hit_count"] == 1 and r["sanctions_hit_count"] == 0 and r["hit"] is False
    statuses = {k: engine.source_status(v) for k, v in r["sources"].items()}
    assert engine.overall_status(statuses) == "MANUAL_REVIEW"
    assert "not a sanctions hit" in engine.describe_source(src, "REVIEW", 85)


def test_provincial_match_names_the_province(fake_sources):
    r = engine.screen("Quillon Faridani")
    top = r["sources"]["PEP"]["matches"][0]
    assert top["pep_level"] == "Provincial" and top["province"] == "Punjab"
    r2 = engine.screen("Quillon Faridani", province="Punjab")
    assert r2["sources"]["PEP"]["matches"][0]["province_match"] is True


def test_a_sanctions_hit_still_outranks_a_pep_match():
    statuses = {"UNSC": "HIT", "PEP": "REVIEW"}
    assert engine.overall_status(statuses) == "ESCALATE_TO_COMPLIANCE"


def test_screening_api_returns_manual_review_for_a_pep(client):
    r = client.post("/api/screen", json={"full_name": "Mirzada Pelethrone"}, headers=API_HEADERS)
    body = r.json()
    assert r.status_code == 200 and body["overall_status"] == "MANUAL_REVIEW"
    assert body["pep_hit_count"] == 1 and body["sanctions_hit_count"] == 0
    row = next(x for x in body["results"] if x["source"] == "PEP")
    assert row["status"] == "REVIEW" and row["matches"][0]["pep_level"] == "Provincial"
    assert row["matches"][0]["province"] == "Sindh"


# ---- upload API ----------------------------------------------------------------------

def _upload(client, body, headers=API_HEADERS, filename="pep.csv"):
    return client.post(f"/api/admin/pep?filename={filename}", content=body, headers={**headers, "Content-Type": "text/csv"})


def test_upload_requires_an_admin(client):
    assert _upload(client, fx.PEP_CSV, USER_HEADERS).status_code == 403
    assert client.post("/api/admin/pep", content=b"x").status_code == 401


def test_upload_stores_the_list_and_screening_uses_it(client, fake_sources, monkeypatch):
    from app import database as db
    fake_sources["pep"] = None
    monkeypatch.setattr(loader, "db_pep", db.pep_get)           # the real store this time
    r = _upload(client, fx.PEP_CSV)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["records"] == 4 and body["national"] == 1 and body["provincial"] == 3
    assert body["upload"]["loaded"] is True and body["upload"]["records"] == 4
    screen = client.post("/api/screen", json={"full_name": "Quillon Faridani"}, headers=API_HEADERS).json()
    assert next(x for x in screen["results"] if x["source"] == "PEP")["status"] == "REVIEW"
    assert client.get("/api/admin/pep", headers=USER_HEADERS).json()["upload"]["records"] == 4
    d = client.delete("/api/admin/pep", headers=API_HEADERS)
    assert d.status_code == 200 and d.json()["upload"]["loaded"] is False


def test_unreadable_upload_is_refused_and_keeps_the_old_list(client, fake_sources, monkeypatch):
    from app import database as db
    monkeypatch.setattr(loader, "db_pep", db.pep_get)
    assert _upload(client, fx.PEP_CSV).status_code == 200
    bad = _upload(client, "Position,Level\nSenator,National\n")
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "PEP_FILE_UNREADABLE"
    assert client.get("/api/admin/pep", headers=API_HEADERS).json()["upload"]["records"] == 4


def test_merge_keeps_one_record_per_person_and_prefers_the_first_source():
    a, _ = pep.parse_pep_persons("Name,Position\nArbuthnot Rana,Senator\n")
    b, _ = pep.records_from_snapshot({"people": [{"id": "Q1", "name": "Arbuthnot Rana", "aliases": [], "positions": [
        {"label": "Member of the National Assembly of Pakistan"}]}]}, date(2026, 10, 10))
    merged = pep.merge(a, b)
    assert len(merged) == 1 and merged[0].id.startswith("PEP-UP-")


def test_batch_counts_a_pep_separately_from_sanctions(client):
    from tests.test_batch import make_xlsx, post, wait
    bid = post(client, make_xlsx([["Zorawar Khanzada Mehtab", "", "", "", "", ""],
                                  ["Completely Unrelated Person", "", "", "", "", ""]])).json()["id"]
    rows = {x["full_name"]: x for x in wait(client, bid)["rows"]}
    pep_row = rows["Zorawar Khanzada Mehtab"]
    assert pep_row["pep"] == 1 and pep_row["sanctions"] == 0 and pep_row["overall_status"] == "MANUAL_REVIEW"
    assert rows["Completely Unrelated Person"]["pep"] == 0


# ---- assembly member lists and politicians -------------------------------------

ASSEMBLY_HTML = """
<table class="wikitable">
<tr><th colspan="3">Members by constituency</th></tr>
<tr><th>Constituency</th><th>Member</th><th>Party</th></tr>
<tr><td rowspan="2">NA-1 Chitral</td><td>Dr. Xanthippe Marwat<sup>[1]</sup></td><td>PTI</td></tr>
<tr><td>Mian Oberon Qureshi</td><td>PML(N)</td></tr>
<tr><td>NA-3</td><td>Vacant</td><td></td></tr>
<tr><td>NA-4</td><td>Zephyrine Lashari (died)</td><td>PPP</td></tr>
<tr><td>NA-5</td><td>Cornelius Abbasi Jatoi</td><td>Independent</td></tr>
</table>
<table><tr><th>Year</th><th>Speaker</th></tr><tr><td>2024</td><td>Somebody Else</td></tr></table>
"""


def _many(n):
    rows = "".join(f"<tr><td>NA-{i}</td><td>Member Number{chr(65 + i % 26)}{chr(97 + i % 26)} Khan</td><td>PTI</td></tr>" for i in range(n))
    return f"<table><tr><th>Constituency</th><th>Name</th><th>Party</th></tr>{rows}</table>"


def test_assembly_table_reader_handles_spans_titles_and_vacancies():
    from app.screening import pep_assemblies as pa
    members = pa.members_from_html(ASSEMBLY_HTML)
    names = [m["name"] for m in members]
    assert names == ["Dr. Xanthippe Marwat", "Mian Oberon Qureshi", "Cornelius Abbasi Jatoi"]    # vacant and died skipped
    first = members[0]
    assert "Xanthippe Marwat" in first["names"] and first["constituency"] == "NA-1 Chitral" and first["party"] == "PTI"
    assert members[1]["constituency"] == "NA-1 Chitral"            # carried down by the rowspan
    assert "Oberon Qureshi" in members[1]["names"]                  # the form without the honorific is also screened


def test_fetch_assemblies_tolerates_a_missing_page_and_reports_each_source():
    from app.screening import pep_assemblies as pa
    html = _many(12)

    def get(url, params):
        if params and params.get("page") == "Good Page":
            return json.dumps({"parse": {"text": html}})
        if params:
            return json.dumps({"error": {"code": "missingtitle"}})
        raise ConnectionError("blocked")

    srcs = [{"key": "a", "label": "A", "level": "National", "province": "", "titles": ["Missing", "Good Page"]},
            {"key": "b", "label": "B", "level": "Provincial", "province": "Sindh", "titles": ["Missing"]}]
    off = [{"key": "c", "label": "C", "level": "National", "province": "", "url": "https://example.org/members"}]
    assemblies, report = pa.fetch_assemblies(get, "https://w/api", srcs, off)
    assert [a["key"] for a in assemblies] == ["a"] and len(assemblies[0]["members"]) == 12
    by = {r["key"]: r for r in report}
    assert by["a"]["count"] == 12 and not by["a"]["error"]
    assert by["b"]["count"] == 0 and by["b"]["error"] and "http" not in by["b"]["error"]
    assert by["c"]["count"] == 0 and by["c"]["error"]


def test_snapshot_adds_assembly_members_and_politicians_without_duplicates():
    snap = {"people": [
        {"id": "Q1", "name": "Arbuthnot Rana", "aliases": [], "dob": "", "positions": [
            {"label": "Member of the National Assembly of Pakistan", "start": "2018-08-13", "end": ""}]},
        {"id": "Q2", "name": "Deceased Senator", "dod": "2020-01-01", "aliases": [], "positions": [
            {"label": "Senator", "start": "2012-03-01", "end": "2018-03-01"}]}],
        "politicians": [{"id": "Q3", "name": "Partyleader Example", "dob": "1950-01-01", "party": "Example Party"},
                        {"id": "Q4", "name": "Arbuthnot Rana", "dob": "", "party": "X"}],
        "assemblies": [
            {"key": "na", "label": "National Assembly (16th)", "level": "National", "province": "", "source": "en.wikipedia.org",
             "members": [{"name": "Arbuthnot Rana", "names": ["Arbuthnot Rana"], "constituency": "NA-1", "party": "PTI"},
                         {"name": "Newmember Khan", "names": ["Newmember Khan"], "constituency": "NA-2", "party": "PPP"}]},
            {"key": "sindh", "label": "Sindh Assembly (16th)", "level": "Provincial", "province": "Sindh", "source": "en.wikipedia.org",
             "members": [{"name": "Sindhi Member", "names": ["Sindhi Member"], "constituency": "PS-1", "party": ""}]}]}
    records, info = pep.records_from_snapshot(snap, today=date(2026, 6, 1))
    by = {r.primary: r for r in records}
    assert set(by) == {"Arbuthnot Rana", "Partyleader Example", "Newmember Khan", "Sindhi Member"}   # no dead person, no duplicate
    assert info["politicians"] == 1 and info["assembly_members"] == 2
    assert by["Partyleader Example"].pep_level == "Political figure" and "Example Party" in by["Partyleader Example"].position
    assert by["Newmember Khan"].pep_level == "National" and "NA-2" in by["Newmember Khan"].position
    assert by["Sindhi Member"].pep_level == "Provincial" and by["Sindhi Member"].province == "Sindh"


def test_refresh_keeps_working_when_wikidata_fails_but_assemblies_load(monkeypatch):
    saved = []
    monkeypatch.setattr(loader, "db_pep", lambda kind: None)
    monkeypatch.setattr(loader, "db_pep_put", lambda *a: saved.append(a))
    monkeypatch.setattr(config, "PEP_ASSEMBLIES_ENABLED", True)

    def boom(*a, **k):
        raise ConnectionError("504")

    monkeypatch.setattr(pep, "fetch_snapshot", boom)
    members = [{"name": f"Person{i} Khan", "names": [f"Person{i} Khan"], "constituency": "", "party": ""} for i in range(12)]
    monkeypatch.setattr(loader.pep_assemblies, "fetch_assemblies", lambda get, api: (
        [{"key": "na", "label": "National Assembly", "level": "National", "province": "", "source": "x", "members": members}],
        [{"key": "na", "label": "National Assembly", "source": "x", "count": 12, "error": ""}]))
    snap = loader.refresh_wikidata()
    assert saved and len(snap["assemblies"]) == 1 and snap["problems"] and snap["problems"][0].startswith("Wikidata:")
    # and when nothing at all can be fetched, nothing is saved
    saved.clear()
    monkeypatch.setattr(loader.pep_assemblies, "fetch_assemblies", lambda get, api: (
        [], [{"key": "na", "label": "N", "source": "x", "count": 0, "error": "page not found"}]))
    with pytest.raises(ConnectionError):
        loader.refresh_wikidata()
    assert not saved


def test_load_pep_lists_each_assembly_with_its_own_count_and_error(monkeypatch):
    members = [{"name": "Person Khan", "names": ["Person Khan"], "constituency": "NA-1", "party": "PTI"}]
    snap = {"fetched_at": "2026-10-01T00:00:00+00:00", "people": [], "politicians": [], "assemblies": [
        {"key": "na", "label": "National Assembly (16th)", "level": "National", "province": "", "source": "x", "members": members}],
        "assembly_report": [{"key": "na", "label": "National Assembly (16th)", "source": "x", "count": 1, "error": ""},
                            {"key": "gb", "label": "Gilgit-Baltistan Assembly", "source": "y", "count": 0, "error": "page not found"}]}
    monkeypatch.setattr(loader, "_wikidata_snapshot", lambda: (snap, None))
    monkeypatch.setattr(loader, "db_pep", lambda kind: None)
    g = loader._load_pep()
    rows = {m["list"]: m for m in g.meta}
    assert rows["PEP members: National Assembly (16th)"]["records"] == 1
    assert "page was not found" in rows["PEP members: Gilgit-Baltistan Assembly"]["note"]
    assert rows["PEP members: Gilgit-Baltistan Assembly"]["detail"] == "page not found"
    assert len(g.records) == 1


def test_page_get_retries_429_with_wikipedia_user_agent(monkeypatch):
    import requests
    from app import config
    from app.screening import loader

    monkeypatch.setattr(config, "PEP_PAGE_INTERVAL", 0)
    monkeypatch.setattr(loader.time, "sleep", lambda s: None)
    calls = []

    def fake_get(url, timeout, headers=None, **kw):
        calls.append(headers)
        if len(calls) < 3:
            resp = requests.Response()
            resp.status_code = 429
            resp.headers["Retry-After"] = "2"
            raise requests.HTTPError("429 Client Error: Too Many Requests", response=resp)
        r = requests.Response()
        r._content = b"ok"
        return r

    monkeypatch.setattr(loader, "_get", fake_get)
    assert loader._polite_page_get("https://en.wikipedia.org/w/api.php", {"a": "b"}) == "ok"
    assert len(calls) == 3
    assert all(h["User-Agent"] == config.WIKI_USER_AGENT for h in calls)


def test_page_get_gives_up_on_non_retryable_error(monkeypatch):
    import pytest
    import requests
    from app import config
    from app.screening import loader

    monkeypatch.setattr(config, "PEP_PAGE_INTERVAL", 0)
    n = []

    def fake_get(url, timeout, headers=None, **kw):
        if not url.endswith("robots.txt"):
            n.append(1)
        resp = requests.Response()
        resp.status_code = 404
        raise requests.HTTPError("404", response=resp)

    monkeypatch.setattr(loader, "_get", fake_get)
    with pytest.raises(requests.HTTPError):
        loader._polite_page_get("https://na.gov.pk/en/all_members.php", None)
    assert len(n) == 1


_KP_PAGE = "".join(
    f'<a href="https://www.pakp.gov.pk/members/member-{i}-pk-{i}-2024/">Member Number{i} PK-{i} Swat-{i}</a>'
    for i in range(1, 13)
) + ('<a href="https://www.pakp.gov.pk/members/x/">Rehana Ismail WR-1</a>'
     '<a href="https://www.pakp.gov.pk/members/y/">Mr. Gurpal Singh MR-2</a>'
     '<a href="https://www.pakp.gov.pk/members/">Members Listing</a>'
     '<a href="https://www.pakp.gov.pk/act">Acts PK-1</a>')


def test_kp_cards_parser_reads_name_seat_and_skips_other_links():
    from app.screening import pep_assemblies as pa
    got = pa.members_from_cards(_KP_PAGE)
    assert len(got) == 14
    assert got[0]["name"] == "Member Number1" and got[0]["constituency"] == "PK-1 Swat-1"
    assert any(m["constituency"] == "WR-1" for m in got)       # reserved seats are members too


def test_official_sources_are_used_without_wikipedia_and_pending_ones_are_reported(monkeypatch):
    from app import config
    from app.screening import pep_assemblies as pa
    monkeypatch.setattr(config, "PEP_WIKIPEDIA_FALLBACK", False)
    monkeypatch.setattr(config, "PEP_EXTRA_SOURCES", "")
    seen = []

    def get(url, params):
        seen.append(url)
        if "pakp.gov.pk" in url:
            return _KP_PAGE
        raise ConnectionError("blocked")

    assemblies, report = pa.fetch_assemblies(get, "https://en.wikipedia.org/w/api.php")
    assert not any("wikipedia" in u for u in seen)
    assert [a["key"] for a in assemblies] == ["kp_official"]
    by = {r["key"]: r for r in report}
    assert by["kp_official"]["count"] == 14 and by["senate_official"]["error"]
    assert "not set up" in by["gb_official"]["error"] and by["punjab_official"]["error"]


def test_extra_sources_add_and_replace_official_entries(monkeypatch):
    from app import config
    from app.screening import pep_assemblies as pa
    monkeypatch.setattr(config, "PEP_EXTRA_SOURCES", json.dumps([
        {"key": "punjab_official", "label": "Punjab", "province": "Punjab", "url": "https://www.pap.gov.pk/members/", "parser": "cards"},
        {"key": "bad", "url": "http://insecure.example/"}]))
    sources, pending = pa.official_sources()
    keys = [s["key"] for s in sources]
    assert "punjab_official" in keys and "bad" not in keys
    assert "punjab_official" not in [p["key"] for p in pending]


def _senate_html(n=12):
    rows = "".join(
        f'<tr><td>{i}</td><td><a href="/en/profile.php?uid={i}">Senator Name{i}</a>    (In vice of Someone Else)</td>'
        f'<td>March 2021 to March 2027</td><td>Pakistan Peoples Party Parliamentarians (PPPP)</td><td>Sindh</td>'
        f'<td><img src="x.jpg"></td></tr>' for i in range(1, n + 1))
    return ("<table><tr><th>S.No.</th><th>Name</th><th>Tenure</th><th>Party</th><th>Province</th><th>Picture</th></tr>"
            + rows + "</table>")


def test_senate_page_layout_is_read_and_the_in_vice_note_is_not_part_of_the_name():
    from app.screening import pep_assemblies as pa
    got = pa.members_from_html(_senate_html())
    assert len(got) == 12
    assert got[0]["name"] == "Senator Name1" or got[0]["name"] == "Name1"
    assert "vice" not in " ".join(got[0]["names"]).lower()
    assert "Peoples Party" in got[0]["party"]


def _sindh_html(n=12):
    body = "".join(
        f'<div><span>{chr(65 + i % 3)}</span><img src="x.jpg"><a href="javascript:void(0)">Member Person{i}</a>'
        f'<p>PS-{i} Karachi East-{i}</p></div>' for i in range(1, n + 1))
    body += '<div><a href="javascript:void(0)">Reserved Lady</a><p>RSW-146</p></div>'
    return "<html><body><h2>Members Directory</h2>" + body + "<p>Total: 13</p></body></html>"


def test_sindh_pairs_parser_reads_name_and_seat_including_reserved_seats():
    from app.screening import pep_assemblies as pa
    got = pa.members_from_pairs(_sindh_html())
    assert len(got) == 13
    assert got[0]["name"] == "Member Person1" and got[0]["constituency"] == "PS-1 Karachi East-1"
    assert got[-1]["name"] == "Reserved Lady" and got[-1]["constituency"] == "RSW-146"


def _balochistan_html(n=12):
    cards = "".join(
        f'<a href="https://pabalochistan.gov.pk/member-profile/{500 + i}"><span>Member Name</span>'
        f'<h5>Mr. Member Person{i}</h5><small>N-A</small><b>Tenure:</b> 2024-2029 <b>Constituency:</b> PB-{i} '
        f'<b>Seat Type:</b> Elected</a>' for i in range(1, n + 1))
    cards += ('<a href="/member-profile/900"><span>Member Name</span><h5>Ms. Reserved Lady N-A</h5>'
              '<b>Constituency:</b> Reserved</a><a href="/standing-committees">Committees</a>')
    return "<html><body>" + cards + "</body></html>"


def test_balochistan_profiles_parser_reads_name_and_constituency_and_skips_other_links():
    from app.screening import pep_assemblies as pa
    got = pa.members_from_profiles(_balochistan_html())
    assert len(got) == 13
    assert got[0]["constituency"] == "PB-1" and "N-A" not in " ".join(got[0]["names"])
    assert any("Reserved Lady" in n for n in got[-1]["names"]) and not got[-1]["name"].endswith("N-A")


def _robots_env(monkeypatch, robots_text, status=200):
    import requests
    from app import config
    from app.screening import loader
    monkeypatch.setattr(config, "PEP_PAGE_INTERVAL", 0)
    monkeypatch.setattr(config, "PEP_RESPECT_ROBOTS", True)
    loader._robots.clear()
    seen = []

    def fake_get(url, timeout, headers=None, **kw):
        seen.append(url)
        if url.endswith("robots.txt"):
            if status != 200:
                resp = requests.Response()
                resp.status_code = status
                raise requests.HTTPError(str(status), response=resp)
            r = requests.Response()
            r._content = robots_text.encode()
            return r
        r = requests.Response()
        r._content = b"<html>page</html>"
        return r

    monkeypatch.setattr(loader, "_get", fake_get)
    return loader, seen


def test_a_site_whose_robots_txt_disallows_us_is_not_read(monkeypatch):
    import pytest
    loader, seen = _robots_env(monkeypatch, "User-agent: *\nDisallow: /members/\n")
    with pytest.raises(loader.RobotsDisallowed):
        loader._polite_page_get("https://www.pap.gov.pk/members/listing/en/22?byname=true", None)
    assert not any("members/listing" in u for u in seen)


def test_robots_txt_that_only_blocks_other_agents_lets_us_read(monkeypatch):
    loader, _seen = _robots_env(monkeypatch, "User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n")
    assert "page" in loader._polite_page_get("https://www.pap.gov.pk/members/listing/en/22?byname=true", None)


def test_missing_robots_txt_allows_and_unreadable_robots_txt_blocks(monkeypatch):
    import pytest
    loader, _ = _robots_env(monkeypatch, "", status=404)
    assert "page" in loader._polite_page_get("https://pas.gov.pk/members/byname/33", None)
    loader, _ = _robots_env(monkeypatch, "", status=503)
    with pytest.raises(loader.RobotsDisallowed):
        loader._polite_page_get("https://pas.gov.pk/members/byname/33", None)


def _punjab_html(n=12):
    rows = "".join(
        f"<div><b>Member Person{i} | pp{i}@pap.gov.pk</b><p>PP-{i} (Lahore-{i}) | Lahore</p>"
        f"<p>Pakistan Muslim League (Nawaz)</p><p>5{i} Years</p></div>" for i in range(1, n + 1))
    rows += "<div><b>Reserved Lady</b><p>W-348 | Reserved Seats</p><p>Pakistan Muslim League (Nawaz)</p></div>"
    return "<html><body><h1>Members' Directory</h1>" + rows + "</body></html>"


def test_punjab_directory_layout_is_read_by_the_pairs_parser():
    from app.screening import pep_assemblies as pa
    got = pa.members_from_pairs(_punjab_html())
    assert len(got) == 13
    assert got[0]["name"] == "Member Person1" and "pap.gov.pk" not in " ".join(got[0]["names"])
    assert got[0]["constituency"].startswith("PP-1 ") and got[-1]["name"] == "Reserved Lady"


def test_a_saved_copy_made_from_other_sources_is_refetched_after_a_deploy(monkeypatch):
    from app.screening import nacta_store
    from app.screening import pep_assemblies
    started = []
    monkeypatch.setattr(config, "PEP_WIKIDATA_ENABLED", True)
    monkeypatch.setattr(config, "PEP_ASSEMBLIES_ENABLED", True)
    monkeypatch.setattr(nacta_store, "age_days", lambda _meta: 1)          # young: only the sources can make it outdated
    monkeypatch.setattr(loader, "_refresh_wikidata_in_background", lambda: started.append(1))
    old = {"people": [], "politicians": [], "assemblies": [], "assembly_report": []}      # no signature: the old code
    monkeypatch.setattr(loader, "db_pep", lambda kind: (json.dumps(old).encode(), {}))
    loader._wd_error = None
    loader._wikidata_snapshot()
    assert started == [1]
    current = {**old, "source_signature": pep_assemblies.source_signature()}
    monkeypatch.setattr(loader, "db_pep", lambda kind: (json.dumps(current).encode(), {}))
    loader._wikidata_snapshot()
    assert started == [1]                                                  # same sources: no refetch


def test_an_operator_listed_host_is_read_despite_robots_txt_and_other_hosts_are_not(monkeypatch):
    import pytest
    from app import config
    loader, seen = _robots_env(monkeypatch, "User-agent: *\nDisallow: /\n")
    monkeypatch.setattr(config, "PEP_ROBOTS_OVERRIDE_HOSTS", ("www.pap.gov.pk",))
    assert "page" in loader._polite_page_get("https://www.pap.gov.pk/members/listing/en/22", None)
    with pytest.raises(loader.RobotsDisallowed):
        loader._polite_page_get("https://www.pakp.gov.pk/members/", None)


def _punjab_page(n, total):
    rows = "".join(
        f'<div class="mpa_name"><strong><a href="/p/{i}">Member {i} Khan\t\t | m{i}@x.com\t</a></strong></div>\n'
        f'<div class="mpa_const">\n\t\tPP-{i} (City-{i}) | City\t</div>' for i in range(1, n + 1))
    return f"<html>{rows}<span>Showing <b>1</b> - <b>{n}</b> of <b>{total}</b> Records</span></html>"


def test_a_paged_member_list_that_shows_only_the_first_page_is_reported_not_accepted(monkeypatch):
    from app.screening import pep_assemblies as pa
    assert pa.expected_total(_punjab_page(10, 369)) == 369
    assert pa.expected_total("<html>no count</html>") is None
    src = {"key": "punjab_official", "label": "Punjab Assembly website (pap.gov.pk)", "level": "Provincial",
           "province": "Punjab", "url": "https://x.test/p", "parser": "pairs"}
    _a, report = pa.fetch_assemblies(lambda url, params: _punjab_page(12, 369), "", sources=[], official=[src])
    assert report[0]["count"] == 0 and "12 of the 369" in report[0]["error"]
    asm, report = pa.fetch_assemblies(lambda url, params: _punjab_page(12, 12), "", sources=[], official=[src])
    assert report[0]["count"] == 12 and not report[0]["error"]
    assert "limit=all" in next(s for s in pa.OFFICIAL_SOURCES if s["key"] == "punjab_official")["url"]


def test_punjab_minority_seats_are_read_like_the_other_reserved_seats():
    from app.screening import pep_assemblies as pa
    html = ('<div><a href="/p/1">Gen Member One</a></div><div>PP-5 (City-v) | City</div>'
            '<div><a href="/p/2">Minority Member Two | m@x.com</a></div><div>NM-368 | Reserved Seats</div>')
    got = pa.members_from_pairs(html)
    assert [m["name"] for m in got] == ["Gen Member One", "Minority Member Two"]


def test_a_failed_assembly_row_says_when_the_uploaded_list_covers_its_province(monkeypatch):
    snap = {"fetched_at": "2026-10-01T00:00:00+00:00", "people": [], "politicians": [], "assemblies": [],
            "assembly_report": [{"key": "punjab_official", "label": "Punjab Assembly website", "source": "u", "count": 0,
                                 "error": "ConnectTimeout", "province": "Punjab"},
                                {"key": "gb_official", "label": "Gilgit-Baltistan Assembly website", "source": "", "count": 0,
                                 "error": "official member list not set up yet", "province": "Gilgit-Baltistan"}]}
    csv = ("Name,Position,Level,Province\nAbdul Razzaq Khan,Member of the Punjab Assembly (PP-117),Provincial,Punjab\n"
           "Ahmad Iqbal Chaudhary,Member of the Punjab Assembly (PP-54),Provincial,Punjab\n")
    monkeypatch.setattr(loader, "_wikidata_snapshot", lambda: (snap, None))
    monkeypatch.setattr(loader, "db_pep", lambda kind: (csv.encode(), {"filename": "assemblies.csv"}) if kind == "upload" else None)
    rows = {m["list"]: m for m in loader._load_pep().meta}
    assert "Your uploaded list (2 members for Punjab) is used instead" in rows["PEP members: Punjab Assembly website"]["note"]
    assert rows["PEP members: Punjab Assembly website"]["detail"] == "ConnectTimeout"
    assert "uploaded list" not in rows["PEP members: Gilgit-Baltistan Assembly website"]["note"]


@pytest.mark.parametrize("raw,short", [
    ("HTTPSConnectionPool(host='www.pap.gov.pk', port=443): Max retries exceeded with url: /m (Caused by )", "did not answer"),
    ("HTTPSConnectionPool(host='x'): Max retries exceeded (Caused by NameResolutionError(\"HTTPSConnect)", "address could not be found"),
    ("the site's robots.txt does not allow automated access", "robots.txt"),
    ("429 Client Error: Too Many Requests for url", "fewer requests"),
    ("page not found", "page was not found"),
    ("only 12 of the 369 members the page lists were read (it shows them in pages)", "only part"),
])
def test_plain_reason_turns_a_raw_error_into_a_short_sentence(raw, short):
    from app.screening import pep_assemblies as pa
    assert short in pa.plain_reason(raw)


def test_the_full_error_text_is_kept_not_cut_at_140_characters():
    from app.screening import pep_assemblies as pa
    cause = ("ConnectTimeoutError(<HTTPSConnection(host='www.pap.gov.pk', port=443) at 0x7f>, "
             "'Connection to www.pap.gov.pk timed out. (connect timeout=30)')")
    exc = RuntimeError("HTTPSConnectionPool(host='www.pap.gov.pk', port=443): Max retries exceeded with url: "
                       "/members/listing/en/22?byname=true&limit=all (Caused by " + cause + ")")
    text = pa._why(exc)
    assert len(text) > 140 and "connect timeout=30" in text and text.endswith("))")


# ---- local government offices, and nothing from Bangladesh ----------------------

def test_local_office_makes_a_local_pep_and_the_higher_level_wins():
    snap = {"people": [
        {"id": "Q1", "name": "Aslam Lahori", "positions": [{"label": "Member of the Metropolitan Corporation Lahore"}]},
        {"id": "Q2", "name": "Bashir Senator", "positions": [{"label": "Member of the Municipal Corporation Multan"},
                                                              {"label": "Senator"}]},
    ]}
    recs, info = pep.records_from_snapshot(snap, date(2026, 10, 13), lookback_years=-1)
    by = {r.primary: r for r in recs}
    assert by["Aslam Lahori"].pep_level == "Local" and by["Aslam Lahori"].province == "Punjab"
    assert by["Aslam Lahori"].programs == "PEP, Local, Punjab"
    assert by["Bashir Senator"].pep_level == "National"
    assert info["unclassified_positions"] == 0


def test_no_bangladesh_data_reaches_a_pep_record():
    snap = {"people": [
        {"id": "Q3", "name": "Only Dhaka", "positions": [{"label": "Member of the Parliament of Bangladesh"}]},
        {"id": "Q4", "name": "Both Countries", "positions": [{"label": "Member of the Parliament of Bangladesh"},
                                                             {"label": "Member of the National Assembly of Pakistan"}]},
    ], "politicians": [{"id": "Q5", "name": "Odd Party", "party": "Bangladesh Nationalist Party"}]}
    recs, info = pep.records_from_snapshot(snap, date(2026, 10, 13), lookback_years=-1)
    names = {r.primary for r in recs}
    assert "Only Dhaka" not in names
    assert all(not pep.mentions_excluded_country(r.position, r.remarks, r.programs) for r in recs)
    both = next(r for r in recs if r.primary == "Both Countries")
    assert both.pep_level == "National" and "Bangladesh" not in both.remarks
    # an excluded office is left out on purpose and is not listed as a position that was missed
    assert not any("Bangladesh" in x for x in info["top_unclassified"])


def test_uploaded_pep_row_naming_bangladesh_is_dropped():
    records, _ = pep.parse_pep_persons("Name,Position\nA Khan,Member of the Parliament of Bangladesh\n"
                                        "B Khan,Member of the District Council Peshawar\n")
    assert [r.primary for r in records] == ["B Khan"] and records[0].pep_level == "Local"
    assert all("Bangladesh" not in r.remarks for r in records)


def test_snapshot_reports_how_many_peps_were_counted_per_level():
    snap = {"people": [
        {"id": "Q1", "name": "A One", "positions": [{"label": "Senator"}]},
        {"id": "Q2", "name": "B Two", "positions": [{"label": "Member of the Provincial Assembly of the Punjab"}]},
        {"id": "Q3", "name": "C Three", "positions": [{"label": "councillor in the district council of Kasur"}]},
        {"id": "Q4", "name": "D Four", "positions": [{"label": "Member of the Municipal Corporation of Delhi"}]},
    ]}
    recs, info = pep.records_from_snapshot(snap, date(2026, 10, 13), lookback_years=-1)
    assert info["by_level"] == {"National": 1, "Provincial": 1, "Local": 1}
    assert info["top_unclassified"] == ["Member of the Municipal Corporation of Delhi"]


def test_politicians_with_a_recorded_office_are_counted_once_and_reported():
    snap = {"people": [{"id": "Q1", "name": "Arsalan Islam Shaikh",
                        "positions": [{"label": "councillor in the municipal corporation of Sukkar"}]}],
            "politicians": [{"id": "Q1", "name": "Arsalan Islam Shaikh"}, {"id": "Q2", "name": "Only Politician"}]}
    recs, info = pep.records_from_snapshot(snap, date(2026, 10, 13), lookback_years=-1)
    assert len(recs) == 2
    assert info["by_level"] == {"Local": 1, "Political figure": 1}
    assert info["politicians_now_with_office"] == 1


def test_level_words_keep_national_and_provincial_before_local():
    from app.screening import pep
    assert pep._level_from_text("Provincial Council") == pep.PROVINCIAL
    assert pep._level_from_text("National Council") == pep.NATIONAL
    assert pep._level_from_text("Federal") == pep.NATIONAL
    assert pep._level_from_text("Local") == pep.LOCAL
    assert pep._level_from_text("Municipal Corporation") == pep.LOCAL
    assert pep._level_from_text("District Council") == pep.LOCAL
    assert pep._level_from_text("") == ""


# ---- review points: left-out records are reported, Hyderabad needs Pakistani context, short place names ----

def test_records_left_out_for_naming_bangladesh_are_counted():
    snap = {"people": [
        {"id": "Q3", "name": "Only Dhaka", "positions": [{"label": "Member of the Parliament of Bangladesh"}]},
        {"id": "Q4", "name": "Both", "positions": [{"label": "Member of the Parliament of Bangladesh"}, {"label": "Senator"}]},
    ]}
    _, info = pep.records_from_snapshot(snap, date(2026, 10, 13), lookback_years=-1)
    assert info["excluded_country"] == 1          # "Both" still has a Pakistani office and stays
    _, up = pep.parse_pep_persons("Name,Position\nA Khan,Member of the Parliament of Bangladesh\nB Khan,Senator\n")
    assert up["excluded_country"] == 1


def test_load_pep_tells_the_admin_how_many_were_left_out(monkeypatch):
    from app.screening import loader
    snap = {"fetched_at": "2026-10-13", "people": [
        {"id": "Q3", "name": "Only Dhaka", "positions": [{"label": "Member of the Parliament of Bangladesh"}]},
        {"id": "Q4", "name": "Real Senator", "positions": [{"label": "Senator"}]}]}
    monkeypatch.setattr(loader, "_wikidata_snapshot", lambda: (snap, None))
    monkeypatch.setattr(loader, "db_pep", lambda kind: None)
    data = loader._load_pep()
    note = " ".join(x.get("note", "") for x in data.meta)
    assert "1 people left out" in note and "Bangladesh" in note


def test_bare_hyderabad_needs_pakistani_context():
    label = "Hyderabad Municipal Corporation councillor"
    assert pep.classify_position(label) is None                              # e.g. an uploaded row: could be India's
    assert pep.classify_position(label, pakistani=True) == ("Local", "Sindh")
    assert pep.classify_position("Hyderabad Municipal Corporation, Sindh") == ("Local", "Sindh")
    assert pep.classify_position("Hyderabad Municipal Corporation, Pakistan") == ("Local", "Sindh")
    assert pep.classify_position("Greater Hyderabad Municipal Corporation", pakistani=True) is None
    assert pep.classify_position("Hyderabad Municipal Corporation India", pakistani=True) is None


@pytest.mark.parametrize("label,expected", [
    ("Mayor of Wah Cantonment", None),                         # bare "Mayor of ..." with only a wordy name is not enough
    ("Wah Cantonment Board member", ("Local", "Punjab")),
    ("Member of the Municipal Committee of Chaman", ("Local", "Balochistan")),
    ("Hub Town Committee member", ("Local", "Balochistan")),
    ("Member of the district council of Swat", ("Local", "Khyber Pakhtunkhwa")),
    ("Union Council chairman of Swat", ("Local", "Khyber Pakhtunkhwa")),
    ("City council member, Wah, Ohio", None),                  # ambiguous name, no body next to it and no Pakistan
    ("Union council member, Hub Chemicals Ltd", None),
    ("Wah Pakistan Municipal Committee member", ("Local", "Punjab")),
])
def test_short_place_names_need_the_local_body_beside_them(label, expected):
    assert pep.classify_position(label) == expected

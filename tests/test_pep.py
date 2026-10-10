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
    ("captain of Pakistan cricket team", None),
    ("Chief Minister of Gujarat", None),
    ("Governor of Reserve Bank of India", None),
    ("", None),
])
def test_classify_position(label, expected):
    assert pep.classify_position(label) == expected


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
    assert len(calls) == 2 and "wd:Q843" in calls[0]                         # one page of people, then one chunk of aliases
    assert "VALUES ?person" in calls[1] and "wd:Q9000001" in calls[1]       # aliases are asked for by id, which Wikidata answers quickly


def test_alias_failure_keeps_the_people_and_a_flaky_page_is_retried(monkeypatch):
    monkeypatch.setattr(pep.time, "sleep", lambda s: None)
    state = {"people_calls": 0}

    def get(url, params):
        q = params["query"]
        if "altLabel" in q:
            raise ConnectionError("504 Gateway Timeout")            # the alias query always fails
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

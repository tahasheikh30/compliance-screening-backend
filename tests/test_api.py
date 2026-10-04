"""API contract: auth, error envelope, screening flow, failure isolation, evidence download."""
from app.screening import loader
from tests import fixtures as fx
from tests.conftest import API_HEADERS


def _screen(client, **body):
    body.setdefault("full_name", "Completely Unrelated Person")
    return client.post("/api/screen", json=body, headers=API_HEADERS)


def _by_source(resp):
    return {r["source"]: r for r in resp.json()["results"]}


# ---- envelope / auth ---------------------------------------------------------

def test_validation_error_is_structured_and_does_not_echo_input(client):
    r = client.post("/api/screen", json={"full_name": "x"}, headers=API_HEADERS)
    assert r.status_code == 422
    body = r.json()
    assert isinstance(body["detail"], str) and body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["fields"][0]["field"] == "full_name" and "input" not in body["error"]["fields"][0]
    assert r.headers["X-Request-ID"] == body["error"]["request_id"]


def test_missing_vs_wrong_key_are_distinguishable(client):
    r1 = client.get("/api/applicants")
    r2 = client.get("/api/applicants", headers={"X-API-Key": "nope"})
    assert r1.status_code == r2.status_code == 401
    assert (r1.json()["error"]["code"], r2.json()["error"]["code"]) == ("AUTH_MISSING_KEY", "AUTH_INVALID_KEY")


def test_non_ascii_key_does_not_500(client):
    r = client.get("/api/applicants", headers=[(b"x-api-key", "café-key".encode("latin-1"))])
    assert r.status_code == 401


def test_unconfigured_api_key_is_503(client, monkeypatch):
    from app import auth
    monkeypatch.setattr(auth, "API_KEY", None)
    r = client.get("/api/applicants", headers=API_HEADERS)
    assert r.status_code == 503 and r.json()["error"]["code"] == "AUTH_NOT_CONFIGURED"


def test_health_is_open_and_404s_have_envelope(client):
    assert client.get("/api/health").json() == {"status": "ok"}
    r = client.get("/api/applicants/999999", headers=API_HEADERS)
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
    assert client.get("/api/nope", headers=API_HEADERS).json()["error"]["hint"]


def test_rate_limit_is_structured(client):
    last = None
    for _ in range(11):
        last = _screen(client)
    assert last.status_code == 429 and last.json()["error"]["code"] == "RATE_LIMITED"
    assert last.headers.get("Retry-After") == "60"


def test_request_id_echo_and_garbage_rejected(client):
    assert client.get("/api/health", headers={"X-Request-ID": "my-trace-abc123"}).headers["X-Request-ID"] == "my-trace-abc123"
    bad = "../../etc/passwd; drop table"
    assert client.get("/api/health", headers={"X-Request-ID": bad}).headers["X-Request-ID"] != bad


def test_500_response_still_has_cors_header(client, monkeypatch):
    from app import database
    monkeypatch.setattr(database, "insert_applicant", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db exploded")))
    r = client.post("/api/screen", json={"full_name": "Ali Khan Malik"},
                    headers={**API_HEADERS, "Origin": "http://localhost:5173"})
    assert r.status_code == 500 and r.json()["error"]["code"] == "INTERNAL_ERROR"
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_api_responses_are_not_cacheable(client):
    assert client.get("/api/applicants", headers=API_HEADERS).headers["Cache-Control"] == "no-store"


# ---- screening flow ----------------------------------------------------------

def test_clear_applicant_is_auto_clear_with_one_row_per_source(client):
    r = _screen(client)
    assert r.status_code == 200
    body = r.json()
    assert body["overall_status"] == "AUTO_CLEAR" and body["case_ref"].startswith("CS-")
    rows = _by_source(r)
    assert set(rows) == {"UNSC", "OFAC", "UKSL", "FIA_REDBOOK", "NACTA", "ADVERSE_MEDIA"}
    assert all(x["status"] == "CLEAR" and x["evidence_file"] is None for x in rows.values())
    assert body["threshold"] == 85 and body["records_screened"] > 0
    assert rows["UNSC"]["list_version"] == "2026-09-30T08:00:00.000Z"


def test_hit_escalates_and_evidence_is_downloadable_by_both_routes(client):
    r = _screen(client, full_name="Muhammad Ali Khan", dob="1975-03-04", nationality="Pakistan", threshold=90)
    body = r.json()
    assert body["overall_status"] == "ESCALATE_TO_COMPLIANCE" and body["sanctions_hit_count"] >= 1
    un = _by_source(r)["UNSC"]
    assert un["status"] == "HIT" and un["score"] >= 95 and un["matched_entry"] == "MOHAMMAD ALI KHAN"
    assert un["matches"][0]["id"] == "QDi.001" and un["matches"][0]["dob_year_match"] == "Yes"
    assert un["match_count"] == 1
    assert un["evidence_file"] and un["evidence_file"].endswith(".pdf")
    assert _by_source(r)["UKSL"]["evidence_file"] is None            # clear rows carry no PDF

    pdf = client.get(f"/api/evidence/{un['id']}", headers=API_HEADERS)
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf" and pdf.content[:5] == b"%PDF-"
    same = client.get(f"/api/applicants/{body['applicant_id']}/evidence", headers=API_HEADERS)
    assert same.status_code == 200 and same.content == pdf.content


def test_adverse_media_only_is_manual_review_with_evidence(client, fake_sources):
    fake_sources["news"] = fx.NEWS_RSS_HIT
    r = _screen(client, full_name="Bilal Ahmed Qureshi")
    assert r.json()["overall_status"] == "MANUAL_REVIEW"
    media = _by_source(r)["ADVERSE_MEDIA"]
    assert media["status"] == "REVIEW" and media["articles"][0]["keyword"] == "arrested" and media["evidence_file"]


def test_unrelated_cnic_and_father_name_are_stored_and_do_not_create_matches(client):
    r = _screen(client, cnic="42101-9999999-9", father_name="Nobody Known")
    assert r.json()["overall_status"] == "AUTO_CLEAR"
    assert client.get("/api/applicants", headers=API_HEADERS).json()[0]["cnic"] == "42101-9999999-9"


def test_history_endpoints_round_trip(client):
    aid = _screen(client, full_name="Muhammad Ali Khan").json()["applicant_id"]
    listing = client.get("/api/applicants", headers=API_HEADERS).json()
    assert listing[0]["id"] == aid and listing[0]["overall_status"] == "ESCALATE_TO_COMPLIANCE"
    full = client.get(f"/api/applicants/{aid}", headers=API_HEADERS).json()
    un = next(x for x in full["results"] if x["source"] == "UNSC")
    assert full["overall_status"] == "ESCALATE_TO_COMPLIANCE" and un["matches"][0]["id"] == "QDi.001"
    assert full["case_ref"].endswith(f"{aid:05d}")


# ---- resilience --------------------------------------------------------------

def test_failed_list_never_resolves_to_auto_clear(client, fake_sources):
    fake_sources["fail"].add(loader.UN_URL)
    r = _screen(client)
    assert r.status_code == 200
    rows = _by_source(r)
    assert rows["UNSC"]["status"] == "ERROR" and rows["UNSC"]["detail"].startswith("Not screened.")
    assert rows["OFAC"]["status"] == "CLEAR"                          # other sources unaffected
    assert r.json()["overall_status"] == "MANUAL_REVIEW"


def test_fia_unavailable_goes_to_manual_review_unless_fia_not_required(client, fake_sources, monkeypatch):
    fake_sources["fail"].add("https://www.fia.gov.pk/press-pub")
    r = _screen(client)
    assert _by_source(r)["FIA_REDBOOK"]["status"] == "NOT_CONFIGURED" and r.json()["overall_status"] == "MANUAL_REVIEW"
    from app import main
    monkeypatch.setattr(main, "FIA_REQUIRED", False)
    assert _screen(client).json()["overall_status"] == "AUTO_CLEAR"


def test_news_outage_is_manual_review(client, fake_sources):
    fake_sources["news_fail"] = True
    r = _screen(client)
    assert _by_source(r)["ADVERSE_MEDIA"]["status"] == "ERROR" and r.json()["overall_status"] == "MANUAL_REVIEW"


def test_evidence_pdf_failure_still_saves_the_hit(client, monkeypatch):
    from app import evidence
    monkeypatch.setattr(evidence, "generate_evidence_pdf", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    r = _screen(client, full_name="Muhammad Ali Khan")
    assert r.status_code == 200
    un = _by_source(r)["UNSC"]
    assert un["status"] == "HIT" and un["evidence_file"] is None and "could not be generated" in un["detail"]
    assert client.get("/api/applicants", headers=API_HEADERS).status_code == 200


def test_evidence_errors_are_explained(client, storage):
    rid = _by_source(_screen(client))["UNSC"]["id"]
    r = client.get(f"/api/evidence/{rid}", headers=API_HEADERS)
    assert r.status_code == 404 and r.json()["error"]["code"] == "EVIDENCE_NOT_GENERATED"
    assert client.get("/api/evidence/99999", headers=API_HEADERS).json()["error"]["code"] == "RESULT_NOT_FOUND"
    assert client.get("/api/applicants/99999/evidence", headers=API_HEADERS).json()["error"]["code"] == "APPLICANT_NOT_FOUND"
    hit = _by_source(_screen(client, full_name="Muhammad Ali Khan"))["UNSC"]
    for f in storage.joinpath("evidence").iterdir():
        f.unlink()
    r = client.get(f"/api/evidence/{hit['id']}", headers=API_HEADERS)
    assert r.json()["error"]["code"] == "EVIDENCE_FILE_MISSING"


def test_admin_refresh_reports_each_source_independently(client, fake_sources):
    fake_sources["fail"].add(loader.UK_URL)
    out = client.post("/api/admin/refresh", headers=API_HEADERS).json()
    assert out["UNSC"]["records"] == 3 and "error" in out["UKSL"] and out["OFAC"]["records"] > 0
    assert client.get("/api/admin/lists", headers=API_HEADERS).status_code == 200


def test_old_database_is_migrated_in_place(tmp_path, monkeypatch):
    import sqlite3
    from app import config, database
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE applicants (id INTEGER PRIMARY KEY AUTOINCREMENT, full_name TEXT NOT NULL, cnic TEXT, "
                "father_name TEXT, submitted_at TEXT NOT NULL, overall_status TEXT NOT NULL)")
    con.execute("CREATE TABLE screening_results (id INTEGER PRIMARY KEY AUTOINCREMENT, applicant_id INTEGER NOT NULL, "
                "source TEXT NOT NULL, matched_entry TEXT, score REAL, status TEXT NOT NULL, detail TEXT, "
                "evidence_file TEXT, checked_at TEXT NOT NULL)")
    con.execute("INSERT INTO applicants (full_name, submitted_at, overall_status) VALUES ('Old Row','2026-01-01T00:00:00','AUTO_CLEAR')")
    con.execute("INSERT INTO screening_results (applicant_id, source, status, checked_at) VALUES (1,'UNSC','CLEAR','2026-01-01T00:00:00')")
    con.commit(); con.close()
    monkeypatch.setattr(config, "DB_PATH", path)
    database.init_db()
    assert database.get_applicant(1)["full_name"] == "Old Row"
    row = database.get_results_for_applicant(1)[0]
    assert row["matches"] == [] and row["status"] == "CLEAR"


def test_partial_fia_book_is_reported_through_the_api(client, fake_sources):
    from tests.test_engine import _second_redbook
    _second_redbook(fake_sources, readable=False)
    r = _screen(client)
    fia = _by_source(r)["FIA_REDBOOK"]
    assert fia["status"] == "PARTIAL" and r.json()["overall_status"] == "MANUAL_REVIEW"
    assert fia["detail"].startswith("Incomplete screening.")
    assert {li["status"] == "OK" for li in fia["lists"]} == {True, False}
    # and it is stored, so reopening the screening from history shows the same thing
    again = client.get(f"/api/applicants/{r.json()['applicant_id']}", headers=API_HEADERS).json()
    assert next(x for x in again["results"] if x["source"] == "FIA_REDBOOK")["lists"] == fia["lists"]


# ---- NACTA -----------------------------------------------------------------

def _upload(client, body, filename="nacta.csv", content_type="text/csv"):
    return client.post(f"/api/admin/nacta?filename={filename}", content=body,
                       headers={**API_HEADERS, "Content-Type": content_type})


def test_matching_cnic_is_reported_even_when_the_name_is_completely_different(client):
    r = _screen(client, full_name="Totally Different Name", cnic="3740565359881")
    nacta = _by_source(r)["NACTA"]
    assert nacta["status"] == "HIT" and r.json()["overall_status"] == "ESCALATE_TO_COMPLIANCE"
    m = nacta["matches"][0]
    assert m["primary_name"] == "Muhammad Shakir" and m["cnic_match"] is True and m["cnic"] == "3740565359881"
    assert m["father_name"] == "Qabil Khan" and m["list"] == "NACTA Proscribed Persons (Fourth Schedule)"
    assert nacta["cnic_match"] is True and nacta["evidence_file"]


def test_name_match_on_nacta_with_a_different_cnic_is_flagged_as_differing(client):
    r = _screen(client, full_name="Muhammad Shakir", cnic="35202-1234567-1", father_name="Qabil Khan")
    m = _by_source(r)["NACTA"]["matches"][0]
    assert m["cnic_match"] is False and m["father_match"] is True and m["score"] == 100.0


def test_no_cnic_given_means_cnic_match_is_unknown_not_false(client):
    m = _by_source(_screen(client, full_name="Muhammad Shakir"))["NACTA"]["matches"][0]
    assert m["cnic_match"] is None and m["father_match"] is None


def test_alias_in_a_nacta_name_is_matched(client):
    m = _by_source(_screen(client, full_name="Babu Jhangvee"))["NACTA"]["matches"][0]
    assert m["primary_name"] == "Aamir Bilal" and m["matched_name"] == "Babu Jhangvee"


def test_placeholder_cnic_in_the_list_can_never_match(client):
    # the list holds 1111111111166 for one person: that is not an identifier
    r = _screen(client, full_name="Totally Different Name", cnic="1111111111166")
    assert _by_source(r)["NACTA"]["status"] == "CLEAR"


def test_invalid_cnic_from_the_applicant_is_ignored(client):
    assert _by_source(_screen(client, full_name="Totally Different Name", cnic="12345"))["NACTA"]["status"] == "CLEAR"


def test_missing_nacta_list_is_not_screened_and_blocks_auto_clear(client, fake_sources, monkeypatch):
    fake_sources["nacta"] = None
    r = _screen(client)
    nacta = _by_source(r)["NACTA"]
    assert nacta["status"] == "NOT_CONFIGURED" and nacta["detail"].startswith("Not screened.")
    assert r.json()["overall_status"] == "MANUAL_REVIEW"
    from app import main
    monkeypatch.setattr(main, "NACTA_REQUIRED", False)
    assert _screen(client).json()["overall_status"] == "AUTO_CLEAR"


def test_out_of_date_nacta_list_is_incomplete_not_clear(client, fake_sources):
    fake_sources["nacta_age_days"] = 45
    r = _screen(client)
    nacta = _by_source(r)["NACTA"]
    assert nacta["status"] == "PARTIAL" and "Out of date" in nacta["lists"][0]["status"]
    assert r.json()["overall_status"] == "MANUAL_REVIEW"


def test_upload_requires_the_api_key(client):
    assert client.post("/api/admin/nacta", content=b"x").status_code == 401


def test_upload_endpoint_validates_and_stores(client, fake_sources):
    fake_sources["nacta"] = "real"
    assert _upload(client, b"").json()["error"]["code"] == "NACTA_FILE_EMPTY"
    bad = _upload(client, b"just some words\nwith no headers\n")
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "NACTA_FILE_UNREADABLE"
    assert _upload(client, b'{"data": 5}', content_type="application/json").status_code == 422
    assert client.get("/api/admin/nacta", headers=API_HEADERS).json()["loaded"] is False

    good = "Name,Father Name,CNIC\nZara Test Person,Some Father,4210112345671\nAli Test,Other Father,N/A\n"
    r = _upload(client, good.encode())
    body = r.json()
    assert r.status_code == 200 and body["records"] == 2 and body["with_cnic"] == 1 and body["loaded"] is True
    assert any("Only 2 people" in w for w in body["warnings"])        # far fewer than the real list
    st = client.get("/api/admin/nacta", headers=API_HEADERS).json()
    assert st["source"] == "upload" and st["records"] == 2 and st["filename"] == "nacta.csv" and st["stale"] is False


def test_nothing_uploaded_yet_means_nacta_is_not_screened(client, fake_sources):
    fake_sources["nacta"] = "real"
    r = _screen(client)
    assert _by_source(r)["NACTA"]["status"] == "NOT_CONFIGURED"
    assert "Lists page" in _by_source(r)["NACTA"]["detail"] or "upload" in _by_source(r)["NACTA"]["detail"].lower()


def test_an_uploaded_list_is_used_by_the_next_screening(client, fake_sources):
    fake_sources["nacta"] = "real"
    csv = "Name,Father Name,CNIC\nZara Test Person,Some Father,4210112345671\n"
    assert _upload(client, csv.encode()).status_code == 200
    r = _screen(client, full_name="Zara Test Person", cnic="4210112345671")
    m = _by_source(r)["NACTA"]["matches"][0]
    assert m["primary_name"] == "Zara Test Person" and m["cnic_match"] is True
    # replacing the file replaces the list
    assert _upload(client, b"Name,CNIC\nSomeone Else,4210100000001\n").status_code == 200
    assert _by_source(_screen(client, full_name="Zara Test Person"))["NACTA"]["status"] == "CLEAR"


def test_a_rejected_upload_does_not_replace_the_current_list(client, fake_sources):
    fake_sources["nacta"] = "real"
    assert _upload(client, b"Name,CNIC\nZara Test Person,4210112345671\n").status_code == 200
    assert _upload(client, b"garbage with no headers").status_code == 422
    assert client.get("/api/admin/nacta", headers=API_HEADERS).json()["records"] == 1
    assert _by_source(_screen(client, full_name="Zara Test Person"))["NACTA"]["status"] == "HIT"


def test_json_upload_is_accepted(client, fake_sources):
    fake_sources["nacta"] = "real"
    import json
    body = json.dumps({"data": [{"Name": "Json Person", "Father Name": "F", "CNIC": "4210112345671"}]})
    r = _upload(client, body.encode(), "nacta.json", "application/json")
    assert r.status_code == 200 and r.json()["records"] == 1


def test_stale_uploaded_file_is_reported_by_the_status_endpoint(client, fake_sources, monkeypatch):
    fake_sources["nacta"] = "real"
    assert _upload(client, b"Name,CNIC\nZara Test Person,4210112345671\n").status_code == 200
    from app import config
    monkeypatch.setattr(config, "NACTA_MAX_AGE_DAYS", -1)           # any file is now "too old"
    assert client.get("/api/admin/nacta", headers=API_HEADERS).json()["stale"] is True
    r = _screen(client)
    assert _by_source(r)["NACTA"]["status"] == "PARTIAL" and r.json()["overall_status"] == "MANUAL_REVIEW"

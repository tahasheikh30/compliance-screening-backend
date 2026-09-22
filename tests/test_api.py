"""End-to-end API tests: error envelope shape, resilience, and the FIA edition endpoints."""
import io

from tests.conftest import make_table_pdf, sample_people, API_HEADERS


# ---------- structured error envelope ----------------------------------------

def test_validation_error_has_structured_fields_no_raw_input_echoed(client):
    r = client.post("/api/screen", json={"full_name": "x"}, headers=API_HEADERS)
    assert r.status_code == 422
    body = r.json()
    assert isinstance(body["detail"], str)  # not the raw pydantic list -> no "[object Object]" in the UI
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["fields"][0]["field"] == "full_name"
    assert "input" not in body["error"]["fields"][0]  # never echo the submitted value back
    assert "request_id" in body["error"] and r.headers["X-Request-ID"] == body["error"]["request_id"]


def test_missing_vs_wrong_key_are_distinguishable(client):
    r1 = client.get("/api/applicants")
    r2 = client.get("/api/applicants", headers={"X-API-Key": "nope"})
    assert r1.status_code == r2.status_code == 401
    assert r1.json()["error"]["code"] == "AUTH_MISSING_KEY"
    assert r2.json()["error"]["code"] == "AUTH_INVALID_KEY"


def test_non_ascii_key_does_not_500(client):
    # HTTP header VALUES only carry ISO-8859-1 bytes on the wire (ASGI decodes
    # them that way) — httpx's dict-of-str convenience API refuses non-ASCII
    # client-side, so exercise it via raw header bytes instead, the way a
    # real non-ASCII paste like "café-key" would actually arrive server-side.
    r = client.get("/api/applicants", headers=[(b"x-api-key", "café-key".encode("latin-1"))])
    assert r.status_code == 401  # not 500


def test_unconfigured_api_key_is_503_with_code(client, monkeypatch):
    from app import auth
    monkeypatch.setattr(auth, "API_KEY", None)
    r = client.get("/api/applicants", headers=API_HEADERS)
    assert r.status_code == 503 and r.json()["error"]["code"] == "AUTH_NOT_CONFIGURED"


def test_404_has_envelope_and_request_id(client):
    r = client.get("/api/applicants/999999", headers=API_HEADERS)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"
    assert "X-Request-ID" in r.headers


def test_unknown_route_has_structured_body_and_hint(client):
    r = client.get("/api/does-not-exist", headers=API_HEADERS)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"
    assert r.json()["error"]["hint"]


def test_rate_limit_body_is_structured(client):
    last = None
    for _ in range(11):
        last = client.post("/api/screen", json={"full_name": "Ali Khan Malik"}, headers=API_HEADERS)
    assert last.status_code == 429
    assert last.json()["error"]["code"] == "RATE_LIMITED"
    assert last.headers.get("Retry-After") == "60"


def test_client_supplied_request_id_is_echoed_back(client):
    r = client.get("/api/health", headers={"X-Request-ID": "my-trace-abc123"})
    assert r.headers["X-Request-ID"] == "my-trace-abc123"


def test_garbage_request_id_header_is_not_reflected(client):
    r = client.get("/api/health", headers={"X-Request-ID": "../../etc/passwd; drop table"})
    assert r.headers["X-Request-ID"] != "../../etc/passwd; drop table"


# ---------- resilience: one source failing must not sink the whole screen ----

def test_one_source_exception_does_not_500_the_whole_screen(client, monkeypatch):
    from app.screening import unsc
    def boom(*a, **k):
        raise RuntimeError("simulated corrupt cache")
    monkeypatch.setattr(unsc, "check", boom)
    r = client.post("/api/screen", json={"full_name": "Completely Clear Person"}, headers=API_HEADERS)
    assert r.status_code == 200
    body = r.json()
    by_source = {res["source"]: res for res in body["results"]}
    assert by_source["UNSC"]["status"] == "ERROR"
    assert "UNSC" in by_source["UNSC"]["detail"] or by_source["UNSC"]["detail"]
    assert by_source["OFAC"]["status"] in ("NOT_CONFIGURED",)  # unaffected, still ran
    assert body["overall_status"] == "MANUAL_REVIEW"  # never AUTO_CLEAR when a source errored


def test_500_response_still_has_cors_header(client, monkeypatch):
    """
    Regression check for middleware ordering: if request_context isn't
    outermost, a crash response bypasses CORSMiddleware and the browser
    reports an opaque network error instead of a readable one.
    """
    from app import database
    def boom(*a, **k):
        raise RuntimeError("db exploded")
    monkeypatch.setattr(database, "insert_applicant", boom)
    r = client.post("/api/screen", json={"full_name": "Ali Khan Malik"},
                    headers={**API_HEADERS, "Origin": "http://localhost:5173"})
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "INTERNAL_ERROR"
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert "X-Request-ID" in r.headers


def test_evidence_pdf_failure_still_saves_the_hit(client, monkeypatch, fia):
    people = sample_people(30)
    pdf = make_table_pdf(people)
    files = {"file": ("rb.pdf", pdf, "application/pdf")}
    r = client.post("/api/admin/fia-redbook/editions", files=files, headers=API_HEADERS)
    ed_id = r.json()["id"]
    client.post(f"/api/admin/fia-redbook/editions/{ed_id}/activate",
               json={"confirm_reviewed": True}, headers=API_HEADERS)

    from app import evidence as ev
    def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(ev, "generate_evidence_pdf", boom)

    r = client.post("/api/screen", json={"full_name": people[3][0]}, headers=API_HEADERS)
    assert r.status_code == 200
    fia_res = next(x for x in r.json()["results"] if x["source"] == "FIA_REDBOOK")
    assert fia_res["status"] == "HIT"
    assert fia_res["evidence_file"] is None
    assert "could not be generated" in fia_res["detail"]
    # the applicant record itself is not lost
    assert client.get("/api/applicants", headers=API_HEADERS).status_code == 200


# ---------- FIA edition endpoints, end to end --------------------------------

def test_fia_upload_review_activate_flow(client):
    people = sample_people(40)
    pdf = make_table_pdf(people)

    r = client.post("/api/admin/fia-redbook/editions",
                    files={"file": ("rb2026.pdf", pdf, "application/pdf")},
                    data={"notes": "downloaded from fia.gov.pk 2026-09-01"}, headers=API_HEADERS)
    assert r.status_code == 200
    ed = r.json()
    assert ed["status"] == "staged" and ed["names_found"] == 40

    listing = client.get("/api/admin/fia-redbook/editions", headers=API_HEADERS).json()
    assert listing["active_edition_id"] is None
    assert len(listing["editions"]) == 1

    entries = client.get(f"/api/admin/fia-redbook/editions/{ed['id']}/entries", headers=API_HEADERS).json()
    assert entries["total"] == 40

    png = client.get(f"/api/admin/fia-redbook/editions/{ed['id']}/page/1", headers=API_HEADERS)
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"

    # activation without confirmation is refused
    r = client.post(f"/api/admin/fia-redbook/editions/{ed['id']}/activate", json={}, headers=API_HEADERS)
    assert r.status_code == 400 and r.json()["error"]["code"] == "FIA_REVIEW_CONFIRMATION_REQUIRED"

    r = client.post(f"/api/admin/fia-redbook/editions/{ed['id']}/activate",
                    json={"confirm_reviewed": True, "note": "spot-checked 5 pages"}, headers=API_HEADERS)
    assert r.status_code == 200 and r.json()["status"] == "active"

    status = client.get("/api/admin/fia-redbook/status", headers=API_HEADERS).json()
    assert status["configured"] and status["names_found"] == 40

    # audit trail recorded both actions
    activity = client.get("/api/admin/activity", headers=API_HEADERS).json()
    actions = [a["action"] for a in activity]
    assert "fia_edition_staged" in actions and "fia_edition_activated" in actions

    # screening now finds someone from this edition, and the evidence PDF downloads
    r = client.post("/api/screen", json={"full_name": people[10][0]}, headers=API_HEADERS)
    fia_res = next(x for x in r.json()["results"] if x["source"] == "FIA_REDBOOK")
    assert fia_res["status"] == "HIT" and fia_res["list_version"] == ed["id"]
    dl = client.get(f"/api/evidence/{fia_res['id']}", headers=API_HEADERS)
    assert dl.status_code == 200 and dl.headers["content-type"] == "application/pdf"


def test_upload_rejects_non_pdf_with_specific_code(client):
    r = client.post("/api/admin/fia-redbook/editions",
                    files={"file": ("notes.txt", b"hello world", "text/plain")}, headers=API_HEADERS)
    assert r.status_code == 400 and r.json()["error"]["code"] == "FIA_WRONG_EXTENSION"


def test_upload_rejects_fake_pdf_extension_by_content(client):
    r = client.post("/api/admin/fia-redbook/editions",
                    files={"file": ("fake.pdf", b"not a real pdf, just renamed", "application/pdf")},
                    headers=API_HEADERS)
    assert r.status_code == 400 and r.json()["error"]["code"] == "FIA_NOT_A_PDF"


def test_delete_requires_auth_and_id_validation(client):
    r = client.delete("/api/admin/fia-redbook/editions/nonsense-id", headers=API_HEADERS)
    assert r.status_code == 404
    r = client.delete("/api/admin/fia-redbook/editions/nonsense-id")
    assert r.status_code == 401


def test_legacy_upload_endpoint_still_works(client):
    pdf = make_table_pdf(sample_people(20))
    r = client.post("/api/admin/fia-redbook/upload",
                    files={"file": ("legacy.pdf", pdf, "application/pdf")}, headers=API_HEADERS)
    assert r.status_code == 200 and r.json()["status"] == "INGESTED"
    assert client.get("/api/admin/fia-redbook/status", headers=API_HEADERS).json()["configured"] is True

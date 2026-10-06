"""Security hardening: network safety, response headers, audit trail, evidence integrity, access scoping."""
import hashlib

import pytest
import requests

from app import config, database
from app.screening import loader, parsers
from tests import fixtures as fx
from tests.conftest import (ADMIN_ID, API_HEADERS, PENDING_ID, USER_HEADERS, USER_ID, USER2_HEADERS)


# ---- outbound requests -----------------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://example.com/list.xml",            # not https
    "https://127.0.0.1/x", "https://localhost/x", "https://[::1]/x",
    "https://169.254.169.254/latest/meta-data",   # cloud metadata service
    "https://10.1.2.3/x", "https://192.168.0.10/x", "https://internal.local/x",
    "https://user:secret@example.com/x",
])
def test_unsafe_addresses_are_never_fetched(url):
    with pytest.raises(loader.UnsafeDownload):
        loader._check_url(url)


def test_public_https_address_is_allowed():
    loader._check_url("https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.xml")


def test_fia_links_scraped_from_a_page_must_stay_on_the_fia_site():
    html = ('<a href="https://fia.gov.pk.evil.example/RedBook.pdf">Red Book</a>'
            '<a href="http://169.254.169.254/RedBook.pdf">Red Book</a>'
            '<a href="https://fia.gov.pk@evil.example/RedBook.pdf">Red Book</a>'
            '<a href="/files/RedBook2026.pdf">Red Book 2026</a>')
    urls = [e["url"] for e in parsers.find_redbook_editions([{"data": html, "error": None}])]
    assert urls == ["https://www.fia.gov.pk/files/RedBook2026.pdf"]


class _FakeResp:
    def __init__(self, status=200, headers=None, chunks=(b"data",), url=""):
        self.status_code, self.headers, self._chunks, self.url = status, headers or {}, chunks, url
        self.is_redirect = status in (301, 302, 307, 308)

    @property
    def content(self):
        return self._content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def iter_content(self, n):
        yield from self._chunks

    def close(self):
        pass


def _session_returning(monkeypatch, *responses):
    calls = []
    it = iter(responses)
    monkeypatch.setattr(loader._session, "get", lambda url, **kw: (calls.append(url), next(it))[1])
    return calls


def test_download_is_capped(monkeypatch):
    monkeypatch.setattr(config, "MAX_DOWNLOAD_BYTES", 1000)
    _session_returning(monkeypatch, _FakeResp(chunks=[b"x" * 600, b"x" * 600]))
    with pytest.raises(loader.UnsafeDownload, match="larger"):
        loader._get("https://example.com/big")


def test_download_with_a_declared_oversize_length_is_refused_before_reading(monkeypatch):
    monkeypatch.setattr(config, "MAX_DOWNLOAD_BYTES", 1000)
    _session_returning(monkeypatch, _FakeResp(headers={"Content-Length": "999999"}, chunks=[]))
    with pytest.raises(loader.UnsafeDownload):
        loader._get("https://example.com/big")


def test_a_redirect_to_an_internal_address_is_not_followed(monkeypatch):
    calls = _session_returning(monkeypatch, _FakeResp(302, {"Location": "https://169.254.169.254/latest"}))
    with pytest.raises(loader.UnsafeDownload):
        loader._get("https://example.com/list")
    assert calls == ["https://example.com/list"]       # the internal address was never requested


def test_a_safe_redirect_is_followed_and_the_body_is_read(monkeypatch):
    calls = _session_returning(monkeypatch, _FakeResp(301, {"Location": "/moved"}), _FakeResp(chunks=[b"ab", b"cd"]))
    assert loader._get("https://example.com/list").content == b"abcd"
    assert calls == ["https://example.com/list", "https://example.com/moved"]


def test_redirect_loop_stops(monkeypatch):
    _session_returning(monkeypatch, *[_FakeResp(302, {"Location": "https://example.com/again"})] * 20)
    with pytest.raises(loader.UnsafeDownload, match="redirects"):
        loader._get("https://example.com/list")


# ---- response headers ------------------------------------------------------------------------------------

def test_security_headers_on_every_response(client):
    for r in (client.get("/api/health"), client.get("/api/applicants", headers=API_HEADERS), client.get("/api/nope")):
        h = r.headers
        assert h["X-Content-Type-Options"] == "nosniff" and h["X-Frame-Options"] == "DENY"
        assert "default-src 'none'" in h["Content-Security-Policy"]
        assert h["Strict-Transport-Security"].startswith("max-age=")
        assert "camera=()" in h["Permissions-Policy"]


def test_api_docs_are_not_exposed_by_default(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_cors_wildcard_is_refused():
    import importlib
    import os
    from app import main
    old = os.environ.get("ALLOWED_ORIGINS")
    os.environ["ALLOWED_ORIGINS"] = " https://a.example/ , * ,"
    try:
        importlib.reload(main)
        assert main.allowed_origins == ["https://a.example"]
    finally:
        if old is None:
            os.environ.pop("ALLOWED_ORIGINS")
        else:
            os.environ["ALLOWED_ORIGINS"] = old
        importlib.reload(main)


def test_large_responses_are_compressed(client):
    for _ in range(3):
        client.post("/api/screen", json={"full_name": "Muhammad Ali Khan", "threshold": 50}, headers=API_HEADERS)
    r = client.get("/api/applicants", headers={**API_HEADERS, "Accept-Encoding": "gzip"})
    assert r.status_code == 200 and r.headers.get("Content-Encoding") == "gzip"


# ---- admin-only detail -----------------------------------------------------------------------------------

def test_list_source_addresses_and_pdf_samples_are_admin_only(client, fake_sources):
    client.post("/api/screen", json={"full_name": "Someone Else"}, headers=API_HEADERS)    # loads the lists
    admin = client.get("/api/admin/lists", headers=API_HEADERS).json()
    user = client.get("/api/admin/lists", headers=USER_HEADERS).json()
    assert any("source" in li for g in admin.values() for li in g["lists"])
    assert not any("source" in li or "sample" in li for g in user.values() for li in g["lists"])
    assert all(g["records"] == admin[k]["records"] for k, g in user.items())     # the counts are still visible


def test_configured_nacta_url_does_not_crash_the_status_page(client, monkeypatch):
    """main.py used urlparse without importing it, so this endpoint raised NameError once NACTA_PERSONS_URL was set."""
    monkeypatch.setattr(config, "NACTA_PERSONS_URL", "https://nacta.example.gov.pk/list.json?token=SECRET")
    r = client.get("/api/admin/nacta", headers=API_HEADERS)
    assert r.status_code == 200 and r.json()["url_host"] == "nacta.example.gov.pk"
    assert "SECRET" not in r.text


# ---- audit trail ------------------------------------------------------------------------------------------

def test_actions_are_audited_without_personal_data(client, storage):
    r = client.post("/api/screen", json={"full_name": "Zebediah Quillfeather", "cnic": "4220112345671"},
                    headers=USER_HEADERS)
    aid = r.json()["applicant_id"]
    client.get(f"/api/applicants/{aid}", headers=USER_HEADERS)
    page = client.get("/api/admin/audit", headers=API_HEADERS).json()
    actions = [(e["action"], e["actor_email"]) for e in page["entries"]]
    assert ("screening.run", "ana@example.com") in actions
    assert ("screening.view", "ana@example.com") in actions
    assert "Zebediah" not in str(page) and "4220112345671" not in str(page)       # no PII in the trail
    run = next(e for e in page["entries"] if e["action"] == "screening.run")
    assert run["target_id"] == str(aid) and run["detail"]["overall_status"]


def test_audit_trail_is_admin_only(client):
    assert client.get("/api/admin/audit", headers=USER_HEADERS).status_code == 403
    assert client.get("/api/admin/audit/verify", headers=USER_HEADERS).status_code == 403


def test_audit_chain_verifies_and_detects_tampering(client, storage):
    for n in ("Alpha Person", "Beta Person", "Gamma Person"):
        client.post("/api/screen", json={"full_name": n}, headers=API_HEADERS)
    ok = client.get("/api/admin/audit/verify", headers=API_HEADERS).json()
    assert ok["ok"] is True and ok["checked"] >= 3 and len(ok["head"]) == 64

    # the database refuses an edit or a delete outright ...
    with pytest.raises(Exception, match="append only"):
        with storage.pool().connection() as conn:
            conn.execute("UPDATE audit_log SET action = 'nothing' WHERE id = 1")
    with pytest.raises(Exception, match="append only"):
        with storage.pool().connection() as conn:
            conn.execute("DELETE FROM audit_log WHERE id = 2")

    # ... and even someone who disables that guard cannot do it silently
    with storage.pool().connection() as conn:
        conn.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_change")
        conn.execute("UPDATE audit_log SET actor_email = 'someone.else@example.com' WHERE id = 2")
        conn.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_change")
    bad = client.get("/api/admin/audit/verify", headers=API_HEADERS).json()
    assert bad["ok"] is False and bad["first_bad_id"] == 2


def test_removing_an_entry_breaks_the_chain(client, storage):
    for n in ("Alpha Person", "Beta Person", "Gamma Person"):
        client.post("/api/screen", json={"full_name": n}, headers=API_HEADERS)
    with storage.pool().connection() as conn:
        conn.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_change")
        conn.execute("DELETE FROM audit_log WHERE id = 2")
        conn.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_change")
    assert client.get("/api/admin/audit/verify", headers=API_HEADERS).json()["ok"] is False


def test_user_changes_and_nacta_uploads_are_audited(client, storage):
    r = client.post(f"/api/admin/users/{PENDING_ID}/status", json={"status": "approved"}, headers=API_HEADERS)
    assert r.status_code == 200
    change = client.get("/api/admin/audit?action=user.change", headers=API_HEADERS).json()["entries"][0]
    assert change["actor_id"] == ADMIN_ID and change["target_id"] == PENDING_ID
    assert change["detail"] == {"status": "approved"}
    up = client.post("/api/admin/nacta?filename=n.csv", content=fx.NACTA_CSV.encode(), headers=API_HEADERS)
    assert up.status_code == 200
    entries = client.get("/api/admin/audit?action=nacta.upload", headers=API_HEADERS).json()["entries"]
    assert len(entries) == 1
    assert entries[0]["detail"]["sha256"] == hashlib.sha256(fx.NACTA_CSV.encode()).hexdigest()


def test_a_failing_audit_write_never_breaks_a_screening(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("audit store down")
    monkeypatch.setattr(database, "audit", boom)
    assert client.post("/api/screen", json={"full_name": "Alpha Person"}, headers=API_HEADERS).status_code == 200


# ---- evidence integrity & scoping ---------------------------------------------------------------------------

def _screen_with_hit(client, headers=API_HEADERS):
    from tests.test_api import _screen
    for name in ("Muhammad Ali Khan", "Mohammed Ali Khan", "Osama Bin Laden", "Ali Raza"):
        r = _screen(client, full_name=name, threshold=50)
        if r.json()["overall_status"] == "ESCALATE_TO_COMPLIANCE":
            return r.json()
    raise AssertionError("the synthetic lists should produce a hit for one of these names")


def test_evidence_download_carries_its_sha256(client):
    res = _screen_with_hit(client)
    r = client.get(f"/api/applicants/{res['applicant_id']}/evidence", headers=API_HEADERS)
    assert r.status_code == 200 and r.content[:4] == b"%PDF"
    assert r.headers["X-Content-SHA256"] == hashlib.sha256(r.content).hexdigest()
    assert any(e["action"] == "evidence.download"
               for e in client.get("/api/admin/audit", headers=API_HEADERS).json()["entries"])


def test_one_user_cannot_see_or_page_into_anothers_screenings(client):
    a = client.post("/api/screen", json={"full_name": "Alpha Person"}, headers=USER_HEADERS).json()
    assert client.get(f"/api/applicants/{a['applicant_id']}", headers=USER2_HEADERS).status_code == 404
    assert client.get(f"/api/applicants/{a['applicant_id']}/evidence", headers=USER2_HEADERS).status_code == 404
    assert client.get("/api/applicants", headers=USER2_HEADERS).json() == []


def test_applicant_list_pages_and_reports_the_total(client):
    for n in ("Alpha Person", "Beta Person", "Gamma Person", "Delta Person"):
        client.post("/api/screen", json={"full_name": n}, headers=API_HEADERS)
    r = client.get("/api/applicants?limit=2&offset=1", headers=API_HEADERS)
    assert r.status_code == 200 and len(r.json()) == 2 and r.headers["X-Total-Count"] == "4"
    assert [a["full_name"] for a in r.json()] == ["Gamma Person", "Beta Person"]       # newest first, skipping one
    assert client.get("/api/applicants?limit=0", headers=API_HEADERS).status_code == 422
    assert client.get("/api/applicants?limit=9999", headers=API_HEADERS).status_code == 422
    assert client.get("/api/applicants?status=AUTO_CLEAR", headers=API_HEADERS).headers["X-Total-Count"] == "4"
    assert client.get("/api/applicants?status=bogus", headers=API_HEADERS).status_code == 422


def test_buffered_body_works_like_a_normal_requests_response():
    """_get stores the capped body the way requests itself does, so .content and .text keep working."""
    r = requests.Response()
    r._content, r._content_consumed, r.encoding = "héllo".encode("utf-8"), True, "utf-8"
    assert r.content == "héllo".encode("utf-8") and r.text == "héllo"

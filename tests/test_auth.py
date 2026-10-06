"""Sign in: the app key plus a token on every request, the approval flow, per-user history, the machine key, and the database being down."""
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from app import auth, config
from tests.conftest import (ADMIN_ID, API_HEADERS, APP_KEY_HEADERS, JWT_SECRET, PENDING_HEADERS, PENDING_ID, REJECTED_HEADERS,
                            SERVICE_HEADERS, SUPABASE_URL, USER2_HEADERS, USER2_ID, USER_HEADERS, USER_ID, bearer,
                            make_token)


def _code(r):
    return r.json()["error"]["code"]


def _screen(client, headers, name="Completely Unrelated Person"):
    return client.post("/api/screen", json={"full_name": name}, headers=headers)


# ---- access tokens -----------------------------------------------------------

def test_the_app_key_alone_is_not_a_sign_in(client):
    r = client.get("/api/applicants", headers=APP_KEY_HEADERS)
    assert r.status_code == 401 and _code(r) == "AUTH_REQUIRED"
    r = client.post("/api/screen", json={"full_name": "Ali Khan"}, headers=APP_KEY_HEADERS)
    assert r.status_code == 401 and _code(r) == "AUTH_REQUIRED"


@pytest.mark.parametrize("header", ["Bearer not-a-jwt", "Basic abc", "Bearer ", "Bearer"])
def test_garbage_authorization_is_401(client, header):
    r = client.get("/api/applicants", headers={**APP_KEY_HEADERS, "Authorization": header})
    assert r.status_code == 401 and _code(r) == "AUTH_INVALID_TOKEN"


def test_valid_token_is_accepted(client):
    assert client.get("/api/applicants", headers=USER_HEADERS).status_code == 200


def test_expired_token_says_so(client):
    r = client.get("/api/applicants", headers=bearer(USER_ID, exp=int(time.time()) - 3600))
    assert r.status_code == 401 and _code(r) == "AUTH_TOKEN_EXPIRED"


@pytest.mark.parametrize("kw", [
    {"secret": "x" * 48},                                   # signed with some other secret
    {"aud": "someone-else"},
    {"iss": "https://evil.supabase.co/auth/v1"},
    {"role": "anon"},                                       # the public anon key is not a signed in user
    {"is_anonymous": True},
    {"sub": None},
    {"exp": None},
])
def test_tokens_that_must_be_refused(client, kw):
    r = client.get("/api/applicants", headers={**APP_KEY_HEADERS, "Authorization": "Bearer " + make_token(**{"sub": USER_ID, **kw})})
    assert r.status_code == 401 and _code(r) in ("AUTH_INVALID_TOKEN", "AUTH_TOKEN_EXPIRED")


def test_token_without_an_email_is_refused(client):
    tok = make_token(USER_ID, email="")
    assert client.get("/api/applicants", headers={**APP_KEY_HEADERS, "Authorization": "Bearer " + tok}).status_code == 401


def test_unsigned_token_is_refused(client):
    now = int(time.time())
    tok = jwt.encode({"sub": ADMIN_ID, "email": "a@b.c", "aud": "authenticated", "role": "authenticated",
                      "iss": f"{SUPABASE_URL}/auth/v1", "exp": now + 3600}, None, algorithm="none")
    r = client.get("/api/applicants", headers={**APP_KEY_HEADERS, "Authorization": "Bearer " + tok})
    assert r.status_code == 401


def test_hs256_without_a_configured_secret_fails_closed(client, monkeypatch):
    monkeypatch.setattr(config, "SUPABASE_JWT_SECRET", "")
    r = client.get("/api/applicants", headers=USER_HEADERS)
    assert r.status_code == 503 and _code(r) == "AUTH_NOT_CONFIGURED"


def test_public_key_tokens_work_and_a_wrong_key_is_refused(client, monkeypatch):
    key, other = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    monkeypatch.setattr(auth, "_signing_key", lambda token: key.public_key())
    good = {**APP_KEY_HEADERS, "Authorization": "Bearer " + make_token(USER_ID, secret=key, alg="ES256", headers={"kid": "k1"})}
    forged = {**APP_KEY_HEADERS, "Authorization": "Bearer " + make_token(USER_ID, secret=other, alg="ES256", headers={"kid": "k1"})}
    assert client.get("/api/applicants", headers=good).status_code == 200
    r = client.get("/api/applicants", headers=forged)
    assert r.status_code == 401 and _code(r) == "AUTH_INVALID_TOKEN"


def test_public_key_tokens_without_a_project_url_fail_closed(client, monkeypatch):
    key = ec.generate_private_key(ec.SECP256R1())
    monkeypatch.setattr(config, "SUPABASE_URL", "")
    tok = make_token(USER_ID, secret=key, alg="ES256", headers={"kid": "k1"})
    r = client.get("/api/applicants", headers={**APP_KEY_HEADERS, "Authorization": "Bearer " + tok})
    assert r.status_code == 503 and _code(r) == "AUTH_NOT_CONFIGURED"


def test_signing_key_outage_is_a_503_not_a_401(client, monkeypatch):
    from jwt.exceptions import PyJWKClientConnectionError
    key = ec.generate_private_key(ec.SECP256R1())

    def down(token):
        raise PyJWKClientConnectionError("could not fetch")
    monkeypatch.setattr(auth, "_signing_key", down)
    tok = make_token(USER_ID, secret=key, alg="ES256", headers={"kid": "k1"})
    r = client.get("/api/applicants", headers={**APP_KEY_HEADERS, "Authorization": "Bearer " + tok})
    assert r.status_code == 503 and _code(r) == "AUTH_UNAVAILABLE"


def test_unknown_key_ids_do_not_make_us_refetch_the_key_set_every_time(monkeypatch):
    from jwt.exceptions import PyJWKClientError
    calls = []

    class Fake:
        def get_signing_key_from_jwt(self, token):
            calls.append(1)
            raise PyJWKClientError("no such key")
    monkeypatch.setattr(auth, "_jwks", Fake())
    auth._bad_kids.clear()
    tok = make_token(USER_ID, secret=ec.generate_private_key(ec.SECP256R1()), alg="ES256", headers={"kid": "made-up"})
    for _ in range(5):
        with pytest.raises(PyJWKClientError):
            auth._signing_key(tok)
    assert len(calls) == 1
    auth._bad_kids.clear()


# ---- approval ----------------------------------------------------------------

def test_pending_user_can_see_their_status_but_nothing_else(client):
    me = client.get("/api/me", headers=PENDING_HEADERS)
    assert me.status_code == 200 and me.json()["status"] == "pending" and me.json()["role"] == "user"
    for r in (_screen(client, PENDING_HEADERS), client.get("/api/applicants", headers=PENDING_HEADERS),
              client.get("/api/admin/lists", headers=PENDING_HEADERS)):
        assert r.status_code == 403 and _code(r) == "ACCOUNT_PENDING"


def test_rejected_user_is_refused(client):
    r = _screen(client, REJECTED_HEADERS)
    assert r.status_code == 403 and _code(r) == "ACCOUNT_REJECTED"
    assert client.get("/api/me", headers=REJECTED_HEADERS).json()["status"] == "rejected"


def test_a_user_the_trigger_has_not_seen_becomes_pending(client, storage):
    newcomer = str(uuid.uuid4())
    h = bearer(newcomer, email="fresh@example.com")
    me = client.get("/api/me", headers=h).json()
    assert me["status"] == "pending" and me["email"] == "fresh@example.com"
    assert storage.get_profile(newcomer)["status"] == "pending"
    assert _screen(client, h).status_code == 403


def test_a_changed_email_is_picked_up(client, storage):
    client.get("/api/me", headers=bearer(USER_ID, email="ana.new@example.com"))
    assert storage.get_profile(USER_ID)["email"] == "ana.new@example.com"


def test_admin_approves_and_it_takes_effect_at_once(client):
    assert _screen(client, PENDING_HEADERS).status_code == 403
    r = client.post(f"/api/admin/users/{PENDING_ID}/status", json={"status": "approved"}, headers=API_HEADERS)
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert _screen(client, PENDING_HEADERS).status_code == 200


def test_rejecting_an_approved_user_takes_effect_at_once(client):
    assert _screen(client, USER_HEADERS).status_code == 200
    client.post(f"/api/admin/users/{USER_ID}/status", json={"status": "rejected"}, headers=API_HEADERS)
    r = _screen(client, USER_HEADERS)
    assert r.status_code == 403 and _code(r) == "ACCOUNT_REJECTED"


def test_only_admins_manage_users(client):
    for h in (USER_HEADERS, PENDING_HEADERS, REJECTED_HEADERS):
        assert client.get("/api/admin/users", headers=h).status_code == 403
    r = client.post(f"/api/admin/users/{USER2_ID}/status", json={"status": "rejected"}, headers=USER_HEADERS)
    assert r.status_code == 403 and _code(r) == "ADMIN_ONLY"
    r = client.post(f"/api/admin/users/{USER_ID}/role", json={"role": "admin"}, headers=USER_HEADERS)
    assert r.status_code == 403
    assert client.get("/api/admin/users", headers=API_HEADERS).status_code == 200


def test_user_list_and_status_filter(client):
    everyone = client.get("/api/admin/users", headers=API_HEADERS).json()
    assert len(everyone) == 5
    pending = client.get("/api/admin/users?status=pending", headers=API_HEADERS).json()
    assert [u["email"] for u in pending] == ["new@example.com"]
    assert client.get("/api/admin/users?status=bogus", headers=API_HEADERS).status_code == 422


def test_unknown_or_malformed_user_ids(client):
    r = client.post(f"/api/admin/users/{uuid.uuid4()}/status", json={"status": "approved"}, headers=API_HEADERS)
    assert r.status_code == 404 and _code(r) == "USER_NOT_FOUND"
    assert client.post("/api/admin/users/nope/status", json={"status": "approved"}, headers=API_HEADERS).status_code == 422
    assert client.post(f"/api/admin/users/{USER_ID}/status", json={"status": "root"}, headers=API_HEADERS).status_code == 422


def test_the_last_admin_cannot_be_removed(client):
    for body, path in (({"role": "user"}, "role"), ({"status": "rejected"}, "status"), ({"status": "pending"}, "status")):
        r = client.post(f"/api/admin/users/{ADMIN_ID}/{path}", json=body, headers=API_HEADERS)
        assert r.status_code == 409 and _code(r) == "LAST_ADMIN"
    # with a second admin the first can step down
    assert client.post(f"/api/admin/users/{USER_ID}/role", json={"role": "admin"}, headers=API_HEADERS).status_code == 200
    r = client.post(f"/api/admin/users/{ADMIN_ID}/role", json={"role": "user"}, headers=API_HEADERS)
    assert r.status_code == 200 and r.json()["role"] == "user"
    # ... and now the other one is the last
    r = client.post(f"/api/admin/users/{USER_ID}/role", json={"role": "user"}, headers=USER_HEADERS)
    assert r.status_code == 409


def test_new_admin_gets_admin_powers(client):
    assert client.get("/api/admin/users", headers=USER_HEADERS).status_code == 403
    client.post(f"/api/admin/users/{USER_ID}/role", json={"role": "admin"}, headers=API_HEADERS)
    assert client.get("/api/admin/users", headers=USER_HEADERS).status_code == 200


# ---- everyone sees only their own history -----------------------------------------

def test_users_see_only_their_own_screenings(client):
    a = _screen(client, USER_HEADERS, "Ana Screened This").json()["applicant_id"]
    b = _screen(client, USER2_HEADERS, "Bilal Screened That").json()["applicant_id"]

    mine = client.get("/api/applicants", headers=USER_HEADERS).json()
    assert [x["id"] for x in mine] == [a] and mine[0]["full_name"] == "Ana Screened This"
    assert [x["id"] for x in client.get("/api/applicants", headers=USER2_HEADERS).json()] == [b]

    # someone else's screening looks exactly like one that does not exist
    other = client.get(f"/api/applicants/{b}", headers=USER_HEADERS)
    missing = client.get("/api/applicants/99999", headers=USER_HEADERS)
    assert other.status_code == missing.status_code == 404 and other.json()["detail"] == missing.json()["detail"]
    assert client.get(f"/api/applicants/{a}", headers=USER_HEADERS).status_code == 200


def test_evidence_follows_the_same_rule(client):
    hit = _screen(client, USER_HEADERS, "Muhammad Ali Khan").json()
    un = next(r for r in hit["results"] if r["source"] == "UNSC")
    assert client.get(f"/api/applicants/{hit['applicant_id']}/evidence", headers=USER_HEADERS).status_code == 200
    assert client.get(f"/api/evidence/{un['id']}", headers=USER_HEADERS).content.startswith(b"%PDF")
    for url in (f"/api/applicants/{hit['applicant_id']}/evidence", f"/api/evidence/{un['id']}"):
        assert client.get(url, headers=USER2_HEADERS).status_code == 404
        assert client.get(url, headers=API_HEADERS).status_code == 200      # an admin may read anything


def test_admin_sees_everyones_history_and_who_ran_it(client):
    _screen(client, USER_HEADERS, "Ana Screened This")
    _screen(client, USER2_HEADERS, "Bilal Screened That")
    allrows = client.get("/api/applicants", headers=API_HEADERS).json()
    assert {(x["full_name"], x["screened_by"]) for x in allrows} == {
        ("Ana Screened This", "ana@example.com"), ("Bilal Screened That", "bilal@example.com")}
    assert client.get("/api/applicants?mine=true", headers=API_HEADERS).json() == []
    _screen(client, API_HEADERS, "Admin Screened")
    assert [x["full_name"] for x in client.get("/api/applicants?mine=true", headers=API_HEADERS).json()] == ["Admin Screened"]


def test_screenings_survive_their_user_being_removed(client, storage):
    aid = _screen(client, USER_HEADERS).json()["applicant_id"]
    with storage.pool().connection() as conn:
        conn.execute("DELETE FROM profiles WHERE id = %s", (uuid.UUID(USER_ID),))
    row = client.get(f"/api/applicants/{aid}", headers=API_HEADERS)
    assert row.status_code == 200
    assert client.get("/api/applicants", headers=API_HEADERS).json()[0]["screened_by"] is None


def test_each_person_has_their_own_rate_limit(client):
    last = [_screen(client, USER_HEADERS) for _ in range(11)][-1]
    assert last.status_code == 429
    assert _screen(client, USER2_HEADERS).status_code == 200


# ---- the app key: which app is calling ------------------------------------------------

PROTECTED = [("get", "/api/me"), ("get", "/api/applicants"), ("get", "/api/applicants/1"),
             ("post", "/api/screen"), ("get", "/api/evidence/1"), ("get", "/api/admin/lists"),
             ("get", "/api/admin/nacta"), ("post", "/api/admin/refresh"), ("post", "/api/admin/nacta"),
             ("get", "/api/admin/users")]


def _token_only(sub=ADMIN_ID):
    return {k: v for k, v in bearer(sub).items() if k == "Authorization"}


@pytest.mark.parametrize("method,path", PROTECTED)
def test_every_route_needs_the_app_key_even_with_a_valid_sign_in(client, method, path):
    r = getattr(client, method)(path, headers=_token_only(ADMIN_ID))
    assert r.status_code == 401 and _code(r) == "AUTH_MISSING_KEY"
    r = getattr(client, method)(path, headers={**_token_only(ADMIN_ID), "X-API-Key": "nope"})
    assert r.status_code == 401 and _code(r) == "AUTH_INVALID_KEY"


def test_the_app_key_is_checked_before_anything_else_is_revealed(client):
    # no sign in and no key: the answer is about the key, not the token
    r = client.get("/api/applicants")
    assert r.status_code == 401 and _code(r) == "AUTH_MISSING_KEY"
    r = client.get("/api/applicants", headers={"Authorization": "Bearer garbage"})
    assert _code(r) == "AUTH_MISSING_KEY"


def test_health_needs_neither(client):
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/health?deep=true").status_code == 200


def test_cors_preflight_for_the_app_headers_is_allowed(client):
    r = client.options("/api/applicants", headers={
        "Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "authorization,x-api-key,x-request-id"})
    assert r.status_code == 200
    allowed = r.headers["access-control-allow-headers"].lower()
    assert "x-api-key" in allowed and "authorization" in allowed


def test_a_non_ascii_app_key_is_a_clean_401_not_a_500(client):
    r = client.get("/api/applicants", headers=[(b"x-api-key", "café-key".encode("latin-1")),
                                               (b"authorization", _token_only(USER_ID)["Authorization"].encode())])
    assert r.status_code == 401 and _code(r) == "AUTH_INVALID_KEY"


def test_app_key_not_configured_fails_closed(client, monkeypatch):
    monkeypatch.setattr(config, "APP_API_KEY", "")
    r = client.get("/api/applicants", headers=USER_HEADERS)
    assert r.status_code == 503 and _code(r) == "AUTH_NOT_CONFIGURED"
    assert client.get("/api/health").status_code == 200


def test_the_app_key_check_can_be_switched_off_for_local_development(client, monkeypatch):
    monkeypatch.setattr(config, "REQUIRE_APP_KEY", False)
    assert client.get("/api/applicants", headers=_token_only(USER_ID)).status_code == 200
    assert client.get("/api/applicants").status_code == 401        # a sign in is still required


def test_the_app_key_never_opens_data_without_a_sign_in(client):
    for method, path in PROTECTED:
        if path == "/api/admin/nacta" and method == "post":
            continue                                                # covered by the NACTA permissions test
        r = getattr(client, method)(path, headers=APP_KEY_HEADERS)
        assert r.status_code == 401 and _code(r) == "AUTH_REQUIRED", (method, path)


# ---- the secret API key is a machine credential --------------------------------------------

def test_the_secret_api_key_cannot_read_applicant_data(client):
    for headers in (SERVICE_HEADERS, {**SERVICE_HEADERS, **_token_only(ADMIN_ID)}):
        r = client.get("/api/applicants", headers=headers)
        assert r.status_code == 401 and _code(r) == "AUTH_INVALID_KEY"      # it is not the app's key
    assert client.post("/api/screen", json={"full_name": "Ali Khan"}, headers=SERVICE_HEADERS).status_code == 401


def test_secret_api_key_not_configured_disables_only_the_scheduled_upload(client, monkeypatch):
    monkeypatch.setattr(auth, "API_KEY", None)
    body = b"name,cnic\nSome Person,3740565359881\n"
    r = client.post("/api/admin/nacta?filename=n.csv", content=body, headers={**SERVICE_HEADERS, "Content-Type": "text/csv"})
    assert r.status_code == 401
    assert client.get("/api/applicants", headers=USER_HEADERS).status_code == 200   # signed in users are unaffected


def test_nacta_upload_permissions(client, storage):
    body = b"name,cnic\nSome Person,3740565359881\n"
    up = lambda h: client.post("/api/admin/nacta?filename=n.csv", content=body, headers={**h, "Content-Type": "text/csv"})  # noqa: E731
    assert up(USER_HEADERS).status_code == 403
    assert up(PENDING_HEADERS).status_code == 403
    assert up({}).status_code == 401
    assert up({"X-API-Key": "nope"}).status_code == 401
    r = up(APP_KEY_HEADERS)                                  # the app key is public: on its own it never uploads
    assert r.status_code == 401 and _code(r) == "AUTH_REQUIRED"
    assert up({**SERVICE_HEADERS, "Authorization": "Bearer junk"}).status_code == 401   # the machine key means no sign in
    assert up(SERVICE_HEADERS).status_code == 200            # the scheduled workflow
    assert up(API_HEADERS).status_code == 200                # an admin using the app
    assert storage.nacta_meta()["records"] == 1


def test_only_admins_refresh_lists(client):
    assert client.post("/api/admin/refresh", headers=USER_HEADERS).status_code == 403
    assert client.get("/api/admin/lists", headers=USER_HEADERS).status_code == 200      # read only status is for everyone approved
    assert client.get("/api/admin/nacta", headers=USER_HEADERS).status_code == 200


# ---- the database ------------------------------------------------------------------

def test_nacta_list_is_stored_in_the_database_and_replaced_by_each_upload(client, storage):
    from app.screening import nacta_store
    h = {**API_HEADERS, "Content-Type": "text/csv"}
    client.post("/api/admin/nacta?filename=first.csv", content=b"name,cnic\nOne Person,3740565359881\n", headers=h)
    client.post("/api/admin/nacta?filename=second.csv", content=b"name,cnic\nTwo Person,3740565359882\nThree Person,3740565359883\n", headers=h)
    meta = nacta_store.meta()
    assert meta["filename"] == "second.csv" and meta["records"] == 2
    with storage.pool().connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM nacta_list").fetchone()["n"] == 1
    raw, _ = nacta_store.load()
    assert raw.startswith(b"name,cnic\nTwo Person")


def test_nacta_list_survives_a_server_restart(client, storage):
    """The bug this whole change exists for: an uploaded list must not vanish when the web server restarts."""
    from app.screening import nacta_store
    client.post("/api/admin/nacta?filename=n.csv", content=b"name,cnic\nOne Person,3740565359881\n",
                headers={**API_HEADERS, "Content-Type": "text/csv"})
    storage.close_pool()                                       # a restart: every connection is gone
    assert nacta_store.meta()["records"] == 1
    assert client.get("/api/admin/nacta", headers=API_HEADERS).json()["loaded"] is True


def test_history_and_evidence_survive_a_server_restart(client, storage):
    aid = _screen(client, USER_HEADERS, "Muhammad Ali Khan").json()["applicant_id"]
    storage.close_pool()
    assert client.get(f"/api/applicants/{aid}", headers=USER_HEADERS).status_code == 200
    assert client.get(f"/api/applicants/{aid}/evidence", headers=USER_HEADERS).content.startswith(b"%PDF")


def test_database_outage_is_a_clear_503(client, monkeypatch):
    import psycopg
    from app import database

    def down(*a, **k):
        raise psycopg.OperationalError("connection refused")
    monkeypatch.setattr(database, "list_applicants", down)
    r = client.get("/api/applicants", headers=USER_HEADERS)
    assert r.status_code == 503 and _code(r) == "DATABASE_UNAVAILABLE" and r.headers["Retry-After"] == "5"
    assert r.headers["X-Request-ID"] == r.json()["error"]["request_id"]


def test_missing_database_url_is_a_clear_503(client, monkeypatch):
    from app import database

    def nope(*a, **k):
        raise database.DatabaseNotConfigured("DATABASE_URL is not set")
    monkeypatch.setattr(database, "list_applicants", nope)
    r = client.get("/api/applicants", headers=USER_HEADERS)
    assert r.status_code == 503 and _code(r) == "DATABASE_NOT_CONFIGURED"


def test_deep_health_checks_the_database(client, monkeypatch):
    from app import database
    assert client.get("/api/health?deep=true").json() == {"status": "ok"}
    monkeypatch.setattr(database, "ping", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    assert client.get("/api/health?deep=true").status_code == 503
    assert client.get("/api/health").status_code == 200        # the plain check never touches the database


def test_secret_is_never_the_test_default_in_production_code():
    # the tests set their own secret; make sure nothing in the app ships a built in one
    import pathlib
    src = "".join(p.read_text() for p in pathlib.Path("app").rglob("*.py"))
    assert JWT_SECRET not in src and "test-key" not in src

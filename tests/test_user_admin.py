"""Disabling, re-enabling and deleting people (the People tab's buttons)."""
import uuid

import pytest

from app import config, supabase_admin
from tests.conftest import (ADMIN_ID, API_HEADERS, EMAILS, USER2_HEADERS, USER2_ID, USER_HEADERS, USER_ID, bearer)


def _code(r):
    return r.json()["error"]["code"]


def _status(storage, uid):
    p = storage.get_profile(uid)
    return p["status"] if p else None


@pytest.fixture
def auth_service(monkeypatch):
    """The sign in service is set up, and deleting an account there succeeds. `calls` lists what was deleted."""
    monkeypatch.setattr(config, "SUPABASE_SERVICE_ROLE_KEY", "test-service-key")
    calls = []
    monkeypatch.setattr(supabase_admin, "delete_auth_user", lambda uid: calls.append(uid) or True)
    return calls


def _screen(client, headers):
    return client.post("/api/screen", json={"full_name": "Completely Unrelated Person"}, headers=headers)


# ---- disable / enable -------------------------------------------------------

def test_disabling_locks_the_person_out_at_once_and_enabling_restores_them(client, fake_sources):
    assert _screen(client, USER_HEADERS).status_code == 200
    r = client.post(f"/api/admin/users/{USER_ID}/status", json={"status": "disabled"}, headers=API_HEADERS)
    assert r.status_code == 200 and r.json()["status"] == "disabled"
    blocked = _screen(client, USER_HEADERS)
    assert blocked.status_code == 403 and _code(blocked) == "ACCOUNT_DISABLED"
    assert client.get("/api/me", headers=USER_HEADERS).json()["status"] == "disabled"     # the app can tell them why
    assert client.get("/api/admin/users?status=disabled", headers=API_HEADERS).json()[0]["id"] == USER_ID
    r = client.post(f"/api/admin/users/{USER_ID}/status", json={"status": "approved"}, headers=API_HEADERS)
    assert r.status_code == 200
    assert _screen(client, USER_HEADERS).status_code == 200


def test_an_admin_cannot_disable_themselves(client):
    r = client.post(f"/api/admin/users/{ADMIN_ID}/status", json={"status": "disabled"}, headers=API_HEADERS)
    assert r.status_code == 409 and _code(r) == "CANNOT_DISABLE_SELF"


def test_the_last_admin_cannot_be_disabled(storage):
    with pytest.raises(storage.LastAdminError):
        storage.update_profile(ADMIN_ID, status="disabled")


def test_a_disabled_admin_has_no_admin_powers(client):
    client.post(f"/api/admin/users/{USER_ID}/role", json={"role": "admin"}, headers=API_HEADERS)
    client.post(f"/api/admin/users/{USER_ID}/status", json={"status": "disabled"}, headers=API_HEADERS)
    assert client.get("/api/admin/users", headers=USER_HEADERS).status_code == 403


def test_a_batch_stops_when_its_owner_is_disabled(client, storage, monkeypatch):
    import time
    from tests.test_batch import ROWS, _slow_screening, make_xlsx, post
    gate, started = _slow_screening(monkeypatch)
    bid = post(client, make_xlsx(ROWS)).json()["id"]
    assert started.wait(5)
    client.post(f"/api/admin/users/{USER_ID}/status", json={"status": "disabled"}, headers=API_HEADERS)
    gate.set()
    for _ in range(100):                                         # the person can no longer poll, so look in the database
        with storage.pool().connection() as conn:
            status = conn.execute("SELECT status FROM batches WHERE id = %s", (bid,)).fetchone()["status"]
        if status != "running":
            break
        time.sleep(0.1)
    with storage.pool().connection() as conn:
        screened = conn.execute("SELECT count(*) AS n FROM batch_rows WHERE batch_id = %s AND state = 'screened'",
                                (bid,)).fetchone()["n"]
    assert status == "cancelled" and screened == 1               # the row in progress finished, nothing after it


# ---- delete -----------------------------------------------------------------

def test_deleting_removes_the_person_but_keeps_their_screenings(client, storage, auth_service):
    with storage.pool().connection() as conn:
        conn.execute("INSERT INTO applicants (user_id, full_name, submitted_at, overall_status) "
                     "VALUES (%s, 'Kept Record', now(), 'AUTO_CLEAR')", (USER2_ID,))
    r = client.delete(f"/api/admin/users/{USER2_ID}", headers=API_HEADERS)
    assert r.status_code == 200
    assert r.json() == {"id": USER2_ID, "email": EMAILS[USER2_ID], "sign_in_removed": True}
    assert auth_service == [USER2_ID]
    assert storage.get_profile(USER2_ID) is None
    with storage.pool().connection() as conn:
        row = conn.execute("SELECT user_id FROM applicants WHERE full_name = 'Kept Record'").fetchone()
        audit = conn.execute("SELECT detail FROM audit_log WHERE action = 'user.delete'").fetchone()
    assert row is not None and row["user_id"] is None            # the screening stays, with nobody's name on it
    assert audit is not None and audit["detail"]["email"] == EMAILS[USER2_ID]
    listed = [u["id"] for u in client.get("/api/admin/users", headers=API_HEADERS).json()]
    assert USER2_ID not in listed


def test_a_deleted_persons_old_token_does_not_create_a_new_profile(client, storage, auth_service):
    client.delete(f"/api/admin/users/{USER2_ID}", headers=API_HEADERS)
    r = client.get("/api/me", headers=USER2_HEADERS)
    assert r.status_code == 401 and _code(r) == "AUTH_ACCOUNT_DELETED"
    assert storage.get_profile(USER2_ID) is None                 # no ghost "pending" entry
    # a brand new account is unaffected
    assert client.get("/api/me", headers=bearer(str(uuid.uuid4()), email="fresh@example.com")).status_code == 200


def test_delete_is_refused_when_the_sign_in_service_is_not_set_up(client, storage, monkeypatch):
    monkeypatch.setattr(config, "SUPABASE_SERVICE_ROLE_KEY", "")
    r = client.delete(f"/api/admin/users/{USER2_ID}", headers=API_HEADERS)
    assert r.status_code == 503 and _code(r) == "USER_DELETE_NOT_CONFIGURED"
    assert _status(storage, USER2_ID) == "approved"              # nothing changed


def test_if_the_sign_in_service_fails_the_person_is_left_disabled_not_active(client, storage, monkeypatch):
    monkeypatch.setattr(config, "SUPABASE_SERVICE_ROLE_KEY", "test-service-key")

    def boom(uid):
        raise supabase_admin.AuthServiceError("the authentication service answered 500")

    monkeypatch.setattr(supabase_admin, "delete_auth_user", boom)
    r = client.delete(f"/api/admin/users/{USER2_ID}", headers=API_HEADERS)
    assert r.status_code == 502 and _code(r) == "USER_DELETE_FAILED"
    assert _status(storage, USER2_ID) == "disabled"             # locked out, and Delete can simply be retried
    assert client.get("/api/me", headers=USER2_HEADERS).json()["status"] == "disabled"


def test_cannot_delete_yourself_or_an_unknown_or_without_being_admin(client, auth_service):
    r = client.delete(f"/api/admin/users/{ADMIN_ID}", headers=API_HEADERS)
    assert r.status_code == 409 and _code(r) == "CANNOT_DELETE_SELF"
    assert client.delete(f"/api/admin/users/{uuid.uuid4()}", headers=API_HEADERS).status_code == 404
    r = client.delete(f"/api/admin/users/{USER2_ID}", headers=USER_HEADERS)
    assert r.status_code == 403 and _code(r) == "ADMIN_ONLY"
    assert auth_service == []


def test_the_last_admin_cannot_be_deleted(storage):
    with pytest.raises(storage.LastAdminError):
        storage.delete_profile(ADMIN_ID)
    assert storage.get_profile(ADMIN_ID) is not None


def test_an_already_removed_sign_in_account_is_fine(client, storage, monkeypatch):
    monkeypatch.setattr(config, "SUPABASE_SERVICE_ROLE_KEY", "test-service-key")
    monkeypatch.setattr(supabase_admin, "delete_auth_user", lambda uid: False)            # 404 at Supabase
    r = client.delete(f"/api/admin/users/{USER2_ID}", headers=API_HEADERS)
    assert r.status_code == 200 and r.json()["sign_in_removed"] is False
    assert storage.get_profile(USER2_ID) is None


# ---- the Supabase client ----------------------------------------------------

class _Resp:
    def __init__(self, code):
        self.status_code = code


def test_supabase_client_calls_the_admin_api_with_the_key(monkeypatch):
    seen = {}

    def fake_delete(url, headers, timeout):
        seen.update(url=url, headers=headers, timeout=timeout)
        return _Resp(200)

    monkeypatch.setattr(supabase_admin.requests, "delete", fake_delete)
    monkeypatch.setattr(config, "SUPABASE_URL", "https://p.supabase.co")
    monkeypatch.setattr(config, "SUPABASE_SERVICE_ROLE_KEY", "eyJlegacy.jwt.key")
    assert supabase_admin.delete_auth_user("abc") is True
    assert seen["url"] == "https://p.supabase.co/auth/v1/admin/users/abc" and seen["timeout"] > 0
    assert seen["headers"] == {"apikey": "eyJlegacy.jwt.key", "Authorization": "Bearer eyJlegacy.jwt.key"}
    monkeypatch.setattr(config, "SUPABASE_SERVICE_ROLE_KEY", "sb_secret_xyz")           # the newer, non JWT key
    supabase_admin.delete_auth_user("abc")
    assert seen["headers"] == {"apikey": "sb_secret_xyz"}


@pytest.mark.parametrize("code,expected", [(204, True), (404, False)])
def test_supabase_client_success_and_already_gone(monkeypatch, code, expected):
    monkeypatch.setattr(supabase_admin.requests, "delete", lambda *a, **k: _Resp(code))
    monkeypatch.setattr(config, "SUPABASE_URL", "https://p.supabase.co")
    monkeypatch.setattr(config, "SUPABASE_SERVICE_ROLE_KEY", "k")
    assert supabase_admin.delete_auth_user("abc") is expected


def test_supabase_client_errors(monkeypatch):
    monkeypatch.setattr(config, "SUPABASE_URL", "https://p.supabase.co")
    monkeypatch.setattr(config, "SUPABASE_SERVICE_ROLE_KEY", "k")
    monkeypatch.setattr(supabase_admin.requests, "delete", lambda *a, **k: _Resp(401))
    with pytest.raises(supabase_admin.AuthServiceError):
        supabase_admin.delete_auth_user("abc")

    def down(*a, **k):
        raise supabase_admin.requests.ConnectionError("no route")

    monkeypatch.setattr(supabase_admin.requests, "delete", down)
    with pytest.raises(supabase_admin.AuthServiceError):
        supabase_admin.delete_auth_user("abc")
    monkeypatch.setattr(config, "SUPABASE_SERVICE_ROLE_KEY", "")
    with pytest.raises(supabase_admin.NotConfigured):
        supabase_admin.delete_auth_user("abc")


# ---- upgrading an existing database -----------------------------------------

def test_an_older_database_accepts_the_disabled_status_after_start_up(storage):
    with storage.pool().connection() as conn:
        conn.execute("ALTER TABLE profiles DROP CONSTRAINT profiles_status_check")
        conn.execute("ALTER TABLE profiles ADD CONSTRAINT profiles_status_check "
                     "CHECK (status IN ('pending', 'approved', 'rejected'))")
    storage.init_db()                                           # what every start-up does
    storage.init_db()                                           # and it is safe to repeat
    assert storage.update_profile(USER_ID, status="disabled")["status"] == "disabled"

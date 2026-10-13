"""A person's own settings, lists they switch off, and switching off or deleting their own account."""
import pytest

from app import config, supabase_admin
from tests.conftest import ADMIN_ID, API_HEADERS, EMAILS, USER_HEADERS, USER_ID, USER2_HEADERS, USER2_ID


def _code(r):
    return r.json()["error"]["code"]


def _prefs(client, headers=USER_HEADERS):
    return client.get("/api/account/preferences", headers=headers).json()["preferences"]


def _save(client, change, headers=USER_HEADERS):
    prefs = _prefs(client, headers)
    for path, value in change.items():
        node = prefs
        *parents, leaf = path.split(".")
        for p in parents:
            node = node[p]
        node[leaf] = value
    return client.put("/api/account/preferences", json=prefs, headers=headers)


@pytest.fixture
def auth_service(monkeypatch):
    monkeypatch.setattr(config, "SUPABASE_SERVICE_ROLE_KEY", "test-service-key")
    calls = []
    monkeypatch.setattr(supabase_admin, "delete_auth_user", lambda uid: calls.append(uid) or True)
    return calls


# ---- preferences ------------------------------------------------------------

def test_a_new_person_gets_the_defaults(client):
    r = client.get("/api/account/preferences", headers=USER_HEADERS).json()
    p = r["preferences"]
    assert p["notifications"] == {"enabled": True, "popups": True, "categories": {
        "screening": True, "monitoring": True, "lists": True, "account": True, "admin": True}}
    assert p["lists"]["off"] == [] and p["screening"]["threshold"] is None
    assert p["security"]["idle_minutes"] == 30 and p["display"]["default_tab"] == "screen"
    assert set(r["switchable_sources"]) == {"UKSL", "PEP", "ADVERSE_MEDIA"}


def test_settings_are_saved_per_person(client):
    r = _save(client, {"notifications.enabled": False, "notifications.categories": {
        "screening": False, "monitoring": True, "lists": True, "account": True, "admin": True},
        "security.idle_minutes": 10, "display.density": "compact"})
    assert r.status_code == 200
    mine = _prefs(client)
    assert mine["notifications"]["enabled"] is False and mine["notifications"]["categories"]["screening"] is False
    assert mine["security"]["idle_minutes"] == 10 and mine["display"]["density"] == "compact"
    assert _prefs(client, USER2_HEADERS)["notifications"]["enabled"] is True      # someone else is untouched


@pytest.mark.parametrize("change", [
    {"security.idle_minutes": 90},                 # never longer than the 30 minute policy
    {"screening.threshold": 20},
    {"display.default_tab": "people"},
    {"notifications.enabled": "maybe"},
])
def test_a_value_that_is_not_an_allowed_choice_is_refused(client, change):
    r = _save(client, change)
    assert r.status_code == 422 and _code(r) == "PREFERENCES_INVALID"


def test_an_unknown_setting_is_refused(client):
    prefs = _prefs(client)
    prefs["is_admin"] = True
    assert client.put("/api/account/preferences", json=prefs, headers=USER_HEADERS).status_code == 422


def test_the_core_lists_cannot_be_switched_off(client):
    for key in ("UNSC", "OFAC", "FIA_REDBOOK", "NACTA"):
        r = _save(client, {"lists.off": [key]})
        assert r.status_code == 422 and _code(r) == "LIST_CANNOT_BE_SWITCHED_OFF"
    assert _prefs(client)["lists"]["off"] == []


def test_settings_need_an_approved_account(client):
    from tests.conftest import bearer
    from tests.conftest import PENDING_ID
    r = client.get("/api/account/preferences", headers=bearer(PENDING_ID))
    assert r.status_code == 403


# ---- lists switched off -----------------------------------------------------

def _rows(resp):
    return {r["source"]: r for r in resp.json()["results"]}


def test_a_list_that_is_switched_off_is_not_screened_and_says_so(client, monkeypatch):
    from app.screening import loader
    asked = []
    monkeypatch.setattr(loader, "fetch_news", lambda name: asked.append(name) or "<rss></rss>")
    assert _save(client, {"lists.off": ["PEP", "ADVERSE_MEDIA"]}).status_code == 200
    r = client.post("/api/screen", json={"full_name": "Completely Unrelated Person"}, headers=USER_HEADERS)
    assert r.status_code == 200
    rows = _rows(r)
    for key in ("PEP", "ADVERSE_MEDIA"):
        assert rows[key]["status"] == "SKIPPED" and "switched off" in rows[key]["detail"].lower()
        assert rows[key]["records_screened"] in (0, None)
    assert rows["UNSC"]["status"] == "CLEAR" and rows["OFAC"]["status"] == "CLEAR"
    assert asked == []                         # the news search was not even run
    assert r.json()["overall_status"] == "AUTO_CLEAR"


def test_switching_a_list_off_never_hides_a_sanctions_match(client):
    assert _save(client, {"lists.off": ["PEP", "ADVERSE_MEDIA", "UKSL"]}).status_code == 200
    r = client.post("/api/screen", json={"full_name": "Muhammad Ali Khan", "dob": "1975-03-04", "threshold": 90},
                    headers=USER_HEADERS)
    rows = _rows(r)
    assert rows["UNSC"]["status"] == "HIT" and r.json()["overall_status"] == "ESCALATE_TO_COMPLIANCE"
    assert all(rows[k]["status"] == "SKIPPED" for k in ("PEP", "UKSL", "ADVERSE_MEDIA"))


def test_the_engine_never_counts_a_switched_off_list_as_clear():
    from app.screening import engine
    assert engine.source_status({"skipped": True, "available": False, "key": "PEP", "matches": [], "articles": []}) == "SKIPPED"
    only_skipped = {"UNSC": "CLEAR", "OFAC": "CLEAR", "FIA_REDBOOK": "CLEAR", "NACTA": "CLEAR", "PEP": "SKIPPED"}
    assert engine.overall_status(only_skipped) == "AUTO_CLEAR"          # does not block, and the row says not screened
    assert engine.overall_status({**only_skipped, "UNSC": "ERROR"}) == "MANUAL_REVIEW"
    assert engine.overall_status({**only_skipped, "OFAC": "HIT"}) == "ESCALATE_TO_COMPLIANCE"


def test_the_screening_audit_entry_records_which_lists_were_off(client, storage):
    _save(client, {"lists.off": ["PEP"]})
    client.post("/api/screen", json={"full_name": "Completely Unrelated Person"}, headers=USER_HEADERS)
    with storage.pool().connection() as conn:
        row = conn.execute("SELECT detail FROM audit_log WHERE action = 'screening.run' ORDER BY id DESC LIMIT 1").fetchone()
    assert row["detail"]["lists_off"] == ["PEP"]


def test_the_default_threshold_from_settings_is_used_when_none_is_sent(client):
    _save(client, {"screening.threshold": 92})
    r = client.post("/api/screen", json={"full_name": "Completely Unrelated Person"}, headers=USER_HEADERS)
    assert r.json()["threshold"] == 92
    r = client.post("/api/screen", json={"full_name": "Completely Unrelated Person", "threshold": 70}, headers=USER_HEADERS)
    assert r.json()["threshold"] == 70                                  # an explicit choice on the form wins


# ---- switching off and deleting your own account ---------------------------------

def test_a_person_can_disable_their_own_account(client, storage):
    client.post("/api/screen", json={"full_name": "Completely Unrelated Person"}, headers=USER_HEADERS)
    r = client.post("/api/account/disable", headers=USER_HEADERS)
    assert r.status_code == 200 and r.json()["status"] == "disabled"
    blocked = client.post("/api/screen", json={"full_name": "Completely Unrelated Person"}, headers=USER_HEADERS)
    assert blocked.status_code == 403 and _code(blocked) == "ACCOUNT_DISABLED"
    assert client.get("/api/me", headers=USER_HEADERS).json()["status"] == "disabled"
    # an administrator can switch them back on, and their history is still there
    assert client.post(f"/api/admin/users/{USER_ID}/status", json={"status": "approved"}, headers=API_HEADERS).status_code == 200
    assert len(client.get("/api/applicants", headers=USER_HEADERS).json()) == 1
    with storage.pool().connection() as conn:
        assert conn.execute("SELECT 1 FROM audit_log WHERE action = 'account.disable.self'").fetchone()


def test_the_last_administrator_cannot_disable_or_delete_themselves(client, auth_service):
    r = client.post("/api/account/disable", headers=API_HEADERS)
    assert r.status_code == 409 and _code(r) == "LAST_ADMIN"
    r = client.request("DELETE", "/api/account", json={"confirm_email": EMAILS[ADMIN_ID]}, headers=API_HEADERS)
    assert r.status_code == 409 and _code(r) == "LAST_ADMIN"
    assert client.get("/api/me", headers=API_HEADERS).json()["status"] == "approved"
    assert auth_service == []


def test_an_administrator_can_disable_themselves_when_another_exists(client):
    client.post(f"/api/admin/users/{USER_ID}/role", json={"role": "admin"}, headers=API_HEADERS)
    assert client.post("/api/account/disable", headers=API_HEADERS).status_code == 200


def test_deleting_your_account_needs_your_email_typed_exactly(client, storage, auth_service):
    r = client.request("DELETE", "/api/account", json={"confirm_email": "someone@else.com"}, headers=USER2_HEADERS)
    assert r.status_code == 422 and _code(r) == "CONFIRM_EMAIL_MISMATCH"
    assert storage.get_profile(USER2_ID)["status"] == "approved" and auth_service == []


def test_a_person_can_delete_their_own_account_and_the_record_stays(client, storage, auth_service):
    client.post("/api/screen", json={"full_name": "Kept Record"}, headers=USER2_HEADERS)
    _save(client, {"display.density": "compact"}, USER2_HEADERS)
    r = client.request("DELETE", "/api/account", json={"confirm_email": EMAILS[USER2_ID].upper()}, headers=USER2_HEADERS)
    assert r.status_code == 200 and r.json()["status"] == "deleted" and r.json()["sign_in_removed"] is True
    assert auth_service == [USER2_ID]
    assert storage.get_profile(USER2_ID) is None
    with storage.pool().connection() as conn:
        row = conn.execute("SELECT user_id FROM applicants WHERE full_name = 'Kept Record'").fetchone()
        prefs = conn.execute("SELECT 1 FROM user_preferences WHERE user_id = %s", (USER2_ID,)).fetchone()
        audit = conn.execute("SELECT detail FROM audit_log WHERE action = 'account.delete.self'").fetchone()
    assert row is not None and row["user_id"] is None            # the screening stays, with nobody's name on it
    assert prefs is None                                         # their settings go with them
    assert audit is not None
    again = client.get("/api/me", headers=USER2_HEADERS)
    assert again.status_code == 401 and _code(again) == "AUTH_ACCOUNT_DELETED"


def test_delete_is_refused_when_the_sign_in_service_is_not_set_up(client, storage, monkeypatch):
    monkeypatch.setattr(config, "SUPABASE_SERVICE_ROLE_KEY", "")
    r = client.request("DELETE", "/api/account", json={"confirm_email": EMAILS[USER2_ID]}, headers=USER2_HEADERS)
    assert r.status_code == 503 and _code(r) == "USER_DELETE_NOT_CONFIGURED"
    assert storage.get_profile(USER2_ID)["status"] == "approved"


def test_if_the_sign_in_service_fails_the_account_is_left_disabled(client, storage, monkeypatch):
    monkeypatch.setattr(config, "SUPABASE_SERVICE_ROLE_KEY", "test-service-key")

    def boom(uid):
        raise supabase_admin.AuthServiceError("the authentication service answered 500")

    monkeypatch.setattr(supabase_admin, "delete_auth_user", boom)
    r = client.request("DELETE", "/api/account", json={"confirm_email": EMAILS[USER2_ID]}, headers=USER2_HEADERS)
    assert r.status_code == 502 and _code(r) == "USER_DELETE_FAILED"
    assert storage.get_profile(USER2_ID)["status"] == "disabled"

"""Continuous monitoring: re-screen enrolled applicants when a list changes, alert only on NEW matches."""
import json

import pytest

from app import config, database, monitoring
from app.screening import loader
from tests import fixtures as fx
from tests.conftest import ADMIN_ID, API_HEADERS, USER_HEADERS, USER2_HEADERS

NAME = "Zebediah Quillfeather"


def _list_person(fake_sources, first="ZEBEDIAH", second="QUILLFEATHER", dataid=900):
    """The publisher adds a person to the UN list."""
    xml = fake_sources["texts"][loader.UN_URL]
    entry = (f"<INDIVIDUAL><DATAID>{dataid}</DATAID><REFERENCE_NUMBER>QDi.{dataid}</REFERENCE_NUMBER>"
             f"<FIRST_NAME>{first}</FIRST_NAME><SECOND_NAME>{second}</SECOND_NAME>"
             f"<UN_LIST_TYPE>Al-Qaida</UN_LIST_TYPE><LISTED_ON>2026-10-01</LISTED_ON></INDIVIDUAL>")
    fake_sources["texts"][loader.UN_URL] = xml.replace("</INDIVIDUALS>", entry + "</INDIVIDUALS>")
    loader.clear_cache()


def _screen(client, name=NAME, headers=USER_HEADERS, **extra):
    r = client.post("/api/screen", json={"full_name": name, **extra}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _alerts(client, headers=USER_HEADERS, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    return client.get("/api/monitoring/alerts" + ("?" + q if q else ""), headers=headers)


# ---- the core promise ---------------------------------------------------------------------------------------

def test_a_person_listed_after_screening_raises_an_alert(client, fake_sources):
    a = _screen(client, monitor=True)
    assert a["overall_status"] == "AUTO_CLEAR" and a["monitored"] is True
    assert monitoring.run_once()["first_seen"]                    # first pass only records each list's fingerprint

    _list_person(fake_sources)
    summary = monitoring.run_once()
    assert summary["changed_sources"] == ["UNSC"] and summary["applicants_checked"] == 1
    assert summary["new_alerts"] == 1

    alerts = _alerts(client).json()
    assert len(alerts) == 1
    al = alerts[0]
    assert al["applicant_id"] == a["applicant_id"] and al["applicant_name"] == NAME
    assert al["source"] == "UNSC" and al["status"] == "open" and al["score"] == 100.0
    assert al["match"]["primary_name"].startswith("ZEBEDIAH") and al["match"]["id"] == "QDi.900"

    # the original screening is a record of that day: it is never rewritten
    assert client.get(f"/api/applicants/{a['applicant_id']}", headers=USER_HEADERS).json()["overall_status"] == "AUTO_CLEAR"


def test_nothing_changed_means_nothing_is_rescreened(client, fake_sources):
    _screen(client, monitor=True)
    monitoring.run_once()
    again = monitoring.run_once()
    assert again["changed_sources"] == [] and again["applicants_checked"] == 0 and again["new_alerts"] == 0


def test_a_match_is_alerted_once_however_often_the_list_changes(client, fake_sources):
    _screen(client, monitor=True)
    monitoring.run_once()
    _list_person(fake_sources)
    assert monitoring.run_once()["new_alerts"] == 1
    _list_person(fake_sources, "OTHER", "PERSON", 901)            # an unrelated change: the list differs again
    again = monitoring.run_once()
    assert again["changed_sources"] == ["UNSC"] and again["new_alerts"] == 0
    assert monitoring.run_once(force=True)["new_alerts"] == 0     # even a forced full re-screen
    assert _alerts(client).headers["X-Total-Count"] == "1"


def test_a_match_that_was_already_there_when_screened_is_not_an_alert(client, fake_sources):
    r = _screen(client, "Mohammad Ali Khan", monitor=True)       # matches the synthetic UN entry QDi.001 right away
    assert r["overall_status"] == "ESCALATE_TO_COMPLIANCE"
    monitoring.run_once()
    _list_person(fake_sources)                                    # some other change to the same list
    assert monitoring.run_once()["new_alerts"] == 0
    assert _alerts(client).json() == []


def test_only_enrolled_people_are_rescreened(client, fake_sources):
    _screen(client, monitor=False)
    monitoring.run_once()
    _list_person(fake_sources)
    s = monitoring.run_once()
    assert s["applicants_checked"] == 0 and s["new_alerts"] == 0 and _alerts(client).json() == []


def test_a_variant_spelling_of_the_name_is_caught_too(client, fake_sources):
    _screen(client, "Mohammed Ayub Zafar", monitor=True)
    monitoring.run_once()
    _list_person(fake_sources, "MUHAMMAD", "AYUB ZAFAR", 902)
    assert monitoring.run_once()["new_alerts"] == 1


def test_each_applicants_own_threshold_is_used(client, fake_sources):
    """The same list change alerts the person screened at 85 and not the one screened at 99."""
    strict = _screen(client, "Zebediah Quillfeather", monitor=True, threshold=99)
    loose = _screen(client, "Zebediah Quillfeather", monitor=True, threshold=85)
    monitoring.run_once()
    _list_person(fake_sources, "ZEBEDIAH", "QUILFEATHERS", 903)       # scores 98.3 against the applicant
    assert monitoring.run_once()["new_alerts"] == 1
    assert [a["applicant_id"] for a in _alerts(client).json()] == [loose["applicant_id"]]
    assert strict["applicant_id"] != loose["applicant_id"]


def test_a_cnic_match_alerts_even_when_the_name_looks_different(client, fake_sources):
    _screen(client, "Completely Different Name", monitor=True, cnic="4220112345671")
    monitoring.run_once()
    fake_sources["nacta"] += "5,Someone Else Entirely,Some Father,4220112345671,KARACHI,SINDH\n"
    loader.clear_cache()
    s = monitoring.run_once()
    assert s["changed_sources"] == ["NACTA"] and s["new_alerts"] == 1
    al = _alerts(client).json()[0]
    assert al["source"] == "NACTA" and al["match"]["cnic_match"] is True and al["match"]["primary_name"] == "Someone Else Entirely"


# ---- enrolling later ----------------------------------------------------------------------------------------

def test_enrolling_later_checks_against_the_current_lists_immediately(client, fake_sources):
    a = _screen(client)                                           # screened clear, not monitored
    _list_person(fake_sources)                                    # listed afterwards
    r = client.post(f"/api/applicants/{a['applicant_id']}/monitoring", json={"enabled": True}, headers=USER_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["monitored"] is True and body["new_alerts"] == 1 and body["monitored_since"] and body["last_monitored_at"]
    assert len(_alerts(client).json()) == 1


def test_stopping_monitoring_stops_rescreening_but_keeps_existing_alerts(client, fake_sources):
    a = _screen(client, monitor=True)
    monitoring.run_once()
    _list_person(fake_sources)
    monitoring.run_once()
    off = client.post(f"/api/applicants/{a['applicant_id']}/monitoring", json={"enabled": False}, headers=USER_HEADERS)
    assert off.json()["monitored"] is False and off.json()["new_alerts"] == 0
    _list_person(fake_sources, "NEW", "NAME", 904)
    assert monitoring.run_once()["applicants_checked"] == 0
    assert len(_alerts(client).json()) == 1                       # the earlier alert is still there


def test_a_name_that_cannot_be_screened_cannot_be_enrolled(client, storage):
    aid = storage.insert_applicant("محمد علی", None, None, "2026-01-01T00:00:00+00:00", "AUTO_CLEAR",
                                   user_id=USER_HEADERS and "00000000-0000-4000-8000-0000000000b1")
    r = client.post(f"/api/applicants/{aid}/monitoring", json={"enabled": True}, headers=USER_HEADERS)
    assert r.status_code == 422 and r.json()["error"]["code"] == "NAME_NOT_SCREENABLE"


def test_a_stored_unscreenable_name_is_counted_not_silently_cleared(client, fake_sources, storage):
    aid = storage.insert_applicant("محمد علی", None, None, "2026-01-01T00:00:00+00:00", "AUTO_CLEAR",
                                   user_id="00000000-0000-4000-8000-0000000000b1")
    storage.set_monitoring(aid, True)
    monitoring.run_once()
    _list_person(fake_sources)
    s = monitoring.run_once()
    assert s["skipped_unscreenable"] == 1 and s["applicants_checked"] == 0


# ---- failure handling ---------------------------------------------------------------------------------------

def test_a_list_that_fails_to_load_is_not_treated_as_changed(client, fake_sources):
    _screen(client, monitor=True)
    monitoring.run_once()
    fake_sources["fail"].add(loader.UN_URL)                       # outage
    loader.clear_cache()
    s = monitoring.run_once()
    assert "UNSC" in s["unavailable"] and s["changed_sources"] == [] and s["new_alerts"] == 0
    # the outage did not move the stored fingerprint, so when the list is back the change is still noticed
    fake_sources["fail"].discard(loader.UN_URL)
    _list_person(fake_sources)
    assert monitoring.run_once()["new_alerts"] == 1


def test_an_interrupted_run_is_repeated_and_creates_no_duplicates(client, fake_sources, monkeypatch):
    _screen(client, monitor=True)
    monitoring.run_once()
    _list_person(fake_sources)
    real = database.mark_monitored_checked

    def crash(ids):
        raise RuntimeError("server died mid-run")
    monkeypatch.setattr(database, "mark_monitored_checked", crash)
    with pytest.raises(RuntimeError):
        monitoring.run_once()
    monkeypatch.setattr(database, "mark_monitored_checked", real)
    assert database.monitoring_state()["UNSC"]["fingerprint"]     # stored fingerprint did NOT advance past the change
    retry = monitoring.run_once()
    assert retry["changed_sources"] == ["UNSC"] and retry["new_alerts"] == 0   # the alert from the crashed run stands
    assert _alerts(client).headers["X-Total-Count"] == "1"


def test_only_one_instance_runs_the_check_at_a_time(client, fake_sources):
    with database.pool().connection() as other:
        other.execute("SELECT pg_advisory_lock(%s)", (monitoring._LOCK_KEY,))
        other.commit()
        try:
            s = monitoring.run_once()
            assert s["ran"] is False and "another instance" in s["reason"]
        finally:
            other.execute("SELECT pg_advisory_unlock(%s)", (monitoring._LOCK_KEY,))
            other.commit()
    assert monitoring.run_once()["ran"] is True                   # and the lock is released afterwards


def test_the_fingerprint_changes_only_when_the_list_does(fake_sources):
    loader.clear_cache()
    a = loader.load_group("UNSC").fingerprint
    loader.clear_cache()
    assert loader.load_group("UNSC").fingerprint == a             # same content, same fingerprint
    _list_person(fake_sources)
    assert loader.load_group("UNSC").fingerprint != a


# ---- who can see and decide ---------------------------------------------------------------------------------

def test_alerts_are_private_to_the_analyst_who_screened_and_visible_to_admins(client, fake_sources):
    _screen(client, monitor=True, headers=USER_HEADERS)
    monitoring.run_once()
    _list_person(fake_sources)
    monitoring.run_once()
    assert len(_alerts(client, USER_HEADERS).json()) == 1
    assert _alerts(client, USER2_HEADERS).json() == []
    assert len(_alerts(client, API_HEADERS).json()) == 1          # admin sees everyone's
    alert_id = _alerts(client, USER_HEADERS).json()[0]["id"]
    assert client.post(f"/api/monitoring/alerts/{alert_id}/decision", json={"status": "dismissed"},
                       headers=USER2_HEADERS).status_code == 404


def test_one_user_cannot_enrol_anothers_screening(client):
    a = _screen(client, headers=USER_HEADERS)
    r = client.post(f"/api/applicants/{a['applicant_id']}/monitoring", json={"enabled": True}, headers=USER2_HEADERS)
    assert r.status_code == 404


def test_deciding_an_alert(client, fake_sources):
    _screen(client, monitor=True)
    monitoring.run_once()
    _list_person(fake_sources)
    monitoring.run_once()
    al = _alerts(client).json()[0]
    r = client.post(f"/api/monitoring/alerts/{al['id']}/decision",
                    json={"status": "dismissed", "note": "Different date of birth, confirmed with the applicant"},
                    headers=USER_HEADERS)
    assert r.status_code == 200 and r.json()["status"] == "dismissed" and r.json()["decided_at"]
    assert _alerts(client).json() == []                           # no longer open
    assert len(_alerts(client, status="dismissed").json()) == 1
    reopened = client.post(f"/api/monitoring/alerts/{al['id']}/decision", json={"status": "open"}, headers=USER_HEADERS)
    assert reopened.json()["status"] == "open" and reopened.json()["decided_at"] is None
    assert client.post(f"/api/monitoring/alerts/{al['id']}/decision", json={"status": "maybe"},
                       headers=USER_HEADERS).status_code == 422
    assert client.post("/api/monitoring/alerts/99999/decision", json={"status": "open"},
                       headers=USER_HEADERS).status_code == 404


def test_status_endpoint_reports_scope_and_progress(client, fake_sources):
    _screen(client, monitor=True)
    monitoring.run_once()
    _list_person(fake_sources)
    monitoring.run_once()
    st = client.get("/api/monitoring/status", headers=USER_HEADERS).json()
    assert st["monitored_applicants"] == 1 and st["open_alerts"] == 1 and st["interval_seconds"] >= 60
    un = next(s for s in st["sources"] if s["source"] == "UNSC")
    assert un["last_checked_at"] and un["new_alerts"] == 1
    other = client.get("/api/monitoring/status", headers=USER2_HEADERS).json()
    assert other["monitored_applicants"] == 0 and other["open_alerts"] == 0


def test_admin_can_run_the_check_by_hand_and_users_cannot(client, fake_sources):
    assert client.post("/api/admin/monitoring/run", headers=USER_HEADERS).status_code == 403
    _screen(client, monitor=True)
    r = client.post("/api/admin/monitoring/run", headers=API_HEADERS)
    assert r.status_code == 200 and r.json()["ran"] is True
    forced = client.post("/api/admin/monitoring/run?force=true", headers=API_HEADERS).json()
    assert forced["forced"] is True and forced["applicants_checked"] == 1


def test_applicant_list_shows_who_is_monitored(client):
    _screen(client, "Alpha Person", monitor=True)
    _screen(client, "Beta Person")
    rows = {r["full_name"]: r for r in client.get("/api/applicants", headers=USER_HEADERS).json()}
    assert rows["Alpha Person"]["monitored"] is True and rows["Beta Person"]["monitored"] is False


# ---- audit and notification --------------------------------------------------------------------------------

def test_monitoring_is_audited_and_the_trail_holds_no_names(client, fake_sources):
    _screen(client, monitor=True)
    monitoring.run_once()
    _list_person(fake_sources)
    monitoring.run_once()
    entries = client.get("/api/admin/audit?limit=200", headers=API_HEADERS).json()["entries"]
    actions = {e["action"] for e in entries}
    assert {"monitoring.enrol", "monitoring.alert", "monitoring.run"} <= actions
    alert = next(e for e in entries if e["action"] == "monitoring.alert")
    assert alert["via"] == "system" and alert["detail"]["source"] == "UNSC"
    assert "Zebediah" not in json.dumps(entries) and "Quillfeather" not in json.dumps(entries)
    assert client.get("/api/admin/audit/verify", headers=API_HEADERS).json()["ok"] is True


def test_webhook_carries_ids_only_and_is_signed(client, fake_sources, monkeypatch):
    sent = []
    monkeypatch.setattr(config, "MONITOR_WEBHOOK_URL", "https://hooks.example.com/screening")
    monkeypatch.setattr(config, "MONITOR_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setattr(monitoring.requests, "post", lambda url, **kw: sent.append((url, kw)))
    _screen(client, monitor=True)
    monitoring.run_once()
    _list_person(fake_sources)
    monitoring.run_once()
    assert len(sent) == 1
    url, kw = sent[0]
    body = json.loads(kw["data"])
    assert url == "https://hooks.example.com/screening" and body["count"] == 1 and body["alert_ids"] and body["applicant_ids"]
    assert "Zebediah" not in kw["data"].decode() and kw["allow_redirects"] is False
    import hashlib
    import hmac
    assert kw["headers"]["X-Signature"] == "sha256=" + hmac.new(b"s3cret", kw["data"], hashlib.sha256).hexdigest()


def test_webhook_to_an_internal_address_is_refused_and_never_breaks_monitoring(client, fake_sources, monkeypatch):
    sent = []
    monkeypatch.setattr(config, "MONITOR_WEBHOOK_URL", "https://169.254.169.254/hook")
    monkeypatch.setattr(monitoring.requests, "post", lambda *a, **k: sent.append(a))
    _screen(client, monitor=True)
    monitoring.run_once()
    _list_person(fake_sources)
    assert monitoring.run_once()["new_alerts"] == 1 and sent == []


def test_background_thread_is_off_when_switched_off(monkeypatch):
    monkeypatch.setattr(config, "MONITORING_ENABLED", False)
    assert monitoring.start() is False


def test_the_background_loop_keeps_running_after_a_failed_pass(monkeypatch):
    """The thread checks repeatedly, survives a pass that raises, and stops when asked."""
    import threading
    import time
    calls = []

    def flaky(force=False):
        calls.append(time.monotonic())
        if len(calls) == 1:
            raise RuntimeError("the first pass blows up")
        return {"ran": True}

    monkeypatch.setattr(config, "MONITORING_ENABLED", True)
    monkeypatch.setattr(config, "MONITOR_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(monitoring, "INITIAL_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(monitoring, "run_once", flaky)
    assert monitoring.start() is True
    assert monitoring.start() is False                    # already running: a second thread is never started
    deadline = time.monotonic() + 5
    while len(calls) < 3 and time.monotonic() < deadline:
        time.sleep(0.02)
    monitoring.stop()
    monitoring._thread.join(timeout=5)
    assert len(calls) >= 3                                # kept going after the exception on the first pass
    assert not monitoring._thread.is_alive()
    assert not [t for t in threading.enumerate() if t.name == "monitoring"]

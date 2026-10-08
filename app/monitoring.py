"""
Continuous monitoring: screen people again when a watch list changes.

A screening is a photograph of one day. A person who is clear today can be listed next week, so applicants who
are enrolled in monitoring are re-screened automatically.

How it works
------------
* Every list has a fingerprint (loader.GroupData.fingerprint): a hash of its records' ids, names, dates of birth
  and CNICs. The last fingerprint at which everyone was checked is stored per source (monitoring_state).
* run_once() compares the current fingerprints with the stored ones. For each source that changed it screens every
  monitored applicant against that source only, using the same matcher, threshold, CNIC, father's name and province as the
  original screening.
* A match is an ALERT only if it is new: not among the matches the applicant already had when first screened, and
  not already alerted (a unique key per applicant, list and record). So a hit is raised once, never repeatedly,
  and the original screening record is never rewritten.
* The stored fingerprint moves forward only after every applicant was processed. If a run is interrupted, the next
  run does the work again; the unique key makes that safe.
* A database lock means only one server instance does the check at a time.
* A source that could not be loaded is left alone (never treated as "changed to empty"), and a source seen for
  the first time is only recorded: people screened before monitoring existed are checked when they are enrolled.

Adverse media is not part of monitoring: it is a per-person web search that would be repeated for everyone on
every change, and its hits are unverified leads. Re-screen a person by hand when you want a fresh news check.
"""

import hashlib
import hmac
import json
import re
import threading
from contextlib import contextmanager

import requests

from app import config
from app import database as db
from app.config import MAX_MATCHES
from app.errors import logger
from app.screening import engine, loader
from app.screening.names import NameScorer
from app.screening.parsers import normalize_cnic, normalize_province

BATCH = 500
INITIAL_DELAY_SECONDS = 60.0     # let the lists load at start-up before the first check
_LOCK_KEY = db.MONITORING_LOCK   # advisory lock: one instance at a time (all lock keys are listed in app/database.py)

_stop = threading.Event()
_thread: threading.Thread | None = None


@contextmanager
def _single_instance():
    """Yields True if this process may run the check now, False if another instance already is."""
    with db.pool().connection() as conn:
        got = bool(conn.execute("SELECT pg_try_advisory_lock(%s) AS got", (_LOCK_KEY,)).fetchone()["got"])
        conn.commit()    # do not sit "idle in transaction" for the whole run
        try:
            yield got
        finally:
            if got:
                conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
                conn.commit()


def _check_one(applicant: dict, sources: list, groups: dict, baseline: set) -> list | None:
    """
    Screen one applicant against `sources` and record the NEW matches as alerts. Returns the new alerts
    (dicts with alert_id, applicant_id, source, score), or None when the stored name can no longer be screened
    (it would otherwise look clear, so it is counted and reported, not silently passed).
    """
    try:
        engine.check_screenable(applicant["full_name"])
    except engine.UnscreenableName:
        logger.warning("Monitoring: applicant %s has a name that cannot be screened; skipped", applicant["id"])
        return None
    thr = engine.resolve_threshold(applicant.get("threshold"))
    scorer = NameScorer(applicant["full_name"])
    father = NameScorer(applicant["father_name"]) if applicant.get("father_name") else None
    province = normalize_province(applicant.get("province"))
    year = re.search(r"(\d{4})", applicant.get("dob") or "")
    dob_year = year.group(1) if year else ""
    cnic = normalize_cnic(applicant.get("cnic") or "")

    new = []
    for key in sources:
        g = groups[key]
        matches, _total = engine.match_records(scorer, g.records, thr, dob_year, cnic, father, limit=MAX_MATCHES,
                                               index=g.index, province=province)
        for m in matches:
            ident = (key, m["list"] or "", str(m["id"]))
            if ident in baseline:
                continue
            alert_id = db.add_alert(applicant["id"], key, m["list"] or "", str(m["id"]), m["primary_name"],
                                    m["score"], m)
            if alert_id is None:
                continue                      # already alerted on an earlier run
            new.append({"alert_id": alert_id, "applicant_id": applicant["id"], "source": key, "score": m["score"]})
            _audit_alert(alert_id, applicant["id"], key, m["score"])
    return new


def _audit_alert(alert_id: int, applicant_id: int, source: str, score: float) -> None:
    try:
        db.audit("monitoring.alert", via="system", target_type="applicant", target_id=applicant_id,
                 detail={"alert_id": alert_id, "source": source, "score": score})
    except Exception:
        logger.exception("AUDIT WRITE FAILED for monitoring alert %s", alert_id)


def check_applicant(applicant_id: int) -> list:
    """
    Check one enrolled applicant against every list right now, so someone enrolled after their screening is
    compared with the current lists straight away. Returns the new alerts.
    """
    applicant = db.get_applicant(applicant_id)
    if not applicant:
        return []
    groups = {k: loader.load_group(k) for k in loader.SOURCE_KEYS}
    sources = [k for k, g in groups.items() if g.available]
    baseline = db.baseline_matches([applicant_id]).get(applicant_id, set())
    new = _check_one(applicant, sources, groups, baseline)
    db.mark_monitored_checked([applicant_id])
    if new:
        _notify(new)
    return new or []


def run_once(force: bool = False) -> dict:
    """
    Re-screen the monitored applicants against every list that changed since the last check. `force` treats
    every available list as changed. Returns a summary of what happened.
    """
    with _single_instance() as got:
        if not got:
            return {"ran": False, "reason": "another instance is already running the check"}
        return _run(force)


def _run(force: bool) -> dict:
    groups = {k: loader.load_group(k) for k in loader.SOURCE_KEYS}
    state = db.monitoring_state()
    changed, first_seen, unavailable, fingerprints = [], [], [], {}
    for key, g in groups.items():
        if not g.available:
            unavailable.append(key)           # never read as "the list became empty"
            continue
        fingerprints[key] = g.fingerprint
        if key not in state and not force:
            first_seen.append(key)
        elif force or state[key]["fingerprint"] != fingerprints[key]:
            changed.append(key)
    for key in first_seen:
        db.monitoring_state_put(key, fingerprints[key], 0, 0)

    summary = {"ran": True, "forced": force, "changed_sources": changed, "first_seen": first_seen,
               "unavailable": unavailable, "applicants_checked": 0, "skipped_unscreenable": 0, "new_alerts": 0}
    if not changed:
        return summary

    per_source = {k: 0 for k in changed}
    all_new: list = []
    after = 0
    while True:
        batch = db.monitored_batch(after, BATCH)
        if not batch:
            break
        after = batch[-1]["id"]
        baseline = db.baseline_matches([a["id"] for a in batch])
        for a in batch:
            new = _check_one(a, changed, groups, baseline.get(a["id"], set()))
            if new is None:
                summary["skipped_unscreenable"] += 1
                continue
            summary["applicants_checked"] += 1
            for n in new:
                per_source[n["source"]] += 1
            all_new.extend(new)
        db.mark_monitored_checked([a["id"] for a in batch])

    summary["new_alerts"] = len(all_new)
    # only now, with everyone checked, does the stored fingerprint move on
    for key in changed:
        db.monitoring_state_put(key, fingerprints[key], summary["applicants_checked"], per_source[key])
    try:
        db.audit("monitoring.run", via="system", detail={
            "changed_sources": changed, "applicants_checked": summary["applicants_checked"],
            "new_alerts": summary["new_alerts"], "forced": force})
    except Exception:
        logger.exception("AUDIT WRITE FAILED for a monitoring run")
    logger.info("Monitoring: %s changed, %d applicants checked, %d new alerts", changed,
                summary["applicants_checked"], summary["new_alerts"])
    if all_new:
        _notify(all_new)
    return summary


# --------------------------------------------------------------------------
# Notification (optional)
# --------------------------------------------------------------------------

def _notify(new_alerts: list) -> None:
    """Tell MONITOR_WEBHOOK_URL that there are new alerts. Ids only, never a name. Best effort, never raises."""
    url = config.MONITOR_WEBHOOK_URL
    if not url or not new_alerts:
        return
    body = json.dumps({
        "event": "monitoring.alerts", "count": len(new_alerts),
        "alert_ids": [a["alert_id"] for a in new_alerts][:500],
        "applicant_ids": sorted({a["applicant_id"] for a in new_alerts})[:500],
    }, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": config.HTTP_USER_AGENT}
    if config.MONITOR_WEBHOOK_SECRET:
        digest = hmac.new(config.MONITOR_WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["X-Signature"] = "sha256=" + digest
    try:
        loader._check_url(url)                # same rule as every outbound request: https, nothing internal
        requests.post(url, data=body, headers=headers, timeout=10, allow_redirects=False)
    except Exception as exc:
        logger.warning("Monitoring webhook failed: %s", type(exc).__name__)


# --------------------------------------------------------------------------
# Background thread
# --------------------------------------------------------------------------

def _loop() -> None:
    if _stop.wait(INITIAL_DELAY_SECONDS):
        return
    while not _stop.is_set():
        try:
            run_once()
        except Exception:      # a failed pass must never stop monitoring
            logger.exception("Monitoring pass failed; will try again")
        _stop.wait(config.MONITOR_INTERVAL_SECONDS)


def start() -> bool:
    """Start the background check. Returns False if it is switched off or already running."""
    global _thread
    if not config.MONITORING_ENABLED or (_thread and _thread.is_alive()):
        return False
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="monitoring", daemon=True)
    _thread.start()
    return True


def stop() -> None:
    _stop.set()

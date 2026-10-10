"""
PostgreSQL persistence layer (Supabase).

The schema is in app/schema.sql (profiles, applicants, screening_results, evidence_files,
nacta_list). The backend keeps a small pool of connections, so a request does not pay for a
new TLS connection to the database, and each public function below is one transaction:
either everything it writes is saved or nothing is.

Deliberately simple: psycopg 3, no ORM, parameterised queries only. Timestamps leave this
module as ISO 8601 strings in UTC and ids as plain strings, so the API output looks the same
as it did on the previous SQLite version.

Connection string: DATABASE_URL. On Supabase use the "Session pooler" string from the
dashboard's Connect dialog (it also works from hosts without IPv6, like Render's free plan).
"""

import hashlib
import json
import threading
import time
import weakref
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app import config

_SCHEMA = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
# Every advisory lock key the app uses, in one place so two features can never pick the same number (a clash
# makes one feature wait for the other, or for itself).
_ADMIN_LOCK = 727_274_001   # serialises changes to who is an admin, so the last admin cannot be removed by a race
SCHEMA_LOCK = _ADMIN_LOCK + 1       # init_db: several servers starting at once
AUDIT_LOCK = _ADMIN_LOCK + 2        # one writer at a time, so the audit hash chain has a single order
MONITORING_LOCK = 727_274_010       # one instance at a time runs the continuous monitoring check

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


class DatabaseNotConfigured(RuntimeError):
    """DATABASE_URL is missing."""


class LastAdminError(Exception):
    """The change would leave the system without an approved admin."""


class BatchAlreadyRunning(Exception):
    """This person already has a batch running."""


# --------------------------------------------------------------------------
# Connections
# --------------------------------------------------------------------------

_last_used: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _check_connection(conn) -> None:
    """
    psycopg_pool's own check sends a query on EVERY checkout, which is a full network round trip
    added to every request. A connection used a moment ago is not stale, so only test the ones that
    sat idle for a while (the case the check exists for: the server or a proxy closed them).
    A connection that breaks anyway surfaces as an OperationalError, which the API turns into a 503.
    """
    now = time.monotonic()
    seen = _last_used.get(conn)
    _last_used[conn] = now
    if seen is not None and now - seen < config.DB_CHECK_IDLE_SECONDS:
        return
    ConnectionPool.check_connection(conn)


def _new_pool() -> ConnectionPool:
    if not config.DATABASE_URL:
        raise DatabaseNotConfigured("DATABASE_URL is not set. Set it to the Supabase connection string (see README).")
    return ConnectionPool(
        config.DATABASE_URL,
        min_size=config.DB_POOL_MIN,
        max_size=config.DB_POOL_MAX,
        # prepare_threshold=None: no server side prepared statements, so this also works through
        # Supabase's transaction mode pooler (port 6543)
        kwargs={"row_factory": dict_row, "prepare_threshold": None, "connect_timeout": 10},
        check=_check_connection,   # replace a connection the server closed while it sat idle (only if it sat long)
        timeout=15,          # how long a request waits for a free connection before it fails with a 503
        max_lifetime=1800,
        max_idle=300,
        open=False,
        name="screening-db",
    )


def pool() -> ConnectionPool:
    """The shared pool, opened on first use."""
    global _pool
    if _pool is None or _pool.closed:
        with _pool_lock:
            if _pool is None or _pool.closed:
                p = _new_pool()
                p.open(wait=True, timeout=30)
                _pool = p
    return _pool


def close_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None and not _pool.closed:
            _pool.close()
        _pool = None


def init_db() -> None:
    """Open the pool and make sure every table exists (idempotent; safe with several servers starting at once)."""
    with pool().connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK,))
        conn.execute(_SCHEMA)


def ping() -> bool:
    with pool().connection() as conn:
        return conn.execute("SELECT 1 AS ok").fetchone()["ok"] == 1


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _when(value) -> datetime:
    """A datetime from a datetime or an ISO 8601 string; naive values are taken as UTC."""
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _clean(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = {}
    for k, v in row.items():
        if isinstance(v, datetime):
            v = _iso(v)
        elif isinstance(v, uuid.UUID):
            v = str(v)
        out[k] = v
    return out


def _result_row(row: dict | None) -> dict | None:
    d = _clean(row)
    if d is None:
        return None
    p = d.pop("payload", None) or {}
    d["matches"] = p.get("matches", [])
    d["articles"] = p.get("articles", [])
    d["match_count"] = p.get("match_count", len(d["matches"]))
    d["lists"] = p.get("lists", [])
    d["cnic_match"] = any(m.get("cnic_match") for m in d["matches"])
    return d


# --------------------------------------------------------------------------
# Screenings
# --------------------------------------------------------------------------

def insert_applicant(full_name, cnic, father_name, submitted_at, overall_status,
                     dob=None, nationality=None, threshold=None, user_id=None, province=None) -> int:
    with pool().connection() as conn:
        row = conn.execute(
            "INSERT INTO applicants (user_id, full_name, cnic, father_name, submitted_at, overall_status, "
            "dob, nationality, threshold, province) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (user_id, full_name, cnic, father_name, _when(submitted_at), overall_status, dob, nationality, threshold,
             province),
        ).fetchone()
        return row["id"]


def save_screening(applicant_id, overall_status, records_screened, rows,
                   evidence: tuple | None = None, evidence_failed: bool = False) -> list:
    """
    Store every source's result row, the applicant's final status and the evidence PDF in ONE
    transaction. `rows` is a list of dicts with the keys source, matched_entry, score, status,
    detail, checked_at, list_version, records_screened and payload. `evidence` is
    (file name, PDF bytes) when a PDF was made; `evidence_failed` notes on the findings that it
    could not be. Returns the new result row ids in the same order as `rows`.
    """
    ids = []
    with pool().connection() as conn:
        for r in rows:
            row = conn.execute(
                "INSERT INTO screening_results (applicant_id, source, matched_entry, score, status, detail, "
                "checked_at, list_version, records_screened, payload) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (applicant_id, r["source"], r["matched_entry"], r["score"], r["status"], r["detail"],
                 _when(r["checked_at"]), r["list_version"], r["records_screened"],
                 Jsonb(r["payload"]) if r["payload"] is not None else None),
            ).fetchone()
            ids.append(row["id"])
        conn.execute("UPDATE applicants SET overall_status = %s, records_screened = %s WHERE id = %s",
                     (overall_status, records_screened, applicant_id))
        if evidence:
            name, pdf = evidence
            conn.execute(
                "INSERT INTO evidence_files (applicant_id, filename, content, sha256) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (applicant_id) DO UPDATE SET filename = EXCLUDED.filename, "
                "content = EXCLUDED.content, sha256 = EXCLUDED.sha256, created_at = now()",
                (applicant_id, name, pdf, hashlib.sha256(pdf).hexdigest()),
            )
            conn.execute("UPDATE screening_results SET evidence_file = %s "
                         "WHERE applicant_id = %s AND status IN ('HIT', 'REVIEW')", (name, applicant_id))
        elif evidence_failed:
            conn.execute(
                "UPDATE screening_results SET detail = COALESCE(detail, '') || %s "
                "WHERE applicant_id = %s AND status IN ('HIT', 'REVIEW')",
                (" [Evidence PDF could not be generated. See the server log for this request ID.]", applicant_id),
            )
    return ids


def get_applicant(applicant_id, user_id=None):
    """One screening. With user_id, only if that user ran it (None when it is someone else's)."""
    sql, args = "SELECT * FROM applicants WHERE id = %s", [applicant_id]
    if user_id is not None:
        sql, args = sql + " AND user_id = %s", args + [user_id]
    with pool().connection() as conn:
        return _clean(conn.execute(sql, args).fetchone())


def get_results_for_applicant(applicant_id):
    with pool().connection() as conn:
        rows = conn.execute("SELECT * FROM screening_results WHERE applicant_id = %s ORDER BY id",
                            (applicant_id,)).fetchall()
        return [_result_row(r) for r in rows]


def list_applicants(user_id=None, limit=100):
    """Newest first. With user_id only that user's screenings; without it everyone's, each with who ran it."""
    sql = ("SELECT a.*, p.email AS screened_by FROM applicants a "
           "LEFT JOIN profiles p ON p.id = a.user_id")
    args: list = []
    if user_id is not None:
        sql, args = sql + " WHERE a.user_id = %s", [user_id]
    with pool().connection() as conn:
        rows = conn.execute(sql + " ORDER BY a.id DESC LIMIT %s", args + [limit]).fetchall()
        return [_clean(r) for r in rows]


def search_applicants(user_id=None, status=None, limit=100, offset=0) -> tuple:
    """(rows, total): one page of screenings, newest first, optionally only one overall status."""
    where, args = [], []
    if user_id is not None:
        where.append("a.user_id = %s")
        args.append(user_id)
    if status:
        where.append("a.overall_status = %s")
        args.append(status)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with pool().connection() as conn:
        rows = conn.execute(
            "SELECT a.*, p.email AS screened_by FROM applicants a LEFT JOIN profiles p ON p.id = a.user_id"
            + clause + " ORDER BY a.id DESC LIMIT %s OFFSET %s", args + [limit, offset]).fetchall()
        total = conn.execute("SELECT count(*) AS n FROM applicants a" + clause, args).fetchone()["n"]
    return [_clean(r) for r in rows], total


def get_result(result_id, user_id=None):
    sql = ("SELECT r.* FROM screening_results r JOIN applicants a ON a.id = r.applicant_id WHERE r.id = %s")
    args: list = [result_id]
    if user_id is not None:
        sql, args = sql + " AND a.user_id = %s", args + [user_id]
    with pool().connection() as conn:
        return _result_row(conn.execute(sql, args).fetchone())


def get_evidence(applicant_id):
    """{'filename', 'content', 'sha256'} of the screening's evidence PDF, or None."""
    with pool().connection() as conn:
        row = conn.execute("SELECT filename, content, sha256 FROM evidence_files WHERE applicant_id = %s",
                           (applicant_id,)).fetchone()
    if row and not row["sha256"]:      # saved before the hash was recorded
        row["sha256"] = hashlib.sha256(bytes(row["content"])).hexdigest()
    return row


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------

def get_profile(user_id):
    with pool().connection() as conn:
        return _clean(conn.execute("SELECT * FROM profiles WHERE id = %s", (user_id,)).fetchone())


def upsert_profile(user_id, email: str) -> dict:
    """Create the profile of a user the sign up trigger has not seen (status pending), or refresh its email."""
    with pool().connection() as conn:
        return _clean(conn.execute(
            "INSERT INTO profiles (id, email) VALUES (%s, %s) "
            "ON CONFLICT (id) DO UPDATE SET email = EXCLUDED.email RETURNING *",
            (user_id, email or ""),
        ).fetchone())


def list_profiles(status=None):
    sql, args = "SELECT * FROM profiles", []
    if status:
        sql, args = sql + " WHERE status = %s", [status]
    with pool().connection() as conn:
        return [_clean(r) for r in conn.execute(sql + " ORDER BY created_at DESC", args).fetchall()]


def update_profile(user_id, status=None, role=None, decided_by=None):
    """
    Approve / reject a user or change their role. Returns the updated profile, or None when there
    is no such user. Raises LastAdminError if it would leave no approved admin.
    """
    with pool().connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_ADMIN_LOCK,))
        cur = conn.execute("SELECT * FROM profiles WHERE id = %s FOR UPDATE", (user_id,)).fetchone()
        if cur is None:
            return None
        new_status, new_role = status or cur["status"], role or cur["role"]
        was_admin = cur["status"] == "approved" and cur["role"] == "admin"
        will_be_admin = new_status == "approved" and new_role == "admin"
        if was_admin and not will_be_admin:
            n = conn.execute("SELECT count(*) AS n FROM profiles WHERE status = 'approved' AND role = 'admin'"
                             ).fetchone()["n"]
            if n <= 1:
                raise LastAdminError()
        return _clean(conn.execute(
            "UPDATE profiles SET status = %s, role = %s, decided_at = now(), decided_by = %s "
            "WHERE id = %s RETURNING *",
            (new_status, new_role, decided_by, user_id),
        ).fetchone())


def delete_profile(user_id) -> dict | None:
    """
    Delete a person's profile and remember that they were deleted. Their screenings, batches and the decisions
    they made stay, with the link to them cleared (user_id becomes NULL and only admins see those screenings).
    Returns the deleted profile, or None when there is no such user. Raises LastAdminError if they are the
    last approved admin.
    """
    with pool().connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_ADMIN_LOCK,))
        cur = conn.execute("SELECT * FROM profiles WHERE id = %s FOR UPDATE", (user_id,)).fetchone()
        if cur is None:
            return None
        if cur["status"] == "approved" and cur["role"] == "admin":
            n = conn.execute("SELECT count(*) AS n FROM profiles WHERE status = 'approved' AND role = 'admin'"
                             ).fetchone()["n"]
            if n <= 1:
                raise LastAdminError()
        conn.execute("INSERT INTO deleted_users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (user_id,))
        conn.execute("DELETE FROM profiles WHERE id = %s", (user_id,))
        return _clean(cur)


def is_deleted_user(user_id) -> bool:
    with pool().connection() as conn:
        return conn.execute("SELECT 1 FROM deleted_users WHERE id = %s", (user_id,)).fetchone() is not None


# --------------------------------------------------------------------------
# NACTA list
# --------------------------------------------------------------------------

_NACTA_META = "filename, uploaded_at, records, size, sha256, live"


def nacta_put(data: bytes, filename: str, records: int, live: bool = False) -> dict:
    """Replace the stored NACTA list (one statement, so there is never a half written list)."""
    with pool().connection() as conn:
        row = conn.execute(
            "INSERT INTO nacta_list (id, content, filename, uploaded_at, records, size, sha256, live) "
            "VALUES (1, %s, %s, now(), %s, %s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET content = EXCLUDED.content, filename = EXCLUDED.filename, "
            "uploaded_at = EXCLUDED.uploaded_at, records = EXCLUDED.records, size = EXCLUDED.size, "
            "sha256 = EXCLUDED.sha256, live = EXCLUDED.live "
            f"RETURNING {_NACTA_META}",
            (data, filename, int(records), len(data), hashlib.sha256(data).hexdigest(), bool(live)),
        ).fetchone()
        return _clean(row)


def nacta_meta() -> dict | None:
    with pool().connection() as conn:
        return _clean(conn.execute(f"SELECT {_NACTA_META} FROM nacta_list WHERE id = 1").fetchone())


def nacta_get() -> tuple | None:
    """(file bytes, metadata) or None when no list was ever stored."""
    with pool().connection() as conn:
        row = conn.execute(f"SELECT content, {_NACTA_META} FROM nacta_list WHERE id = 1").fetchone()
    if not row:
        return None
    content = bytes(row.pop("content"))
    return content, _clean(row)


# --------------------------------------------------------------------------
# PEP data (kind 'upload' = the admin's list, 'wikidata' = the last good fetch)
# --------------------------------------------------------------------------

_PEP_META = "kind, filename, uploaded_at, records, size, sha256"


def pep_put(kind: str, data: bytes, filename: str, records: int) -> dict:
    with pool().connection() as conn:
        row = conn.execute(
            "INSERT INTO pep_files (kind, content, filename, uploaded_at, records, size, sha256) "
            "VALUES (%s, %s, %s, now(), %s, %s, %s) "
            "ON CONFLICT (kind) DO UPDATE SET content = EXCLUDED.content, filename = EXCLUDED.filename, "
            "uploaded_at = EXCLUDED.uploaded_at, records = EXCLUDED.records, size = EXCLUDED.size, "
            f"sha256 = EXCLUDED.sha256 RETURNING {_PEP_META}",
            (kind, data, filename, int(records), len(data), hashlib.sha256(data).hexdigest()),
        ).fetchone()
        return _clean(row)


def pep_meta(kind: str) -> dict | None:
    with pool().connection() as conn:
        return _clean(conn.execute(f"SELECT {_PEP_META} FROM pep_files WHERE kind = %s", (kind,)).fetchone())


def pep_get(kind: str) -> tuple | None:
    """(bytes, metadata) or None."""
    with pool().connection() as conn:
        row = conn.execute(f"SELECT content, {_PEP_META} FROM pep_files WHERE kind = %s", (kind,)).fetchone()
    if not row:
        return None
    content = bytes(row.pop("content"))
    return content, _clean(row)


# --------------------------------------------------------------------------
# Audit log (append only, hash chained)
# --------------------------------------------------------------------------

AUDIT_GENESIS = "0" * 64


def _audit_hash(prev_hash: str, at: datetime, actor_id, actor_email, via, action, target_type, target_id,
                detail, request_id, ip) -> str:
    body = json.dumps([prev_hash, _iso(at), str(actor_id) if actor_id else None, actor_email, via, action,
                       target_type, target_id, detail, request_id, ip],
                      sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _plain(v):
    """JSON that survives a trip through jsonb unchanged: numbers that are floats become text."""
    if isinstance(v, float):
        return f"{v:g}"
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return v


def audit(action: str, actor_id=None, actor_email=None, via="token", target_type=None, target_id=None,
          detail=None, request_id=None, ip=None) -> None:
    """Append one entry. Entries are serialised by an advisory lock so the chain has exactly one order."""
    at = datetime.now(timezone.utc)
    target_id = None if target_id is None else str(target_id)
    actor_id = str(uuid.UUID(str(actor_id))) if actor_id else None   # one spelling, so the hash re-computes
    detail = _plain(detail)
    with pool().connection() as conn:
        # two round trips instead of four: the lock and the read of the last hash go out together
        # (the server runs them in order), then the insert and the commit go out together
        with conn.pipeline():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (AUDIT_LOCK,))
            cur = conn.execute("SELECT row_hash FROM audit_log ORDER BY id DESC LIMIT 1")
        last = cur.fetchone()
        prev = last["row_hash"] if last else AUDIT_GENESIS
        row_hash = _audit_hash(prev, at, actor_id, actor_email, via, action, target_type, target_id,
                               detail, request_id, ip)
        with conn.pipeline():
            conn.execute(
                "INSERT INTO audit_log (at, actor_id, actor_email, via, action, target_type, target_id, detail, "
                "request_id, ip, prev_hash, row_hash) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (at, actor_id, actor_email, via, action, target_type, target_id,
                 Jsonb(detail) if detail is not None else None, request_id, ip, prev, row_hash))
            conn.commit()


def audit_list(limit=100, offset=0, action=None, actor_id=None) -> tuple:
    """(entries, total), newest first."""
    where, args = [], []
    if action:
        where.append("action = %s")
        args.append(action)
    if actor_id:
        where.append("actor_id = %s")
        args.append(actor_id)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with pool().connection() as conn:
        rows = conn.execute("SELECT id, at, actor_id, actor_email, via, action, target_type, target_id, detail, "
                            "request_id, ip, row_hash FROM audit_log" + clause +
                            " ORDER BY id DESC LIMIT %s OFFSET %s", args + [limit, offset]).fetchall()
        total = conn.execute("SELECT count(*) AS n FROM audit_log" + clause, args).fetchone()["n"]
    return [_clean(r) for r in rows], total


def audit_verify() -> dict:
    """
    Walk the whole chain. ok is False at the first entry whose stored hash does not match its content or
    whose prev_hash is not the hash of the entry before it (an entry was changed, removed or inserted).
    `head` is the newest hash: keep a copy somewhere else (a ticket, an email) and a later rewrite of the
    whole table, chain included, is still detectable.
    """
    prev, checked, last_id = AUDIT_GENESIS, 0, 0
    with pool().connection() as conn:
        while True:
            rows = conn.execute("SELECT * FROM audit_log WHERE id > %s ORDER BY id LIMIT 1000", (last_id,)).fetchall()
            if not rows:
                break
            for r in rows:
                expected = _audit_hash(r["prev_hash"], r["at"], r["actor_id"], r["actor_email"], r["via"], r["action"],
                                       r["target_type"], r["target_id"], r["detail"], r["request_id"], r["ip"])
                if r["prev_hash"] != prev or r["row_hash"] != expected:
                    return {"ok": False, "checked": checked, "first_bad_id": r["id"], "head": None}
                prev, checked, last_id = r["row_hash"], checked + 1, r["id"]
    return {"ok": True, "checked": checked, "first_bad_id": None, "head": prev if checked else None}


# --------------------------------------------------------------------------
# Continuous monitoring
# --------------------------------------------------------------------------

def set_monitoring(applicant_id: int, enabled: bool, user_id=None) -> dict | None:
    """Enrol or un-enrol one screening. With user_id, only if that user ran it. Returns the row, or None."""
    scope, args = ("", []) if user_id is None else (" AND user_id = %s", [user_id])
    if enabled:
        sql = ("UPDATE applicants SET monitored = true, "
               "monitored_since = CASE WHEN monitored THEN monitored_since ELSE now() END WHERE id = %s")
    else:
        sql = "UPDATE applicants SET monitored = false WHERE id = %s"
    with pool().connection() as conn:
        return _clean(conn.execute(sql + scope + " RETURNING *", [applicant_id] + args).fetchone())


def monitored_batch(after_id: int, limit: int = 500) -> list:
    """The next monitored applicants after `after_id`, with what is needed to screen them again."""
    with pool().connection() as conn:
        return [_clean(r) for r in conn.execute(
            "SELECT id, user_id, full_name, cnic, father_name, province, dob, nationality, threshold FROM applicants "
            "WHERE monitored AND id > %s ORDER BY id LIMIT %s", (after_id, limit)).fetchall()]


def baseline_matches(applicant_ids: list) -> dict:
    """
    {applicant_id: {(source, list, id), ...}}: the matches already found when each person was first screened, so
    only a NEW match is alerted. (An alert that was raised later is excluded by the unique key instead.)
    """
    out: dict = {}
    if not applicant_ids:
        return out
    with pool().connection() as conn:
        rows = conn.execute(
            "SELECT r.applicant_id, r.source, m->>'list' AS list, m->>'id' AS ref FROM screening_results r "
            "CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(r.payload->'matches') = 'array' "
            "THEN r.payload->'matches' ELSE '[]'::jsonb END) m WHERE r.applicant_id = ANY(%s)",
            (list(applicant_ids),)).fetchall()
    for r in rows:
        out.setdefault(r["applicant_id"], set()).add((r["source"], r["list"] or "", r["ref"] or ""))
    return out


def add_alert(applicant_id: int, source: str, list_name: str, ref: str, matched_name: str, score: float,
              payload: dict) -> int | None:
    """Record a new potential match. Returns the alert id, or None if this match was already alerted."""
    with pool().connection() as conn:
        row = conn.execute(
            "INSERT INTO monitoring_alerts (applicant_id, source, list, ref, matched_name, score, payload) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (applicant_id, source, list, ref) DO NOTHING "
            "RETURNING id", (applicant_id, source, list_name or "", str(ref), matched_name, score, Jsonb(payload))
        ).fetchone()
        return row["id"] if row else None


def mark_monitored_checked(applicant_ids: list) -> None:
    if applicant_ids:
        with pool().connection() as conn:
            conn.execute("UPDATE applicants SET last_monitored_at = now() WHERE id = ANY(%s)", (list(applicant_ids),))


_ALERT_COLS = ("al.id, al.applicant_id, a.full_name AS applicant_name, al.source, al.list, al.ref, al.matched_name, "
               "al.score, al.payload, al.status, al.created_at, al.decided_at, al.note")


def _alert_row(row: dict | None) -> dict | None:
    d = _clean(row)
    if d is not None:
        d["match"] = d.pop("payload", None)
    return d


def list_alerts(user_id=None, status=None, limit=100, offset=0) -> tuple:
    """(rows, total): alerts newest first. With user_id only those on that user's own screenings."""
    where, args = [], []
    if user_id is not None:
        where.append("a.user_id = %s")
        args.append(user_id)
    if status:
        where.append("al.status = %s")
        args.append(status)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    frm = " FROM monitoring_alerts al JOIN applicants a ON a.id = al.applicant_id"
    with pool().connection() as conn:
        rows = conn.execute("SELECT " + _ALERT_COLS + frm + clause + " ORDER BY al.id DESC LIMIT %s OFFSET %s",
                            args + [limit, offset]).fetchall()
        total = conn.execute("SELECT count(*) AS n" + frm + clause, args).fetchone()["n"]
    return [_alert_row(r) for r in rows], total


def get_alert(alert_id: int, user_id=None) -> dict | None:
    sql = ("SELECT " + _ALERT_COLS + " FROM monitoring_alerts al JOIN applicants a ON a.id = al.applicant_id "
           "WHERE al.id = %s")
    args: list = [alert_id]
    if user_id is not None:
        sql, args = sql + " AND a.user_id = %s", args + [user_id]
    with pool().connection() as conn:
        return _alert_row(conn.execute(sql, args).fetchone())


def decide_alert(alert_id: int, status: str, note: str | None, decided_by) -> None:
    with pool().connection() as conn:
        conn.execute("UPDATE monitoring_alerts SET status = %s, note = %s, decided_by = %s, "
                     "decided_at = CASE WHEN %s = 'open' THEN NULL ELSE now() END WHERE id = %s",
                     (status, note, decided_by, status, alert_id))


def monitoring_state() -> dict:
    """{source: {'fingerprint', 'checked_at', 'rescreened', 'new_alerts'}}"""
    with pool().connection() as conn:
        return {r["source"]: _clean(r) for r in conn.execute("SELECT * FROM monitoring_state").fetchall()}


def monitoring_state_put(source: str, fingerprint: str, rescreened: int, new_alerts: int) -> None:
    with pool().connection() as conn:
        conn.execute(
            "INSERT INTO monitoring_state (source, fingerprint, checked_at, rescreened, new_alerts) "
            "VALUES (%s, %s, now(), %s, %s) ON CONFLICT (source) DO UPDATE SET fingerprint = EXCLUDED.fingerprint, "
            "checked_at = now(), rescreened = EXCLUDED.rescreened, new_alerts = EXCLUDED.new_alerts",
            (source, fingerprint, rescreened, new_alerts))


def monitoring_counts(user_id=None) -> dict:
    """How many applicants are monitored and how many alerts are open (only the user's own, with user_id)."""
    scope, args = ("", []) if user_id is None else (" AND user_id = %s", [user_id])
    with pool().connection() as conn:
        monitored = conn.execute("SELECT count(*) AS n FROM applicants WHERE monitored" + scope, args).fetchone()["n"]
        open_alerts = conn.execute(
            "SELECT count(*) AS n FROM monitoring_alerts al JOIN applicants a ON a.id = al.applicant_id "
            "WHERE al.status = 'open'" + scope.replace("user_id", "a.user_id"), args).fetchone()["n"]
    return {"monitored_applicants": monitored, "open_alerts": open_alerts}


# --------------------------------------------------------------------------
# Batch screening
# --------------------------------------------------------------------------

def batch_create(user_id, filename: str, threshold: float, monitor: bool, rows: list, owner: str | None = None) -> int:
    """
    Record an uploaded file and every row in it, in one transaction. `rows` is a list of dicts with row_no,
    full_name, and either state 'pending' (will be screened) or state 'invalid' with an error message.
    """
    with pool().connection() as conn:
        try:
            b = conn.execute(
                "INSERT INTO batches (user_id, filename, threshold, monitor, total, owner) VALUES (%s, %s, %s, %s, %s, %s) "
                "RETURNING id", (user_id, filename, threshold, monitor, len(rows), owner)).fetchone()
        except psycopg.errors.UniqueViolation:
            raise BatchAlreadyRunning() from None
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO batch_rows (batch_id, row_no, full_name, state, error) VALUES (%s, %s, %s, %s, %s)",
                [(b["id"], r["row_no"], r["full_name"][:200], r["state"], r.get("error")) for r in rows])
        return b["id"]


def batch_row_screened(batch_id: int, row_no: int, applicant_id: int) -> None:
    with pool().connection() as conn:
        conn.execute("UPDATE batch_rows SET state = 'screened', applicant_id = %s, error = NULL "
                     "WHERE batch_id = %s AND row_no = %s", (applicant_id, batch_id, row_no))


def batch_row_failed(batch_id: int, row_no: int, error: str) -> None:
    with pool().connection() as conn:
        conn.execute("UPDATE batch_rows SET state = 'failed', error = %s WHERE batch_id = %s AND row_no = %s",
                     (error[:500], batch_id, row_no))


def batch_finish(batch_id: int, status: str) -> None:
    with pool().connection() as conn:
        conn.execute("UPDATE batches SET status = %s, finished_at = now() WHERE id = %s AND status = 'running'",
                     (status, batch_id))


def batch_checkpoint(batch_id: int) -> dict | None:
    """
    Called by the server running a batch before each row: records that it is alive and says whether to go on.
    Returns {"cancel_requested": bool, "user_status": str|None}, or None when the batch is no longer 'running'
    (another server gave it up as stale, or it was interrupted): the caller must stop.
    """
    with pool().connection() as conn:
        return conn.execute(
            "UPDATE batches b SET heartbeat_at = now() WHERE b.id = %s AND b.status = 'running' "
            "RETURNING b.cancel_requested, (SELECT p.status FROM profiles p WHERE p.id = b.user_id) AS user_status",
            (batch_id,)).fetchone()


def batch_request_cancel(batch_id: int, user_id) -> bool:
    """Ask a running batch to stop after its current row, whichever server runs it. True if newly requested."""
    with pool().connection() as conn:
        return conn.execute(
            "UPDATE batches SET cancel_requested = true WHERE id = %s AND user_id = %s AND status = 'running' "
            "AND NOT cancel_requested RETURNING id", (batch_id, user_id)).fetchone() is not None


def batch_interrupt_stale(stale_seconds: float) -> int:
    """
    Mark as interrupted every batch that says 'running' but whose server has stopped reporting (no heartbeat
    for `stale_seconds`): its server died. A batch that another, healthy server is running is left alone.
    Returns how many.
    """
    with pool().connection() as conn:
        cur = conn.execute(
            "UPDATE batches SET status = 'interrupted', finished_at = now() WHERE status = 'running' "
            "AND heartbeat_at < now() - make_interval(secs => %s)", (stale_seconds,))
        return cur.rowcount


def batch_interrupt_owned(owner: str) -> int:
    """A server shutting down on purpose gives up its own running batches at once, without waiting to go stale."""
    with pool().connection() as conn:
        cur = conn.execute("UPDATE batches SET status = 'interrupted', finished_at = now() "
                           "WHERE status = 'running' AND owner = %s", (owner,))
        return cur.rowcount


def batch_running_count(user_id=None) -> int:
    sql, args = "SELECT count(*) AS n FROM batches WHERE status = 'running'", []
    if user_id is not None:
        sql, args = sql + " AND user_id = %s", [user_id]
    with pool().connection() as conn:
        return conn.execute(sql, args).fetchone()["n"]


def batch_get(batch_id: int, user_id=None) -> dict | None:
    """One batch. With user_id, only if that user uploaded it."""
    sql, args = "SELECT * FROM batches WHERE id = %s", [batch_id]
    if user_id is not None:
        sql, args = sql + " AND user_id = %s", args + [user_id]
    with pool().connection() as conn:
        return _clean(conn.execute(sql, args).fetchone())


def batch_rows(batch_id: int) -> list:
    """Every row of a batch in file order, with the screening it produced (outcome and how many hits)."""
    with pool().connection() as conn:
        rows = conn.execute(
            "SELECT r.row_no, r.full_name, r.state, r.error, r.applicant_id, "
            "       a.overall_status, a.submitted_at, a.dob, a.nationality, a.cnic, a.father_name, a.province, "
            "       COALESCE(s.sanctions, 0) AS sanctions, COALESCE(s.news, 0) AS news, COALESCE(s.pep, 0) AS pep "
            "FROM batch_rows r "
            "LEFT JOIN applicants a ON a.id = r.applicant_id "
            "LEFT JOIN LATERAL ("
            "    SELECT sum(CASE WHEN sr.source NOT IN ('ADVERSE_MEDIA', 'PEP') "
            "                    THEN COALESCE((sr.payload->>'match_count')::int, 0) ELSE 0 END)::int AS sanctions, "
            "           sum(CASE WHEN sr.source = 'PEP' "
            "                    THEN COALESCE((sr.payload->>'match_count')::int, 0) ELSE 0 END)::int AS pep, "
            "           sum(CASE WHEN sr.source = 'ADVERSE_MEDIA' AND jsonb_typeof(sr.payload->'articles') = 'array' "
            "                    THEN jsonb_array_length(sr.payload->'articles') ELSE 0 END)::int AS news "
            "    FROM screening_results sr WHERE sr.applicant_id = a.id) s ON true "
            "WHERE r.batch_id = %s ORDER BY r.row_no", (batch_id,)).fetchall()
    return [_clean(r) for r in rows]


def batch_evidence(batch_id: int) -> list:
    """(row_no, applicant_id, filename, content, sha256) of every evidence PDF a batch produced."""
    with pool().connection() as conn:
        rows = conn.execute(
            "SELECT r.row_no, r.applicant_id, e.filename, e.content, e.sha256 FROM batch_rows r "
            "JOIN evidence_files e ON e.applicant_id = r.applicant_id WHERE r.batch_id = %s ORDER BY r.row_no",
            (batch_id,)).fetchall()
    for r in rows:
        if not r["sha256"]:
            r["sha256"] = hashlib.sha256(bytes(r["content"])).hexdigest()
    return rows

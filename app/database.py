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
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app import config

_SCHEMA = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
_ADMIN_LOCK = 727_274_001   # serialises changes to who is an admin, so the last admin cannot be removed by a race

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


class DatabaseNotConfigured(RuntimeError):
    """DATABASE_URL is missing."""


class LastAdminError(Exception):
    """The change would leave the system without an approved admin."""


# --------------------------------------------------------------------------
# Connections
# --------------------------------------------------------------------------

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
        check=ConnectionPool.check_connection,   # replace a connection the server closed while it sat idle
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
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_ADMIN_LOCK + 1,))
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
                     dob=None, nationality=None, threshold=None, user_id=None) -> int:
    with pool().connection() as conn:
        row = conn.execute(
            "INSERT INTO applicants (user_id, full_name, cnic, father_name, submitted_at, overall_status, "
            "dob, nationality, threshold) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (user_id, full_name, cnic, father_name, _when(submitted_at), overall_status, dob, nationality, threshold),
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
                "INSERT INTO evidence_files (applicant_id, filename, content) VALUES (%s, %s, %s) "
                "ON CONFLICT (applicant_id) DO UPDATE SET filename = EXCLUDED.filename, "
                "content = EXCLUDED.content, created_at = now()",
                (applicant_id, name, pdf),
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


def get_result(result_id, user_id=None):
    sql = ("SELECT r.* FROM screening_results r JOIN applicants a ON a.id = r.applicant_id WHERE r.id = %s")
    args: list = [result_id]
    if user_id is not None:
        sql, args = sql + " AND a.user_id = %s", args + [user_id]
    with pool().connection() as conn:
        return _result_row(conn.execute(sql, args).fetchone())


def get_evidence(applicant_id):
    """{'filename', 'content'} of the screening's evidence PDF, or None."""
    with pool().connection() as conn:
        return conn.execute("SELECT filename, content FROM evidence_files WHERE applicant_id = %s",
                            (applicant_id,)).fetchone()


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

"""
SQLite persistence layer.

Tables:
  - applicants: one row per screening request submitted
  - screening_results: one row per source checked for that applicant
    (UNSC / FIA_REDBOOK / ADVERSE_MEDIA), with an optional evidence_file
    path when the result was a HIT, plus cnic_match / near_miss flags
    (see app/screening/matching.py for what those mean).
  - admin_audit_log: append-only record of administrative actions (Red Book
    upload/activate/discard, list refreshes) with timestamp, client address
    and request ID — so "who changed the watchlist, and when" is answerable.
  - near_miss_log: append-only audit trail of scores that came close to a
    threshold but didn't cross it. A result that never runs must never
    look identical to one that ran and cleared cleanly — this table is
    what lets a compliance analyst periodically sanity-check where the
    thresholds are actually sitting relative to real applicant traffic.

This is deliberately simple (stdlib sqlite3, no ORM) so it's easy to swap
for Postgres later if this moves past a pilot.
"""

import sqlite3
from contextlib import contextmanager
from app.config import DB_PATH


def _column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS applicants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                cnic TEXT,
                father_name TEXT,
                submitted_at TEXT NOT NULL,
                overall_status TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS screening_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                applicant_id INTEGER NOT NULL,
                source TEXT NOT NULL,
                matched_entry TEXT,
                score REAL,
                status TEXT NOT NULL,
                detail TEXT,
                evidence_file TEXT,
                checked_at TEXT NOT NULL,
                FOREIGN KEY (applicant_id) REFERENCES applicants(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS near_miss_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                applicant_id INTEGER NOT NULL,
                source TEXT NOT NULL,
                matched_entry TEXT,
                score REAL,
                threshold REAL,
                detail TEXT,
                logged_at TEXT NOT NULL,
                FOREIGN KEY (applicant_id) REFERENCES applicants(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                target TEXT,
                detail TEXT,
                client_ip TEXT,
                request_id TEXT,
                logged_at TEXT NOT NULL
            )
        """)
        # Additive migration for DBs created before these columns
        # existed — safe to run every startup, only ALTERs if missing.
        # list_version records WHICH version of a watchlist a result was
        # checked against (FIA edition id, or the feed's last-refresh time),
        # so a questioned result can be traced to the exact list in force.
        for column, ddl in (
            ("cnic_match", "ALTER TABLE screening_results ADD COLUMN cnic_match INTEGER DEFAULT 0"),
            ("near_miss", "ALTER TABLE screening_results ADD COLUMN near_miss INTEGER DEFAULT 0"),
            ("list_version", "ALTER TABLE screening_results ADD COLUMN list_version TEXT"),
        ):
            if not _column_exists(conn, "screening_results", column):
                conn.execute(ddl)
        conn.commit()


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def insert_applicant(full_name, cnic, father_name, submitted_at, overall_status):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO applicants (full_name, cnic, father_name, submitted_at, overall_status) "
            "VALUES (?, ?, ?, ?, ?)",
            (full_name, cnic, father_name, submitted_at, overall_status),
        )
        conn.commit()
        return cur.lastrowid


def update_applicant_status(applicant_id, overall_status):
    with get_conn() as conn:
        conn.execute(
            "UPDATE applicants SET overall_status = ? WHERE id = ?",
            (overall_status, applicant_id),
        )
        conn.commit()


def insert_result(applicant_id, source, matched_entry, score, status, detail,
                   evidence_file, checked_at, cnic_match=False, near_miss=False,
                   list_version=None):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO screening_results "
            "(applicant_id, source, matched_entry, score, status, detail, evidence_file, "
            "checked_at, cnic_match, near_miss, list_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (applicant_id, source, matched_entry, score, status, detail, evidence_file,
             checked_at, int(bool(cnic_match)), int(bool(near_miss)), list_version),
        )
        conn.commit()
        return cur.lastrowid


def set_evidence_file(result_id, evidence_file):
    with get_conn() as conn:
        conn.execute("UPDATE screening_results SET evidence_file = ? WHERE id = ?",
                     (evidence_file, result_id))
        conn.commit()


def append_result_detail(result_id, extra: str):
    """Append a note to a stored result's detail (e.g. 'evidence PDF failed')."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE screening_results SET detail = COALESCE(detail, '') || ? WHERE id = ?",
            (extra, result_id),
        )
        conn.commit()


def log_admin_action(action, target, detail, client_ip, request_id, logged_at):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO admin_audit_log (action, target, detail, client_ip, request_id, logged_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (action, target, detail, client_ip, request_id, logged_at),
        )
        conn.commit()


def list_admin_actions(limit=100):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM admin_audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def insert_near_miss(applicant_id, source, matched_entry, score, threshold, detail, logged_at):
    """
    Append-only log of scores that fell within NEAR_MISS_MARGIN of a
    threshold but didn't cross it. Does not affect the applicant's status —
    purely an audit trail for periodically reviewing whether thresholds
    are set where compliance actually wants them.
    """
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO near_miss_log "
            "(applicant_id, source, matched_entry, score, threshold, detail, logged_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (applicant_id, source, matched_entry, score, threshold, detail, logged_at),
        )
        conn.commit()
        return cur.lastrowid


def list_near_misses(limit=200):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM near_miss_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_applicant(applicant_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM applicants WHERE id = ?", (applicant_id,)).fetchone()
        return dict(row) if row else None


def get_results_for_applicant(applicant_id):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM screening_results WHERE applicant_id = ? ORDER BY id", (applicant_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def list_applicants(limit=100):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM applicants ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_result(result_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM screening_results WHERE id = ?", (result_id,)).fetchone()
        return dict(row) if row else None

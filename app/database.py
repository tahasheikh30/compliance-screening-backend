"""
SQLite persistence layer.

Tables:
  - applicants: one row per screening request
  - screening_results: one row per source checked for that applicant
    (UNSC / OFAC / UKSL / FIA_REDBOOK / ADVERSE_MEDIA). `payload` holds the
    full match / article detail as JSON, `evidence_file` the evidence PDF name
    when the screening found something.

Databases created by the earlier version of this service open fine: new
columns are added on startup, old tables and rows are left untouched.

Deliberately simple (stdlib sqlite3, no ORM, parameterised queries only).
"""

import json
import sqlite3
from contextlib import contextmanager

from app import config


def _column_exists(conn, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})").fetchall())


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
        # additive migrations, safe to run on every startup
        for table, column, ddl in (
            ("applicants", "dob", "ALTER TABLE applicants ADD COLUMN dob TEXT"),
            ("applicants", "nationality", "ALTER TABLE applicants ADD COLUMN nationality TEXT"),
            ("applicants", "threshold", "ALTER TABLE applicants ADD COLUMN threshold REAL"),
            ("applicants", "records_screened", "ALTER TABLE applicants ADD COLUMN records_screened INTEGER"),
            ("screening_results", "cnic_match", "ALTER TABLE screening_results ADD COLUMN cnic_match INTEGER DEFAULT 0"),
            ("screening_results", "near_miss", "ALTER TABLE screening_results ADD COLUMN near_miss INTEGER DEFAULT 0"),
            ("screening_results", "list_version", "ALTER TABLE screening_results ADD COLUMN list_version TEXT"),
            ("screening_results", "records_screened", "ALTER TABLE screening_results ADD COLUMN records_screened INTEGER"),
            ("screening_results", "payload", "ALTER TABLE screening_results ADD COLUMN payload TEXT"),
        ):
            if not _column_exists(conn, table, column):
                conn.execute(ddl)
        conn.commit()


@contextmanager
def get_conn():
    # read DB_PATH at call time so tests can point it at a temporary folder
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def insert_applicant(full_name, cnic, father_name, submitted_at, overall_status,
                     dob=None, nationality=None, threshold=None):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO applicants (full_name, cnic, father_name, submitted_at, overall_status, "
            "dob, nationality, threshold) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (full_name, cnic, father_name, submitted_at, overall_status, dob, nationality, threshold),
        )
        conn.commit()
        return cur.lastrowid


def update_applicant_status(applicant_id, overall_status, records_screened=None):
    with get_conn() as conn:
        conn.execute("UPDATE applicants SET overall_status = ?, records_screened = ? WHERE id = ?",
                     (overall_status, records_screened, applicant_id))
        conn.commit()


def insert_result(applicant_id, source, matched_entry, score, status, detail, evidence_file, checked_at,
                  list_version=None, records_screened=None, payload=None):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO screening_results (applicant_id, source, matched_entry, score, status, detail, "
            "evidence_file, checked_at, list_version, records_screened, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (applicant_id, source, matched_entry, score, status, detail, evidence_file, checked_at,
             list_version, records_screened, json.dumps(payload) if payload is not None else None),
        )
        conn.commit()
        return cur.lastrowid


def set_evidence_file_for_applicant(applicant_id, evidence_file, statuses=("HIT", "REVIEW")):
    """Attach the screening's evidence PDF to every result row that found something."""
    marks = ",".join("?" for _ in statuses)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE screening_results SET evidence_file = ? WHERE applicant_id = ? AND status IN ({marks})",
            (evidence_file, applicant_id, *statuses),
        )
        conn.commit()


def append_result_detail(result_id, extra: str):
    with get_conn() as conn:
        conn.execute("UPDATE screening_results SET detail = COALESCE(detail, '') || ? WHERE id = ?",
                     (extra, result_id))
        conn.commit()


def _result_row(row) -> dict:
    d = dict(row)
    payload = d.pop("payload", None)
    try:
        p = json.loads(payload) if payload else {}
    except ValueError:
        p = {}
    d["matches"] = p.get("matches", [])
    d["articles"] = p.get("articles", [])
    d["match_count"] = p.get("match_count", len(d["matches"]))
    return d


def get_applicant(applicant_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM applicants WHERE id = ?", (applicant_id,)).fetchone()
        return dict(row) if row else None


def get_results_for_applicant(applicant_id):
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM screening_results WHERE applicant_id = ? ORDER BY id",
                            (applicant_id,)).fetchall()
        return [_result_row(r) for r in rows]


def list_applicants(limit=100):
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM applicants ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def get_result(result_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM screening_results WHERE id = ?", (result_id,)).fetchone()
        return _result_row(row) if row else None

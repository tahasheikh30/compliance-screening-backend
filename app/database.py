"""
SQLite persistence layer.

Two tables:
  - applicants: one row per screening request submitted
  - screening_results: one row per source checked for that applicant
    (UNSC / FIA_REDBOOK / ADVERSE_MEDIA), with an optional evidence_file
    path when the result was a HIT.

This is deliberately simple (stdlib sqlite3, no ORM) so it's easy to swap
for Postgres later if this moves past a pilot.
"""

import sqlite3
from contextlib import contextmanager
from app.config import DB_PATH


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


def insert_result(applicant_id, source, matched_entry, score, status, detail, evidence_file, checked_at):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO screening_results "
            "(applicant_id, source, matched_entry, score, status, detail, evidence_file, checked_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (applicant_id, source, matched_entry, score, status, detail, evidence_file, checked_at),
        )
        conn.commit()
        return cur.lastrowid


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

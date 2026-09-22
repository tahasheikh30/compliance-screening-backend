"""
Shared fixtures. The app reads STORAGE_DIR / API_KEY at import time, so they
are set here BEFORE anything from `app` is imported. Each test then gets its
own empty storage folder by re-pointing the module-level paths.
"""
import io
import os
import sys
import tempfile
from pathlib import Path

_SESSION_DIR = tempfile.mkdtemp(prefix="screening_tests_")
os.environ["STORAGE_DIR"] = _SESSION_DIR
os.environ["API_KEY"] = "test-key"
os.environ["ALLOWED_ORIGINS"] = "http://localhost:5173"
os.environ.pop("ADVERSE_MEDIA_API_KEY", None)
os.environ.pop("ANTHROPIC_API_KEY", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf as fitz  # noqa: E402
import pytest  # noqa: E402
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.pagesizes import A4, landscape  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle  # noqa: E402

API_HEADERS = {"X-API-Key": "test-key"}

FIRST = ["Muhammad", "Ali", "Ahmed", "Bilal", "Tariq", "Usman", "Imran", "Hamza", "Zubair", "Kashif"]
MIDDLE = ["Anwar", "Yaqub", "Rashid", "Haroon", "Shakeel", "Naveed", "Rizwan", "Faisal", "Adeel", "Sarfraz"]
LAST = ["Khan", "Raza", "Butt", "Malik", "Qureshi", "Sheikh", "Baig", "Niazi", "Cheema", "Gondal"]


def sample_people(n: int, with_cnic: bool = True):
    """
    n distinct, deterministic (name, cnic) pairs, n <= 1000. Each token comes
    from its own disjoint 10-word pool, so names are unique by construction
    and no name repeats a token — rapidfuzz's token_set_ratio dedupes repeated
    tokens before comparing, so e.g. "Muhammad Muhammad Khan" would otherwise
    score 100 against ANY name containing "Muhammad" and "Khan" plus one more
    token, a real (pre-existing) matching-engine edge case not worth
    reproducing by accident in fixture data.
    """
    if n > 1000:
        raise ValueError("sample_people supports at most 1000 distinct names (10x10x10 token pools)")
    out = []
    for i in range(n):
        name = f"{FIRST[i % 10]} {MIDDLE[(i // 10) % 10]} {LAST[(i // 100) % 10]}"
        out.append((name, f"35202-{1000000 + i:07d}-{i % 10}" if with_cnic else None))
    return out


def make_table_pdf(people, rows_per_page: int = 25) -> bytes:
    """A Red-Book-like PDF with real gridded tables (S.No | Name | Father | CNIC)."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4))
    story = []
    from reportlab.platypus import PageBreak
    for start in range(0, len(people), rows_per_page):
        chunk = people[start:start + rows_per_page]
        data = [["S.No", "Name", "Father Name", "CNIC"]]
        for j, (name, cnic) in enumerate(chunk):
            data.append([str(start + j + 1), name, "Abdul Rehman", cnic or ""])
        t = Table(data, repeatRows=1)
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
        story.append(t)
        story.append(PageBreak())
    doc.build(story)
    return buf.getvalue()


def make_text_pdf(people) -> bytes:
    """No table structure: 'Name CNIC ...' lines, forcing the text-heuristic path."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 800
    for name, cnic in people:
        c.drawString(50, y, f"{name} son of somebody")
        y -= 14
        if cnic:
            c.drawString(50, y, f"CNIC {cnic}")
            y -= 14
        if y < 60:
            c.showPage()
            y = 800
    c.save()
    return buf.getvalue()


def make_scanned_pdf() -> bytes:
    """A PDF whose only content is an image — no text layer."""
    doc = fitz.open()
    page = doc.new_page()
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 200, 200), False)
    pix.set_rect(pix.irect, (200, 200, 200))
    page.insert_image(fitz.Rect(50, 50, 300, 300), pixmap=pix)
    data = doc.tobytes()
    doc.close()
    return data


def make_encrypted_pdf(inner: bytes) -> bytes:
    doc = fitz.open(stream=inner, filetype="pdf")
    data = doc.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="owner")
    doc.close()
    return data


@pytest.fixture
def fia(tmp_path, monkeypatch):
    """The fia_redbook module pointed at an empty per-test storage folder."""
    from app.screening import fia_redbook as m
    cache = tmp_path / "cache"
    (cache / "fia_redbook_archive").mkdir(parents=True)
    (cache / "fia_redbook_editions").mkdir(parents=True)
    monkeypatch.setattr(m, "EDITIONS_DIR", cache / "fia_redbook_editions")
    monkeypatch.setattr(m, "FIA_REDBOOK_ARCHIVE_DIR", cache / "fia_redbook_archive")
    monkeypatch.setattr(m, "PDF_CACHE", cache / "fia_redbook_latest.pdf")
    monkeypatch.setattr(m, "NAMES_CACHE", cache / "fia_redbook_names.txt")
    monkeypatch.setattr(m, "META_CACHE", cache / "fia_redbook_meta.json")
    monkeypatch.setattr(m, "ACTIVE_POINTER", cache / "fia_redbook_active.json")
    return m


@pytest.fixture
def client(fia, tmp_path, monkeypatch):
    """A TestClient with an isolated DB + evidence dir + FIA storage."""
    from fastapi.testclient import TestClient
    from app import database, evidence, main
    from app.screening import unsc, ofac, uksl
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "screening.db")
    ev = tmp_path / "evidence"
    (ev / "screenshots").mkdir(parents=True)
    monkeypatch.setattr(evidence, "EVIDENCE_DIR", ev)
    monkeypatch.setattr(main, "SCREENSHOT_DIR", ev / "screenshots")
    empty = tmp_path / "cache"
    monkeypatch.setattr(unsc, "CACHE_FILE", empty / "unsc.xml")
    monkeypatch.setattr(ofac, "SDN_CACHE_FILE", empty / "sdn.xml")
    monkeypatch.setattr(ofac, "CONSOLIDATED_CACHE_FILE", empty / "cons.xml")
    monkeypatch.setattr(ofac, "_SOURCES", (
        ("SDN", ofac.SDN_XML_URL, empty / "sdn.xml"),
        ("Consolidated Non-SDN", ofac.CONSOLIDATED_XML_URL, empty / "cons.xml"),
    ))
    monkeypatch.setattr(uksl, "CACHE_FILE", empty / "uksl.csv")
    main.limiter.reset()  # slowapi's in-memory counters are process-global; don't leak across tests
    with TestClient(main.app, raise_server_exceptions=False) as c:
        yield c

"""Tests for the FIA Red Book edition registry (staging, activation, rollback, migration)."""
import json
import time

import pytest

from app.errors import AppError
from tests.conftest import (
    make_table_pdf, make_text_pdf, make_scanned_pdf, make_encrypted_pdf, sample_people,
)


# ---------- creating editions -------------------------------------------------

def test_table_pdf_is_staged_not_live(fia):
    people = sample_people(60)
    ed = fia.create_edition(make_table_pdf(people), "redbook_2026.pdf")
    assert ed["status"] == "staged"
    assert ed["names_found"] == 60
    assert ed["cnics_found"] == 60
    assert ed["parse"]["table_pages"] >= 2 and ed["parse"]["text_pages"] == 0
    assert ed["original_filename"] == "redbook_2026.pdf"
    # nothing is live yet
    assert fia.get_status()["configured"] is False
    assert fia.check("Muhammad Muhammad Khan")["available"] is False


def test_activation_makes_edition_live_and_screening_uses_it(fia):
    people = sample_people(60)
    ed = fia.create_edition(make_table_pdf(people), "rb.pdf")
    live = fia.activate_edition(ed["id"], confirm_reviewed=True)
    assert live["status"] == "active"
    st = fia.get_status()
    assert st["configured"] and st["edition_id"] == ed["id"] and st["names_found"] == 60
    hit = fia.check(people[3][0], applicant_cnic=None, threshold=60)
    assert hit["available"] and hit["matched_entry"] == people[3][0]
    assert hit["list_version"] == ed["id"]
    assert hit["page_number"] is not None  # page recorded at parse time


def test_exact_cnic_match_against_edition(fia):
    people = sample_people(40)
    ed = fia.create_edition(make_table_pdf(people), "rb.pdf")
    fia.activate_edition(ed["id"], confirm_reviewed=True)
    res = fia.check("Completely Different Name", applicant_cnic=people[7][1])
    assert res["cnic_match"] is True and res["matched_entry"] == people[7][0]


def test_first_activation_requires_review_confirmation(fia):
    ed = fia.create_edition(make_table_pdf(sample_people(30)), "rb.pdf")
    with pytest.raises(AppError) as e:
        fia.activate_edition(ed["id"])
    assert e.value.code == "FIA_REVIEW_CONFIRMATION_REQUIRED" and e.value.status == 400


def test_rollback_needs_no_reconfirmation_and_restores_old_list(fia):
    a = fia.create_edition(make_table_pdf(sample_people(30)), "a.pdf")
    fia.activate_edition(a["id"], confirm_reviewed=True)
    time.sleep(1.1)  # edition ids are second-resolution timestamps
    b = fia.create_edition(make_table_pdf(sample_people(50)), "b.pdf")
    fia.activate_edition(b["id"], confirm_reviewed=True)
    assert fia.get_status()["names_found"] == 50
    assert fia.get_edition(a["id"])["status"] == "archived"
    fia.activate_edition(a["id"])  # rollback: no confirm flag needed
    st = fia.get_status()
    assert st["edition_id"] == a["id"] and st["names_found"] == 30
    assert st["previous_editions_archived"] == 1


def test_duplicate_upload_is_rejected_with_pointer_to_existing(fia):
    pdf = make_table_pdf(sample_people(30))
    ed = fia.create_edition(pdf, "one.pdf")
    with pytest.raises(AppError) as e:
        fia.create_edition(pdf, "renamed-copy.pdf")
    assert e.value.code == "FIA_DUPLICATE_EDITION" and e.value.status == 409
    assert ed["id"] in e.value.message


# ---------- rejecting bad files ----------------------------------------------

@pytest.mark.parametrize("payload,code,status", [
    (b"this is definitely not a pdf", "FIA_NOT_A_PDF", 400),
    (b"%PDF-1.7\n garbage garbage garbage", "FIA_PDF_CORRUPT", 422),
])
def test_bad_bytes_get_specific_errors(fia, payload, code, status):
    with pytest.raises(AppError) as e:
        fia.create_edition(payload, "x.pdf")
    assert (e.value.code, e.value.status) == (code, status)
    assert e.value.hint  # every rejection tells the user what to do next


def test_encrypted_pdf_rejected(fia):
    with pytest.raises(AppError) as e:
        fia.create_edition(make_encrypted_pdf(make_table_pdf(sample_people(30))), "locked.pdf")
    assert e.value.code == "FIA_PDF_ENCRYPTED"


def test_scanned_pdf_gets_scan_specific_error(fia):
    with pytest.raises(AppError) as e:
        fia.create_edition(make_scanned_pdf(), "scan.pdf")
    assert e.value.code == "FIA_PDF_IS_SCAN"


def test_rejected_upload_leaves_no_files_behind(fia):
    with pytest.raises(AppError):
        fia.create_edition(make_scanned_pdf(), "scan.pdf")
    leftovers = [p for p in fia.EDITIONS_DIR.iterdir() if p.name != ".migrated"]
    assert leftovers == []


# ---------- warnings ----------------------------------------------------------

def test_text_layout_still_parses_but_is_flagged(fia):
    ed = fia.create_edition(make_text_pdf(sample_people(30)), "text.pdf")
    codes = {w["code"] for w in ed["warnings"]}
    assert "FIA_TEXT_FALLBACK_USED" in codes
    assert ed["names_found"] > 0


def test_no_cnic_edition_warns(fia):
    ed = fia.create_edition(make_table_pdf(sample_people(40, with_cnic=False)), "nocnic.pdf")
    assert "FIA_NO_CNICS" in {w["code"] for w in ed["warnings"]}


def test_edition_much_smaller_than_live_is_flagged_high(fia):
    a = fia.create_edition(make_table_pdf(sample_people(60)), "big.pdf")
    fia.activate_edition(a["id"], confirm_reviewed=True)
    time.sleep(1.1)
    b = fia.create_edition(make_table_pdf(sample_people(30)), "small.pdf")
    shrunk = [w for w in b["warnings"] if w["code"] == "FIA_SHRUNK"]
    assert shrunk and shrunk[0]["severity"] == "high"


# ---------- discard / retention ----------------------------------------------

def test_staged_can_be_discarded_but_live_and_archived_cannot(fia):
    a = fia.create_edition(make_table_pdf(sample_people(30)), "a.pdf")
    fia.activate_edition(a["id"], confirm_reviewed=True)
    with pytest.raises(AppError) as e:
        fia.delete_edition(a["id"])
    assert e.value.code == "FIA_EDITION_ACTIVE"

    time.sleep(1.1)
    b = fia.create_edition(make_table_pdf(sample_people(31)), "b.pdf")
    fia.activate_edition(b["id"], confirm_reviewed=True)
    with pytest.raises(AppError) as e:
        fia.delete_edition(a["id"])  # a is archived now
    assert e.value.code == "FIA_EDITION_RETAINED"

    time.sleep(1.1)
    c = fia.create_edition(make_table_pdf(sample_people(32)), "c.pdf")
    assert fia.delete_edition(c["id"]) == {"deleted": c["id"]}
    with pytest.raises(AppError) as e:
        fia.get_edition(c["id"])
    assert e.value.code == "FIA_EDITION_NOT_FOUND"


# ---------- security ----------------------------------------------------------

@pytest.mark.parametrize("bad_id", ["..", "../etc", "a/b", "20260101T000000Z_zzzzzzzz", "", "x" * 300])
def test_edition_id_is_validated_against_path_traversal(fia, bad_id):
    with pytest.raises(AppError) as e:
        fia.get_edition(bad_id)
    assert e.value.status == 404


def test_tampered_pdf_refuses_to_activate(fia):
    ed = fia.create_edition(make_table_pdf(sample_people(30)), "a.pdf")
    (fia.EDITIONS_DIR / ed["id"] / "redbook.pdf").write_bytes(b"%PDF-1.4 tampered")
    with pytest.raises(AppError) as e:
        fia.activate_edition(ed["id"], confirm_reviewed=True)
    assert e.value.code == "FIA_EDITION_INTEGRITY_FAILED"
    assert fia.get_status()["configured"] is False  # live edition untouched


def test_uploaded_filename_is_sanitised(fia):
    ed = fia.create_edition(make_table_pdf(sample_people(30)), "..\\..\\evil\x00name.pdf")
    assert "/" not in ed["original_filename"] and "\\" not in ed["original_filename"]
    assert "\x00" not in ed["original_filename"]


# ---------- browsing / diff / render -----------------------------------------

def test_browse_search_and_pagination(fia):
    people = sample_people(60)
    ed = fia.create_edition(make_table_pdf(people), "a.pdf")
    page1 = fia.browse_entries(ed["id"], limit=25)
    assert page1["total"] == 60 and len(page1["items"]) == 25
    assert page1["items"][0]["page"] == 1
    hits = fia.browse_entries(ed["id"], q=people[5][0].split()[0])
    assert hits["total"] >= 1
    by_cnic = fia.browse_entries(ed["id"], q=people[5][1].replace("-", ""))
    assert [i["name"] for i in by_cnic["items"]] == [people[5][0]]
    assert fia.browse_entries(ed["id"], offset=55, limit=25)["items"].__len__() == 5


def test_diff_against_active(fia):
    a = fia.create_edition(make_table_pdf(sample_people(40)), "a.pdf")
    fia.activate_edition(a["id"], confirm_reviewed=True)
    time.sleep(1.1)
    b = fia.create_edition(make_table_pdf(sample_people(45)), "b.pdf")
    d = fia.diff_against_active(b["id"])
    assert d["active_edition_id"] == a["id"] and d["added"] == 5 and d["removed"] == 0 and d["unchanged"] == 40


def test_render_page_returns_png_and_rejects_out_of_range(fia):
    ed = fia.create_edition(make_table_pdf(sample_people(30)), "a.pdf")
    png = fia.render_edition_page(ed["id"], 1)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    with pytest.raises(AppError) as e:
        fia.render_edition_page(ed["id"], 999)
    assert e.value.code == "FIA_PAGE_OUT_OF_RANGE"


# ---------- migration from the pre-registry layout ---------------------------

def test_legacy_live_and_archive_are_migrated_once(fia):
    old_pdf = make_table_pdf(sample_people(30))
    new_pdf = make_table_pdf(sample_people(50))
    # pre-registry layout: live files + one timestamped archive pair
    fia.PDF_CACHE.write_bytes(new_pdf)
    fia.NAMES_CACHE.write_text("\n".join(f"{n}|{c or ''}" for n, c in sample_people(50)), encoding="utf-8")
    fia.META_CACHE.write_text(json.dumps({
        "source": "upload", "original_filename": "redbook_2026.pdf", "names_found": 50,
        "cnics_found": 50, "loaded_at": "2026-05-01T10:00:00+00:00"}), encoding="utf-8")
    (fia.FIA_REDBOOK_ARCHIVE_DIR / "20250101T100000Z_redbook.pdf").write_bytes(old_pdf)
    (fia.FIA_REDBOOK_ARCHIVE_DIR / "20250101T100000Z_names.txt").write_text(
        "\n".join(f"{n}|{c or ''}" for n, c in sample_people(30)), encoding="utf-8")

    listing = fia.list_editions()
    assert len(listing["editions"]) == 2
    by_status = {e["status"]: e for e in listing["editions"]}
    assert by_status["active"]["names_found"] == 50
    assert by_status["active"]["original_filename"] == "redbook_2026.pdf"
    assert by_status["archived"]["names_found"] == 30
    assert by_status["archived"]["legacy"] is True
    # live screening still works exactly as before, from the untouched live files
    assert fia.check(sample_people(50)[10][0])["available"] is True
    # idempotent: a second call does not duplicate anything
    assert len(fia.list_editions()["editions"]) == 2
    # originals untouched
    assert fia.PDF_CACHE.read_bytes() == new_pdf


def test_old_two_field_cache_lines_still_parse(fia):
    fia.NAMES_CACHE.write_text("Ali Khan|35202-1234567-1\nPlain Name\n", encoding="utf-8")
    assert fia._load_entries() == [("Ali Khan", "35202-1234567-1"), ("Plain Name", None)]
    assert fia._load_entries_full()[0][2] is None

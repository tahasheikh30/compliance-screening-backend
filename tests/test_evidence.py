import pymupdf

from app import evidence
from app.screening import engine
from tests import fixtures as fx


def _text(pdf: bytes):
    with pymupdf.open(stream=pdf, filetype="pdf") as d:
        return len(d), "\n".join(p.get_text() for p in d)


def test_evidence_pdf_contains_all_sections(fake_sources):
    fake_sources["news"] = fx.NEWS_RSS_HIT
    r = engine.screen("Muhammad Ali Khan", dob="1975-03-04", nationality="Pakistan")
    name, pdf = evidence.generate_evidence_pdf(r, "CS-20261001-00007")
    assert name == "evidence_CS-20261001-00007.pdf" and pdf.startswith(b"%PDF")
    pages, text = _text(pdf)
    for needle in ("SANCTIONS SCREENING", "POTENTIAL MATCH", "Muhammad Ali Khan", "CS-20261001-00007", "LISTS SCREENED",
                   "UN Security Council Consolidated List", "METHOD", "SANCTIONS / WATCH-LIST MATCHES", "QDi.001",
                   "ADVERSE MEDIA", "REVIEWER DECISION", "Page 1 of"):
        assert needle in text, needle
    assert "SCORE" in text and "ALI KHAN BHAI" in text


def test_evidence_lists_unavailable_sources_as_not_screened(fake_sources):
    fake_sources["fail"].add("https://www.fia.gov.pk/press-pub")
    r = engine.screen("Muhammad Ali Khan")
    _, text = _text(evidence.generate_evidence_pdf(r, "CS-1")[1])
    assert "Not screened." in text and "FIA website could not be reached" in text


def test_many_matches_paginate_with_page_numbers(fake_sources):
    r = engine.screen("Ali Khan", threshold=50)
    r["matches"] = r["matches"] * 12                  # force a long document
    pages, text = _text(evidence.generate_evidence_pdf(r, "CS-2")[1])
    assert pages > 2 and f"Page {pages} of {pages}" in text


def test_non_latin_names_do_not_crash_the_pdf(fake_sources):
    r = engine.screen("Muhammad Ali Khan")
    r["applicant"]["name"] = "محمد علي خان Zoë"
    r["matches"][0]["remarks"] = "ünïcödé \u2013 dash \u201cquote\u201d"
    _, text = _text(evidence.generate_evidence_pdf(r, "CS-3")[1])
    assert "Zoe" in text and "dash" in text


def test_clean_helper():
    assert evidence.clean("Zoë \u2014 x") == "Zoe - x" and evidence.clean("日本") == "??"


def test_pep_matches_get_their_own_section_and_one_person_is_shown_once():
    import pymupdf
    from app.evidence import generate_evidence_pdf
    from tests.test_grouping import azma_records

    recs = azma_records()
    for m in recs:
        m.update(type="Individual", programs="PEP, Provincial, Punjab", dob_year_match="n/a", nationality="Pakistan",
                 listed_on="", province_match=True, cnic_match=None, father_match=None, father_name="", position="",
                 match_reason="", list="Politically Exposed Persons (national and provincial)")
    result = {"applicant": {"name": "Azma Bokhari", "dob": "", "nationality": "Pakistan", "cnic": "", "father_name": "",
                            "province": "Punjab"},
              "threshold": 85.0, "screened_at": "2026-10-12T10:00:00+00:00", "lists": [], "total_records": 10,
              "sanctions_hit_count": 0, "media_hit_count": 0, "pep_hit_count": 3, "hit_count": 0, "hit": False,
              "adverse_media": None, "matches": recs, "truncated": False, "sources": {}}
    _, pdf = generate_evidence_pdf(result, "CS-TEST")
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        text = "\n".join(page.get_text() for page in doc)
    assert "POLITICALLY EXPOSED PERSONS (PEP)" in text
    assert "1 (from 3 source records)" in text
    assert text.count("3 RECORDS") == 1
    assert "None at the selected threshold." in text          # the sanctions section is empty


def _bare_result(**over):
    r = {"applicant": {"name": "Test Person", "dob": "", "nationality": "", "cnic": "", "father_name": "", "province": ""},
         "threshold": 85.0, "screened_at": "2026-10-12T10:00:00+00:00", "lists": [], "total_records": 10,
         "sanctions_hit_count": 0, "media_hit_count": 0, "pep_hit_count": 0, "hit_count": 0, "hit": False,
         "adverse_media": None, "matches": [], "truncated": False, "sources": {}}
    r.update(over)
    return r


def test_banner_says_what_was_found():
    head, detail, *_ = evidence.result_banner(_bare_result(sanctions_hit_count=2, media_hit_count=1), None, 1)
    assert "POTENTIAL MATCH" in head and "Escalate" in head
    assert "2 sanctions / watch-list matches need a decision" in detail and "1 politically exposed person" in detail

    head, detail, *_ = evidence.result_banner(_bare_result(media_hit_count=8), None, 1)
    assert head == "RESULT: MANUAL REVIEW REQUIRED."
    assert detail.startswith("No sanctions or watch-list match.")
    assert "1 politically exposed person" in detail and "8 adverse media articles" in detail

    head, detail, *_ = evidence.result_banner(_bare_result(), None, 0)
    assert head.startswith("RESULT: NO MATCH") and detail == ""


def test_banner_follows_the_screenings_own_status_and_counts_unscreened_lists():
    r = _bare_result(lists=[{"list": "A", "records": 5}, {"list": "B", "records": 0}])
    head, detail, *_ = evidence.result_banner(r, "MANUAL_REVIEW", 0)
    assert head == "RESULT: MANUAL REVIEW REQUIRED." and "1 list that could not be screened" in detail
    assert evidence.result_banner(_bare_result(), "AUTO_CLEAR", 0)[0].startswith("RESULT: NO MATCH")


def test_the_pdf_carries_the_banner():
    _, pdf = evidence.generate_evidence_pdf(_bare_result(media_hit_count=3), "CS-B", overall_status="MANUAL_REVIEW")
    _, text = _text(pdf)
    assert "MANUAL REVIEW REQUIRED" in text and "3 adverse media articles" in text and "POTENTIAL MATCH" not in text

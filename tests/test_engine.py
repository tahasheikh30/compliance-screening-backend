"""End to end screening logic against synthetic feeds (no network)."""

from app.screening import engine, loader
from tests import fixtures as fx


def test_clear_applicant_has_no_hits_and_every_source_clear(fake_sources):
    r = engine.screen("Completely Unrelated Person")
    assert r["hit"] is False and r["hit_count"] == 0
    assert {k: engine.source_status(v) for k, v in r["sources"].items()} == {
        "UNSC": "CLEAR", "OFAC": "CLEAR", "UKSL": "CLEAR", "FIA_REDBOOK": "CLEAR", "ADVERSE_MEDIA": "CLEAR"}
    assert engine.overall_status({k: "CLEAR" for k in r["sources"]}) == "AUTO_CLEAR"
    assert r["total_records"] == 3 + 4 + 1 + 2 or r["total_records"] > 0
    assert r["lists"][0]["records"] == 3


def test_un_match_includes_alias_dob_year_and_details(fake_sources):
    r = engine.screen("Muhammad Ali Khan", dob="1975-01-01", nationality="Pakistan")
    un = r["sources"]["UNSC"]["matches"]
    assert len(un) == 1 and un[0]["id"] == "QDi.001"
    m = un[0]
    assert m["score"] >= 95 and m["dob_year_match"] == "Yes" and m["nationality"] == "Pakistan"
    assert "ALI KHAN BHAI" in m["aliases"] and m["list"] == "UN Security Council Consolidated List"
    assert r["hit"] and r["sanctions_hit_count"] >= 1
    assert engine.source_status(r["sources"]["UNSC"]) == "HIT"


def test_dob_year_mismatch_and_missing(fake_sources):
    assert engine.screen("Muhammad Ali Khan", dob="1990-01-01")["matches"][0]["dob_year_match"] == "No"
    assert engine.screen("Muhammad Ali Khan")["matches"][0]["dob_year_match"] == "n/a"


def test_dob_and_nationality_never_filter_matches(fake_sources):
    a = engine.screen("Muhammad Ali Khan", dob="1990-01-01", nationality="Brazil")
    assert any(m["id"] == "QDi.001" for m in a["matches"])


def test_ofac_alias_match_reports_the_alias_that_matched(fake_sources):
    r = engine.screen("Hassan Zulfikar")
    m = next(x for x in r["sources"]["OFAC"]["matches"] if x["id"] == "OFAC-1001")
    assert m["matched_name"] == "HASSAN ZULFIKAR" and m["score"] == 100.0


def test_uk_and_fia_matches_and_source_routing(fake_sources):
    assert engine.screen("Usama Rehman")["sources"]["UKSL"]["matches"][0]["id"] == "UK0001"
    r = engine.screen("Tariq Bhatti")
    fia = r["sources"]["FIA_REDBOOK"]["matches"]
    assert fia and fia[0]["list"].startswith("FIA ") and "CNIC: 35202-1111111-1" in fia[0]["remarks"]


def test_threshold_controls_matching(fake_sources):
    assert engine.screen("Mohammad Ali Khan Bhatti", threshold=95)["sources"]["UNSC"]["matches"] == []
    assert engine.screen("Mohammad Ali Khan Bhatti", threshold=70)["sources"]["UNSC"]["matches"]
    assert engine.screen("Muhammad Ali Khan", threshold=10)["threshold"] == 85   # out of range -> default


def test_adverse_media_hit_is_review_not_hit(fake_sources):
    fake_sources["news"] = fx.NEWS_RSS_HIT
    r = engine.screen("Bilal Ahmed Qureshi")
    assert r["media_hit_count"] == 1 and r["sanctions_hit_count"] == 0 and r["hit"]
    assert engine.source_status(r["sources"]["ADVERSE_MEDIA"]) == "REVIEW"
    assert engine.overall_status({"UNSC": "CLEAR", "ADVERSE_MEDIA": "REVIEW"}) == "MANUAL_REVIEW"


def test_matches_sorted_best_first_and_truncated(fake_sources, monkeypatch):
    extra = ("<INDIVIDUAL><DATAID>9</DATAID><REFERENCE_NUMBER>QDi.009</REFERENCE_NUMBER>"
             "<FIRST_NAME>MUHAMMED</FIRST_NAME><SECOND_NAME>ALI</SECOND_NAME><THIRD_NAME>KHANN</THIRD_NAME>"
             "<UN_LIST_TYPE>Taliban</UN_LIST_TYPE></INDIVIDUAL></INDIVIDUALS>")
    fake_sources["texts"][loader.UN_URL] = fx.UN_XML.replace("</INDIVIDUALS>", extra)
    full = engine.screen("Muhammad Ali Khan", threshold=80)
    ids = [m["id"] for m in full["sources"]["UNSC"]["matches"]]
    assert ids[:2] == ["QDi.001", "QDi.009"] and full["truncated"] is False
    scores = [m["score"] for m in full["matches"]]
    assert scores == sorted(scores, reverse=True)

    monkeypatch.setattr(engine, "MAX_MATCHES", 1)
    cut = engine.screen("Muhammad Ali Khan", threshold=80)
    assert cut["truncated"] is True and len(cut["matches"]) == 1
    assert cut["sanctions_hit_count"] == 2          # the count is the real total, not the kept list
    assert cut["matches"][0]["id"] == "QDi.001"      # best score kept


# ---- a source that did not run must never look clean ------------------------

def test_failed_list_download_is_error_not_clear(fake_sources):
    fake_sources["fail"].add(loader.UN_URL)
    r = engine.screen("Completely Unrelated Person")
    un = r["sources"]["UNSC"]
    assert not un["available"] and "could not be downloaded" in un["error"]
    assert engine.source_status(un) == "ERROR"
    # the other sources still ran
    assert engine.source_status(r["sources"]["UKSL"]) == "CLEAR"
    statuses = {k: engine.source_status(v) for k, v in r["sources"].items()}
    assert engine.overall_status(statuses) == "MANUAL_REVIEW"


def test_list_that_parses_to_zero_records_is_an_error(fake_sources):
    fake_sources["texts"][loader.UK_URL] = "<Designations><DateGenerated>x</DateGenerated></Designations>"
    r = engine.screen("Anyone Here")
    assert engine.source_status(r["sources"]["UKSL"]) == "ERROR"
    assert "format may have changed" in r["sources"]["UKSL"]["error"]


def test_one_ofac_file_failing_fails_the_whole_ofac_source(fake_sources):
    fake_sources["fail"].add(loader.OFAC_CONS_ALT_URL)
    r = engine.screen("Anyone Here")
    assert engine.source_status(r["sources"]["OFAC"]) == "ERROR"


def test_fia_unavailable_is_not_configured_and_blocks_auto_clear_by_default(fake_sources):
    fake_sources["fail"].add("https://www.fia.gov.pk/press-pub")
    r = engine.screen("Completely Unrelated Person")
    fia = r["sources"]["FIA_REDBOOK"]
    assert engine.source_status(fia) == "NOT_CONFIGURED" and "FIA Red Book could not be loaded" in fia["error"]
    statuses = {k: engine.source_status(v) for k, v in r["sources"].items()}
    assert engine.overall_status(statuses, fia_required=True) == "MANUAL_REVIEW"
    assert engine.overall_status(statuses, fia_required=False) == "AUTO_CLEAR"   # the workflow's behaviour


def test_fia_pdf_download_failure_and_blocked_html_are_reported(fake_sources):
    fake_sources["bytes"]["https://www.fia.gov.pk/files/redbook-2026.pdf"] = b"<html>access denied</html>"
    fia = engine.screen("Anyone Here")["sources"]["FIA_REDBOOK"]
    assert not fia["available"] and "not a PDF" in fia["error"]


def test_news_failure_is_error_not_clear(fake_sources):
    fake_sources["news_fail"] = True
    r = engine.screen("Completely Unrelated Person")
    media = r["sources"]["ADVERSE_MEDIA"]
    assert engine.source_status(media) == "ERROR" and "Not available" in media["error"]
    assert r["adverse_media"]["status"].startswith("Not available")


def test_non_rss_news_response_is_error(fake_sources):
    fake_sources["news"] = "<html>captcha</html>"
    assert engine.source_status(engine.screen("Anyone Here")["sources"]["ADVERSE_MEDIA"]) == "ERROR"


def test_overall_status_precedence():
    assert engine.overall_status({"A": "HIT", "B": "ERROR"}) == "ESCALATE_TO_COMPLIANCE"
    assert engine.overall_status({"A": "REVIEW", "B": "ERROR"}) == "MANUAL_REVIEW"
    assert engine.overall_status({"A": "CLEAR", "B": "CLEAR"}) == "AUTO_CLEAR"


def test_describe_source_wording(fake_sources):
    r = engine.screen("Muhammad Ali Khan")
    d = engine.describe_source(r["sources"]["UNSC"], "HIT", r["threshold"])
    assert "potential match" in d and "QDi.001" in d
    assert "No match at" in engine.describe_source(r["sources"]["UKSL"], "CLEAR", 85)
    fake_sources["fail"].add(loader.UN_URL)
    bad = engine.screen("x y")["sources"]["UNSC"]
    assert "NOT screened" in engine.describe_source(bad, "ERROR", 85)


# ---- cache -------------------------------------------------------------------

def test_cache_reuses_lists_and_never_caches_failures(fake_sources, monkeypatch):
    monkeypatch.setattr(loader, "LIST_CACHE_TTL_SECONDS", 3600)
    calls = {"n": 0}
    orig = loader.fetch_text

    def counting(url, read_timeout=0):
        calls["n"] += 1
        return orig(url, read_timeout)

    monkeypatch.setattr(loader, "fetch_text", counting)
    loader.load_group("UNSC")
    loader.load_group("UNSC")
    assert calls["n"] == 1 and loader.cache_status()["UNSC"]["cached"]
    loader.clear_cache()
    fake_sources["fail"].add(loader.UK_URL)
    assert loader.load_group("UKSL").error and not loader.cache_status()["UKSL"]["cached"]
    fake_sources["fail"].clear()
    assert loader.load_group("UKSL").available            # retried, not stuck on the failure

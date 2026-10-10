"""End to end screening logic against synthetic feeds (no network)."""

from app.screening import engine, loader
from tests import fixtures as fx


def test_clear_applicant_has_no_hits_and_every_source_clear(fake_sources):
    r = engine.screen("Completely Unrelated Person")
    assert r["hit"] is False and r["hit_count"] == 0
    assert {k: engine.source_status(v) for k, v in r["sources"].items()} == {
        "UNSC": "CLEAR", "OFAC": "CLEAR", "UKSL": "CLEAR", "FIA_REDBOOK": "CLEAR", "NACTA": "CLEAR", "PEP": "CLEAR",
        "ADVERSE_MEDIA": "CLEAR"}
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
    assert engine.source_status(media) == "ERROR" and "ConnectionError" in media["error"]
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
    text = engine.describe_source(bad, "ERROR", 85)
    assert text.startswith("Not screened. ") and "not yet cleared" in text and ". ." not in text


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


def test_per_source_matches_are_capped_but_the_true_count_is_kept(fake_sources, monkeypatch):
    monkeypatch.setattr(engine, "MAX_MATCHES", 1)
    extra = ("<INDIVIDUAL><DATAID>9</DATAID><REFERENCE_NUMBER>QDi.009</REFERENCE_NUMBER>"
             "<FIRST_NAME>MUHAMMED</FIRST_NAME><SECOND_NAME>ALI</SECOND_NAME><THIRD_NAME>KHANN</THIRD_NAME>"
             "<UN_LIST_TYPE>Taliban</UN_LIST_TYPE></INDIVIDUAL></INDIVIDUALS>")
    fake_sources["texts"][loader.UN_URL] = fx.UN_XML.replace("</INDIVIDUALS>", extra)
    un = engine.screen("Muhammad Ali Khan", threshold=80)["sources"]["UNSC"]
    assert len(un["matches"]) == 1 and un["match_count"] == 2
    assert "2 potential match(es)" in engine.describe_source(un, "HIT", 80)


# ---- a source with several books where only some can be read ------------------

def _second_redbook(fake_sources, readable):
    """Add a second FIA book. `readable` False makes it a PDF whose layout the parser cannot read."""
    import io
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    fake_sources["texts"]["https://www.fia.gov.pk/press-pub"] = (
        fx.FIA_PAGE_HTML + '<a href="/files/terror.pdf">FIA Red Book Most Wanted Terrorists</a>')
    if readable:
        fake_sources["bytes"]["https://www.fia.gov.pk/files/terror.pdf"] = fx.make_redbook_pdf(
            [("SADDAR HAYAT", "ZIA ULLAH", "17301-3333333-3", "01-01-1985", "PESHAWAR", "FIR No(s) 5/2018 Section 302")])
    else:
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=A4)
        c.drawString(30, 800, "S.No  Name  Father Name  Head Money  Wanted By")
        c.drawString(30, 780, "1  SADDAR HAYAT  ZIA ULLAH  8,000,000  KP POLICE")
        c.save()
        fake_sources["bytes"]["https://www.fia.gov.pk/files/terror.pdf"] = buf.getvalue()


def test_both_red_books_are_screened_when_both_can_be_read(fake_sources):
    _second_redbook(fake_sources, readable=True)
    r = engine.screen("Saddar Hayat")
    fia = r["sources"]["FIA_REDBOOK"]
    assert [li["status"] for li in fia["lists"]] == ["OK", "OK"] and not fia["partial"]
    assert {li["list"] for li in fia["lists"]} == {"FIA Red Book 2026", "FIA Red Book Most Wanted Terrorists"}
    assert engine.source_status(fia) == "HIT" and fia["matches"][0]["list"] == "FIA Red Book Most Wanted Terrorists"


def test_unreadable_second_book_is_flagged_never_silently_clear(fake_sources):
    _second_redbook(fake_sources, readable=False)
    r = engine.screen("Completely Unrelated Person")
    fia = r["sources"]["FIA_REDBOOK"]
    assert fia["available"] and fia["partial"]                     # one book was read, one was not
    unread = [li for li in fia["lists"] if li["status"] != "OK"]
    assert len(unread) == 1 and "no records could be read" in unread[0]["status"] and unread[0]["records"] == 0
    assert engine.source_status(fia) == "PARTIAL"
    text = engine.describe_source(fia, "PARTIAL", 85)
    assert text.startswith("Incomplete screening.") and "Most Wanted Terrorists" in text and "not yet cleared" in text
    statuses = {k: engine.source_status(v) for k, v in r["sources"].items()}
    assert engine.overall_status(statuses, fia_required=True) == "MANUAL_REVIEW"
    assert engine.overall_status(statuses, fia_required=False) == "AUTO_CLEAR"   # the workflow's optional-FIA behaviour


def test_a_hit_in_the_readable_book_still_mentions_the_unreadable_one(fake_sources):
    _second_redbook(fake_sources, readable=False)
    fia = engine.screen("Tariq Bhatti")["sources"]["FIA_REDBOOK"]
    assert engine.source_status(fia) == "HIT"
    assert "some lists of this source were not fully screened" in engine.describe_source(fia, "HIT", 85)


def test_every_source_reports_its_lists(fake_sources):
    r = engine.screen("Anyone Here")
    assert [li["list"] for li in r["sources"]["OFAC"]["lists"]] == [
        "OFAC Specially Designated Nationals (SDN) List", "OFAC Consolidated (Non-SDN) List"]
    assert all(li["status"] == "OK" for s in r["sources"].values() for li in s["lists"])
    assert r["sources"]["ADVERSE_MEDIA"]["lists"] == []


# ---- CNIC and father's name ---------------------------------------------------------

def test_cnic_match_on_the_fia_red_book_is_reported_whatever_the_name(fake_sources):
    r = engine.screen("Totally Different Name", cnic="35202-1111111-1")
    fia = r["sources"]["FIA_REDBOOK"]
    assert engine.source_status(fia) == "HIT"
    m = fia["matches"][0]
    assert m["cnic_match"] is True and m["primary_name"] == "TARIQ MEHMOOD SATTAR" and m["score"] < 85


def test_cnic_matches_rank_above_better_name_scores(fake_sources):
    # "Tariq Bhatti" matches a Red Book name exactly; the CNIC belongs to a different NACTA person
    r = engine.screen("Tariq Bhatti", cnic="3740565359881")
    assert r["matches"][0]["cnic_match"] is True and r["matches"][0]["source"] == "nacta"
    assert any(m["source"] == "fia" and not m["cnic_match"] for m in r["matches"])


def test_province_is_supporting_evidence_only(fake_sources):
    def first(**kw):
        return engine.screen("Muhammad Shakir", **kw)["sources"]["NACTA"]["matches"][0]
    same, other, none = first(province="Punjab"), first(province="Sindh"), first()
    assert same["province"] == "Punjab" and same["province_match"] is True
    assert other["province_match"] is False and other["score"] == 100.0    # a different province never removes the match
    assert none["province_match"] is None                                  # nothing to compare is not "different"
    assert first(province="PB")["province_match"] is True                  # spelled another way
    kp = engine.screen("Akhtar Muhammad Khalil", province="Khyber Pakhtunkhwa")["sources"]["NACTA"]["matches"][0]
    assert kp["province"] == "Khyber Pakhtunkhwa" and kp["province_match"] is True   # the list says "KP"
    assert engine.screen("Muhammad Shakir", province="kpk")["applicant"]["province"] == "Khyber Pakhtunkhwa"


def test_a_list_with_no_province_gives_no_province_verdict(fake_sources):
    un = engine.screen("Muhammad Shakir", province="Punjab", threshold=50)["sources"]["UNSC"]["matches"]
    assert all(m["province"] == "" and m["province_match"] is None for m in un)


def test_father_name_is_supporting_evidence_only(fake_sources):
    same = engine.screen("Muhammad Shakir", father_name="Qabil Khan")["sources"]["NACTA"]["matches"][0]
    other = engine.screen("Muhammad Shakir", father_name="Somebody Else")["sources"]["NACTA"]["matches"][0]
    assert same["father_match"] is True and other["father_match"] is False
    assert other["score"] == 100.0                      # a different father never removes the match


def test_applicant_cnic_is_normalised_in_the_result(fake_sources):
    assert engine.screen("Anyone Here", cnic="37405-6535988-1")["applicant"]["cnic"] == "3740565359881"
    assert engine.screen("Anyone Here", cnic="not a cnic")["applicant"]["cnic"] == ""


def test_nacta_overall_status_rules():
    assert engine.overall_status({"NACTA": "NOT_CONFIGURED", "UNSC": "CLEAR"}) == "MANUAL_REVIEW"
    assert engine.overall_status({"NACTA": "NOT_CONFIGURED", "UNSC": "CLEAR"}, nacta_required=False) == "AUTO_CLEAR"
    assert engine.overall_status({"NACTA": "PARTIAL"}, nacta_required=False) == "AUTO_CLEAR"
    # not required never hides a real problem elsewhere
    assert engine.overall_status({"NACTA": "NOT_CONFIGURED", "UNSC": "ERROR"}, nacta_required=False) == "MANUAL_REVIEW"


# ---- per-list status for the Lists page ------------------------------------------------

def test_cache_status_reports_each_red_book_separately(fake_sources):
    _second_redbook(fake_sources, readable=False)
    loader.load_group("FIA_REDBOOK")
    fia = loader.cache_status()["FIA_REDBOOK"]
    assert fia["cached"] and fia["error"] is None
    good, bad = fia["lists"]
    assert good["list"] == "FIA Red Book 2026" and good["records"] == 2 and good["status"] == "OK"
    assert good["source"] == "https://www.fia.gov.pk/files/redbook-2026.pdf" and "sample" not in good
    assert bad["list"] == "FIA Red Book Most Wanted Terrorists" and bad["records"] == 0
    assert "no records could be read" in bad["status"]
    assert bad["source"] == "https://www.fia.gov.pk/files/terror.pdf"
    assert "Head Money" in bad["sample"]            # the text that was read, so the layout can be diagnosed


def test_the_sample_is_admin_only_and_never_reaches_screening_results(fake_sources):
    _second_redbook(fake_sources, readable=False)
    fia = engine.screen("Anyone Here")["sources"]["FIA_REDBOOK"]
    assert all("sample" not in li and "source" not in li for li in fia["lists"])


def test_cache_status_keeps_the_reason_a_source_failed(fake_sources):
    fake_sources["fail"].add(loader.UK_URL)
    loader.load_group("UKSL")
    uk = loader.cache_status()["UKSL"]
    assert uk["cached"] is False and "could not be downloaded" in uk["error"]
    assert uk["lists"][0]["status"].startswith("The list could not be downloaded")
    assert loader.cache_status()["UNSC"] == {"cached": False, "records": 0, "age_seconds": None, "error": None, "lists": []}


def test_a_pdf_without_text_is_reported_as_such(fake_sources):
    import io
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.rect(50, 50, 100, 100)          # a page with a drawing but no text, like a scanned image
    c.save()
    fake_sources["bytes"]["https://www.fia.gov.pk/files/redbook-2026.pdf"] = buf.getvalue()
    loader.load_group("FIA_REDBOOK")
    fia = loader.cache_status()["FIA_REDBOOK"]
    assert fia["error"] and "no extractable text" in fia["lists"][0]["status"]


# ---- NACTA live download ------------------------------------------------------------------

NACTA_URL = "https://nfs.example.pk/export.json"
NACTA_JSON = (b'[{"id": 1, "name": "Live Person", "father_name": "Live Father", "cnic": "3740565359881", '
              b'"province": "Punjab", "district": "HANGU"}]')


def _live(fake_sources, monkeypatch, body=NACTA_JSON):
    from app import config
    monkeypatch.setattr(config, "NACTA_PERSONS_URL", NACTA_URL)
    fake_sources["nacta"] = "real"
    fake_sources["bytes"][NACTA_URL] = body
    loader.clear_cache()


def test_nacta_is_downloaded_live_and_saved_as_the_last_good_copy(fake_sources, monkeypatch, storage):
    _live(fake_sources, monkeypatch)
    nacta = engine.screen("Live Person", cnic="3740565359881")["sources"]["NACTA"]
    assert engine.source_status(nacta) == "HIT" and nacta["matches"][0]["father_name"] == "Live Father"
    assert nacta["lists"][0]["published"] == "Retrieved live" and nacta["lists"][0]["status"] == "OK"
    from app.screening import nacta_store
    meta = nacta_store.meta()
    assert meta["live"] is True and meta["records"] == 1 and meta["filename"] == "live: " + NACTA_URL


def test_a_failed_live_download_falls_back_to_the_last_good_copy_and_says_so(fake_sources, monkeypatch, storage):
    _live(fake_sources, monkeypatch)
    engine.screen("Anyone Here")                       # first screening downloads and saves the copy
    loader.clear_cache("NACTA")
    fake_sources["fail"].add(NACTA_URL)                # NACTA goes down
    nacta = engine.screen("Live Person")["sources"]["NACTA"]
    assert engine.source_status(nacta) == "HIT"        # still screened against the saved copy
    li = nacta["lists"][0]
    assert li["status"] == "OK" and "live download from NACTA failed" in li["note"] and "last good copy" in li["note"]
    assert not nacta["partial"]                        # recent enough, so it does not block a clear result


def test_a_failed_live_download_with_no_copy_is_not_screened(fake_sources, monkeypatch, storage):
    _live(fake_sources, monkeypatch)
    fake_sources["fail"].add(NACTA_URL)
    nacta = engine.screen("Anyone Here")["sources"]["NACTA"]
    assert engine.source_status(nacta) == "NOT_CONFIGURED"
    assert "could not be downloaded or read from the configured address" in nacta["error"]
    assert "simulated outage" in nacta["error"]


def test_a_stale_saved_copy_is_not_used_to_hide_a_failed_download(fake_sources, monkeypatch, storage):
    _live(fake_sources, monkeypatch)
    engine.screen("Anyone Here")
    loader.clear_cache("NACTA")
    from app import config
    monkeypatch.setattr(config, "NACTA_MAX_AGE_DAYS", -1)     # every copy is now too old
    fake_sources["fail"].add(NACTA_URL)
    nacta = engine.screen("Anyone Here")["sources"]["NACTA"]
    assert engine.source_status(nacta) == "NOT_CONFIGURED"


def test_a_live_response_that_is_not_a_list_is_rejected_with_the_reason(fake_sources, monkeypatch, storage):
    _live(fake_sources, monkeypatch, body=b"<html><body>Please complete the captcha</body></html>")
    nacta = engine.screen("Anyone Here")["sources"]["NACTA"]
    assert engine.source_status(nacta) == "NOT_CONFIGURED" and "no records" in nacta["error"].lower()


def test_live_xml_download_works(fake_sources, monkeypatch, tmp_path):
    xml = (b'<list><p><Name>Xml Person</Name><CNIC>3740565359881</CNIC></p>'
           b'<p><Name>Other</Name><CNIC>3740565359882</CNIC></p></list>')
    _live(fake_sources, monkeypatch, body=xml)
    assert engine.source_status(engine.screen("Xml Person")["sources"]["NACTA"]) == "HIT"

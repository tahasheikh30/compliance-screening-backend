"""Parsers for every record system, run against synthetic feeds in the real formats."""
from app.screening import parsers as p
from tests import fixtures as fx


def test_parse_un_individuals_entities_aliases_dob_nationality():
    recs, published = p.parse_un(fx.UN_XML)
    assert published == "2026-09-30T08:00:00.000Z"
    assert [r.id for r in recs] == ["QDi.001", "QDi.002", "QDe.003"]
    first = recs[0]
    assert first.primary == "MOHAMMAD ALI KHAN" and first.names == ["MOHAMMAD ALI KHAN", "ALI KHAN BHAI"]
    assert first.dob == "1975-03-04" and first.nationality == "Pakistan"
    assert first.remarks == "Test entry & remarks"          # XML entity decoded
    assert recs[1].dob == "1960"                             # YEAR-only birth date
    assert recs[2].type == "Entity"


def test_parse_ofac_handles_quotes_commas_null_marker_and_aliases():
    recs = p.parse_ofac(fx.OFAC_SDN_CSV, fx.OFAC_ALT_CSV, p.OFAC_SDN_LABEL)
    assert [r.id for r in recs] == ["OFAC-1001", "OFAC-1002"]
    a = recs[0]
    assert a.primary == "ZULFIQAR, Hassan Raza"              # comma inside quotes kept
    assert a.names == ["ZULFIQAR, Hassan Raza", "HASSAN ZULFIKAR"]   # "-0-" alias dropped
    assert a.dob == "12 Jan 1982" and a.nationality == "Pakistan" and a.type == "Individual"
    assert recs[1].type == "Entity"                          # "-0-" type defaults to Entity
    assert recs[1].remarks == ""


def test_parse_csv_multiline_and_doubled_quotes():
    rows = p.parse_csv('1,"a ""quoted"" word","line1\nline2"\r\n2,x,y\r\n')
    assert rows[0] == ["1", 'a "quoted" word', "line1\nline2"] and rows[1] == ["2", "x", "y"]


def test_parse_uk_names_primary_alias_and_fields():
    recs, published = p.parse_uk(fx.UK_XML)
    assert published == "2026-09-29" and len(recs) == 1
    r = recs[0]
    assert r.primary == "OSAMA RAHMAN" and "USAMA REHMAN" in r.names
    assert r.programs == "Global Human Rights" and r.dob == "1969-06-06" and r.nationality == "Pakistan"
    assert r.remarks == "Involved in test activity"


def test_find_redbook_editions_from_anchor_and_json_and_dedupe():
    pages = [{"data": fx.FIA_PAGE_HTML, "error": None},
             {"data": '{"pub_name":"Red Book 2025","pub_url":"https:\\/\\/fia.gov.pk\\/f\\/rb25.pdf","x":1,"updated_at":"2025-01-02"}'
                      + fx.FIA_PAGE_HTML, "error": None}]
    eds = p.find_redbook_editions(pages)
    urls = [e["url"] for e in eds]
    assert "https://www.fia.gov.pk/files/redbook-2026.pdf" in urls
    assert "https://www.fia.gov.pk/f/rb25.pdf" in urls          # host normalised, JSON escapes removed
    assert len(urls) == len(set(urls))                           # duplicates dropped
    assert not any("other.pdf" in u for u in urls)               # non Red Book link ignored


def test_find_redbook_editions_reports_why_nothing_was_found():
    out = p.find_redbook_editions([{"data": None, "error": "Timeout: slow"}, {"data": "<x>", "error": None}])
    assert out[0]["url"] == "" and "could not be reached" in out[0]["note"] and "Timeout" in out[0]["note"]
    out = p.find_redbook_editions([{"data": "<x>", "error": None}])
    assert "no Red Book link" in out[0]["note"]


def test_parse_redbook_blocks_aliases_cnic_dob_zone_fir():
    recs = p.parse_redbook(p.pdf_to_text(fx.make_redbook_pdf()), "Red Book 2026")
    assert len(recs) == 2
    a, b = recs
    assert a.names == ["TARIQ MEHMOOD SATTAR", "TARIQ BHATTI"] and a.id == "FIA-RB-35202-1111111-1"
    assert a.dob == "01-02-1980"
    assert "Father/Husband: ABDUL SATTAR" in a.remarks and "CNIC: 35202-1111111-1" in a.remarks
    assert "CIRCLE: LAHORE CIRCLE" in a.remarks and "FIR: 12/2019" in a.remarks
    assert b.names == ["SAJID IQBAL"]                            # "(MWS/T)" marker removed
    assert a.list == "FIA Red Book 2026" and a.type == "Individual"          # no doubled prefix
    assert p.fia_label("Red Book 2026") == "FIA Red Book 2026" and p.fia_label("") == "FIA Red Book"


def test_parse_redbook_without_blocks_is_empty_not_an_error():
    assert p.parse_redbook("nothing relevant in here", "x") == []


def test_pdf_to_text_rejects_non_pdf():
    import pytest
    with pytest.raises(ValueError):
        p.pdf_to_text(b"<html>blocked by firewall</html>")


def test_news_hit_needs_surname_plus_another_part_and_a_keyword():
    reviewed, hits = p.parse_news_hits(fx.NEWS_RSS_HIT, "Bilal Ahmed Qureshi")
    assert reviewed == 3
    assert [h["title"] for h in hits] == ["Bilal Ahmed Qureshi arrested in fraud case"]
    assert hits[0]["keyword"] == "arrested" and hits[0]["source"] == "The News"
    # surname alone is not enough, and a keyword alone is not enough
    assert p.parse_news_hits(fx.NEWS_RSS_CLEAR, "Bilal Ahmed Qureshi")[1] == []


def test_news_single_word_name_needs_the_whole_name():
    xml = fx.NEWS_RSS_HIT.replace("Bilal Ahmed Qureshi arrested", "Qureshi arrested")
    assert len(p.parse_news_hits(xml, "Qureshi")[1]) == 1

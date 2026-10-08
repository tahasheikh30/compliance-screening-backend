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


# ---- NACTA Proscribed Persons (Fourth Schedule) ----------------------------------

import json  # noqa: E402

import pytest  # noqa: E402


def test_normalize_cnic_accepts_real_numbers_and_rejects_junk():
    assert p.normalize_cnic("35202-1234567-1") == "3520212345671"
    assert p.normalize_cnic(" 3520212345671 ") == "3520212345671"
    assert p.normalize_cnic("1111111111166") == ""      # placeholder seen in published data
    assert p.normalize_cnic("0000000000000") == ""
    assert p.normalize_cnic("12345") == "" and p.normalize_cnic("N/A") == "" and p.normalize_cnic(None) == ""
    assert p.normalize_cnic("352021234567") == ""       # 12 digits


def test_nacta_csv_in_the_portal_layout():
    recs, info = p.parse_nacta_persons(fx.NACTA_CSV)
    assert info == {"rows": 4, "skipped": 0, "with_cnic": 3, "has_father": True, "has_province": True}
    a, b, c, d = recs
    assert a.primary == "Muhammad Shakir" and a.father == "Qabil Khan" and a.cnic == "3740565359881"
    assert a.id == "NACTA-1" and a.list == p.NACTA_LABEL and a.source_key == "nacta" and a.type == "Individual"
    assert "District: HANGU" in a.remarks and "Province: PUNJAB" in a.remarks
    assert b.names == ["Aamir Bilal", "Babu Jhangvee"]          # "alias" split into separate names
    assert c.father == "" and "Father" not in c.remarks          # "nill" is not a father's name
    assert d.cnic == "" and "CNIC: 1111111111166" in d.remarks   # shown, but never used for matching
    # the province is a field of its own, written one standard way ("KP" is Khyber Pakhtunkhwa)
    assert [x.province for x in recs] == ["Punjab", "Punjab", "Balochistan", "Khyber Pakhtunkhwa"]


def test_nacta_csv_in_the_other_column_order_and_with_other_delimiters():
    other = "ID,Name,Father Name,CNIC,Province,District\n7,Zain Haider,Ahmad Nawaz,3810481580749,Punjab,BHAKKAR\n"
    for text in (other, other.replace(",", ";"), other.replace(",", "\t")):
        recs, _ = p.parse_nacta_persons(text)
        assert recs[0].primary == "Zain Haider" and recs[0].cnic == "3810481580749" and recs[0].id == "NACTA-7"


def test_nacta_csv_with_a_title_row_above_the_headers_and_a_bom():
    text = "\ufeffProscribed Persons List\nTotal Record : 2\nID,Name,Father Name,CNIC\n1,A B,C D,3810481580749\n"
    recs, info = p.parse_nacta_persons(text)
    assert [r.primary for r in recs] == ["A B"] and info["rows"] == 1


def test_nacta_json_shapes():
    rows = [{"Name": "A One", "Father Name": "F", "CNIC": "3810481580749", "District": "X", "Province": "Y"}]
    assert p.parse_nacta_persons(json.dumps(rows))[0][0].cnic == "3810481580749"
    assert p.parse_nacta_persons(json.dumps({"total": 1, "data": rows}))[0][0].primary == "A One"
    snake = [{"full_name": "B Two", "fatherName": "G", "cnic_no": "3810481580749"}]
    r = p.parse_nacta_persons(json.dumps(snake))[0][0]
    assert r.primary == "B Two" and r.father == "G" and r.cnic == "3810481580749"


def test_nacta_rows_without_a_name_are_skipped_and_counted():
    recs, info = p.parse_nacta_persons("Name,CNIC\nAli Raza,3810481580749\n,3810481580750\n  ,3810481580751\n")
    assert len(recs) == 1 and info["skipped"] == 2


@pytest.mark.parametrize("text,fragment", [
    ("", "empty"),
    ("   \n  ", "empty"),
    ("just words\nno headers here\n", "Name column"),
    ("{broken json", "JSON"),
    ('{"data": 5}', "no list of records"),
    ("[]", "no records"),
])
def test_nacta_unreadable_files_raise_a_clear_message(text, fragment):
    with pytest.raises(ValueError) as e:
        p.parse_nacta_persons(text)
    assert fragment.lower() in str(e.value).lower()


def test_fia_red_book_records_carry_cnic_and_father_for_matching():
    recs = p.parse_redbook(p.pdf_to_text(fx.make_redbook_pdf()), "Red Book 2026")
    assert recs[0].cnic == "3520211111111" and recs[0].father == "ABDUL SATTAR"
    assert recs[1].cnic == "4210122222223" and recs[1].father == "MUHAMMAD IQBAL"


def test_nacta_xml_export():
    xml = ('<?xml version="1.0"?><ProscribedPersons><Total>2</Total>'
           '<Person><ID>1</ID><Name>Zain Haider</Name><FatherName>Ahmad Nawaz</FatherName><CNIC>3810481580749</CNIC>'
           '<Province>Punjab</Province><District>BHAKKAR</District></Person>'
           '<Person><ID>2</ID><Name>Aamir Bilal alias Babu Jhangvee</Name><FatherName>nill</FatherName>'
           '<CNIC>3640177467701</CNIC></Person></ProscribedPersons>')
    recs, info = p.parse_nacta_persons(xml)
    assert info["rows"] == 2 and recs[0].primary == "Zain Haider" and recs[0].cnic == "3810481580749"
    assert recs[1].names == ["Aamir Bilal", "Babu Jhangvee"] and recs[1].father == ""


def test_nacta_xml_with_attributes_and_wrapper_elements():
    xml = '<root><data><items><row name="A B" cnic="3810481580749"/><row name="C D" cnic="3810481580750"/></items></data></root>'
    assert [r.primary for r in p.parse_nacta_persons(xml)[0]] == ["A B", "C D"]


@pytest.mark.parametrize("xml,fragment", [
    ('<!DOCTYPE x [<!ENTITY a "b">]><x><y/></x>', "entity"),
    ("<broken><x>", "could not be parsed"),
    ("<root/>", "no records"),
])
def test_nacta_bad_xml_is_refused_with_a_message(xml, fragment):
    with pytest.raises(ValueError) as e:
        p.parse_nacta_persons(xml)
    assert fragment in str(e.value).lower()


def test_provinces_are_written_one_standard_way():
    n = p.normalize_province
    assert n("PUNJAB") == n("punjab") == n(" Punjab ") == "Punjab"
    assert n("KPK") == n("K.P.K.") == n("NWFP") == n("Khyber-Pakhtunkhwa") == n("KP") == "Khyber Pakhtunkhwa"
    assert n("FATA") == "Khyber Pakhtunkhwa"                  # merged into it in 2018
    assert n("Baluchistan") == n("BALOCHISTAN") == "Balochistan"
    assert n("Islamabad") == n("ICT") == "Islamabad Capital Territory"
    assert n("Gilgit Baltistan") == n("GB") == n("Northern Areas") == "Gilgit-Baltistan"
    assert n("AJK") == n("Azad Kashmir") == n("Azad Jammu & Kashmir") == "Azad Jammu and Kashmir"
    assert n("") == n(None) == n("nill") == n("N/A") == ""      # missing, not a province
    assert n("some other place") == "Some Other Place"        # an unknown spelling is kept, not guessed at


def test_nacta_file_without_a_province_column_says_so():
    _, info = p.parse_nacta_persons("ID,Name,Father Name,CNIC\n1,A One,F One,3810481580749\n")
    assert info["has_province"] is False

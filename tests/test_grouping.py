"""Grouping of PEP matches: one person listed by several sources is shown once, namesakes stay apart."""
from app.screening.grouping import group_pep_matches, same_person, source_label


def pep(rid, primary, matched=None, aliases=(), dob="", province="Punjab", cnic="", score=88.0, remarks="Source: uploaded list"):
    return {"source": "pep", "id": rid, "primary_name": primary, "matched_name": matched or primary, "aliases": list(aliases),
            "dob": dob, "province": province, "cnic": cnic, "score": score, "remarks": remarks, "list": "PEP"}


def azma_records():
    return [
        pep("PEP-UP-39", "Azma Zahid Bokhari"),
        pep("PEP-WD-Q47863721", "Asma Zahid Bokhari", matched="Azma Zahid Bokhari", dob="1976-08-18",
            aliases=["Azma Zahid Bokhari", "Azma Zahid Bukhari", "Uzma Zahid Bukhari"], remarks="Source: Wikidata Q47863721"),
        pep("PEP-ASM-punjab16-AzmaZahidBukhari", "Azma Zahid Bukhari",
            remarks="Source: en.wikipedia.org: List of members of the 16th Provincial Assembly of the Punjab"),
    ]


def test_the_three_records_of_one_person_become_one_group():
    groups = group_pep_matches(azma_records())
    assert len(groups) == 1
    g = groups[0]
    assert len(g["members"]) == 3
    assert g["lead"]["id"] == "PEP-WD-Q47863721"          # the record that has a date of birth leads
    assert len(g["listed_in"]) == 3 and "uploaded list" in g["listed_in"]
    assert g["basis"].startswith("name and province")


def test_conflicting_birth_years_keep_namesakes_apart():
    a = pep("A", "Azma Zahid Bokhari", dob="1976-08-18")
    b = pep("B", "Azma Zahid Bokhari", dob="1990-01-01")
    assert not same_person(a, b)
    assert len(group_pep_matches([a, b])) == 2


def test_different_provinces_or_different_cnics_keep_people_apart():
    assert not same_person(pep("A", "Ali Raza", province="Punjab"), pep("B", "Ali Raza", province="Sindh"))
    assert not same_person(pep("A", "Ali Raza", cnic="1111111111111"), pep("B", "Ali Raza", cnic="2222222222222"))


def test_a_name_alone_is_not_enough():
    # no province, birth year or CNIC on both sides: nothing but the name agrees
    assert not same_person(pep("A", "Ali Raza", province=""), pep("B", "Ali Raza", province=""))


def test_a_longer_name_is_not_pulled_in_by_the_subset_rule():
    assert not same_person(pep("A", "Azma Bokhari"), pep("B", "Azma Zahid Bokhari"))


def test_initials_do_not_group():
    assert not same_person(pep("A", "A. Bokhari"), pep("B", "Azma Bokhari"))


def test_a_chain_never_joins_two_conflicting_records():
    a = pep("A", "Azma Zahid Bokhari", dob="1976-01-01", score=90)
    b = pep("B", "Azma Zahid Bokhari", score=89)                     # no date of birth: fits both
    c = pep("C", "Azma Zahid Bokhari", dob="1980-01-01", score=88)   # conflicts with a
    groups = group_pep_matches([a, b, c])
    assert sorted(len(g["members"]) for g in groups) == [1, 2]
    assert not any({x["id"] for x in g["members"]} >= {"A", "C"} for g in groups)


def test_same_cnic_groups_even_when_names_differ_in_spelling():
    a = pep("A", "Muhammad Nawaz Sharif", cnic="1234512345671", province="")
    b = pep("B", "Mian Muhammad Nawaz Sharif", cnic="1234512345671", province="")
    assert same_person(a, b)


def test_sanctions_matches_are_ignored_and_input_is_unchanged():
    ofac = {"source": "ofac", "id": "X", "primary_name": "Azma Zahid Bokhari", "score": 99.0}
    records = azma_records()
    before = [dict(r) for r in records]
    assert len(group_pep_matches([ofac, *records])) == 1
    assert records == before


def test_source_label_comes_from_the_remarks():
    assert source_label(pep("A", "X", remarks="Member; Source: Wikidata Q1")) == "Wikidata Q1"
    assert source_label({"remarks": "", "list": "PEP list"}) == "PEP list"

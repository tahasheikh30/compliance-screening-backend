"""Recall and safety of the name matching: spelling variants, non-Latin input, and the candidate index."""
import random

import pytest

from app.screening import engine, names, parsers
from app.screening.names import NameScorer, RecordIndex, has_unsupported_script, tokens
from tests.conftest import API_HEADERS


# ---- spelling variants (these all scored below the 0.88 token cutoff before) ---------------------------

@pytest.mark.parametrize("a,b", [
    ("Mohammed Ali Khan", "Muhammad Ali Khan"),      # 66.7 before
    ("Mohd Ali Khan", "Muhammad Ali Khan"),
    ("Md Ali Khan", "Muhammad Ali Khan"),
    ("Muhamad Ali Khan", "Mohammad Ali Khan"),
    ("Hossain Ahmed", "Hussain Ahmad"),
    ("Syed Raza", "Sayyid Raza"),
    ("Chowdhury Nawaz", "Chaudhry Nawaz"),
    ("Abdulrahman Saeed", "Abdul Rahman Said"),      # fused and split spellings of one name
    ("Abdurrahman Khan", "Abd al-Rahman Khan"),
])
def test_known_spelling_variants_are_the_same_name(a, b):
    assert NameScorer(a).score(b) == 100.0


def test_abdullah_is_never_cut_into_pieces():
    assert tokens("Abdullah") == ("ABDULLAH",)
    assert tokens("Abdalla") == ("ABDULLAH",)
    assert tokens("Abdulaziz") == ("ABDUL", "AZIZ")


def test_variants_do_not_make_different_names_match():
    assert NameScorer("Muhammad Ali Khan").score("Ahmad Raza Khan") < 85
    assert NameScorer("Hassan Ali").score("Hussain Ali") < 85       # HASSAN and HUSSAIN are different people


def test_variant_folding_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(names, "NAME_VARIANTS", False)
    assert tokens("Mohammed Ali") == ("MOHAMMED", "ALI")


def test_letters_that_do_not_decompose_are_folded_not_dropped():
    assert tokens("Łukasz Ørsted") == tokens("Lukasz Orsted") == ("LUKASZ", "ORSTED")
    assert tokens("Đorđe") == ("DORDE",)


# ---- a name that cannot be compared must never look clear ---------------------------------------------------

@pytest.mark.parametrize("name", ["محمد علی خان", "Мохаммед Али", "李小龙", "Muhammad علی"])
def test_non_latin_names_are_detected(name):
    assert has_unsupported_script(name)
    with pytest.raises(engine.UnscreenableName):
        engine.check_screenable(name)


@pytest.mark.parametrize("name", ["José Núñez", "Łukasz", "O'Brien-Smith", "Muhammad Ali Khan"])
def test_latin_names_with_accents_are_fine(name):
    assert not has_unsupported_script(name)
    engine.check_screenable(name)


def test_api_refuses_an_urdu_name_instead_of_returning_clear(client):
    r = client.post("/api/screen", json={"full_name": "محمد علی خان"}, headers=API_HEADERS)
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "VALIDATION_ERROR" and "Latin letters" in body["error"]["message"]
    assert "محمد" not in r.text                       # the submitted name is not echoed back
    assert client.get("/api/applicants", headers=API_HEADERS).json() == []     # and nothing was stored


def test_api_refuses_a_name_of_punctuation_only(client):
    assert client.post("/api/screen", json={"full_name": "-- .. --"}, headers=API_HEADERS).status_code == 422


def test_hidden_control_characters_are_removed_from_a_name(client, fake_sources):
    r = client.post("/api/screen", json={"full_name": "Ali\u200b Raza\u202e Khan"}, headers=API_HEADERS)
    assert r.status_code == 200 and r.json()["full_name"] == "Ali Raza Khan"


# ---- the index must give exactly the brute force answer --------------------------------------------------

def _random_records(rnd, n):
    syll = ["BA", "KA", "MA", "SA", "RA", "ZA", "NA", "LA", "TA", "HA", "MOH", "ALI", "KHAN", "SHAH", "AB", "UL"]
    recs = []
    for i in range(n):
        names_ = [" ".join("".join(rnd.choices(syll, k=rnd.randint(1, 3))) for _ in range(rnd.randint(1, 4)))
                  for _ in range(rnd.randint(1, 3))]
        r = parsers.Record("L", str(i), names_[0], "", "", "", "", "", "", names_, "UNSC")
        if rnd.random() < 0.05:
            r.cnic = f"42201{rnd.randint(1000000, 9999999)}1"
        recs.append(r)
    return parsers.prepare(recs)


@pytest.mark.parametrize("seed", range(6))
def test_indexed_matching_equals_brute_force(seed):
    rnd = random.Random(seed)
    recs = _random_records(rnd, 1500)
    idx = RecordIndex(recs)
    cnics = [r.cnic for r in recs if r.cnic]
    for _ in range(25):
        q = " ".join("".join(rnd.choices(["BA", "KA", "MA", "SA", "RA", "ZA", "MOH", "ALI", "KHAN", "SHAH"],
                                         k=rnd.randint(1, 3))) for _ in range(rnd.randint(1, 3)))
        cnic = rnd.choice(cnics) if cnics and rnd.random() < 0.3 else ""
        for thr in (50.0, 70.0, 85.0, 95.0):
            brute = engine.match_records(NameScorer(q), recs, thr, "", cnic)
            fast = engine.match_records(NameScorer(q), recs, thr, "", cnic, index=idx)
            assert fast == brute, (q, thr)


def test_index_is_ignored_if_it_does_not_belong_to_the_records():
    recs = _random_records(random.Random(1), 50)
    stale = RecordIndex(recs[:10])                   # built for a different list: must not be trusted
    sc = NameScorer("Moh Ali Khan")
    assert engine.match_records(sc, recs, 50.0, "", index=stale) == engine.match_records(sc, recs, 50.0, "")


def test_index_finds_the_variant_spelling_end_to_end():
    recs = parsers.prepare([parsers.Record("UN", "X1", "MUHAMMAD ALI KHAN", "", "", "", "", "", "",
                                           ["MUHAMMAD ALI KHAN"], "UNSC")])
    top, total = engine.match_records(NameScorer("Mohammed Ali Khan"), recs, 85.0, "", index=RecordIndex(recs))
    assert total == 1 and top[0]["score"] == 100.0


@pytest.mark.parametrize("query", ["ali ali khan", "khan", "moh moh moh", "ba ka ma sa ra za", "ali khan khan shah"])
def test_pruning_is_sound_for_repeated_tokens_and_unusual_query_shapes(query):
    rnd = random.Random(99)
    recs = _random_records(rnd, 2500)
    # records that repeat a token inside one name are the case a naive "count distinct tokens" bound gets wrong
    for r, extra in zip(recs[:40], ["ALI ALI KHAN", "KHAN KHAN", "MOH MOH MOH", "ALI KHAN KHAN SHAH"] * 10):
        r.names.append(extra)
    parsers.prepare(recs)
    idx = RecordIndex(recs)
    for thr in (50.0, 60.0, 75.0, 84.9, 85.0, 90.0, 99.9, 100.0):
        brute = engine.match_records(NameScorer(query), recs, thr, "")
        assert engine.match_records(NameScorer(query), recs, thr, "", index=idx) == brute, (query, thr)


def test_pruning_keeps_a_match_exactly_at_the_threshold():
    """Scores are rounded half up to one decimal; a record scoring exactly the threshold must not be pruned."""
    recs = parsers.prepare([
        parsers.Record("L", "1", "ALI KHAN BHATTI", "", "", "", "", "", "", ["ALI KHAN BHATTI"], "UNSC"),
        parsers.Record("L", "2", "ALI KHAN", "", "", "", "", "", "", ["ALI KHAN"], "UNSC"),
    ])
    idx = RecordIndex(recs)
    sc = NameScorer("Ali Khan")
    for r in recs:
        exact = sc.score_tokens(r.toks[0])
        brute = engine.match_records(NameScorer("Ali Khan"), recs, exact, "")
        assert engine.match_records(NameScorer("Ali Khan"), recs, exact, "", index=idx) == brute
        assert any(m["id"] == r.id for m in brute[0])


def test_the_index_narrows_a_common_name_search_a_lot():
    rnd = random.Random(5)
    recs = _random_records(rnd, 5000)
    idx = RecordIndex(recs)
    sc = NameScorer("Moh Ali Khan")
    wide = idx.candidates(sc.q)                       # anything sharing any similar token
    tight = idx.candidates(sc.q, threshold=85.0)      # only records that could actually reach 85
    assert set(tight) <= set(wide) and len(tight) < len(wide) / 3

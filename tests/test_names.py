"""Name normalisation, scoring and the Jaro-Winkler implementation."""
import random

import pytest

from app.screening import engine
from app.screening.names import NameScorer, entity_tokens, jw, jw_exact, norm, tokens


def test_norm_strips_accents_punctuation_and_case():
    assert norm("  José  O'Brien-Smith ") == "JOSE OBRIEN SMITH"
    assert norm(None) == ""


def test_honorifics_and_particles_removed_but_common_surnames_kept():
    # parts are also sound-folded (double letters and oo/ee/ai spellings collapse), the same way on both sides
    assert tokens("Dr. Haji Muhammad Ali Sheikh") == ("MUHAMAD", "ALI", "SHEIKH")
    assert tokens("Abdul Rahman bin Al Sayed") == ("ABDUL", "RAHMAN", "SED")
    # a name made only of honorifics is kept rather than emptied
    assert tokens("Dr") == ("DR",)


@pytest.mark.parametrize("cand,low,high", [
    ("MUHAMMAD ALI KHAN", 100, 100),
    ("Khan, Muhammad Ali", 100, 100),          # order does not matter
    ("Dr. Muhammad Ali Khan", 100, 100),       # titles ignored
    ("Mohammad Ali Khan", 100, 100),           # transliteration variants of one name are folded together
    ("Ahmed Raza", 0, 0),                      # unrelated
])
def test_scores(cand, low, high):
    s = NameScorer("Muhammad Ali Khan").score(cand)
    assert low <= s <= high


def test_extra_unmatched_token_lowers_the_score_symmetrically():
    a = NameScorer("Muhammad Ali Khan").score("Muhammad Ali Khan Bhatti")
    b = NameScorer("Muhammad Ali Khan Bhatti").score("Muhammad Ali Khan")
    assert a == b and 80 < a < 90


def test_initial_letter_prefilter_returns_zero():
    assert NameScorer("Zulfiqar").score("Ahmed Bilal") == 0.0


def test_score_has_one_decimal_and_rounds_half_up():
    assert NameScorer("Ali Khan").score("Ali Khan Bhatti") == round(NameScorer("Ali Khan").score("Ali Khan Bhatti"), 1)


@pytest.mark.parametrize("value,expected", [
    (None, 85), ("", 85), ("abc", 85), (49, 85), (101, 85), (50, 50), (100, 100), ("90", 90), ("92.5%", 92.5),
])
def test_threshold_rule_matches_the_workflow(value, expected):
    assert engine.resolve_threshold(value) == expected


def test_jaro_winkler_decisions_match_the_workflow_algorithm():
    """
    jw() uses rapidfuzz as a pre-filter in front of the workflow's own algorithm.
    Every decision at the cutoffs the code uses (0.88 for tokens, 0.92 for news)
    must be identical, and values at or above 0.88 must be identical too.
    """
    rnd = random.Random(11)
    alpha = "ABCDEMNRSTUKHZ"
    words = ["MUHAMMAD", "MOHAMMAD", "MOHAMMED", "ALI", "KHAN", "AHMED", "AHMAD", "HUSSAIN", "HUSAIN", "ZULFIQAR",
             "ZULFIKAR", "SHEIKH", "SHAIKH", "BILAL", "USMAN", "OSAMA", "USAMA", "RAHMAN", "REHMAN", "SIDDIQUI"]
    pairs = [(a, b) for a in words for b in words]
    for _ in range(40000):
        a = "".join(rnd.choice(alpha) for _ in range(rnd.randint(1, 10)))
        b = list(a)
        for _ in range(rnd.randint(0, 2)):
            i = rnd.randrange(len(b))
            op = rnd.random()
            if op < 0.4:
                b[i] = rnd.choice(alpha)
            elif op < 0.7 and len(b) > 1:
                del b[i]
            else:
                b.insert(i, rnd.choice(alpha))
        pairs.append((a, "".join(b) or "A"))
    for a, b in pairs:
        exact, fast = jw_exact(a, b), jw(a, b)
        for cut in (0.88, 0.92):
            assert (exact >= cut) == (fast >= cut), (a, b, exact, fast)
        if exact >= 0.88:
            assert exact == fast, (a, b)


def test_jw_exact_known_values():
    assert jw_exact("MARTHA", "MARHTA") == pytest.approx(0.9611, abs=1e-3)
    assert jw_exact("", "A") == 0.0 and jw_exact("A", "A") == 1.0


# --- initials and company suffixes must not make a person match a company -----------------------------------

def test_company_suffix_initials_do_not_match_a_person():
    # "S.A." / "B." used to count as initials of AZMA / BOKHARI and the subset rule lifted the score to 88
    for company in ("FELIX B. MADURO S.A.", "B-WORK S.A.S."):
        sc = NameScorer("Azma Bokhari")
        assert sc.score(company) == 0.0
        assert sc.score_tokens(entity_tokens(company)) == 0.0


def test_close_spellings_of_a_longer_name_still_match():
    sc = NameScorer("Azma Bokhari")
    assert sc.score("Azma Zahid Bokhari") >= 85
    assert sc.score("Azma Zahid Bukhari") >= 85


def test_initials_next_to_a_real_part_still_match():
    assert NameScorer("M. Imran Khan").score("Muhammad Imran Khan") >= 85
    assert NameScorer("Muhammad Imran Khan").score("M Imran Khan") >= 85
    assert NameScorer("Azma Bokhari").score("A. Bokhari") >= 85


def test_entity_tokens_strip_legal_forms_only_when_something_remains():
    assert entity_tokens("B-WORK S.A.S.") == ("B", "WORK")
    assert entity_tokens("ACME TRADING LLC") == ("ACME", "TRADING")
    assert entity_tokens("INVERSIONES X S.A. DE C.V.") == ("INVERSIONES", "X")
    assert entity_tokens("S.A.") == ("S", "A")          # nothing else left: unchanged


# --- the reviewer is told why a name matched ------------------------------------------------------------------

def test_match_reason_tells_a_subset_lift_from_a_close_spelling():
    sc = NameScorer("Azma Bokhari")
    assert "fixed 88" in sc.reason(tokens("Azma Zahid Bukhari"))
    assert sc.reason(tokens("Azma Bokhari")).startswith("Same name parts")
    assert sc.reason(tokens("Azma Bukhari")).startswith("Close spelling")
    assert "initial" in sc.reason(tokens("A. Bokhari"))
    assert sc.reason(entity_tokens("B-WORK S.A.S.")) == ""      # no match, no reason

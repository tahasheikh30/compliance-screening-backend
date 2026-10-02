"""Name normalisation, scoring and the Jaro-Winkler implementation."""
import random

import pytest

from app.screening import engine
from app.screening.names import NameScorer, jw, jw_exact, norm, tokens


def test_norm_strips_accents_punctuation_and_case():
    assert norm("  José  O'Brien-Smith ") == "JOSE OBRIEN SMITH"
    assert norm(None) == ""


def test_honorifics_and_particles_removed_but_common_surnames_kept():
    assert tokens("Dr. Haji Muhammad Ali Sheikh") == ("MUHAMMAD", "ALI", "SHEIKH")
    assert tokens("Abdul Rahman bin Al Sayed") == ("ABDUL", "RAHMAN", "SAYED")
    # a name made only of honorifics is kept rather than emptied
    assert tokens("Dr") == ("DR",)


@pytest.mark.parametrize("cand,low,high", [
    ("MUHAMMAD ALI KHAN", 100, 100),
    ("Khan, Muhammad Ali", 100, 100),          # order does not matter
    ("Dr. Muhammad Ali Khan", 100, 100),       # titles ignored
    ("Mohammad Ali Khan", 95, 99.9),           # transliteration variant
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

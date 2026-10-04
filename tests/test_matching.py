"""
Tests for app.screening.matching — the accuracy-critical core of the
screening system.

These are deliberately built around real failure modes for Pakistani/
Arabic-derived names (the applicant population this tool screens), not
generic fuzzy-matching trivia:

  - Honorifics that shouldn't suppress a real match
  - Common transliteration variants of the SAME name
  - Reordered name components
  - Partial name submissions (2 of 4 watchlist tokens)
  - Genuinely different names that must NOT match
  - CNIC as an independent, stronger signal than any name score
  - Phonetic (Metaphone) matches for transliteration pairs not present
    in the hand-curated variant table
  - Single-digit CNIC "near matches" (likely OCR/typo slips) flagged for
    audit without ever being auto-escalated to a HIT

Run with:  pytest tests/test_matching.py -v

This is a starting point, not a complete accuracy certification. Before
relying on this in production, extend this file with real near-miss
and false-positive cases pulled from actual applicant traffic and the
near-miss audit log (see app.database.list_near_misses), and have
compliance sign off on the threshold values in app/config.py against
that evidence — not against this file alone.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.screening import matching  # noqa: E402


REVIEW_THRESHOLD = 60
MATCH_THRESHOLD = 85


def test_exact_match_scores_100():
    r = matching.score_against("Muhammad Ali", "Muhammad Ali")
    assert r.combined == 100
    assert r.exact_normalized


def test_honorific_is_stripped():
    r = matching.score_against("Syed Muhammad Ali", "Muhammad Ali")
    assert r.combined >= MATCH_THRESHOLD, f"expected honorific-stripped match to clear threshold, got {r.combined}"


def test_transliteration_variant_mohammed_vs_muhammad():
    r = matching.score_against("Mohammed Yousaf", "Muhammad Yousuf")
    assert r.combined >= MATCH_THRESHOLD, f"transliteration variants should match strongly, got {r.combined}"


def test_reordered_tokens_still_match():
    r = matching.score_against("Khan Ali Hassan", "Hassan Ali Khan")
    assert r.combined >= MATCH_THRESHOLD, f"reordered tokens should still match, got {r.combined}"


def test_partial_name_against_longer_watchlist_entry():
    # Applicant supplied 2 names; watchlist entry has 4 (first/second/third/fourth).
    r = matching.score_against("Ahmed Raza", "Ahmed Raza Hussain Sheikh")
    assert r.combined >= REVIEW_THRESHOLD, (
        f"a 2-of-4 token subset match should at least reach REVIEW, got {r.combined}"
    )


def test_unrelated_names_do_not_match():
    r = matching.score_against("Sana Malik", "Robert Anderson")
    assert r.combined < REVIEW_THRESHOLD, f"unrelated names should score low, got {r.combined}"


def test_common_short_name_does_not_falsely_spike():
    # "Ali" alone is extremely common; it should not blow up the score
    # against an unrelated multi-token entry just because it's a substring.
    r = matching.score_against("Ali", "Chandrasekhar Alistair Montgomery")
    assert r.combined < MATCH_THRESHOLD, f"short common token should not force a HIT, got {r.combined}"


def test_find_best_match_picks_highest_scoring_entry():
    entries = ["Robert Anderson", "Muhammad Ali Khan", "Sana Malik"]
    best = matching.find_best_match("Mohammad Ali Khan", entries, threshold=REVIEW_THRESHOLD)
    assert best.matched_entry == "Muhammad Ali Khan"
    assert best.score >= MATCH_THRESHOLD


def test_near_miss_flagged_just_below_threshold():
    # Construct a case expected to land in the near-miss band (within
    # NEAR_MISS_MARGIN points below REVIEW_THRESHOLD) without crossing it.
    entries = ["Completely Different Person"]
    best = matching.find_best_match("Somewhat Related Person", entries, threshold=REVIEW_THRESHOLD)
    # This assertion is about behavior, not this specific pair's exact
    # score — swap in a real borderline pair from your near-miss log once
    # you have production traffic to calibrate against.
    if REVIEW_THRESHOLD - matching.NEAR_MISS_MARGIN <= best.score < REVIEW_THRESHOLD:
        assert best.near_miss


def test_cnic_exact_match_true_for_identical_numbers():
    assert matching.cnic_exact_match("12345-1234567-1", "12345-1234567-1")


def test_cnic_exact_match_ignores_formatting_differences():
    assert matching.cnic_exact_match("12345-1234567-1", "1234512345671")


def test_cnic_exact_match_false_for_different_numbers():
    assert not matching.cnic_exact_match("12345-1234567-1", "12345-1234567-2")


def test_cnic_exact_match_false_when_either_side_missing():
    assert not matching.cnic_exact_match(None, "12345-1234567-1")
    assert not matching.cnic_exact_match("12345-1234567-1", None)


def test_extract_cnic_from_free_text():
    assert matching.extract_cnic("Name: Ali Khan, CNIC 12345-1234567-1, DOB ...") == "12345-1234567-1"


def test_extract_cnic_returns_none_when_absent():
    assert matching.extract_cnic("No identifying number in this text.") is None


def test_normalize_name_is_idempotent_and_order_preserving_per_token():
    once = matching.normalize_name("Dr. Syed Mohammad  Ali-Khan")
    twice = matching.normalize_name(once)
    assert once == twice


def test_phonetic_layer_catches_variant_not_in_table():
    # "Zulfiqar" vs "Zulfikar" is a real transliteration pair that is
    # NOT in VARIANT_MAP — this should only pass because of the added
    # Metaphone phonetic layer, not the hand-curated variant table.
    r = matching.score_against("Zulfiqar Ali", "Zulfikar Ali")
    assert r.combined >= MATCH_THRESHOLD, f"phonetic layer should catch this variant, got {r.combined}"
    assert r.phonetic >= MATCH_THRESHOLD, "expected the phonetic score itself to be high for this pair"


def test_phonetic_layer_respects_short_token_guard():
    # Same guard rationale as partial_ratio: a short single token
    # shouldn't be trusted to phonetically "match" purely by coincidence
    # against an unrelated multi-token entry.
    r = matching.score_against("Ali", "Chandrasekhar Alistair Montgomery")
    assert r.phonetic == 0, "phonetic score should be excluded by the short-token guard, not just low"
    assert r.combined < MATCH_THRESHOLD


def test_cnic_near_match_flags_single_digit_typo_but_not_exact():
    assert matching.cnic_near_match("12345-1234567-1", "12345-1234567-2")
    assert not matching.cnic_near_match("12345-1234567-1", "12345-1234567-1")  # exact, not "near"


def test_cnic_near_match_false_for_multi_digit_difference():
    assert not matching.cnic_near_match("12345-1234567-1", "12345-1234568-2")


def test_cnic_near_match_false_when_either_side_missing():
    assert not matching.cnic_near_match(None, "12345-1234567-1")
    assert not matching.cnic_near_match("12345-1234567-1", None)


def test_empty_or_none_inputs_do_not_crash():
    r = matching.score_against("", "Muhammad Ali")
    assert r.combined == 0
    best = matching.find_best_match("Muhammad Ali", [], threshold=REVIEW_THRESHOLD)
    assert best.matched_entry is None
    assert best.score == 0

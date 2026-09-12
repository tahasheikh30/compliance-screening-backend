"""
Core name-matching engine shared by UNSC, FIA Red Book, and any future
watchlist source.

Why this is its own module: matching accuracy directly determines whether
a sanctioned/wanted individual is correctly flagged (miss = regulatory and
reputational risk) or an innocent applicant is wrongly escalated (false
positive = customer harm, wasted analyst time). A single fuzzy-ratio call
is not enough for that job on its own, so this module:

  1. Normalizes names before comparison (case, punctuation, diacritics,
     honorifics, and a conservative list of common transliteration
     variants relevant to Pakistani/Arabic/Urdu names) so that formatting
     noise and known spelling variants don't masquerade as low similarity.
  2. Scores using several complementary RapidFuzz algorithms and combines
     them, because no single ratio handles every name shape well (see
     `score_against` docstring for why each one is included).
  3. Treats an exact CNIC match as a separate, stronger signal from name
     similarity — a shared national ID number is direct identity evidence,
     not a fuzzy inference — and exposes it distinctly so callers can
     escalate on it regardless of the name score.
  4. Returns enough detail (per-algorithm scores, normalized forms, a
     near-miss flag) for both the evidence PDF and an audit log, so a
     compliance analyst can see *why* something scored the way it did,
     not just a bare number.

This module intentionally does NOT decide HIT/REVIEW/CLEAR — thresholds
and routing stay in app/main.py, so there is exactly one place that owns
screening policy and one place (here) that owns the measurement it acts on.

IMPORTANT — read this before trusting the output:
Fuzzy name matching is inherently probabilistic. This module measurably
improves on a single-ratio approach (see tests/test_matching.py for the
concrete cases it fixes), but it cannot and does not guarantee zero false
negatives or false positives. Every HIT/REVIEW result is designed to route
to a human compliance analyst, not to auto-reject or auto-clear an
applicant — that human step is not a formality, it's load-bearing. Treat
the transliteration variant list as a living document: extend it whenever
a real mismatch is found in production, and have a compliance analyst
periodically review both the near-miss log and a sample of CLEAR results.
"""

import re
import unicodedata
from dataclasses import dataclass, field

from rapidfuzz import fuzz

# Honorifics / titles that precede a name and add no identifying signal.
# Stripping them prevents e.g. "Haji Muhammad Ali" from scoring lower
# against a watchlist entry of plain "Muhammad Ali" than it should.
HONORIFICS = {
    "mr", "mrs", "ms", "miss", "mst", "dr", "syed", "syeda", "haji", "hajra",
    "hajjah", "sheikh", "shaikh", "shaykh", "chaudhry", "chaudhary", "ch",
    "malik", "mian", "raja", "sardar", "sahibzada", "pir", "hafiz", "qari",
    "maulana", "mufti", "alhaj", "al", "haj",
}

# Conservative single-token spelling/transliteration equivalences seen
# across Pakistani/Arabic-derived names in official documents, watchlist
# entries, and news transliteration. Each key maps to one canonical form
# so variant spellings of the SAME underlying name compare as equal
# instead of as a fuzzy near-miss. This is NOT a general transliteration
# engine — only near-universally-agreed equivalences belong here. Extend
# it as real mismatches are found; do not add speculative entries, since
# an over-eager mapping can cause unrelated names to collapse together.
VARIANT_MAP = {
    "mohammad": "muhammad", "mohammed": "muhammad", "muhammed": "muhammad",
    "mohamed": "muhammad", "mohd": "muhammad", "mhd": "muhammad",
    "ahmad": "ahmed", "ahamed": "ahmed", "ahmath": "ahmed",
    "farooq": "farooque", "farooqi": "farooque", "farrukh": "farrukh",
    "abdullah": "abdulla", "abdullahi": "abdulla",
    "yousaf": "yousuf", "yusuf": "yousuf", "yousif": "yousuf", "yusif": "yousuf",
    "hussain": "hussein", "husain": "hussein", "hossain": "hussein", "hussein": "hussein",
    "usman": "osman", "othman": "osman",
    "ibrahim": "ibraheem", "ebrahim": "ibraheem",
    "zafar": "zaffar",
    "qadeer": "qadir", "qadeer ": "qadir",
    "rehman": "rahman", "rahmaan": "rahman", "rahmaan ": "rahman",
    "fatima": "fatimah", "fatema": "fatimah", "fathima": "fatimah",
    "aisha": "ayesha", "ayeshah": "ayesha", "aysha": "ayesha",
    "zainab": "zaynab", "zaynub": "zaynab",
    "bakhsh": "baksh", "bux": "baksh",
    "sadiq": "sadiq", "siddique": "siddiqui", "siddiqi": "siddiqui",
    "gilani": "gillani",
    "chisti": "chishti", "chishty": "chishti",
    "nabi": "nabi", "nabhi": "nabi",
}

_WORD_RE = re.compile(r"[a-z]+")
_CNIC_RE = re.compile(r"\b(\d{5})-?(\d{7})-?(\d{1})\b")


def normalize_name(raw: str) -> str:
    """
    Lowercases, strips diacritics, drops punctuation/digits, removes
    honorifics, and maps known spelling variants to a canonical form.
    Returns a space-joined string of normalized tokens.

    Applied identically to BOTH the applicant name and every watchlist
    entry before scoring — matching only works if both sides go through
    the same normalization.
    """
    if not raw:
        return ""
    decomposed = unicodedata.normalize("NFKD", raw)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    tokens = _WORD_RE.findall(ascii_only.lower())
    tokens = [t for t in tokens if t not in HONORIFICS]
    tokens = [VARIANT_MAP.get(t, t) for t in tokens]
    return " ".join(tokens)


def extract_cnic(raw: str) -> str | None:
    """Pulls a Pakistani CNIC (13 digits, optionally dashed as 5-7-1) out of free text, if present."""
    if not raw:
        return None
    m = _CNIC_RE.search(raw)
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def normalize_cnic(raw: str | None) -> str | None:
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    return digits or None


def cnic_exact_match(applicant_cnic: str | None, entry_cnic: str | None) -> bool:
    """
    An exact CNIC match is direct identity evidence, not a fuzzy inference.
    Callers should treat it as at least as strong as a HIT regardless of
    the name similarity score, since names can coincidentally collide but
    a 13-digit national ID number should not.
    """
    a, b = normalize_cnic(applicant_cnic), normalize_cnic(entry_cnic)
    return bool(a and b and a == b)


@dataclass
class MatchScore:
    entry: str
    normalized_entry: str
    normalized_query: str
    token_sort: float
    token_set: float
    partial: float
    weighted: float          # RapidFuzz WRatio — length/coverage-aware
    combined: float          # final recommended score
    exact_normalized: bool   # normalized forms identical — very strong signal


def score_against(applicant_name: str, entry: str) -> MatchScore:
    """
    Scores one applicant name against one watchlist entry using several
    complementary algorithms and combines them by taking the maximum.

    Why several algorithms instead of one:
      - token_sort_ratio: strong for reordered tokens ("Ali Khan" vs
        "Khan Ali"), weak when the applicant supplied fewer name parts
        than the watchlist entry has.
      - token_set_ratio: handles that missing-token case (applicant gave
        2 names, the entry has 4), but can over-match on short/common
        tokens in isolation.
      - partial_ratio: catches one name being a near-substring of the
        other; useful for truncated or abbreviated entries. EXCLUDED from
        the combined score when the applicant's normalized name is a
        single short token (see guard below) — partial_ratio considers a
        short token "matched" whenever it appears anywhere inside a longer
        string (e.g. "ali" inside "alistair"), which turns any short,
        extremely common name into a near-automatic false HIT against
        unrelated longer names. That failure mode is worse than the
        truncated-name cases partial_ratio is meant to catch, so it's
        only trusted once there's enough of a name to make a substring
        match meaningful.
      - WRatio: RapidFuzz's blended heuristic, adds a length-ratio penalty
        the others lack, which helps suppress short-common-name false
        positives against long entries.

    Why max() rather than an average (with the one exclusion above): for a
    compliance screen, a missed true match is the worse failure mode.
    Every result here still routes to threshold-based HIT/REVIEW/CLEAR
    and, for anything above CLEAR, to a human analyst — so the cost of a
    slightly noisier top score is bounded, while averaging a real match
    down below threshold is not.
    """
    norm_q = normalize_name(applicant_name)
    norm_e = normalize_name(entry)

    if not norm_q or not norm_e:
        return MatchScore(entry, norm_e, norm_q, 0, 0, 0, 0, 0, False)

    if norm_q == norm_e:
        return MatchScore(entry, norm_e, norm_q, 100, 100, 100, 100, 100, True)

    ts = fuzz.token_sort_ratio(norm_q, norm_e)
    tset = fuzz.token_set_ratio(norm_q, norm_e)
    pr = fuzz.partial_ratio(norm_q, norm_e)
    wr = fuzz.WRatio(norm_q, norm_e)

    # Guard against partial_ratio's short-substring false-positive mode:
    # only let it contribute to the combined score once the shorter of
    # the two normalized names has at least 2 tokens (or is reasonably
    # long as a single token) — enough that "is a substring of" starts to
    # mean something identity-relevant rather than being a coincidence.
    shorter = min(norm_q, norm_e, key=len)
    partial_is_trustworthy = len(shorter.split()) >= 2 or len(shorter) >= 8
    algo_scores = [ts, tset, wr] + ([pr] if partial_is_trustworthy else [])
    combined = max(algo_scores)

    return MatchScore(entry, norm_e, norm_q, ts, tset, pr, wr, combined, False)


NEAR_MISS_MARGIN = 10  # points below threshold still worth an audit-log entry


@dataclass
class BestMatch:
    matched_entry: str | None
    score: float
    detail: str
    near_miss: bool = False
    breakdown: dict = field(default_factory=dict)


def find_best_match(applicant_name: str, entries: list[str], threshold: float) -> BestMatch:
    """
    Scans every watchlist entry, returns the best-scoring one plus whether
    it cleared `threshold`. Also flags a "near miss" (scored within
    NEAR_MISS_MARGIN points of threshold but didn't clear it) so those
    cases can be logged for audit even when the overall result is CLEAR —
    a checked-and-scored-59 result should never look identical, in an
    audit trail, to a checked-and-scored-5 result.
    """
    if not entries:
        return BestMatch(None, 0, "No entries to check against.")

    best: MatchScore | None = None
    for entry in entries:
        ms = score_against(applicant_name, entry)
        if best is None or ms.combined > best.combined:
            best = ms

    assert best is not None
    is_hit = best.combined >= threshold
    is_near_miss = (not is_hit) and (best.combined >= threshold - NEAR_MISS_MARGIN)

    return BestMatch(
        matched_entry=best.entry if is_hit else None,
        score=round(best.combined, 2),
        detail=(
            f"Best match: '{best.entry}' — combined score {best.combined:.1f} "
            f"(token_sort={best.token_sort:.1f}, token_set={best.token_set:.1f}, "
            f"partial={best.partial:.1f}, weighted={best.weighted:.1f})."
        ),
        near_miss=is_near_miss,
        breakdown={
            "matched_entry_raw": best.entry,
            "normalized_query": best.normalized_query,
            "normalized_entry": best.normalized_entry,
            "token_sort": best.token_sort,
            "token_set": best.token_set,
            "partial": best.partial,
            "weighted": best.weighted,
        },
    )

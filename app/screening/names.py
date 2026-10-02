"""
Name normalisation and fuzzy scoring.

This is a port of the scoring in the n8n "Applicant Screening Engine"
workflow (node "Screen Applicant"):

  * names are upper-cased, stripped of accents/punctuation and of titles and
    name particles (Dr, Haji, Al, Bin ...). Surnames such as Sheikh / Syed are
    deliberately kept;
  * every applicant token is compared with every candidate token using
    Jaro-Winkler similarity; a token pair below 0.88 counts as no match;
  * the score is symmetric: tokens on either side that find no partner pull the
    score down (weighted by token counts);
  * a cheap prefilter skips candidates that share no initial letter with the
    applicant.

Jaro-Winkler is the exact algorithm used by the workflow (`jw_exact`). It is
slow in pure Python, so rapidfuzz's C++ Jaro-Winkler is used first as a filter:
a pair it rates below JW_SKIP (0.70) cannot reach the 0.88 token cutoff under the
workflow's algorithm either. The two implementations differ slightly when letters
repeat (they count transpositions differently), by at most about 0.08 in the Jaro
part for short tokens, and a Jaro below 0.80 cannot produce a Jaro-Winkler of 0.88
even with the full prefix bonus, so the margin is safe. tests/test_names.py checks
every cutoff decision against a literal port over tens of thousands of token pairs.
"""

import math
import re
import unicodedata

from rapidfuzz.distance import JaroWinkler

# Titles and name particles ignored when comparing.
HONORIFICS = frozenset({
    "MR", "MRS", "MS", "MISS", "DR", "PROF", "HAJI", "HAJJI", "MAULANA", "MAWLAWI",
    "MULLAH", "MOLVI", "QARI", "ENGR", "ENG", "SIR", "GENERAL", "COLONEL",
    "AL", "EL", "UL", "BIN", "IBN", "BINT",
})

# Token similarity below this is treated as a non-match.
TOKEN_CUTOFF = 0.88

_COMBINING = re.compile(r"[\u0300-\u036f]")
_APOSTROPHES = re.compile("[\u2018\u2019\u02bb\u02bc'`]")
_NON_ALNUM = re.compile(r"[^A-Z0-9 ]+")
_SPACES = re.compile(r" +")


def norm(s) -> str:
    s = "" if s is None else str(s)
    s = unicodedata.normalize("NFKD", s)
    s = _COMBINING.sub("", s)
    s = _APOSTROPHES.sub("", s)
    s = s.upper()
    s = _NON_ALNUM.sub(" ", s)
    s = _SPACES.sub(" ", s)
    return s.strip()


def tokens(s) -> tuple:
    """Name tokens with honorifics removed (kept if that would leave nothing)."""
    parts = norm(s).split(" ")
    kept = tuple(t for t in parts if t and t not in HONORIFICS)
    if kept:
        return kept
    return tuple(t for t in parts if t)


# Pairs the fast implementation rates below this are skipped (see module docstring).
JW_SKIP = 0.70


def jw_exact(a: str, b: str) -> float:
    """Jaro-Winkler exactly as written in the n8n workflow (greedy matching)."""
    if a == b:
        return 1.0
    la, lb = len(a), len(b)
    if not la or not lb:
        return 0.0
    md = max(0, max(la, lb) // 2 - 1)
    am = [False] * la
    bm = [False] * lb
    m = 0
    for i in range(la):
        lo = max(0, i - md)
        hi = min(i + md + 1, lb)
        for j in range(lo, hi):
            if not bm[j] and a[i] == b[j]:
                am[i] = bm[j] = True
                m += 1
                break
    if not m:
        return 0.0
    t = 0
    k = 0
    for i in range(la):
        if am[i]:
            while not bm[k]:
                k += 1
            if a[i] != b[k]:
                t += 1
            k += 1
    j = (m / la + m / lb + (m - t / 2) / m) / 3
    p = 0
    while p < min(4, la, lb) and a[p] == b[p]:
        p += 1
    return j + p * 0.1 * (1 - j)


def jw(a: str, b: str) -> float:
    """
    Jaro-Winkler in 0..1. Exact for any pair that could matter: pairs the fast
    implementation rates below JW_SKIP are returned with that (lower) value, which
    callers only ever compare against cutoffs of 0.88 and above.
    """
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    fast = JaroWinkler.similarity(a, b)
    if fast < JW_SKIP:
        return fast
    return jw_exact(a, b)


def _js_round(x: float) -> float:
    # JavaScript Math.round rounds halves up; Python's round() rounds to even.
    return math.floor(x + 0.5)


class NameScorer:
    """Scores candidate names against one applicant name."""

    def __init__(self, applicant_name: str):
        self.q = tokens(applicant_name)
        self.initials = frozenset(t[0] for t in self.q)
        self._sim_cache: dict = {}

    def _sims(self, tok: str) -> tuple:
        """Similarity of one candidate token to each applicant token (memoised)."""
        s = self._sim_cache.get(tok)
        if s is None:
            s = tuple(jw(q, tok) for q in self.q)
            self._sim_cache[tok] = s
        return s

    def score_tokens(self, ctoks: tuple) -> float:
        """Score 0..100 (one decimal) for an already tokenised candidate name."""
        q = self.q
        if not ctoks or not q:
            return 0.0
        # cheap prefilter: at least one shared initial letter
        if not any(t[0] in self.initials for t in ctoks):
            return 0.0

        rows = [self._sims(t) for t in ctoks]  # one row per candidate token

        # applicant -> candidate coverage
        q2c = 0.0
        for j in range(len(q)):
            best = max(row[j] for row in rows)
            if best >= TOKEN_CUTOFF:
                q2c += best
        q2c /= len(q)

        # candidate -> applicant coverage
        c2q = 0.0
        for row in rows:
            best = max(row)
            if best >= TOKEN_CUTOFF:
                c2q += best
        c2q /= len(ctoks)

        # symmetric, weighted by token counts: extra unmatched tokens on
        # either side lower the score
        s = (q2c * len(q) + c2q * len(ctoks)) / (len(q) + len(ctoks))
        return _js_round(s * 1000) / 10

    def score(self, name: str) -> float:
        return self.score_tokens(tokens(name))

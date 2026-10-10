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
import os
import re
import unicodedata
from collections import Counter
from functools import lru_cache

from rapidfuzz import process
from rapidfuzz.distance import OSA, JaroWinkler

# Titles and name particles ignored when comparing.
HONORIFICS = frozenset({
    "MR", "MRS", "MS", "MISS", "DR", "PROF", "HAJI", "HAJJI", "MAULANA", "MAWLAWI",
    "MULLAH", "MOLVI", "QARI", "ENGR", "ENG", "SIR", "GENERAL", "COLONEL",
    "AL", "EL", "UL", "UR", "US", "UD", "BIN", "IBN", "BINT",
})

# Titles that are often dropped or added between documents (Syed Ali Raza and Ali Raza are one man on two forms).
# When one has no partner on the other side it is left out of the comparison instead of counting against the
# score, but only while at least two other name parts remain, so "Imran Malik" is never reduced to "Imran".
SOFT_TITLES = frozenset({"SAYED", "MIAN", "SARDAR", "MALIK", "CHAUDHRY", "KHAWAJA", "SAHIBZADA", "SHEIKH", "NAWAB",
                         "RAJA", "RANA", "PIR", "MAKHDOOM", "MIR", "BEGUM", "SARDARZADA", "NAWABZADA"})

# The most common name parts. A name whose parts are all from this set ("Muhammad Khan") says too little to be
# flagged just because it sits inside a longer listed name.
COMMON_PARTS = frozenset({"MUHAMMAD", "ALI", "KHAN", "AHMAD", "HUSSAIN", "HASSAN", "SAYED", "SHAH", "MALIK", "ABDUL",
                          "ABDULLAH", "RAHMAN", "DIN", "ULLAH", "MIAN", "SHEIKH", "RAZA", "BIBI", "BEGUM", "KUMAR"})

# A name that is wholly inside a longer one is a likely same person (a middle or family name left off a form).
SUBSET_SCORE = 88.0
SUBSET_MAX_EXTRA = 3

# A one letter name part is an initial: it matches any part starting with that letter, at this similarity.
INITIAL_SIM = 0.9

# Token similarity below this is treated as a non-match.
TOKEN_CUTOFF = 0.88
# Two short parts that differ by two or more edits are different names (IMRAN and IRFAN), and any two parts that
# differ by three or more are (PETROV and PETRENKO), however close the Jaro-Winkler score looks.
SHORT_PART = 5

_COMBINING = re.compile(r"[\u0300-\u036f]")
# Latin letters that NFKD does not decompose. Without this "LUKASZ" typed by an analyst never meets
# "ŁUKASZ" on a list (the Ł used to be dropped, leaving "UKASZ"), and likewise for Ø, Đ, Ð, Þ, Æ, Œ, ı, Ħ.
_FOLD = str.maketrans({"Æ": "AE", "æ": "ae", "Œ": "OE", "œ": "oe", "Ø": "O", "ø": "o", "Ł": "L", "ł": "l",
                       "Đ": "D", "đ": "d", "Ð": "D", "ð": "d", "Þ": "TH", "þ": "th", "ı": "i", "Ħ": "H", "ħ": "h"})
_APOSTROPHES = re.compile("[\u2018\u2019\u02bb\u02bc'`]")
_NON_ALNUM = re.compile(r"[^A-Z0-9 ]+")
_SPACES = re.compile(r" +")


def norm(s) -> str:
    s = "" if s is None else str(s)
    s = unicodedata.normalize("NFKD", s)
    s = _COMBINING.sub("", s)
    s = s.translate(_FOLD)
    s = _APOSTROPHES.sub("", s)
    s = s.upper()
    s = _NON_ALNUM.sub(" ", s)
    s = _SPACES.sub(" ", s)
    return s.strip()


def has_unsupported_script(s) -> bool:
    """
    True when the text holds letters that norm() cannot represent (Arabic, Urdu, Cyrillic, Chinese ...).
    norm() drops them, so a name written that way would have no tokens, score 0 against every record and
    come back CLEAR. Callers must refuse such a name instead of screening it.
    """
    s = unicodedata.normalize("NFKD", "" if s is None else str(s))
    s = _COMBINING.sub("", s).translate(_FOLD)
    return any(c.isalpha() and ord(c) > 127 for c in s)


# --------------------------------------------------------------------------
# Transliteration variants
# --------------------------------------------------------------------------
# Names that come from Arabic, Urdu and Persian have no single Latin spelling: the same man is
# MUHAMMAD, MOHAMMED, MOHD and MD on different documents. Jaro-Winkler alone scores
# MOHAMMED vs MUHAMMAD at 0.85, under the 0.88 token cutoff, so a listed person could be missed on the
# most common given name in the world. Each family below is folded to one canonical spelling on both
# sides (applicant and list) before scoring. Only well established variants belong here: every entry
# makes two spellings count as the same token. Set NAME_VARIANTS=false to score the raw spellings,
# exactly as the original workflow did.
_VARIANT_FAMILIES = {
    "MUHAMMAD": "MOHAMMED MOHAMMAD MOHAMED MUHAMMED MUHAMED MUHAMAD MOHAMAD MOHD MUHD MD MAHOMED MOUHAMED "
                "MOUHAMMED MUHAMMET MOHAMET MOHAMMADD",
    "AHMAD": "AHMED AHMET AHMAAD", "HUSSAIN": "HUSSEIN HUSAIN HOSSAIN HOSSEIN HUSEIN HOSEIN HUSSAYN HUSAYN HOSAIN",
    "HASSAN": "HASAN HASSEN", "UMAR": "OMAR OMER UMER", "USMAN": "OSMAN UTHMAN OTHMAN UTHMAAN",
    "IBRAHIM": "EBRAHIM IBRAHEEM EBRAHEEM IBRAHEM", "ISMAIL": "ISMAEEL ISMAEL ISMAYIL ISMAIEL ISMAYEL",
    "YUSUF": "YOUSUF YUSSUF YOUSEF YUSEF YOUSOUF YUSOUF YOUSAF YUSAF YUSAAF YOUSAAF", "ZIA": "ZIAUL ZIYA", "FAZAL": "FAZL FAZLUR FAZLE FAZLI", "SAYED": "SYED SAYYED SAYYID SAYID SEYED",
    "SHEIKH": "SHAIKH SHAYKH SHEIK SHAIK SHEKH", "ABDUL": "ABDOL ABDEL ABD ABDOUL ABDOOL ABDELL",
    "ABDULLAH": "ABDALLAH ABDULLA ABDOLLAH ABDELLAH ABDALLA", "RAHMAN": "REHMAN RAHMAAN REHMAAN",
    "MAHMOOD": "MAHMUD MAHMOUD MEHMOOD MEHMUD", "KHALID": "KHALED KHALEED", "SALIM": "SALEEM", "NASIR": "NASSER NASER NASEER",
    "SAEED": "SAID SAYEED", "ZUBAIR": "ZUBAYR ZUBEIR ZOBAIR ZUBEER", "HAMZA": "HAMZAH HAMZEH",
    "USAMA": "OSAMA OSSAMA USAMAH OSAMAH", "ZULFIQAR": "ZULFIKAR ZULFEKAR ZULFIQUAR",
    "SIDDIQUI": "SIDDIQI SIDDIQUE SIDIQI SIDDIQUEE", "QURESHI": "KUREISHI QURAISHI QURAYSHI KURESHI",
    "CHAUDHRY": "CHAUDHARY CHAUDHRI CHAUDHURY CHOUDHARY CHOUDHRY CHAUDRY CHOUDHURY CHOWDHURY CH CHDHRY",
    "HAQ": "HUQ HAQUE HUQUE", "MUSTAFA": "MUSTAPHA MOSTAFA MOUSTAFA", "FATIMA": "FATIMAH FATEMA FATEMAH",
    "AISHA": "AYESHA AYSHA AISHAH AYISHA AAISHA", "JAVED": "JAVID JAVEED JAWED JAWID", "IQBAL": "IKBAL",
    "QASIM": "KASIM QASSIM QASEM KASEM", "TARIQ": "TAREK TAREQ TARIK TAREEQ",
    "SULAIMAN": "SULEIMAN SULAYMAN SULEMAN SOLIMAN SOLEIMAN", "HAFIZ": "HAFEEZ HAFEZ", "MASOOD": "MASUD MASOUD",
    "JAMIL": "JAMEEL JAMEL", "KARIM": "KAREEM KERIM", "RASHID": "RASHEED RASHED", "NOOR": "NUR NOUR NURUL NOORUL",
    "ABU": "ABOU ABOO", "KHAN": "KHANN",
    "FAROOQ": "FAROUK FARUQ FAROUQ FARUK FAROOK FARUKH", "HAMID": "HAMEED HAMED", "MAJID": "MAJEED MAJED",
    "KHAWAJA": "KHWAJA KHAJA KHAWAJAH KHAWJA KHWAJAH",
    "GILANI": "GILLANI GEELANI GILANEE GEELANEE", "REHAN": "RAIHAN RIHAN RAYHAN RAIHAAN",
    "ALEXEI": "ALEKSEI ALEXEY ALEKSEY ALEXEJ ALEKSEJ", "TAREEN": "TARIN TAREN", "MIR": "MEER",
    "GADDAFI": "GADHAFI QADDAFI QADHAFI KADHAFI KADDAFI GHADDAFI QADAFI GADAFI",
 "SHAKEEL": "SHAKIL SHAKEIL",
    "SHAHEEN": "SHAHIN SHAHEIN", "MUSHARRAF": "MUSHARAF MOSHARRAF", "RAFIQ": "RAFEEQ RAFIK RAFEEK",
    "ZAFAR": "ZAFFAR ZUFAR", "SHAUKAT": "SHAWKAT SHAUKET", "NAVEED": "NAVED NAVEEDD NAWEED", "WAQAR": "WAQQAR WAKAR",
    "SHAHID": "SHAHEED SHAHED", "BASHIR": "BASHEER BASHEIR", "AKHTAR": "AKHTER AKTAR", "SULTAN": "SULTHAN SULTANN",
    "BILAL": "BELAL BILLAL", "SOHAIL": "SUHAIL SUHEIL SOHEIL", "ANWAR": "ANWER ANOUAR"
}
_VARIANT = {v: canon for canon, vs in _VARIANT_FAMILIES.items() for v in vs.split()}

# "ABDULRAHMAN" and "ABDUL RAHMAN" are the same name. A fused token is split only when the part after the
# ABDUL- prefix is a known second element, so ABDULLAH and unrelated words are never cut.
_ABDUL_PREFIXES = ("ABDUL", "ABDUR", "ABDUS", "ABDEL", "ABDOL", "ABDOUL")
_ABDUL_SECOND = frozenset({
    "RAHMAN", "REHMAN", "RAHIM", "REHIM", "AZIZ", "KARIM", "KAREEM", "QADIR", "KADER", "SALAM", "SAMAD", "GHAFFAR",
    "GHAFOOR", "RAZZAQ", "RAZAQ", "HAKIM", "HAMID", "MAJID", "WAHID", "WAHAB", "BASIT", "BARI", "JABBAR", "LATIF",
    "MALIK", "MANAN", "QAYYUM", "RASHID", "RAOOF", "SATTAR", "SHAKOOR", "WADOOD", "HAQ", "HADI", "HAFIZ", "GHANI",
    "NASIR", "SABOOR", "RAUF", "QUDDUS", "SAMI", "BASIR", "HALIM", "MUHSIN", "MOHSIN", "NOOR", "HAMEED", "MAJEED",
})


def _split_abdul(t: str) -> tuple:
    for p in _ABDUL_PREFIXES:
        if t.startswith(p) and len(t) > len(p):
            rest = t[len(p):]
            # ABDURRAHMAN / ABDUSSALAM: the doubled letter belongs to the second element
            for cand in (rest, rest[1:] if len(rest) > 1 and rest[0] == rest[1] else None):
                if cand and cand in _ABDUL_SECOND:
                    return ("ABDUL", cand)
    return (t,)


NAME_VARIANTS = os.environ.get("NAME_VARIANTS", "true").strip().lower() not in ("0", "false", "no")


# Name parts that are often written together ("KHANMUHAMMAD", "MUHAMMADALI"). A part that is exactly two of these
# is split, on both the applicant and the list side, so both spellings give the same parts.
_FUSE_WORDS = frozenset({
    "MUHAMMAD", "ALI", "KHAN", "AHMAD", "HUSSAIN", "HASSAN", "SHAH", "HAQ", "RAHMAN", "DIN", "ULLAH", "ALLAH", "NAWAZ",
    "SHARIF", "HAMID", "MAJID", "RAZA", "SAEED", "NOOR", "FAZAL", "SIRAJ", "ZIA", "ABDUL", "RAUF", "AZIZ", "KARIM", "LATIF",
    "MALIK", "AKBAR", "ASGHAR", "REHMAN", "IQBAL", "JAN", "GUL", "BAKHSH", "DAD", "MIR", "ZADA", "SULTAN", "FAROOQ",
    "TARIQ", "IMRAN", "NAEEM", "WALI", "KHALID", "SALIM", "UMAR", "USMAN", "BILAL", "HAIDER", "NASIR", "FAIZ", "SHER",
    "BAZ", "RASHID", "SAJID", "ZAFAR", "AMIN", "YOUSAF", "YUSUF", "IBRAHIM", "ISMAIL", "MUSTAFA", "SULEMAN",
})
_SUFFIX_PARTICLES = ("UL", "UR", "US", "UD")


def _strip_particle(t: str) -> str:
    """FAZALUR to FAZAL, SIRAJUL to SIRAJ: a fused particle (-ul, -ur, -us, -ud) is not part of the name."""
    if len(t) >= 7:
        for suf in _SUFFIX_PARTICLES:
            if t.endswith(suf) and len(t) - 2 >= 4:
                return t[:-2]
    return t


def _split_fused(t: str) -> tuple:
    if len(t) < 6 or _VARIANT.get(t, t) in _FUSE_WORDS:
        return (t,)
    for i in range(3, len(t) - 2):
        a, b = _VARIANT.get(t[:i], t[:i]), _VARIANT.get(t[i:], t[i:])
        if a in _FUSE_WORDS and b in _FUSE_WORDS and not (a == "ABDUL" and b == "ULLAH"):    # ABDULLAH is one name
            return (t[:i], t[i:])
    return (t,)


# Vowel and letter habits that differ between Latin spellings of the same sound: JAVAID and JAVED, MOEEN and MOIN,
# RASOOL and RASUL, GHULAM and GULAM, RAZZAQ and RAZAQ. Applied to every part on both sides after the variant
# families, so the folded spellings are what get compared.
_PHONETIC = (("GH", "G"), ("PH", "F"), ("OO", "U"), ("EE", "I"), ("OU", "U"), ("AI", "E"), ("AY", "E"))
_REPEAT = re.compile(r"(.)\1+")


def _phon(t: str) -> str:
    if len(t) < 3:
        return t
    for a, b in _PHONETIC:
        t = t.replace(a, b)
    return _REPEAT.sub(r"\1", t)


def _split_ullah(t: str) -> tuple:
    """SAIFULLAH to SAIF ULLAH, SANAULLAH to SANA ULLAH (ABDULLAH is one name and is left alone)."""
    if t.endswith("ULLAH") and len(t) >= 8 and _VARIANT.get(t, t) != "ABDULLAH" and not t.startswith(_ABDUL_PREFIXES):
        return (t[:-5], "ULLAH")
    return (t,)


@lru_cache(maxsize=262144)
def _fold_part(t: str) -> tuple:
    out = []
    for u in _split_abdul(t):
        for v in _split_ullah(u):
            for w in _split_fused(_strip_particle(v)):
                out.append(_phon(_VARIANT.get(w, w)))
    return tuple(out)


def _fold(parts: tuple) -> tuple:
    out: list = []
    for t in parts:
        out.extend(_fold_part(t))
    return tuple(out)


SOFT_TITLES = frozenset(_phon(t) for t in SOFT_TITLES)
COMMON_PARTS = frozenset(_phon(t) for t in COMMON_PARTS)


def tokens(s) -> tuple:
    """Name tokens with honorifics removed (kept if that would leave nothing) and spelling variants folded."""
    parts = norm(s).split(" ")
    kept = tuple(t for t in parts if t and t not in HONORIFICS)
    result = kept or tuple(t for t in parts if t)
    return _fold(result) if NAME_VARIANTS else result


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


def part_similarity(a: str, b: str) -> float:
    """Similarity (0..1) of two name parts. Initials match any part that starts with the letter; very short parts
    that differ by two or more edits do not match."""
    if a == b:
        return 1.0
    if len(a) == 1 or len(b) == 1:
        return INITIAL_SIM if a[0] == b[0] else 0.0
    s = jw(a, b)
    if s >= TOKEN_CUTOFF:
        edits = OSA.distance(a, b)
        if edits >= 3 or (edits >= 2 and len(a) <= SHORT_PART and len(b) <= SHORT_PART):
            return 0.0
    return s


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
            s = tuple(part_similarity(q, tok) for q in self.q)
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
        q_best = [max(row[j] for row in rows) for j in range(len(q))]
        c_best = [max(row) for row in rows]

        # a title with no partner on the other side is left out, while two other parts remain
        q_keep = _keep(q, q_best)
        c_keep = _keep(ctoks, c_best)

        # applicant -> candidate coverage
        q2c = sum(q_best[j] for j in q_keep if q_best[j] >= TOKEN_CUTOFF) / len(q_keep)
        # candidate -> applicant coverage
        c2q = sum(c_best[i] for i in c_keep if c_best[i] >= TOKEN_CUTOFF) / len(c_keep)

        # symmetric, weighted by token counts: extra unmatched tokens on
        # either side lower the score
        s = (q2c * len(q_keep) + c2q * len(c_keep)) / (len(q_keep) + len(c_keep))
        score = _js_round(s * 1000) / 10
        if score < SUBSET_SCORE:
            sub = _subset(q, q_best, q_keep, ctoks, c_best, c_keep)
            if sub:
                return sub
        return score

    def score(self, name: str) -> float:
        return self.score_tokens(tokens(name))


def _keep(toks: tuple, best: list) -> list:
    """Positions of the parts that count: all of them, minus titles with no partner when two or more others remain."""
    kept = [i for i, t in enumerate(toks) if not (t in SOFT_TITLES and best[i] < TOKEN_CUTOFF)]
    return kept if len(kept) >= 2 else list(range(len(toks)))


def _subset(q, q_best, q_keep, ctoks, c_best, c_keep) -> float:
    """SUBSET_SCORE when one name, untouched and of at least two parts, sits wholly inside the other (a middle or
    family name left off), unless every part of it is a very common one; otherwise 0."""
    for short, best, keep, full, n_long in ((q, q_best, q_keep, ctoks, len(c_keep)),
                                              (ctoks, c_best, c_keep, q, len(q_keep))):
        if len(keep) == len(short) and len(short) >= 2 and 0 < n_long - len(short) <= SUBSET_MAX_EXTRA \
                and all(b >= TOKEN_CUTOFF for b in best) and any(t not in COMMON_PARTS for t in short):
            return SUBSET_SCORE
    return 0.0


# --------------------------------------------------------------------------
# Candidate index
# --------------------------------------------------------------------------

class RecordIndex:
    """
    An inverted index from name token to the records that contain it, built once when a list loads.

    Why it is exact: NameScorer gives a record a score above zero only when at least one of its tokens
    has a Jaro-Winkler similarity of TOKEN_CUTOFF or more to an applicant token (tokens below the cutoff
    contribute nothing to either side of the score). So every record that could reach a threshold
    (50 or more) is found through the vocabulary: score the applicant's tokens against the list's unique
    tokens, then collect the records that hold a token that passed. The same two stage rule as jw() is
    used (rapidfuzz at JW_SKIP, then the exact algorithm), so the candidates are precisely the records the
    brute force loop would have scored above zero. tests/test_index.py checks that on random data.

    On a 150,000 name list this turns a full scan (about a second of Python) into scoring only the few
    records that share a similar token with the applicant.
    """

    __slots__ = ("vocab", "postings", "by_cnic", "size", "max_len", "_hits", "initials", "by_first", "_toks")

    _HITS_MAX = 4096

    def __init__(self, records: list):
        postings: dict = {}
        by_cnic: dict = {}
        self._toks = [r.toks for r in records]
        self.max_len = [max((len(tk) for tk in r.toks), default=0) for r in records]   # longest name of each record
        for i, r in enumerate(records):
            for tk in r.toks:
                for t in tk:
                    lst = postings.setdefault(t, [])
                    if not lst or lst[-1] != i:
                        lst.append(i)
            if r.cnic:
                by_cnic.setdefault(r.cnic, []).append(i)
        self.postings = postings
        self.vocab = list(postings)
        self.initials = {t: t for t in postings if len(t) == 1}          # one letter parts on the list side
        self.by_first: dict = {}                                            # first letter -> longer parts, for an applicant initial
        for t in postings:
            if len(t) > 1:
                self.by_first.setdefault(t[0], []).append(t)
        self.by_cnic = by_cnic
        self.size = len(records)
        self._hits: dict = {}    # applicant token -> list-side tokens that are close enough, reused across screenings

    def _close_tokens(self, q: str) -> tuple:
        hit = self._hits.get(q)
        if hit is None:
            if len(q) == 1:       # an initial matches every part that starts with the letter (see part_similarity)
                hit = tuple(self.by_first.get(q, ())) + ((q,) if q in self.postings else ())
            else:
                found = process.extract(q, self.vocab, scorer=JaroWinkler.similarity, score_cutoff=JW_SKIP, limit=None)
                hit = tuple(tok for tok, _score, _i in found if jw_exact(q, tok) >= TOKEN_CUTOFF)
                if q[0] in self.initials:          # a one letter part on the list matches this part by its initial
                    hit += (q[0],)
            if len(self._hits) >= self._HITS_MAX:
                self._hits.clear()
            self._hits[q] = hit
        return hit

    def candidates(self, applicant_tokens, cnic: str = "", threshold: float = 0.0) -> list:
        """
        Record positions, in list order, that can score above zero for these tokens (plus any with this CNIC).

        With a `threshold`, records that provably cannot reach it are left out as well. The score of a name
        with c tokens, against an applicant with Q tokens of which `a` have a close partner in the record, is
        at most (a + c) / (Q + c): each matched token is worth at most 1, and a name cannot have more than c
        matched tokens. That bound grows with c, so the longest name of the record is used. For
        "Muhammad Ali Khan" at 85 this skips every record that holds only one or two of the three names,
        which is nearly all of them. Nothing that could reach the threshold is ever skipped. (Titles that may be left
        out and the subset lift, both in NameScorer, are allowed for as described in the code below.)
        """
        q = list(applicant_tokens)
        weight = Counter(q)
        coverage: Counter = Counter()
        close_vocab: set = set()
        for tok, m in weight.items():
            holders: set = set()
            for close in self._close_tokens(tok):
                close_vocab.add(close)
                holders.update(self.postings[close])
            for _ in range(m):
                coverage.update(holders)
        if threshold > 0:
            Q = len(q)
            soft = sum(t in SOFT_TITLES for t in q)
            Q_min = Q - soft if Q - soft >= 2 else Q      # titles with no partner leave the denominator
            # score = round_half_up(s * 1000) / 10 >= threshold  <=>  s >= (threshold * 10 - 0.5) / 1000
            need = (threshold * 10 - 0.5) / 1000 - 1e-9
            max_len = self.max_len
            # a record name wholly inside the applicant's is lifted to SUBSET_SCORE whatever the bound says, so when
            # that could reach the threshold a record with a name of two or more parts that all have a close partner
            # stays a candidate. (An applicant name wholly inside the record's has every part matched, and the
            # bound is then already 1.)
            subset = SUBSET_SCORE >= threshold
            toks = self._toks
            found = {i for i, a in coverage.items()
                     if (a + max_len[i]) / (Q_min + max_len[i]) >= need
                     or (subset and any(len(t) >= 2 and all(x in close_vocab for x in t) for t in toks[i]))}
        else:
            found = set(coverage)
        if cnic:
            found.update(self.by_cnic.get(cnic, ()))
        return sorted(found)

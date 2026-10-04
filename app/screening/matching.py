"""
Core name-matching engine shared by UNSC, FIA Red Book, and any future
watchlist source.

Why this is its own module: matching accuracy directly determines whether
a sanctioned/wanted individual is correctly flagged (miss = regulatory and
reputational risk) or an innocent applicant is wrongly escalated (false
positive = customer harm, wasted analyst time). A single fuzzy-ratio call
is not enough for that job on its own, so this module:

  1. Normalizes names before comparison (case, punctuation, diacritics,
     honorifics, and a hand-maintained list of common transliteration /
     nickname variants relevant to Pakistani/Arabic/Urdu names) so that
     formatting noise and known spelling variants don't masquerade as low
     similarity.
  2. Scores using several complementary algorithms and combines them,
     because no single ratio handles every name shape well — see
     `score_against`'s docstring for what each one is for. This now
     includes a *phonetic* layer (Metaphone-coded token overlap) in
     addition to RapidFuzz's literal-edit-distance algorithms, because a
     literal-only approach and a fixed variant table both only catch
     spelling variants someone already thought to write down. Phonetic
     coding catches variants nobody enumerated, at the cost of being a
     coarser signal — so it's combined with the same guard rails as
     partial_ratio (see below), not trusted blindly.
  3. Treats an exact CNIC match as a separate, stronger signal from name
     similarity — a shared national ID number is direct identity evidence,
     not a fuzzy inference — and exposes it distinctly so callers can
     escalate on it regardless of the name score. A *near* CNIC match
     (one-digit difference, e.g. a scanning/OCR transcription slip) is
     tracked separately again, as a weaker audit-only signal — never an
     auto-HIT, since a single-digit CNIC "match" is far more likely to be
     two different people than a genuine typo.
  4. Returns enough detail (per-algorithm scores, normalized forms, a
     near-miss flag) for both the evidence PDF and an audit log, so a
     compliance analyst can see *why* something scored the way it did,
     not just a bare number.

This module intentionally does NOT decide HIT/REVIEW/CLEAR — thresholds
and routing stay in app/main.py, so there is exactly one place that owns
screening policy and one place (here) that owns the measurement it acts on.

IMPORTANT — read this before trusting the output, and before describing
this tool to anyone else:
Fuzzy name matching is inherently probabilistic, on this codebase or any
other vendor's. Adding a phonetic layer and a larger variant table (this
revision) measurably improves coverage of transliteration variants over
the previous single-literal-ratio version (see tests/test_matching.py for
the concrete cases it fixes) — but "improves coverage" is not the same
claim as "eliminates false negatives" or "outperforms every commercial
screening product," and neither this module nor its docstrings should be
represented that way. No independent benchmark exists comparing this
against a licensed AML vendor's matching engine, and none is claimed here.
Every HIT/REVIEW result is designed to route to a human compliance
analyst, not to auto-reject or auto-clear an applicant — that human step
is not a formality, it's load-bearing. Treat the variant table as a
living document: extend it whenever a real mismatch is found in
production, and have a compliance analyst periodically review both the
near-miss log and a sample of CLEAR results.
"""

import re
import unicodedata
from dataclasses import dataclass, field

from rapidfuzz import fuzz

try:
    import jellyfish
    _HAVE_JELLYFISH = True
except ImportError:  # pragma: no cover - exercised only if dependency missing
    _HAVE_JELLYFISH = False

# Honorifics / titles that precede a name and add no identifying signal.
# Stripping them prevents e.g. "Haji Muhammad Ali" from scoring lower
# against a watchlist entry of plain "Muhammad Ali" than it should.
HONORIFICS = {
    "mr", "mrs", "ms", "miss", "mst", "dr", "syed", "syeda", "haji", "hajra",
    "hajjah", "sheikh", "shaikh", "shaykh", "chaudhry", "chaudhary", "ch",
    "malik", "mian", "raja", "sardar", "sahibzada", "pir", "hafiz", "qari",
    "maulana", "mufti", "alhaj", "al", "haj", "khawaja", "khwaja", "nawab",
    "sahib", "sahiba", "begum", "shahzada", "shahzadi", "agha", "mirza",
}

# Conservative single-token spelling/transliteration and common-nickname
# equivalences seen across Pakistani/Arabic-derived names in official
# documents, watchlist entries, and news transliteration. Each key maps to
# one canonical form so variant spellings of the SAME underlying name
# compare as equal instead of as a fuzzy near-miss. This is NOT a general
# transliteration engine — only near-universally-agreed equivalences (or
# genuinely common nicknames/diminutives) belong here. Extend it as real
# mismatches are found; do not add speculative entries, since an
# over-eager mapping can cause unrelated names to collapse together.
#
# Note: this table is a supplement to, not a substitute for, the phonetic
# layer in `score_against` — entries here should still be added whenever a
# real mismatch surfaces, because an explicit mapping is more precise
# (and auditable) than relying on phonetic coincidence.
VARIANT_MAP = {
    "mohammad": "muhammad", "mohammed": "muhammad", "muhammed": "muhammad",
    "mohamed": "muhammad", "mohd": "muhammad", "mhd": "muhammad",
    "mahmud": "mahmood", "mehmood": "mahmood", "mahmoud": "mahmood",
    "ahmad": "ahmed", "ahamed": "ahmed", "ahmath": "ahmed", "ahmet": "ahmed",
    "farooq": "farooque", "farooqi": "farooque",
    "abdullah": "abdulla", "abdullahi": "abdulla", "abdalla": "abdulla",
    "yousaf": "yousuf", "yusuf": "yousuf", "yousif": "yousuf", "yusif": "yousuf",
    "youssef": "yousuf", "yousef": "yousuf",
    "hussain": "hussein", "husain": "hussein", "hossain": "hussein",
    "hussan": "hussein",
    "usman": "osman", "othman": "osman", "uthman": "osman",
    "ibrahim": "ibraheem", "ebrahim": "ibraheem", "ebrahaim": "ibraheem",
    "zafar": "zaffar",
    "qadeer": "qadir", "qadeer ": "qadir",
    "rehman": "rahman", "rahmaan": "rahman", "rehmaan": "rahman",
    "fatima": "fatimah", "fatema": "fatimah", "fathima": "fatimah",
    "aisha": "ayesha", "ayeshah": "ayesha", "aysha": "ayesha", "aeysha": "ayesha",
    "zainab": "zaynab", "zaynub": "zaynab", "zenab": "zaynab",
    "bakhsh": "baksh", "bux": "baksh",
    "siddique": "siddiqui", "siddiqi": "siddiqui", "sadiqi": "siddiqui",
    "gilani": "gillani",
    "chisti": "chishti", "chishty": "chishti",
    # Common given-name diminutives / transliteration pairs not already
    # covered above:
    "zulfikar": "zulfiqar", "zulfeqar": "zulfiqar",
    "bilal": "belal", "billal": "belal",
    "tarik": "tariq", "taariq": "tariq",
    "yakub": "yaqoob", "yaqub": "yaqoob", "yakoob": "yaqoob",
    "ismaeel": "ismail", "ismael": "ismail",
    "yunus": "younus", "younis": "younus", "yunis": "younus",
    "hameed": "hamid", "hamed": "hamid",
    "vaqas": "waqas",
    "zubayr": "zubair", "zobair": "zubair", "zubeir": "zubair",
    "taher": "tahir", "tahar": "tahir",
    "naser": "nasir", "nassir": "nasir", "nasser": "nasir",
    "kawsar": "kauser", "kausar": "kauser", "kowsar": "kauser",
    "saber": "sabir",
    "shabir": "shabbir", "shabeer": "shabbir",
    "anwer": "anwar",
    "akhter": "akhtar",
    "kamar": "qamar",
    "shahzaad": "shahzad",
    "kashef": "kashif",
    "jawaid": "javed", "javaid": "javed", "jawad": "javed",
    "salim": "saleem",
    "naeim": "naeem", "nayeem": "naeem",
    "shuaib": "shoaib",
    "wahid": "waheed",
    "rasheed": "rashid",
    "khaled": "khalid",
    "aamir": "amir", "ameer": "amir",
    "kalim": "kaleem",
    "moin": "mueen", "muin": "mueen",
    "aarif": "arif",
    "eqbal": "iqbal",
    "parvez": "pervez", "pervaiz": "pervez", "pervaz": "pervez",
    "riaz": "riyaz",
    "zia": "ziya",
    "wasim": "waseem",
    "naseem": "nasim",
    "kasim": "qasim",
}

_WORD_RE = re.compile(r"[a-z]+")
_CNIC_RE = re.compile(r"\b(\d{5})-?(\d{7})-?(\d{1})\b")


def normalize_name(raw: str) -> str:
    """
    Lowercases, strips diacritics, drops punctuation/digits, removes
    honorifics, and maps known spelling/nickname variants to a canonical
    form. Returns a space-joined string of normalized tokens.

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


def cnic_near_match(applicant_cnic: str | None, entry_cnic: str | None) -> bool:
    """
    True when two CNICs are the same length and differ by exactly one
    single-character edit (a plausible OCR/typo slip on a scanned or
    hand-keyed number) but are NOT identical.

    This is deliberately NOT escalated to HIT anywhere in this codebase —
    a one-digit difference in a 13-digit ID is still overwhelmingly more
    likely to be a different person than a typo. It exists purely as an
    audit-log signal (see database.insert_near_miss) so a compliance
    analyst can periodically check whether near-identical CNICs are
    showing up in traffic, not to make an automated identity decision.
    """
    a, b = normalize_cnic(applicant_cnic), normalize_cnic(entry_cnic)
    if not a or not b or a == b or len(a) != len(b):
        return False
    if not _HAVE_JELLYFISH:
        # Fallback: plain Hamming distance if jellyfish isn't installed.
        return sum(1 for x, y in zip(a, b) if x != y) == 1
    return jellyfish.damerau_levenshtein_distance(a, b) == 1


def _phonetic_key(token: str) -> str:
    """Metaphone code for one normalized token, or the token itself if jellyfish is unavailable."""
    if not _HAVE_JELLYFISH or not token:
        return token
    try:
        return jellyfish.metaphone(token) or token
    except Exception:
        return token


def _phonetic_token_set_ratio(norm_q: str, norm_e: str) -> float:
    """
    Coarser sibling of RapidFuzz's token_set_ratio: instead of comparing
    literal characters, compares the *sound* of each token (via
    Metaphone). Catches transliteration variants that are phonetically
    the same name but literally quite different strings (e.g. spelling
    divergences the hand-curated VARIANT_MAP hasn't seen yet) — which
    matters a lot for Arabic/Urdu names romanized inconsistently across
    documents.

    Deliberately uses exact phonetic-code equality per token (not
    substring containment) so it doesn't inherit partial_ratio's
    short-common-token false-positive mode — a phonetic code matching
    exactly is a much stronger claim than one string merely containing
    another.
    """
    if not _HAVE_JELLYFISH:
        return 0.0
    q_tokens = norm_q.split()
    e_tokens = norm_e.split()
    if not q_tokens or not e_tokens:
        return 0.0
    q_keys = {_phonetic_key(t) for t in q_tokens if len(t) >= 2}
    e_keys = {_phonetic_key(t) for t in e_tokens if len(t) >= 2}
    if not q_keys or not e_keys:
        return 0.0
    inter = q_keys & e_keys
    smaller = min(len(q_keys), len(e_keys))
    return 100.0 * len(inter) / smaller if smaller else 0.0


@dataclass
class MatchScore:
    entry: str
    normalized_entry: str
    normalized_query: str
    token_sort: float
    token_set: float
    partial: float
    weighted: float          # RapidFuzz WRatio — length/coverage-aware
    phonetic: float          # Metaphone-coded token overlap
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
      - phonetic (Metaphone token overlap): catches transliteration
        variants that are literally quite different strings but sound the
        same, which the literal algorithms above (and the hand-curated
        VARIANT_MAP) can miss if nobody has written that particular
        spelling pair down yet. Uses the SAME short-token guard as
        partial_ratio, for the same reason — exact phonetic-code equality
        is a stronger claim than substring containment, but a very short
        single token can still coincidentally share a phonetic code with
        an unrelated name, so it isn't trusted in isolation either.

    Why max() rather than an average (with the guard above applying to
    both partial_ratio and the phonetic score): for a compliance screen, a
    missed true match is the worse failure mode. Every result here still
    routes to threshold-based HIT/REVIEW/CLEAR and, for anything above
    CLEAR, to a human analyst — so the cost of a slightly noisier top
    score is bounded, while averaging a real match down below threshold
    is not.
    """
    norm_q = normalize_name(applicant_name)
    norm_e = normalize_name(entry)

    if not norm_q or not norm_e:
        return MatchScore(entry, norm_e, norm_q, 0, 0, 0, 0, 0, 0, False)

    if norm_q == norm_e:
        return MatchScore(entry, norm_e, norm_q, 100, 100, 100, 100, 100, 100, True)

    ts = fuzz.token_sort_ratio(norm_q, norm_e)
    tset = fuzz.token_set_ratio(norm_q, norm_e)
    pr = fuzz.partial_ratio(norm_q, norm_e)
    wr = fuzz.WRatio(norm_q, norm_e)
    ph = _phonetic_token_set_ratio(norm_q, norm_e)

    # Guard against partial_ratio's (and the phonetic score's) short-
    # substring / short-token false-positive mode: only let them
    # contribute to the combined score once the shorter of the two
    # normalized names has at least 2 tokens (or is reasonably long as a
    # single token) — enough that "matches" starts to mean something
    # identity-relevant rather than being a coincidence.
    shorter = min(norm_q, norm_e, key=len)
    guard_ok = len(shorter.split()) >= 2 or len(shorter) >= 8
    algo_scores = [ts, tset, wr] + ([pr, ph] if guard_ok else [])
    combined = max(algo_scores)

    return MatchScore(entry, norm_e, norm_q, ts, tset, pr, wr, ph, combined, False)


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
            f"partial={best.partial:.1f}, weighted={best.weighted:.1f}, "
            f"phonetic={best.phonetic:.1f})."
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
            "phonetic": best.phonetic,
        },
    )

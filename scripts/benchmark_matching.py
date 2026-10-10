"""
Measure the name matcher against the labeled benchmark and print accuracy, misses and false alarms.

    python scripts/benchmark_matching.py [threshold]

Accuracy is the share of all pairs decided correctly. Recall is the share of same-person pairs that are flagged,
and the false alarm rate is the share of different-person pairs that are flagged. The default threshold is the
one the application uses (MATCH_THRESHOLD).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import MATCH_THRESHOLD                      # noqa: E402
from app.screening.names import NameScorer                  # noqa: E402
from tests.benchmark.name_pairs import NEGATIVES, POSITIVES, random_negatives   # noqa: E402


def evaluate(threshold: float = MATCH_THRESHOLD) -> dict:
    def score(a, b):
        return NameScorer(a).score(b)

    missed = [(a, b, c, score(a, b)) for a, b, c in POSITIVES if score(a, b) < threshold]
    negatives = NEGATIVES + random_negatives()
    alarms = [(a, b, c, score(a, b)) for a, b, c in negatives if score(a, b) >= threshold]
    p, n = len(POSITIVES), len(negatives)
    correct = (p - len(missed)) + (n - len(alarms))
    by_cat: dict = {}
    for a, b, c in POSITIVES:
        row = by_cat.setdefault(c, [0, 0])
        row[1] += 1
        row[0] += score(a, b) >= threshold
    return {"threshold": threshold, "pairs": p + n, "accuracy": correct / (p + n), "recall": (p - len(missed)) / p,
            "false_alarm_rate": len(alarms) / n, "missed": missed, "alarms": alarms, "by_category": by_cat}


if __name__ == "__main__":
    r = evaluate(float(sys.argv[1]) if len(sys.argv) > 1 else MATCH_THRESHOLD)
    print(f"threshold {r['threshold']:g}: {r['pairs']} pairs, accuracy {r['accuracy']:.1%}, "
          f"recall {r['recall']:.1%}, false alarms {r['false_alarm_rate']:.1%}")
    for cat, (hit, tot) in sorted(r["by_category"].items()):
        print(f"  {cat:10s} {hit}/{tot}")
    print("MISSED (same person, not flagged):")
    for a, b, c, s in r["missed"]:
        print(f"  {s:5.1f}  [{c}] {a}  |  {b}")
    print("FALSE ALARMS (different people, flagged):")
    for a, b, c, s in r["alarms"]:
        print(f"  {s:5.1f}  [{c}] {a}  |  {b}")

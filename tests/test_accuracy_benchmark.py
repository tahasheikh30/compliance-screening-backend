"""The name matcher must stay accurate on the labeled benchmarks (see scripts/benchmark_matching.py)."""
import pytest

from app.screening.names import NameScorer
from tests.benchmark import heldout_pairs, name_pairs

THRESHOLD = 85.0


def _rates(positives, negatives):
    missed = [p for p in positives if NameScorer(p[0]).score(p[1]) < THRESHOLD]
    alarms = [n for n in negatives if NameScorer(n[0]).score(n[1]) >= THRESHOLD]
    recall = 1 - len(missed) / len(positives)
    fa = len(alarms) / len(negatives)
    accuracy = 1 - (len(missed) + len(alarms)) / (len(positives) + len(negatives))
    return recall, fa, accuracy


@pytest.mark.parametrize("label,pos,neg", [
    ("main", name_pairs.POSITIVES, name_pairs.NEGATIVES + name_pairs.random_negatives()),
    ("second set", heldout_pairs.POSITIVES, heldout_pairs.NEGATIVES),
])
def test_matcher_accuracy_floors(label, pos, neg):
    recall, fa, accuracy = _rates(pos, neg)
    assert accuracy >= 0.90, f"{label}: accuracy {accuracy:.1%}"
    assert recall >= 0.85, f"{label}: recall {recall:.1%}"        # same person, written differently, is flagged
    assert fa <= 0.05, f"{label}: false alarms {fa:.1%}"          # different people are not


def test_benchmark_pairs_are_well_formed():
    for a, b, *_ in name_pairs.POSITIVES + name_pairs.NEGATIVES + heldout_pairs.POSITIVES + heldout_pairs.NEGATIVES:
        assert a.strip() and b.strip()

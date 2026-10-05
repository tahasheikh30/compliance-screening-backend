"""Tests for the speed related behaviour: bounded match building, the list cache and the database layer."""
import time

from app.screening import engine, loader, parsers
from app.screening.names import NameScorer


def _records(n):
    recs = []
    for i in range(n):
        name = f"ALI RAZA KHAN{i % 7}"
        recs.append(parsers.Record("L", str(i), name, "Individual", "", "", "", "", "", [name], "unsc"))
    return parsers.prepare(recs)


def test_match_records_limit_keeps_the_best_and_reports_the_true_total():
    recs = _records(40)
    sc = NameScorer("Ali Raza Khan0")
    everything, total_all = engine.match_records(sc, recs, 50.0, "")
    top, total = engine.match_records(sc, recs, 50.0, "", limit=5)
    assert total == total_all == len(everything) and len(top) == 5
    assert top == everything[:5]
    assert [m["score"] for m in everything] == sorted((m["score"] for m in everything), reverse=True)


def test_cnic_match_ranks_first_even_with_a_poor_name_score():
    recs = _records(10)
    recs[9].cnic = "4220112345671"
    top, total = engine.match_records(NameScorer("Ali Raza Khan0"), recs, 85.0, "", cnic="4220112345671", limit=3)
    assert top[0]["cnic_match"] is True and top[0]["id"] == "9"


def test_second_screening_starts_no_download_threads_when_cached(fake_sources, monkeypatch):
    monkeypatch.setattr(loader, "LIST_CACHE_TTL_SECONDS", 3600)
    first = loader.load_groups()
    calls = []
    monkeypatch.setattr(loader, "_reload", lambda *a, **k: calls.append(a))
    second = loader.load_groups()
    assert not calls                      # served straight from memory
    assert {k: id(v) for k, v in first.items()} == {k: id(v) for k, v in second.items()}


def test_background_pass_reloads_only_lists_that_are_getting_old(fake_sources, monkeypatch):
    monkeypatch.setattr(loader, "LIST_CACHE_TTL_SECONDS", 100)
    loader.load_group("UNSC")
    loader.load_group("UKSL")
    loader._cache["UNSC"].loaded_at = time.time() - 90   # past 75% of the TTL: due
    uk_before = loader._cache["UKSL"]
    un_before = loader._cache["UNSC"]
    loader._reload("UNSC", 100 * loader.REFRESH_AT)
    loader._reload("UKSL", 100 * loader.REFRESH_AT)
    assert loader._cache["UNSC"] is not un_before     # reloaded
    assert loader._cache["UKSL"] is uk_before         # still young, left alone


def test_a_failed_background_reload_keeps_the_previous_copy(fake_sources, monkeypatch):
    monkeypatch.setattr(loader, "LIST_CACHE_TTL_SECONDS", 100)
    loader.load_group("UKSL")
    old = loader._cache["UKSL"]
    old.loaded_at = time.time() - 90
    fake_sources["fail"].add(loader.UK_URL)
    g = loader._reload("UKSL", 75)
    assert g.error and loader._cache["UKSL"] is old
    assert loader.cache_status()["UKSL"]["error"]     # the Lists page still shows the failure


def test_background_refresh_is_off_when_caching_is_off(monkeypatch):
    monkeypatch.setattr(loader, "LIST_CACHE_TTL_SECONDS", 0)
    assert loader.start_background_refresh() is False


def test_results_are_saved_in_one_call_and_indexed(storage):
    db = storage
    aid = db.insert_applicant("Test Person", None, None, "2026-01-01T00:00:00+00:00", "PENDING")
    row = {"source": "UNSC", "matched_entry": None, "score": None, "status": "CLEAR", "detail": "ok",
           "checked_at": "2026-01-01T00:00:00+00:00", "list_version": None, "records_screened": 3,
           "payload": {"matches": [], "articles": [], "match_count": 0, "lists": []}}
    ids = db.save_screening(aid, "AUTO_CLEAR", 3, [row, {**row, "source": "OFAC"}])
    assert len(ids) == 2
    assert db.get_applicant(aid)["overall_status"] == "AUTO_CLEAR"
    assert [r["source"] for r in db.get_results_for_applicant(aid)] == ["UNSC", "OFAC"]
    with db.pool().connection() as conn:
        names = {r["indexname"] for r in conn.execute("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")}
    assert {"idx_results_applicant", "idx_applicants_user"} <= names


def test_a_failed_save_stores_nothing(storage):
    """Either every row of a screening is saved or none is."""
    import pytest
    db = storage
    aid = db.insert_applicant("Test Person", None, None, "2026-01-01T00:00:00+00:00", "PENDING")
    good = {"source": "UNSC", "matched_entry": None, "score": None, "status": "CLEAR", "detail": "ok",
            "checked_at": "2026-01-01T00:00:00+00:00", "list_version": None, "records_screened": 3, "payload": None}
    bad = {**good, "source": None}                      # violates NOT NULL on the second row
    with pytest.raises(Exception):
        db.save_screening(aid, "AUTO_CLEAR", 3, [good, bad])
    assert db.get_results_for_applicant(aid) == []
    assert db.get_applicant(aid)["overall_status"] == "PENDING"

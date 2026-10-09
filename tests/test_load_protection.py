"""Overload behaviour: fast refusal, a lane that does not starve others, and the cheaper database layer."""
import asyncio
import threading
import time

import pytest

from app import database as db
from app.errors import AppError
from app.loadshed import Lane


def test_lane_refuses_at_once_when_workers_and_queue_are_full():
    async def go():
        lane = Lane(workers=1, max_queue=1)
        gate = threading.Event()
        t1 = asyncio.create_task(lane.run(gate.wait, 5))     # running
        t2 = asyncio.create_task(lane.run(gate.wait, 5))     # queued
        await asyncio.sleep(0.05)
        start = time.monotonic()
        with pytest.raises(AppError) as exc:
            await lane.run(lambda: None)
        assert time.monotonic() - start < 0.1
        assert exc.value.status == 503 and exc.value.code == "SERVER_BUSY"
        assert exc.value.headers.get("Retry-After")
        gate.set()
        await asyncio.gather(t1, t2)
        assert lane.in_flight == 0
        assert await lane.run(lambda: 42) == 42               # recovers once drained
    asyncio.run(go())


def test_lane_releases_its_slot_when_the_work_raises():
    async def go():
        lane = Lane(workers=1, max_queue=0)
        with pytest.raises(ValueError):
            await lane.run(lambda: (_ for _ in ()).throw(ValueError("x")))
        assert lane.in_flight == 0
    asyncio.run(go())


def test_connection_check_skips_a_recently_used_connection(monkeypatch):
    calls = []
    monkeypatch.setattr(db.ConnectionPool, "check_connection", staticmethod(lambda c: calls.append(c)))
    conn = type("C", (), {})()
    db._last_used.pop(conn, None)
    db._check_connection(conn)          # first sighting: tested
    db._check_connection(conn)          # used a moment ago: trusted
    assert len(calls) == 1
    db._last_used[conn] -= 10_000       # sat idle for a long time
    db._check_connection(conn)
    assert len(calls) == 2


def test_audit_chain_stays_valid_with_concurrent_writers():
    errors = []

    def writer(n):
        try:
            for i in range(5):
                db.audit("test.load", target_id=f"{n}-{i}")
        except Exception as exc:     # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert db.audit_verify()["ok"] is True

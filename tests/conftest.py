"""
Shared fixtures. The app reads STORAGE_DIR / API_KEY at import time, so they are
set here BEFORE anything from `app` is imported. Each test then gets its own empty
storage folder by re-pointing the module level paths.

No test touches the network: tests/fixtures.py builds small synthetic copies of
every feed in the real file formats, and the `fake_sources` fixture swaps the
loader's download functions for them.
"""
import os
import sys
import tempfile
from pathlib import Path

_SESSION_DIR = tempfile.mkdtemp(prefix="screening_tests_")
os.environ["STORAGE_DIR"] = _SESSION_DIR
os.environ["API_KEY"] = "test-key"
os.environ["ALLOWED_ORIGINS"] = "http://localhost:5173"
os.environ["LIST_CACHE_TTL_SECONDS"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

API_HEADERS = {"X-API-Key": "test-key"}


@pytest.fixture
def storage(tmp_path, monkeypatch):
    """Isolated DB + evidence folder."""
    from app import config, evidence, main
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "screening.db")
    ev = tmp_path / "evidence"
    ev.mkdir()
    monkeypatch.setattr(config, "EVIDENCE_DIR", ev)
    monkeypatch.setattr(evidence, "EVIDENCE_DIR", ev)
    monkeypatch.setattr(main, "EVIDENCE_DIR", ev)
    return tmp_path


@pytest.fixture
def fake_sources(monkeypatch):
    """Replace every download with synthetic data. Returns a dict the test can edit."""
    from app.screening import loader
    from tests import fixtures as fx

    state = {
        "texts": {
            loader.UN_URL: fx.UN_XML,
            loader.OFAC_SDN_URL: fx.OFAC_SDN_CSV,
            loader.OFAC_SDN_ALT_URL: fx.OFAC_ALT_CSV,
            loader.OFAC_CONS_URL: fx.OFAC_CONS_CSV,
            loader.OFAC_CONS_ALT_URL: fx.OFAC_CONS_ALT_CSV,
            loader.UK_URL: fx.UK_XML,
            "https://www.fia.gov.pk/press-pub": fx.FIA_PAGE_HTML,
            "https://www.fia.gov.pk/ctw": "<html>nothing here</html>",
        },
        "bytes": {"https://www.fia.gov.pk/files/redbook-2026.pdf": fx.make_redbook_pdf()},
        "fail": set(),          # URLs that raise
        "news": fx.NEWS_RSS_CLEAR,
        "news_fail": False,
    }

    def fake_fetch_text(url, read_timeout=0):
        if url in state["fail"]:
            raise ConnectionError(f"simulated outage for {url}")
        if url not in state["texts"]:
            raise ConnectionError(f"no fixture for {url}")
        return state["texts"][url]

    class _Resp:
        def __init__(self, content):
            self.content = content

    def fake_get(url, read_timeout=0, **kw):
        if url in state["fail"]:
            raise ConnectionError(f"simulated outage for {url}")
        if url not in state["bytes"]:
            raise ConnectionError(f"no fixture for {url}")
        return _Resp(state["bytes"][url])

    def fake_news(name):
        if state["news_fail"]:
            raise ConnectionError("simulated news outage")
        return state["news"]

    monkeypatch.setattr(loader, "fetch_text", fake_fetch_text)
    monkeypatch.setattr(loader, "_get", fake_get)
    monkeypatch.setattr(loader, "fetch_news", fake_news)
    loader.clear_cache()
    yield state
    loader.clear_cache()


@pytest.fixture
def client(storage, fake_sources, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    main.limiter.reset()  # slowapi counters are process global
    with TestClient(main.app, raise_server_exceptions=False) as c:
        yield c

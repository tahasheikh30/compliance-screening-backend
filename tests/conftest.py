"""
Shared fixtures. The app reads its settings at import time, so they are set here BEFORE anything
from `app` is imported.

The tests need a PostgreSQL database to talk to, because that is what the app uses. Point
TEST_DATABASE_URL at an empty database whose name contains "test" (default:
postgresql://postgres:postgres@localhost:5432/screening_test), for example

    docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=screening_test postgres:16

The suite refuses to run against anything that is not clearly a test database (and never against
Supabase), because every test empties the tables.

No test touches the network: tests/fixtures.py builds small synthetic copies of every feed in the
real file formats, and the `fake_sources` fixture swaps the loader's download functions for them.
Sign in is real too: tests mint access tokens signed with a test secret, and a few sign them with
a generated key pair to exercise the public key (JWKS) path.
"""
import os
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

_TEST_DB = os.environ.get("TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/screening_test")
_parsed = urlparse(_TEST_DB)
if "supabase" in (_parsed.hostname or "") or "test" not in _parsed.path.lower():
    raise SystemExit(f"Refusing to run: TEST_DATABASE_URL must point at a throwaway database whose name contains "
                     f"'test' (every test empties the tables). Got host={_parsed.hostname!r} db={_parsed.path!r}.")

JWT_SECRET = "test-secret-test-secret-test-secret-test-secret-1234"
SUPABASE_URL = "https://testproject.supabase.co"

os.environ["DATABASE_URL"] = _TEST_DB
os.environ["API_KEY"] = "test-key"              # the secret machine key (scheduled NACTA upload)
os.environ["APP_API_KEY"] = "test-app-key"      # the key the frontend sends on every request
os.environ["SUPABASE_URL"] = SUPABASE_URL
os.environ["SUPABASE_JWT_SECRET"] = JWT_SECRET
os.environ["ALLOWED_ORIGINS"] = "http://localhost:5173"
os.environ["LIST_CACHE_TTL_SECONDS"] = "0"
os.environ["PRELOAD_LISTS"] = "false"
os.environ.pop("REQUIRE_APP_KEY", None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jwt  # noqa: E402
import pytest  # noqa: E402

ADMIN_ID = "00000000-0000-4000-8000-0000000000a1"
USER_ID = "00000000-0000-4000-8000-0000000000b1"
USER2_ID = "00000000-0000-4000-8000-0000000000b2"
PENDING_ID = "00000000-0000-4000-8000-0000000000c1"
REJECTED_ID = "00000000-0000-4000-8000-0000000000d1"
EMAILS = {ADMIN_ID: "admin@example.com", USER_ID: "ana@example.com", USER2_ID: "bilal@example.com",
          PENDING_ID: "new@example.com", REJECTED_ID: "no@example.com"}


def make_token(sub=ADMIN_ID, email=None, *, secret=JWT_SECRET, alg="HS256", headers=None, **claims) -> str:
    """A Supabase style access token."""
    now = int(time.time())
    payload = {"sub": sub, "email": email if email is not None else EMAILS.get(sub, "someone@example.com"),
               "aud": "authenticated", "role": "authenticated", "iss": f"{SUPABASE_URL}/auth/v1",
               "iat": now, "exp": now + 3600, **claims}
    payload = {k: v for k, v in payload.items() if v is not None}
    return jwt.encode(payload, secret, algorithm=alg, headers=headers)


APP_KEY_HEADERS = {"X-API-Key": "test-app-key"}     # what the frontend sends next to the person's token


def bearer(sub=ADMIN_ID, **kw) -> dict:
    """A signed in person, calling through the app: the person's token plus the app's key."""
    return {**APP_KEY_HEADERS, "Authorization": f"Bearer {make_token(sub, **kw)}"}


API_HEADERS = bearer(ADMIN_ID)      # most tests act as the seeded admin
USER_HEADERS = bearer(USER_ID)
USER2_HEADERS = bearer(USER2_ID)
PENDING_HEADERS = bearer(PENDING_ID)
REJECTED_HEADERS = bearer(REJECTED_ID)
SERVICE_HEADERS = {"X-API-Key": "test-key"}

_schema_ready = False


@pytest.fixture
def storage():
    """An empty database (tables emptied, ids restarted) with five known people: an admin, two approved users,
    one pending and one rejected."""
    global _schema_ready
    from app import auth
    from app import database as db
    if not _schema_ready:
        db.init_db()
        _schema_ready = True
    with db.pool().connection() as conn:
        conn.execute("TRUNCATE evidence_files, nacta_list, screening_results, applicants, profiles, audit_log RESTART IDENTITY CASCADE")
        for uid, role, status in ((ADMIN_ID, "admin", "approved"), (USER_ID, "user", "approved"),
                                  (USER2_ID, "user", "approved"), (PENDING_ID, "user", "pending"),
                                  (REJECTED_ID, "user", "rejected")):
            conn.execute("INSERT INTO profiles (id, email, role, status) VALUES (%s, %s, %s, %s)",
                         (uuid.UUID(uid), EMAILS[uid], role, status))
    auth.invalidate_profile()
    return db


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
        "nacta": fx.NACTA_CSV,          # None = nothing uploaded
        "nacta_age_days": 1.0,
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

    real_read_nacta = loader._read_nacta

    def fake_read_nacta():
        from datetime import datetime, timedelta, timezone
        if state["nacta"] == "real":          # exercise the real file store (needs the `storage` fixture)
            return real_read_nacta()
        if state["nacta"] is None:
            raise loader.SourceUnavailable("No NACTA list has been loaded yet. Upload one.")
        when = datetime.now(timezone.utc) - timedelta(days=state["nacta_age_days"])
        return state["nacta"], {"filename": "nacta.csv", "uploaded_at": when.isoformat(), "live": False}

    monkeypatch.setattr(loader, "_read_nacta", fake_read_nacta)
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

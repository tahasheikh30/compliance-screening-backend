"""
Run the backend for a load test: real app, real Postgres, real auth, but no internet.
The sanctions lists are replaced by LARGE synthetic ones (about 30,000 records, like the real lists) and the
news search by a stub that sleeps NEWS_LATENCY seconds (the real one is a network call to Google News, the slowest
part of a screening and the thing that holds a worker thread).

  python lt_server.py <repo_dir> <port>
"""
import os, random, sys, time

repo, port = sys.argv[1], int(sys.argv[2])
os.environ.update({
    "DATABASE_URL": os.environ.get("LT_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/screening_test"),
    "API_KEY": "test-key", "APP_API_KEY": "test-app-key", "SUPABASE_URL": "https://testproject.supabase.co",
    "SUPABASE_JWT_SECRET": "test-secret-test-secret-test-secret-test-secret-1234",
    "ALLOWED_ORIGINS": "http://localhost:5173", "LIST_CACHE_TTL_SECONDS": "3600", "PRELOAD_LISTS": "false",
    "MONITORING": "false",
})
sys.path.insert(0, repo)
NEWS_LATENCY = float(os.environ.get("NEWS_LATENCY", "0.8"))

from app.screening import loader, parsers   # noqa: E402

FIRST = "MUHAMMAD ALI AHMED HASSAN HUSSAIN OMAR USMAN BILAL IMRAN FAISAL TARIQ NADEEM KASHIF SALMAN ZAHID RASHID SAEED NAVEED KAMRAN ADIL FAHAD SHAHID AKRAM JAVED IQBAL IVAN SERGEI DMITRY ALEXANDER VLADIMIR ABDUL RAHMAN KARIM YOUSAF IBRAHIM ISMAIL HAMZA TALHA".split()
LAST = "KHAN SHAH BUTT MALIK SHEIKH RAZA QURESHI SIDDIQUI ANSARI MIRZA CHAUDHRY BAIG RIZVI ZAIDI NAQVI HASHMI PETROV IVANOV SMIRNOV KUZNETSOV POPOV ABBASI AWAN BHATTI DAR GILL JUTT LODHI MEMON NIAZI PARACHA SOOMRO TARAR WAZIR YOUSUFZAI".split()


def make_records(key, label, n, seed):
    rnd = random.Random(seed)
    recs = []
    for i in range(n):
        parts = [rnd.choice(FIRST), rnd.choice(FIRST), rnd.choice(LAST)]
        primary = " ".join(parts)
        alias = " ".join([parts[0], parts[2]])
        recs.append(parsers.Record(label, f"{key}-{i}", primary, "Individual", "SDGT", "1975-01-01", "Pakistan", "2010-01-01",
                                   "remarks " * 5, [primary, alias], key))
    return parsers.prepare(recs)


def build():
    sizes = {"UNSC": 1000, "OFAC": 18000, "UKSL": 6000, "FIA_REDBOOK": 400, "NACTA": 4000}
    for n, (k, size) in enumerate(sizes.items()):
        label = k + " list"
        data = loader.GroupData(k, make_records(k, label, size, n), [{"list": label, "source": "synthetic", "published": "2026-10-01", "records": size}])
        data.warm()
        loader._cache[k] = data
        loader._last_attempt[k] = data


def fake_news(name):
    time.sleep(NEWS_LATENCY)
    return '<?xml version="1.0"?><rss version="2.0"><channel><title>x</title></channel></rss>'


t0 = time.time(); build(); loader.fetch_news = fake_news
print(f"synthetic lists ready in {time.time() - t0:.1f}s", flush=True)

import uvicorn   # noqa: E402
uvicorn.run("app.main:app", host="127.0.0.1", port=port, log_level="warning", server_header=False, proxy_headers=True,
            forwarded_allow_ips="*", app_dir=repo)

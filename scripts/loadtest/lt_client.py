"""
Closed-loop load: N virtual staff users, each acting like a person (think time between actions), plus a health
prober that behaves like Render's health check. Reports per endpoint latency and errors, server RSS and threads.

  python lt_client.py <port> <users> <seconds> [label]
"""
import asyncio, json, random, statistics, sys, time, uuid
import httpx, jwt, psutil, psycopg

port, USERS, DURATION = int(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3])
label = sys.argv[4] if len(sys.argv) > 4 else ""
SECRET = "test-secret-test-secret-test-secret-test-secret-1234"
BASE = f"http://127.0.0.1:{port}"
DB = "postgresql://postgres:postgres@localhost:5432/screening_test"
THINK = (float(__import__('os').environ.get("THINK_MIN", "1.0")), float(__import__('os').environ.get("THINK_MAX", "3.0")))

NAMES = ["Muhammad Ali Khan", "Hassan Raza", "Ivan Petrov", "Ayesha Siddiqui", "Bilal Ahmed Shah", "Omar Farooq Malik", "Sara Iqbal",
         "Usman Tariq Butt", "Zainab Hussain", "Imran Saeed Qureshi", "Fatima Noor", "Kamran Akram Chaudhry"]


SYL = "ba ka li mo ra se ta vu ze no fi gu he jo ku lu ma ne pi qua ro su ti wa xi yo zu".split()


def fresh_name(rnd):
    """A name nothing on the lists is close to: what nearly every real applicant looks like."""
    word = lambda: "".join(rnd.choice(SYL) for _ in range(rnd.randint(2, 4))).capitalize()
    return f"{word()} {word()} {word()}"


def pick_name(rnd):
    # about 1 in 12 applicants shares a name with someone on a list; the rest are clear
    return rnd.choice(NAMES) if rnd.random() < 0.08 else fresh_name(rnd)


def token(uid):
    now = int(time.time())
    return jwt.encode({"sub": uid, "email": f"u{uid[-6:]}@example.com", "aud": "authenticated", "role": "authenticated",
                       "iss": "https://testproject.supabase.co/auth/v1", "iat": now, "exp": now + 7200}, SECRET, algorithm="HS256")


def seed_users(n):
    ids = [str(uuid.UUID(int=0xA0000000 + i)) for i in range(n)]
    with psycopg.connect(DB, autocommit=True) as c:
        c.execute("DELETE FROM profiles WHERE id::text LIKE '00000000-0000-0000-0000-0000a0%%'")
        for i, uid in enumerate(ids):
            c.execute("INSERT INTO profiles (id, email, role, status) VALUES (%s, %s, 'user', 'approved') ON CONFLICT (id) DO UPDATE SET status='approved'", (uid, f"u{uid[-6:]}@example.com"))
    return ids


stats = {}   # name -> list of (latency, status)


def rec(name, t0, status):
    stats.setdefault(name, []).append((time.perf_counter() - t0, status))


async def call(client, name, method, url, **kw):
    t0 = time.perf_counter()
    try:
        r = await client.request(method, url, **kw)
        rec(name, t0, r.status_code)
        return r
    except Exception as exc:
        rec(name, t0, type(exc).__name__)
        return None


async def user(i, uid, stop):
    h = {"Authorization": f"Bearer {token(uid)}", "X-API-Key": "test-app-key"}
    rnd = random.Random(i)
    ids = []
    async with httpx.AsyncClient(base_url=BASE, headers=h, timeout=60) as c:
        await asyncio.sleep(rnd.uniform(0, 3))          # people do not all arrive in the same millisecond
        while time.time() < stop:
            x = rnd.random()
            if x < 0.30:
                await call(c, "GET /api/me", "GET", "/api/me")
            elif x < 0.52:
                r = await call(c, "GET /api/applicants", "GET", "/api/applicants?limit=100")
                if r is not None and r.status_code == 200 and r.json():
                    ids = [a["id"] for a in r.json()][:20]
            elif x < 0.67 and ids:
                await call(c, "GET /api/applicants/{id}", "GET", f"/api/applicants/{rnd.choice(ids)}")
            elif x < 0.82:
                await call(c, "POST /api/screen", "POST", "/api/screen", json={"full_name": pick_name(rnd), "dob": "1980-05-05", "nationality": "Pakistan"})
            elif x < 0.92:
                await call(c, "GET /api/monitoring/status", "GET", "/api/monitoring/status")
            else:
                await call(c, "GET /api/admin/lists", "GET", "/api/admin/lists")
            await asyncio.sleep(rnd.uniform(*THINK))


async def health(stop):
    async with httpx.AsyncClient(base_url=BASE, timeout=10) as c:      # Render checks with a short timeout
        while time.time() < stop:
            await call(c, "GET /api/health (monitor)", "GET", "/api/health")
            await asyncio.sleep(0.25)


def server_proc():
    for p in psutil.process_iter(["cmdline", "name"]):
        cl = " ".join(p.info["cmdline"] or [])
        if "lt_server.py" in cl and str(port) in cl and (p.info["name"] or "").startswith("python"):
            return p


async def sampler(stop, out):
    p = server_proc()
    while time.time() < stop and p:
        try:
            out.append((p.memory_info().rss / 1e6, p.num_threads(), p.cpu_percent(interval=None)))
        except Exception:
            break
        await asyncio.sleep(1)


def pct(a, q):
    a = sorted(a); return a[min(len(a) - 1, int(len(a) * q))]


async def main():
    ids = seed_users(USERS)
    stop = time.time() + DURATION
    samples = []
    t0 = time.time()
    await asyncio.gather(*[user(i, uid, stop) for i, uid in enumerate(ids)], health(stop), sampler(stop, samples))
    el = time.time() - t0
    total = sum(len(v) for v in stats.values())
    print(f"\n=== {label} users={USERS} duration={el:.0f}s requests={total} throughput={total / el:.1f} req/s")
    print(f"{'endpoint':28s} {'n':>6s} {'p50':>8s} {'p95':>8s} {'p99':>8s} {'max':>8s}  status")
    out = {}
    for name in sorted(stats):
        lat = [x[0] for x in stats[name]]
        codes = {}
        for _, s in stats[name]: codes[s] = codes.get(s, 0) + 1
        print(f"{name:28s} {len(lat):6d} {pct(lat, .5) * 1000:7.0f}ms {pct(lat, .95) * 1000:7.0f}ms {pct(lat, .99) * 1000:7.0f}ms {max(lat) * 1000:7.0f}ms  {codes}")
        out[name] = {"n": len(lat), "p50": pct(lat, .5), "p95": pct(lat, .95), "p99": pct(lat, .99), "max": max(lat), "codes": {str(k): v for k, v in codes.items()}}
    if samples:
        print(f"server RSS peak {max(s[0] for s in samples):.0f} MB   threads peak {max(s[1] for s in samples)}   cpu% avg {statistics.mean(s[2] for s in samples):.0f}")
    errs = sum(v for n in stats.values() for _, s in n for v in [1] if not (isinstance(s, int) and s < 400))
    print(f"errors (non 2xx/3xx): {errs}")
    json.dump({"label": label, "users": USERS, "throughput": total / el, "endpoints": out, "rss_peak": max((s[0] for s in samples), default=0), "errors": errs},
              open(f"/tmp/lt-{label or 'run'}-{USERS}.json", "w"))


asyncio.run(main())

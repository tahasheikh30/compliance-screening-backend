# Load test

Runs the real app against a real local Postgres with synthetic sanctions lists and a stubbed news search.
Needs a database with "test" in its name and `psutil`/`httpx` style deps from requirements-dev.

    python scripts/loadtest/lt_server.py . 8104                 # the app
    python scripts/loadtest/lt_client.py 8104 150 40 label      # port, users, seconds, label

To simulate a remote database (Supabase is not on localhost), put a delay proxy in front of Postgres and
point `LT_DATABASE_URL` at it:

    python scripts/loadtest/delay_proxy.py 5433 5432 15        # listen, target, ms each way
    LT_DATABASE_URL=postgresql://postgres:postgres@localhost:5433/screening_test python scripts/loadtest/lt_server.py . 8104

Tunables: `SCREEN_WORKERS`, `SCREEN_MAX_QUEUE`, `DB_POOL_MAX`, `DB_CHECK_IDLE_SECONDS`.

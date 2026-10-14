"""
Exports all tables from the F1 database to CSV files.

Usage (in the relay folder):
    python export_csv.py                 -> all tables
    python export_csv.py f1_laps         -> only the given table(s)

The database login is read from Azure Key Vault (KEYVAULT_NAME in config/config.env),
exactly like the relay does. No username or password is stored in this file.
Output goes to the folder 'exports' next to this script.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "exports"
sys.path.insert(0, str(HERE / "src"))
import secrets_store  # noqa: E402


def read_env(path):
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def connection_params():
    url, where, err = secrets_store.database_url(read_env(HERE / "config" / "config.env"))
    if not url:
        sys.exit(f"No database login: {err}")
    print(f"Login read from {where}")
    import azure_firewall
    azure_firewall.ensure(read_env(HERE / "config" / "config.env"))   # this computer's IP in the Azure firewall
    return {"dsn": url}


LIST_TABLES = """
    SELECT table_name FROM information_schema.tables
    WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
    ORDER BY table_name
"""


def export_sync(conn, tables, copy_fn):
    cur = conn.cursor()
    cur.execute(LIST_TABLES)
    all_tables = [r[0] for r in cur.fetchall()]
    targets = tables or all_tables
    missing = [t for t in targets if t not in all_tables]
    if missing:
        print("Does not exist:", ", ".join(missing))
        print("Available:", ", ".join(all_tables))
        targets = [t for t in targets if t in all_tables]
    for t in targets:
        out = OUT_DIR / f"{t}.csv"
        with open(out, "wb") as f:
            copy_fn(cur, t, f)
        print(f"  {t:<30} -> {out.name}  ({out.stat().st_size / 1024:.0f} KB)")


def run_psycopg3(params, tables):
    import psycopg
    kw = {"conninfo": params["dsn"]} if "dsn" in params else {**params, "sslmode": "require"}
    with psycopg.connect(**kw) as conn:
        def copy(cur, t, f):
            with cur.copy(f'COPY "{t}" TO STDOUT WITH CSV HEADER') as cp:
                for chunk in cp:
                    f.write(chunk)
        export_sync(conn, tables, copy)


def run_psycopg2(params, tables):
    import psycopg2
    conn = psycopg2.connect(params["dsn"]) if "dsn" in params else psycopg2.connect(**params, sslmode="require")
    try:
        def copy(cur, t, f):
            cur.copy_expert(f'COPY "{t}" TO STDOUT WITH CSV HEADER', f)
        export_sync(conn, tables, copy)
    finally:
        conn.close()


def run_asyncpg(params, tables):
    import asyncio
    import asyncpg

    async def main():
        if "dsn" in params:
            conn = await asyncpg.connect(params["dsn"], ssl="require")
        else:
            conn = await asyncpg.connect(host=params["host"], port=int(params["port"]), user=params["user"],
                                         password=params["password"], database=params["dbname"], ssl="require")
        try:
            all_tables = [r[0] for r in await conn.fetch(LIST_TABLES)]
            targets = tables or all_tables
            for t in targets:
                if t not in all_tables:
                    print(f"Does not exist: {t}")
                    continue
                out = OUT_DIR / f"{t}.csv"
                await conn.copy_from_table(t, output=str(out), format="csv", header=True)
                print(f"  {t:<30} -> {out.name}  ({out.stat().st_size / 1024:.0f} KB)")
        finally:
            await conn.close()

    asyncio.run(main())


def main():
    tables = sys.argv[1:]
    OUT_DIR.mkdir(exist_ok=True)
    params = connection_params()
    print("Connecting ...")

    for name, runner in (("psycopg", run_psycopg3), ("psycopg2", run_psycopg2), ("asyncpg", run_asyncpg)):
        try:
            __import__(name)
        except ImportError:
            continue
        runner(params, tables)
        print(f"\nKlaar. Bestanden staan in: {OUT_DIR}")
        return

    print("Geen PostgreSQL-driver gevonden. Installeer er een met:")
    print("    python -m pip install psycopg2-binary")
    sys.exit(1)


if __name__ == "__main__":
    main()

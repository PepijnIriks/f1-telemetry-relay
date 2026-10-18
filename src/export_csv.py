"""
Exports all tables from the F1 database (Azure) to CSV files.

Usage: tools.bat > "Export the database to CSV", or in the relay folder:
    .venv\\Scripts\\python.exe src\\export_csv.py              -> all tables
    .venv\\Scripts\\python.exe src\\export_csv.py f1_laps      -> only the given table(s)

- The database login comes from Azure Key Vault (like the relay); no username or password in this file.
- This computer's IP is put in the Azure firewall, and a stopped database is started first (2-5 minutes).
- Every export gets its own folder: data/exports/2026-10-15_1116/ with one CSV per table and a summary.
- Tables of the earlier collector are TimescaleDB "hypertables": their rows live in hidden chunk tables,
  so every table is exported with COPY (SELECT * FROM table), which includes all chunks.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import azure_firewall  # noqa: E402
import secrets_store  # noqa: E402
from paths import EXPORTS  # noqa: E402

LIST_TABLES = """
    SELECT table_name FROM information_schema.tables
    WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
    ORDER BY table_name
"""


def database_on(cfg):
    """Starts the Azure database when it is stopped and waits until it is ready.
    Returns (server, resource group, subscription) when THIS script started it (so it can turn it off again)."""
    try:
        server, rg, sub, state = azure_firewall.find_server(cfg)
    except Exception as e:
        print(f"Could not check the database state ({e}); trying to connect anyway")
        return None
    if state == "Ready":
        return None
    started = (server, rg, sub) if state == "Stopped" else None
    print(f"Database {server} is {state}. Starting it (usually 2 to 5 minutes)...")
    if state == "Stopped":
        secrets_store.az(["postgres", "flexible-server", "start", "--resource-group", rg, "--name", server,
                          "--subscription", sub, "--no-wait"], timeout=120)
    for _ in range(80):                       # at most ~13 minutes
        time.sleep(10)
        rc, out, _err = secrets_store.az(["postgres", "flexible-server", "show", "--resource-group", rg,
                                          "--name", server, "--subscription", sub, "--query", "state",
                                          "-o", "tsv"])
        state = out.strip() if rc == 0 else state
        print(f"  status: {state}")
        if state == "Ready":
            return started
    print("The database did not become ready in time; trying to connect anyway")
    return started


def main():
    wanted = sys.argv[1:]
    cfg = azure_firewall.read_cfg()
    url, where, err = secrets_store.database_url(cfg)
    if not url:
        sys.exit(f"No database login: {err}")
    print(f"Login read from {where}")
    started = database_on(cfg)
    azure_firewall.ensure(cfg)

    import psycopg2
    out_dir = EXPORTS / time.strftime("%Y-%m-%d_%H%M")
    out_dir.mkdir(parents=True, exist_ok=True)
    print("Connecting...")
    conn = None
    for attempt in range(6):                  # right after starting, the firewall/database can need a moment
        try:
            conn = psycopg2.connect(url, connect_timeout=20)
            break
        except Exception as e:
            print(f"  not reachable yet ({str(e).strip()[:120]}), retrying in 20 s...")
            time.sleep(20)
    if conn is None:
        sys.exit("Could not connect to the database.")

    summary, t_start = {}, time.time()
    with conn, conn.cursor() as cur:
        cur.execute(LIST_TABLES)
        tables = [r[0] for r in cur.fetchall()]
        targets = [t for t in tables if not wanted or t in wanted]
        for t in wanted:
            if t not in tables:
                print(f"Does not exist: {t}")
        print(f"Exporting {len(targets)} table(s) to {out_dir}\n")
        for t in targets:
            t0 = time.time()
            path = out_dir / f"{t}.csv"
            with open(path, "wb") as f:
                cur.copy_expert(f'COPY (SELECT * FROM public."{t}") TO STDOUT WITH CSV HEADER', f)
            size = path.stat().st_size
            rows = max(0, sum(1 for _ in open(path, "rb")) - 1) if size < 50 * 1024 ** 2 else None
            summary[t] = {"bytes": size, "rows": rows}
            label = f"{rows:,} rows" if rows is not None else "large file"
            print(f"  {t:<34} {size / 1024 ** 2:9.1f} MB   {label:<16} ({time.time() - t0:.0f} s)")
    conn.close()
    (out_dir / "export_summary.json").write_text(json.dumps(
        {"exported_at": time.strftime("%Y-%m-%d %H:%M:%S"), "tables": summary}, indent=2), encoding="utf-8")
    total = sum(v["bytes"] for v in summary.values())
    print(f"\nDone in {time.time() - t_start:.0f} s: {len(summary)} tables, {total / 1024 ** 2:.0f} MB in total.")
    print(f"Folder: {out_dir}")
    if started:                                   # it was off before the export: turn it off again (saves costs)
        server, rg, sub = started
        print(f"Turning database {server} off again (it was off before the export).")
        secrets_store.az(["postgres", "flexible-server", "stop", "--resource-group", rg, "--name", server,
                          "--subscription", sub, "--no-wait"], timeout=120)
    if sys.platform == "win32":
        try:
            subprocess.Popen(["explorer", str(out_dir)])
        except Exception:
            pass


if __name__ == "__main__":
    main()

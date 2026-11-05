"""
Exports all tables from the F1 database (Educloud VM) to CSV files.

Usage: tools.bat > "Export the database to CSV", or in the relay folder:
    .venv\\Scripts\\python.exe src\\export_csv.py              -> all tables
    .venv\\Scripts\\python.exe src\\export_csv.py f1_laps      -> only the given table(s)

- The database login comes from the Windows Credential Manager (like the relay); no password in this file.
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
import secrets_store  # noqa: E402
from paths import EXPORTS, read_config  # noqa: E402

LIST_TABLES = """
    SELECT table_name FROM information_schema.tables
    WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
    ORDER BY table_name
"""


def main():
    wanted = sys.argv[1:]
    cfg = read_config()
    url, where, err = secrets_store.database_url(cfg)
    if not url:
        sys.exit(f"No database login: {err}")
    print(f"Login read from {where}")

    import psycopg2
    out_dir = EXPORTS / time.strftime("%Y-%m-%d_%H%M")
    out_dir.mkdir(parents=True, exist_ok=True)
    print("Connecting...")
    conn = None
    for attempt in range(3):                  # a short network hiccup: try a few times
        try:
            conn = psycopg2.connect(url, connect_timeout=20)
            break
        except Exception as e:
            print(f"  not reachable yet ({str(e).strip()[:120]}), retrying in 10 s...")
            time.sleep(10)
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
    if sys.platform == "win32":
        try:
            subprocess.Popen(["explorer", str(out_dir)])
        except Exception:
            pass


if __name__ == "__main__":
    main()

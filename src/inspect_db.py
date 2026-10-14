"""
Shows what is in the Azure database: all tables, columns and row counts.
Usage: double-click inspect-db.bat (reads the database login from Azure Key Vault, see config/config.env)
"""
import sys
from pathlib import Path

import psycopg2

import secrets_store

ROOT = Path(__file__).resolve().parent.parent
cfg = {}
path = ROOT / "config" / "config.env"
if path.exists():
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"')
url, where, err = secrets_store.database_url(cfg)
if not url:
    sys.exit(f"No database login: {err}")
print(f"Login read from {where}")

import azure_firewall  # noqa: E402
azure_firewall.ensure(cfg)        # make sure this computer's IP is allowed in the Azure firewall

try:
    conn = psycopg2.connect(url, connect_timeout=10)
except Exception as e:
    sys.exit(f"Connecting failed: {str(e).strip()}")

with conn, conn.cursor() as cur:
    cur.execute("SELECT version()")
    print("Connected:", cur.fetchone()[0][:60])
    cur.execute("""SELECT table_schema, table_name FROM information_schema.tables
                   WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
                     AND table_schema NOT LIKE '\\_timescaledb%' AND table_schema NOT LIKE 'timescaledb\\_%'
                   ORDER BY table_schema, table_name""")
    tables = cur.fetchall()
    if not tables:
        print("\nThe database is empty: there are no tables yet.")
    for schema, name in tables:
        cur.execute("SET LOCAL statement_timeout = 20000")
        try:
            cur.execute(f'SELECT count(*) FROM "{schema}"."{name}"')
            rows = f"{cur.fetchone()[0]:,}"
        except Exception:
            conn.rollback()
            rows = "(counting took too long)"
        cur.execute("""SELECT column_name, data_type FROM information_schema.columns
                       WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position""", (schema, name))
        cols = ", ".join(f"{c} ({t})" for c, t in cur.fetchall())
        mark = "  <- from the new relay" if name.startswith("f1_") else ""
        print(f"\n[{schema}.{name}]  {rows} rows{mark}")
        print(f"   columns: {cols}")
conn.close()
print("\nDone.")

"""
Makes (or renews) a READ-ONLY database account for the other group's dashboard.

The account "f1_reader" can read every table (also tables made later), but can never change or delete
anything. Its login is stored in the Windows Credential Manager of this PC (not in a file, not on GitHub) and
shown once, so it can be given to the other group privately.

Usage: tools.bat > "Read-only account for the dashboard group". Running it again makes a new password.
"""
import secrets
import string
import sys
from pathlib import Path
from urllib.parse import quote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import secrets_store as ss  # noqa: E402
from paths import read_config  # noqa: E402

ROLE = "f1_reader"


def main():
    url, where, err = ss.database_url(read_config())
    if not url:
        sys.exit(f"No database login: {err}")

    import psycopg2
    from psycopg2 import sql
    try:
        conn = psycopg2.connect(url, connect_timeout=15)
    except Exception as e:
        sys.exit(f"Connecting failed (is the Educloud VM on?): {str(e).strip()[:200]}")

    pw = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32))
    u = urlparse(url)
    dbname = u.path.lstrip("/") or "postgres"
    with conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (ROLE,))
        exists = cur.fetchone() is not None
        if exists:
            cur.execute(sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD %s").format(sql.Identifier(ROLE)), (pw,))
        else:
            cur.execute(sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD %s").format(sql.Identifier(ROLE)), (pw,))
        r = sql.Identifier(ROLE)
        cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(dbname), r))
        cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(r))
        cur.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(r))
        # tables the relay makes later are readable too
        cur.execute(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {}").format(r))
        # and nothing else: no writing, no new tables
        cur.execute(sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(r))
    conn.close()

    ro_url = f"postgresql://{ROLE}:{quote(pw, safe='')}@{u.hostname}:{u.port or 5432}/{dbname}?sslmode=require"
    err = ss.store(ro_url, ss.READONLY)

    print(f"\nRead-only account '{ROLE}' {'renewed (new password)' if exists else 'made'}.")
    if err:
        print(f"Note: {err}. Copy the login below now, it is not saved anywhere.")
    else:
        print("Its login is also kept in the Windows Credential Manager of this PC ('f1-telemetry-relay').")
    print("\nLogin for the dashboard group (send it to them privately: not in a public chat or repo):\n")
    print(f"  Host:      {u.hostname}")
    print(f"  Port:      {u.port or 5432}")
    print(f"  Database:  {dbname}")
    print(f"  User:      {ROLE}")
    print(f"  Password:  {pw}")
    print("  SSL:       required (sslmode=require)")
    print(f"\n  {ro_url}")
    print("\nThey can connect from anywhere: the VM allows this account from every address, encrypted only.")
    print("Run this again to make a new password (the old one then stops working).")

if __name__ == "__main__":
    main()

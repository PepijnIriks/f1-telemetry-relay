"""
Makes (or renews) a READ-ONLY database account for the other group's dashboard.

The account "f1_reader" can read every table (also tables made later), but can never change or delete
anything. Its login is stored in Azure Key Vault as secret "f1-readonly-url"; nothing is written to a file.

Usage: double-click create-readonly-user.bat (the database must be on: start start.bat first if needed).
"""
import os
import secrets
import string
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import secrets_store as ss  # noqa: E402
import azure_firewall  # noqa: E402

ROLE = "f1_reader"
SECRET = "f1-readonly-url"


def main():
    cfg = azure_firewall.read_cfg()
    url, where, err = ss.database_url(cfg)
    if not url:
        sys.exit(f"No database login: {err}")
    vault = cfg.get("KEYVAULT_NAME", "").strip()
    if not vault:
        sys.exit("KEYVAULT_NAME is empty in config/config.env (run setup-keyvault.bat first)")
    azure_firewall.ensure(cfg)

    import psycopg2
    from psycopg2 import sql
    try:
        conn = psycopg2.connect(url, connect_timeout=15)
    except Exception as e:
        sys.exit(f"Connecting failed (is the database on? start start.bat first): {str(e).strip()[:200]}")

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
    fd, tmp = tempfile.mkstemp(prefix="f1ro-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(ro_url)
        rc, _o, err = ss.az(["keyvault", "secret", "set", "--vault-name", vault, "--name", SECRET,
                             "--file", tmp, "--encoding", "utf-8", "-o", "none"])
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    if rc != 0:
        sys.exit(f"The account was {'renewed' if exists else 'made'}, but storing it in Key Vault failed: {err[:200]}\n"
                 "Run this again (it makes a new password).")

    print(f"\nRead-only account '{ROLE}' {'renewed (new password)' if exists else 'made'}.")
    print(f"Its login is in Azure Key Vault '{vault}', secret '{SECRET}'.")
    print("\nTo give it to the other group:")
    print("  - show it (PowerShell, this PC):")
    print(f"      az keyvault secret show --vault-name {vault} --name {SECRET} --query value -o tsv")
    print("    and send it to them privately (not in a public chat or repo), or")
    print("  - give their Azure account the role 'Key Vault Secrets User' on the vault, then they read it themselves.")
    print("They also need their own IP in the database firewall (Azure portal > f1db > Networking).")


if __name__ == "__main__":
    main()

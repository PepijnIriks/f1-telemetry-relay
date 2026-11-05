"""
One-time setup on a PC: puts the login of the database on the Educloud VM in the Windows Credential Manager,
so no username or password is stored in the code, in config/config.env or on GitHub.

Usage: start.bat runs it the first time on a computer; later tools.bat > Database login.

What it does:
  1. Asks for the login line that server/install-educloud.sh showed on the VM (hidden while you paste it),
     or, if you press Enter, for the address and password separately.
  2. Tests it: connects to the database (encrypted) and checks that the relay may make its tables.
  3. Stores it in the Windows Credential Manager (only when the test worked, unless you say otherwise).
  4. Removes any DATABASE_URL line from config/config.env (an old place with the password in plain text).
"""
import getpass
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import secrets_store as ss  # noqa: E402
from paths import CONFIG_FILE  # noqa: E402


def ask(text, default=""):
    try:
        v = input(f"{text}{f' [{default}]' if default else ''}: ").strip()
    except EOFError:
        v = ""
    return v or default


def normalise(url):
    """Accepts postgres:// or postgresql://, and always asks for an encrypted connection."""
    url = url.strip().strip('"').strip("'")
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if not url.startswith("postgresql://"):
        return ""
    u = urlparse(url)
    if not u.hostname or not u.username or u.password is None:
        return ""
    q = parse_qs(u.query)
    if q.get("sslmode", [""])[0] not in ("require", "verify-ca", "verify-full"):
        q["sslmode"] = ["require"]
    return u._replace(query=urlencode({k: v[0] for k, v in q.items()})).geturl()


def build_url(host, port, db, user, password):
    return (f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}/"
            f"{quote(db, safe='')}?sslmode=require")


def test(url):
    """Returns '' when the relay can connect and make tables, otherwise what went wrong."""
    import psycopg2
    try:
        conn = psycopg2.connect(url, connect_timeout=10)
    except Exception as e:
        msg = str(e).strip()
        low = msg.lower()
        if "password authentication failed" in low:
            return "wrong password or username"
        if "timeout" in low or "timed out" in low or "could not connect" in low or "connection refused" in low:
            return ("the VM can't be reached on port 5432: check the address, that the VM is on, and that "
                    "port 5432 is open in Educloud")
        if "could not translate host" in low:
            return "the address of the VM was not found"
        if "no pg_hba.conf entry" in low:
            return "the server does not allow this user or database (run install-educloud.sh on the VM)"
        return msg.replace("\n", " ")[:200]
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT version()")
            version = cur.fetchone()[0].split(" on ")[0]
            cur.execute("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()")
            row = cur.fetchone()
            encrypted = bool(row and row[0])
            cur.execute("SELECT has_schema_privilege('public', 'CREATE')")
            can_create = cur.fetchone()[0]
        print(f"  Connected: {version}, {'encrypted' if encrypted else 'NOT encrypted'}")
        if not can_create:
            return "connected, but this user may not make tables in the database"
        return ""
    finally:
        conn.close()


def remove_plain_text_login():
    """An older version kept DATABASE_URL (with password) in config/config.env: take it out."""
    try:
        lines = CONFIG_FILE.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        return
    kept = [ln for ln in lines if not re.match(r"\s*DATABASE_URL\s*=", ln)]
    if len(kept) != len(lines):
        CONFIG_FILE.write_text("".join(kept), encoding="utf-8")
        print("  Removed the old DATABASE_URL line (with password) from config/config.env.")


def main():
    print()
    print("  Database login for this PC")
    print("  ==========================")
    current, _err = ss.get(ss.DATABASE)
    if current:
        u = urlparse(current)
        print(f"  Stored now: user '{u.username}' on {u.hostname}:{u.port or 5432}/{u.path.lstrip('/')}")
        if ask("  Replace it? (y/n)", "n").lower() not in ("y", "j", "yes", "ja"):
            print("  Kept.")
            return 0
    print()
    print("  On the Educloud VM, show the login line with:   sudo bash install-educloud.sh --show")
    print("  It starts with postgresql://f1_relay:...")
    print()
    print("  Paste the whole line here (right mouse click pastes; you will not see it), then press Enter.")
    print("  Or press only Enter to type the address and password separately.")
    url = normalise(getpass.getpass("  Login line: "))
    if not url:
        print()
        host = ask("  Address of the VM (IP address or name)")
        if not host:
            print("  Nothing entered: nothing changed.")
            return 1
        port = ask("  Port", "5432")
        db = ask("  Database", "f1")
        user = ask("  User", "f1_relay")
        password = getpass.getpass("  Password (you will not see it): ").strip()
        if not password:
            print("  No password entered: nothing changed.")
            return 1
        url = build_url(host, port, db, user, password)

    print("\n  Testing the connection...")
    problem = test(url)
    if problem:
        print(f"  Problem: {problem}")
        if ask("  Store this login anyway? (y/n)", "n").lower() not in ("y", "j", "yes", "ja"):
            print("  Nothing stored. Run tools.bat > Database login again when it is fixed.")
            return 1
    err = ss.store(url, ss.DATABASE)
    if err:
        print(f"  {err}")
        return 1
    remove_plain_text_login()
    print("  Stored in the Windows Credential Manager of this PC (not in a file, not on GitHub).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

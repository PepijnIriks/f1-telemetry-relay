"""
Keeps the database login (connection string with username and password) in the Windows Credential Manager
of this PC, so no username or password is ever stored in the code, in config/config.env or on GitHub.

    Windows: Control Panel > Credential Manager > Windows Credentials > "f1-telemetry-relay"

The login is put there once with tools.bat > Database login (start.bat asks for it the first time).
It is the line that server/install-educloud.sh shows on the Educloud VM:
    postgresql://f1_relay:<password>@<address of the VM>:5432/f1?sslmode=require

For testing you can still pass a connection string in the environment variable DATABASE_URL
(never in a file that goes to GitHub).
"""
import os

SERVICE = "f1-telemetry-relay"
DATABASE = "database-url"         # the relay's own login (f1_relay)
READONLY = "readonly-url"         # the read-only login for the dashboard group (f1_reader)


def _keyring():
    try:
        import keyring
        return keyring
    except Exception:
        return None


def get(name=DATABASE):
    """Returns (value, error). value is '' when nothing is stored."""
    kr = _keyring()
    if kr is None:
        return "", "the Python package 'keyring' is missing (start.bat installs it)"
    try:
        return kr.get_password(SERVICE, name) or "", ""
    except Exception as e:
        return "", f"the Windows Credential Manager could not be read ({str(e)[:150]})"


def store(value, name=DATABASE):
    """Stores a login in the Windows Credential Manager. Returns '' or an error message."""
    kr = _keyring()
    if kr is None:
        return "the Python package 'keyring' is missing (start.bat installs it)"
    try:
        kr.set_password(SERVICE, name, value)
        return ""
    except Exception as e:
        return f"storing it in the Windows Credential Manager failed ({str(e)[:150]})"


def remove(name=DATABASE):
    kr = _keyring()
    try:
        kr.delete_password(SERVICE, name)
    except Exception:
        pass


def database_url(cfg, interactive=True):
    """Returns (url, where_from, error). url is '' when there is no database to use.
    'interactive' is kept for older callers; nothing here opens a window or a browser."""
    env = os.environ.get("DATABASE_URL", "").strip()
    if env:
        return env, "environment variable DATABASE_URL", ""

    url, err = get(DATABASE)
    if url:
        return url, "the Windows Credential Manager", ""

    legacy = cfg.get("DATABASE_URL", "").strip()
    if legacy:
        print("[SECRETS] WARNING: DATABASE_URL with a password is in config/config.env. Run tools.bat > "
              "Database login to move it to the Windows Credential Manager, then remove it from the file.",
              flush=True)
        return legacy, "config.env (not safe)", ""
    if err:
        return "", "", f"No database login: {err}"
    return "", "", "No database login on this PC yet: run tools.bat > Database login (start.bat asks for it)"


if __name__ == "__main__":
    # "python src/secrets_store.py --check": exit code 0 when the login can be read (used by start.bat)
    import sys
    from paths import read_config
    url, where, err = database_url(read_config())
    print(f"[SECRETS] OK: database login found in {where}" if url else f"[SECRETS] {err}")
    sys.exit(0 if url else 1)

"""
Gets the database connection string (with username and password) from Azure Key Vault,
so no username or password is ever stored in the code or in config/config.env.

config/config.env only says WHERE the secret is:
    KEYVAULT_NAME=<name of your key vault>
    KEYVAULT_SECRET=f1-database-url

The secret is read with the Azure CLI ("az"), with the account you are logged in with (az login).
That account needs the role "Key Vault Secrets User" (read) or "Key Vault Secrets Officer"
(read + write) on the vault. start.bat sets all of this up the first time (or tools.bat > Key Vault setup).

For testing on your own machine you can still pass a connection string in the environment
variable DATABASE_URL (never in a file that goes to GitHub).
"""
import os
import shutil
import subprocess

DEFAULT_SECRET = "f1-database-url"


def az_path():
    return shutil.which("az") or shutil.which("az.cmd")


def az(args, timeout=60):
    """Runs an Azure CLI command. Returns (returncode, stdout, stderr)."""
    exe = az_path()
    if not exe:
        return 127, "", "Azure CLI not found"
    try:
        p = subprocess.run([exe] + args + ["--only-show-errors"], capture_output=True, text=True,
                           timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", "time-out"
    except Exception as e:
        return 1, "", str(e)


def ensure_login(interactive=True):
    rc, _o, _e = az(["account", "show", "-o", "none"])
    if rc == 0 or not interactive:
        return rc == 0
    print("[SECRETS] Not logged in to Azure. A browser opens now: log in with your Fontys account.", flush=True)
    try:
        subprocess.run([az_path(), "login", "--only-show-errors", "-o", "none"], timeout=600)
    except Exception:
        pass
    rc, _o, _e = az(["account", "show", "-o", "none"])
    return rc == 0


def explain(err, vault):
    low = err.lower()
    if "forbidden" in low or "not authorized" in low or "does not have secrets get permission" in low:
        return (f"your Azure account may not read secrets from key vault '{vault}'. Give it the role "
                f"'Key Vault Secrets User' on the vault (or run tools.bat > Key Vault setup again)")
    if "secretnotfound" in low or "was not found in this key vault" in low:
        return f"the secret does not exist in key vault '{vault}' yet: run tools.bat > Key Vault setup"
    if "getaddrinfo" in low or "failed to resolve" in low or "vaultnotfound" in low or "not found" in low:
        return f"key vault '{vault}' was not found (check KEYVAULT_NAME in config/config.env)"
    if "az login" in low or "please run" in low:
        return "not logged in to Azure: run 'az login' once"
    return err.replace("\n", " ")[:220]


def database_url(cfg, interactive=True):
    """Returns (url, where_from, error). url is '' when there is no database to use."""
    env = os.environ.get("DATABASE_URL", "").strip()
    if env:
        return env, "environment variable DATABASE_URL", ""

    vault = cfg.get("KEYVAULT_NAME", "").strip()
    secret = cfg.get("KEYVAULT_SECRET", "").strip() or DEFAULT_SECRET
    if not vault:
        legacy = cfg.get("DATABASE_URL", "").strip()
        if legacy:
            print("[SECRETS] WARNING: DATABASE_URL with a password is still in config/config.env. "
                  "Run tools.bat > Key Vault setup to move it to Azure Key Vault.", flush=True)
            return legacy, "config.env (not safe)", ""
        return "", "", "No KEYVAULT_NAME in config/config.env (start.bat sets it up, or tools.bat > Key Vault setup)"

    if not az_path():
        return "", "", ("Azure CLI not found: install it once (PowerShell): "
                        "winget install -e --id Microsoft.AzureCLI")
    if not ensure_login(interactive):
        return "", "", "Not logged in to Azure (run 'az login')"
    rc, out, err = az(["keyvault", "secret", "show", "--vault-name", vault, "--name", secret,
                       "--query", "value", "-o", "tsv"])
    if rc != 0 or not out:
        return "", "", "Key Vault: " + explain(err or "empty secret", vault)
    return out, f"Azure Key Vault '{vault}'", ""


if __name__ == "__main__":
    # "python src/secrets_store.py --check": exit code 0 when the login can be read (used by start.bat)
    import sys
    from paths import read_config
    url, where, err = database_url(read_config())
    print(f"[SECRETS] OK: database login found in {where}" if url else f"[SECRETS] {err}")
    sys.exit(0 if url else 1)

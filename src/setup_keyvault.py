"""
One-time setup: puts the database login in Azure Key Vault, so no username or password is
stored in the code or in config/config.env.

Usage: double-click setup-keyvault.bat (needs the Azure CLI and 'az login').

What it does:
  1. Logs in to Azure (browser) if needed.
  2. Creates the key vault if it does not exist yet (same resource group and region as the database)
     and gives YOUR account the role "Key Vault Secrets Officer" on it.
  3. Looks up the database server and its admin username in Azure (you never type the username).
  4. Asks for the password (hidden) OR makes a new strong password and sets it on the server.
  5. Stores the connection string as secret "f1-database-url" in the key vault.
  6. Writes KEYVAULT_NAME in config/config.env and removes any DATABASE_URL line from it.
"""
import getpass
import json
import os
import re
import secrets
import string
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import secrets_store as ss  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CFG_FILE = ROOT / "config" / "config.env"


def read_cfg():
    cfg = {}
    if CFG_FILE.exists():
        for line in CFG_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"')
    return cfg


def ask(text, default=""):
    v = input(f"{text}{f' [{default}]' if default else ''}: ").strip()
    return v or default


def az_json(args, timeout=120):
    rc, out, err = ss.az(args + ["-o", "json"], timeout=timeout)
    if rc != 0:
        return None, err
    try:
        return (json.loads(out) if out else None), ""
    except ValueError:
        return None, out


def fail(msg):
    print(f"\nSTOPPED: {msg}")
    sys.exit(1)


def write_cfg(vault, secret):
    lines = CFG_FILE.read_text(encoding="utf-8").splitlines() if CFG_FILE.exists() else []
    out, done = [], set()
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.strip().startswith("#") else ""
        if key == "DATABASE_URL":
            continue                                   # the password must not stay in this file
        if key == "KEYVAULT_NAME":
            line, _ = f"KEYVAULT_NAME={vault}", done.add(key)
        elif key == "KEYVAULT_SECRET":
            line, _ = f"KEYVAULT_SECRET={secret}", done.add(key)
        out.append(line)
    if "KEYVAULT_NAME" not in done:
        out += ["", "# Azure Key Vault with the database login (made by setup-keyvault.bat)", f"KEYVAULT_NAME={vault}"]
    if "KEYVAULT_SECRET" not in done:
        out.append(f"KEYVAULT_SECRET={secret}")
    CFG_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")


def main():
    print("=" * 62)
    print(" F1 relay - put the database login in Azure Key Vault")
    print("=" * 62)
    cfg = read_cfg()
    if not ss.az_path():
        fail("Azure CLI not found. Install it once (PowerShell): winget install -e --id Microsoft.AzureCLI\n"
             "         then open a new window and run this again.")
    if not ss.ensure_login():
        fail("not logged in to Azure (az login did not work)")
    sub = cfg.get("AZURE_SUBSCRIPTION", "").strip()
    if sub:
        ss.az(["account", "set", "--subscription", sub])
    acct, _ = az_json(["account", "show"])
    print(f"Azure account: {acct.get('user', {}).get('name', '?')}  subscription: {acct.get('name', '?')}")

    # ---- 1. the database server (name, resource group, region, admin user)
    servers, err = az_json(["postgres", "flexible-server", "list"])
    if servers is None:
        fail(f"could not list the database servers: {err}")
    if not servers:
        fail("no Azure Database for PostgreSQL server found in this subscription")
    want = cfg.get("AZURE_SERVER_NAME", "").strip()
    if not want:
        names = [s["name"] for s in servers]
        want = names[0] if len(names) == 1 else ask(f"Which database server? ({', '.join(names)})", names[0])
    srv = next((s for s in servers if s["name"].lower() == want.lower()), None)
    if not srv:
        fail(f"server '{want}' not found")
    rg = cfg.get("AZURE_RESOURCE_GROUP", "").strip() or srv["resourceGroup"]
    host, user, region = srv["fullyQualifiedDomainName"], srv["administratorLogin"], srv["location"]
    print(f"Database server: {srv['name']}  (resource group {rg}, region {region}, status {srv.get('state')})")

    # ---- 2. the key vault
    vault = cfg.get("KEYVAULT_NAME", "").strip()
    if not vault:
        suggestion = f"f1-relay-{secrets.token_hex(3)}"     # vault names are unique in all of Azure
        vault = ask("Name for the key vault (3-24 letters/digits/dashes, unique in Azure)", suggestion)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{1,22}[A-Za-z0-9]", vault):
        fail("invalid key vault name")
    secret_name = cfg.get("KEYVAULT_SECRET", "").strip() or ss.DEFAULT_SECRET

    kv, _ = az_json(["keyvault", "show", "--name", vault])
    if kv is None:
        state, _ = az_json(["provider", "show", "--namespace", "Microsoft.KeyVault",
                            "--query", "registrationState"])
        if state != "Registered":
            print("Turning on Key Vault for this subscription (once, can take a minute)...")
            ss.az(["provider", "register", "--namespace", "Microsoft.KeyVault", "--wait"], timeout=600)
        kv_region = cfg.get("KEYVAULT_REGION", "").strip() or region
        print(f"Creating key vault '{vault}' in {kv_region}...")
        kv, err = az_json(["keyvault", "create", "--name", vault, "--resource-group", rg,
                           "--location", kv_region, "--enable-rbac-authorization", "true"], timeout=300)
        if kv is None:
            fail(f"creating the key vault failed: {err[:400]}\n"
                 "         Name taken? Put another name in KEYVAULT_NAME (config.env) and run again.\n"
                 "         Region not allowed? Put e.g. KEYVAULT_REGION=swedencentral in config.env.")
    else:
        print(f"Key vault '{vault}' already exists")

    # Your own account gets the right to read and write secrets in this vault
    me, _ = az_json(["ad", "signed-in-user", "show", "--query", "id"])
    if not me:
        print("Note: could not look up your own account id; if storing fails, give yourself the role")
        print("      'Key Vault Secrets Officer' on the vault in the portal and run this again.")
    rbac = kv.get("properties", {}).get("enableRbacAuthorization")
    if rbac and me:
        rc, _o, err = ss.az(["role", "assignment", "create", "--role", "Key Vault Secrets Officer",
                             "--assignee-object-id", me, "--assignee-principal-type", "User",
                             "--scope", kv["id"], "-o", "none"])
        if rc != 0 and "exists" not in err.lower():
            # e.g. only "Contributor" on the school subscription: then roles can't be given out,
            # but access policies on the vault can. Switch the vault to access policies.
            print("Could not give out an Azure role (normal with only Contributor rights); "
                  "using a key vault access policy instead.")
            ss.az(["keyvault", "update", "--name", vault, "--resource-group", rg,
                   "--enable-rbac-authorization", "false", "-o", "none"], timeout=180)
            rbac = False
    if not rbac and me:
        rc, _o, err = ss.az(["keyvault", "set-policy", "--name", vault, "--object-id", me,
                             "--secret-permissions", "get", "list", "set", "-o", "none"], timeout=180)
        if rc != 0:
            print(f"Note: could not set the access policy: {err[:200]}")

    # ---- 3. the password
    print("\nPassword of the database admin:")
    print("  1 = I type the current password (hidden)")
    print("  2 = make a NEW strong password and set it on the server (recommended:")
    print("      the old one was stored in a text file). Other scripts that use the old")
    print("      password stop working, they should read Key Vault too.")
    choice = ask("Choice", "1")
    if choice == "2":
        alphabet = string.ascii_letters + string.digits + "-_.~"
        pw = "".join(secrets.choice(alphabet) for _ in range(32)) + "aA1"
        print("Setting the new password on the server (takes a minute or two)...")
        rc, _o, err = ss.az(["postgres", "flexible-server", "update", "--resource-group", rg,
                             "--name", srv["name"], "--admin-password", pw, "-o", "none"], timeout=900)
        if rc != 0:
            fail(f"setting the new password failed: {err[:300]}")
    else:
        legacy = cfg.get("DATABASE_URL", "")
        pw = ""
        if legacy:
            use = ask("A DATABASE_URL is still in config.env. Use the password from it? (y/n)", "y")
            if use.lower().startswith("y"):
                from urllib.parse import unquote, urlparse
                pw = unquote(urlparse(legacy).password or "")
        if not pw:
            pw = getpass.getpass("Password for the database admin (typing is hidden): ")
        if not pw:
            fail("no password given")

    dbname = cfg.get("DB_NAME", "").strip() or "postgres"
    url = (f"postgresql://{quote(user, safe='')}:{quote(pw, safe='')}@{host}:5432/{dbname}"
           f"?sslmode=require")

    # ---- 4. store it (through a temporary file, so the password never appears on a command line)
    fd, tmp = tempfile.mkstemp(prefix="f1kv-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(url)
        print(f"Storing the secret '{secret_name}' in key vault '{vault}'...")
        for attempt in range(20):           # a new role can take a few minutes before it works
            rc, _o, err = ss.az(["keyvault", "secret", "set", "--vault-name", vault, "--name", secret_name,
                                 "--file", tmp, "--encoding", "utf-8", "-o", "none"])
            if rc == 0:
                break
            if "forbidden" not in err.lower() and "not authorized" not in err.lower():
                fail(f"storing the secret failed: {err[:300]}")
            print("  waiting for the new permission to become active...")
            time.sleep(15)
        else:
            fail("your account still has no permission to write secrets. Wait 5 minutes and run this again.")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    # ---- 5. check that it can be read back, then clean up config.env
    got, _w, err = ss.database_url({"KEYVAULT_NAME": vault, "KEYVAULT_SECRET": secret_name})
    if got != url:
        fail(f"reading the secret back failed: {err}")
    write_cfg(vault, secret_name)
    print("\nDone. The database login is now in Azure Key Vault.")
    print(f"config/config.env now only says KEYVAULT_NAME={vault} (no username, no password).")
    print("Teammates who run the relay need the role 'Key Vault Secrets User' on this vault")
    print("(Azure portal > the key vault > Access control (IAM) > Add role assignment).")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")

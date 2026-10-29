"""
Keeps THIS computer's public IP address in the firewall of the Azure database, so you never have
to add yourself by hand in the Azure portal.

- Every computer gets its own rule: "relay-<computer name>". When your IP changes (other wifi,
  at school, at home) the rule is updated to the new IP. Other computers' rules are not touched.
- Used automatically by start.bat (relay) and the database tools in tools.bat.
- tools.bat > "Firewall check every 15 minutes" adds a Windows scheduled task that runs this in the
  background, so the computer also has access when you only use other tools (pgAdmin, DBeaver...).

Run by hand:  .venv\\Scripts\\python.exe src\\azure_firewall.py
Note: Azure does not allow firewall changes while the database is stopped. Then nothing happens;
the relay sets the rule itself as soon as it has started the database.
"""
import json
import re
import socket
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import secrets_store as ss  # noqa: E402
from paths import FIREWALL_LOG as LOG, read_config  # noqa: E402

SUFFIX = ".postgres.database.azure.com"


def read_cfg():
    return read_config()


def rule_name():
    return "relay-" + re.sub(r"[^A-Za-z0-9_-]", "-", socket.gethostname())[:40]


def public_ip():
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
            with urllib.request.urlopen(req, timeout=6) as r:
                ip = r.read().decode().strip()
            if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip):
                return ip
        except Exception:
            continue
    return ""


def az_json(args, timeout=120):
    rc, out, err = ss.az(args + ["-o", "json"], timeout=timeout)
    if rc != 0:
        return None, (err or out)[:300]
    try:
        return json.loads(out or "null"), ""
    except ValueError:
        return None, out[:300]


# Newer Azure CLI versions renamed the options of "az postgres flexible-server firewall-rule":
#   new:  --server-name <server> --name <rule>
#   old:  --name <server> --rule-name <rule>
# We try the new form first and fall back to the old one, so every computer works whatever version it has.
_STYLE = {"v": None}


def _rule_flags(style, server, rule):
    if style == "old":
        return ["--name", server, "--rule-name", rule]
    return ["--server-name", server, "--name", rule]


def _wrong_flags(out, err):
    text = f"{err} {out}".lower()
    return "unrecognized arguments" in text or "the following arguments are required" in text


def rule_cmd(run, action, server, rule, extra=(), timeout=300):
    """Runs 'az postgres flexible-server firewall-rule <action>' with the option names this computer's
    Azure CLI understands. run(args, timeout=...) -> (rc, out, err). Returns (rc, out, err)."""
    base = ["postgres", "flexible-server", "firewall-rule", action]
    styles = [_STYLE["v"]] if _STYLE["v"] else ["new", "old"]
    rc, out, err = 1, "", "no attempt"
    for style in styles:
        rc, out, err = run(base + _rule_flags(style, server, rule) + list(extra), timeout=timeout)
        if not _wrong_flags(out, err):
            _STYLE["v"] = style
            return rc, out, err
    return rc, out, err


def find_server(cfg):
    """(server name, resource group, subscription id, state) of the database, or raises RuntimeError."""
    name = cfg.get("AZURE_SERVER_NAME", "").strip()
    if not name:
        url, _w, err = ss.database_url(cfg, interactive=False)
        host = url.split("@", 1)[-1].split(":", 1)[0].split("/", 1)[0] if url else ""
        if not host.endswith(SUFFIX):
            raise RuntimeError(f"database server name unknown ({err or 'not an Azure server'})")
        name = host[:-len(SUFFIX)]
    sub = cfg.get("AZURE_SUBSCRIPTION", "").strip()
    subs = [sub] if sub else []
    if not subs:
        accounts, err = az_json(["account", "list", "--query", "[?state=='Enabled'].id"])
        subs = accounts or []
    for s in subs:
        servers, _e = az_json(["postgres", "flexible-server", "list", "--subscription", s])
        for srv in servers or []:
            if srv.get("name", "").lower() == name.lower():
                rg = cfg.get("AZURE_RESOURCE_GROUP", "").strip() or srv["resourceGroup"]
                return srv["name"], rg, s, srv.get("state", "?")
    raise RuntimeError(f"server '{name}' not found in your Azure subscriptions")


def ensure(cfg=None, quiet=False):
    """Makes sure this computer's IP is allowed. Returns True when it is (or nothing could be done)."""
    def say(msg):
        if not quiet:
            print(f"[FIREWALL] {msg}", flush=True)
        log(msg)

    cfg = cfg if cfg is not None else read_cfg()
    if str(cfg.get("AZURE_AUTO_FIREWALL", "1")).strip() in ("0", "false", "no", "off"):
        return True
    if not ss.az_path():
        say("Azure CLI not found; firewall not checked")
        return False
    if not ss.ensure_login(interactive=not quiet):
        say("not logged in to Azure (run 'az login'); firewall not checked")
        return False
    ip = public_ip()
    if not ip:
        say("could not look up this computer's public IP (no internet?)")
        return False
    try:
        server, rg, sub, state = find_server(cfg)
    except RuntimeError as e:
        say(str(e))
        return False

    rule = rule_name()
    common = ["--resource-group", rg, "--subscription", sub]
    rc, out, _e = rule_cmd(ss.az, "show", server, rule, common + ["-o", "json"], timeout=120)
    try:
        current = json.loads(out) if rc == 0 and out else None
    except ValueError:
        current = None
    if current and current.get("startIpAddress") == ip and current.get("endIpAddress") == ip:
        say(f"OK: {ip} is already allowed on {server} (rule '{rule}')")
        return True
    if state != "Ready":
        say(f"database {server} is {state}: Azure only allows firewall changes when it is on. "
            "start.bat sets the rule after starting the database.")
        return False
    say(f"allowing {ip} on {server} (rule '{rule}', takes about a minute)...")
    args = common + ["--start-ip-address", ip, "--end-ip-address", ip, "-o", "none"]
    rc, out, err = rule_cmd(ss.az, "create", server, rule, args)
    if rc != 0:
        rc, out, err = rule_cmd(ss.az, "update", server, rule, args)
    if rc == 0:
        say(f"OK: {ip} allowed on {server} (rule '{rule}')")
        return True
    say(f"setting the firewall rule failed: {(err or out)[:200]}")
    return False


def log(msg):
    try:
        lines = LOG.read_text(encoding="utf-8").splitlines()[-200:] if LOG.exists() else []
        lines.append(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}")
        LOG.parent.mkdir(parents=True, exist_ok=True)
        LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


if __name__ == "__main__":
    ok = ensure(quiet="--quiet" in sys.argv)
    sys.exit(0 if ok else 1)

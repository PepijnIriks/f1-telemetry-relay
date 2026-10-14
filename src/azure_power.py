"""
Automatically turns the Azure Database for PostgreSQL (Flexible Server) on and off,
and keeps this computer's IP address in the Azure firewall.

- When the relay starts: if the server is stopped, it is started.
- When nobody has raced for a while (car not driving and nobody uses the Races tab),
  the server is stopped. It only starts again when someone actually races again
  (car moving on track), or when someone clicks "Start database" in the Races tab.
  Game menus, a paused game or opening the website do NOT turn it back on.
- When the relay is closed (Ctrl+C or closing the window) the server is stopped.

Works through the Azure CLI ("az"). Needed once on this PC:
    winget install -e --id Microsoft.AzureCLI
    az login
"""
import json
import re
import socket
import urllib.request
import shutil
import subprocess
import sys
import threading
import time

READY, STOPPED = "Ready", "Stopped"


def _yes(v):
    return str(v).strip().lower() not in ("0", "false", "nee", "no", "uit", "off", "")


class AzurePower:
    def __init__(self, cfg, host, state):
        self.enabled = _yes(cfg.get("AZURE_AUTO_POWER", "1"))
        self.stop_on_exit = _yes(cfg.get("AZURE_STOP_ON_EXIT", "1"))
        self.stop_after = float(cfg.get("AZURE_STOP_AFTER_MIN", "15")) * 60
        self.cfg_server = cfg.get("AZURE_SERVER_NAME", "").strip()
        self.set_host(host)
        self.rg = cfg.get("AZURE_RESOURCE_GROUP", "").strip()
        self.sub = cfg.get("AZURE_SUBSCRIPTION", "").strip()
        self.az = shutil.which("az")
        self.state = state
        self.phase = "setup"          # setup -> active / off
        self.status = "?"             # status according to Azure: Ready, Stopped, Starting, Stopping, ...
        self.t0 = self.last_activity = time.time()
        self.start_reason = "relay started"
        self.wake = threading.Event()
        self.exit_done = False
        self.before_exit = None       # set by relay.py: saves unsent data to disk
        self.firewall = _yes(cfg.get("AZURE_AUTO_FIREWALL", "1"))
        self.fw_rule = "relay-" + re.sub(r"[^A-Za-z0-9_-]", "-", socket.gethostname())[:40]
        self.fw_ip = ""
        self.fw_checked = 0.0
        state["azure"] = {"managed": False, "status": "?", "msg": ""}

    # ------------------------------------------------------------ used by relay.py
    def set_host(self, host):
        """Server name from the database host (read from Key Vault, so only known after startup)."""
        suffix = ".postgres.database.azure.com"
        self.server = self.cfg_server or (host[:-len(suffix)] if host and host.endswith(suffix) else "")

    def race(self, reason="racing again"):
        """Someone is really racing (or clicked Start database): keep the DB on, or turn it back on."""
        self.last_activity = time.time()
        if self.phase == "active" and self.status not in (READY, "Starting"):
            self.start_reason = reason
            self.wake.set()

    def touch(self):
        """Someone is viewing Races: keeps a running DB on, but never turns a stopped DB back on."""
        if self.phase != "active" or self.status in (READY, "Starting"):
            self.last_activity = time.time()

    def is_off(self):
        """True when the DB was turned off on purpose and nobody has asked for it again."""
        return (self.phase == "active" and self.status in (STOPPED, "Stopping")
                and time.time() - self.last_activity >= self.stop_after)

    def db_ready(self):
        """May the relay connect to the database right now?"""
        if self.phase == "setup":
            return time.time() - self.t0 > 90     # setup is hanging (e.g. az login): just try anyway
        if self.phase != "active":
            return True
        return self.status == READY

    def not_ready_text(self):
        if self.is_off():
            return ("The database is off because nobody raced for a while. It turns on by itself as soon as "
                    "someone races again, or click 'Start database' (takes 2 to 5 minutes).")
        if self.status in ("Starting", STOPPED, "Stopping"):
            return "Azure database is starting up (usually takes 2 to 5 minutes). Try again in a moment."
        return f"Azure database is not available (status: {self.status})"

    def start_thread(self):
        threading.Thread(target=self._run, daemon=True).start()
        if sys.platform == "win32":
            self._install_close_handler()

    # ------------------------------------------------------------ internal
    def _log(self, msg):
        print(f"[AZURE] {msg}", flush=True)
        self.state["azure"]["msg"] = msg

    def _cmd(self, args):
        cmd = [self.az] + args
        if self.sub:
            cmd += ["--subscription", self.sub]
        return cmd + ["--only-show-errors", "-o", "json"]

    def _az(self, args, timeout=180):
        try:
            p = subprocess.run(self._cmd(args), capture_output=True, text=True, timeout=timeout)
            return p.returncode, p.stdout.strip(), p.stderr.strip()
        except subprocess.TimeoutExpired:
            return 1, "", "time-out"
        except Exception as e:
            return 1, "", str(e)

    def _srv(self):
        return ["--resource-group", self.rg, "--name", self.server]

    def _setup(self):
        if not self.enabled:
            self._log("Automatic on/off is disabled (AZURE_AUTO_POWER=0 in config.env)")
            return False
        if not self.server:
            self._log("No Azure server name known (Key Vault secret not read); automatic on/off is disabled")
            return False
        if not self.az:
            self._log("Azure CLI not found, so the database will NOT be turned on/off automatically.")
            print("[AZURE] Install it once (PowerShell on this PC): winget install -e --id Microsoft.AzureCLI")
            print("[AZURE] Then close this window, run 'az login' and start start.bat again.", flush=True)
            return False

        rc, _out, _err = self._az(["account", "show"], timeout=60)
        if rc != 0:
            self._log("Not logged in to Azure. A browser will open now: log in with your Fontys account.")
            try:
                subprocess.run([self.az, "login", "--only-show-errors", "-o", "none"], timeout=600)
            except Exception:
                pass
            rc, _out, _err = self._az(["account", "show"], timeout=60)
            if rc != 0:
                self._log("Logging in to Azure failed; automatic on/off is disabled")
                return False

        if not self.rg:
            return self._find_server()
        return True

    def _list_in(self, sub):
        """Look for the server in one subscription. Returns (resource group or None, error message)."""
        old, self.sub = self.sub, sub
        rc, out, err = self._az(["postgres", "flexible-server", "list"], timeout=120)
        self.sub = old
        if rc != 0:
            return None, (err or out)[:200]
        try:
            for s in json.loads(out or "[]"):
                if s.get("name", "").lower() == self.server.lower():
                    return s.get("resourceGroup"), ""
        except ValueError:
            pass
        return None, ""

    def _find_server(self):
        if self.sub:
            subs = [{"id": self.sub, "name": self.sub}]
        else:
            rc, out, err = self._az(["account", "list"], timeout=60)
            try:
                subs = [s for s in json.loads(out or "[]") if s.get("state", "Enabled") == "Enabled"]
            except ValueError:
                subs = []
        print(f"[AZURE] Looking for server '{self.server}' in {len(subs)} subscription(s)...", flush=True)
        errors = []
        for s in subs:
            rg, err = self._list_in(s["id"])
            if rg:
                self.rg, self.sub = rg, s["id"]
                print(f"[AZURE] Found in subscription '{s.get('name')}', resource group '{rg}'", flush=True)
                return True
            if err:
                errors.append(f"{s.get('name')}: {err}")
        self._log(f"Server '{self.server}' not found in your Azure subscriptions. "
                  "Fill in AZURE_RESOURCE_GROUP and AZURE_SUBSCRIPTION in config.env "
                  "(shown in the Azure Portal on the server's Overview page).")
        for e in errors:
            print(f"[AZURE]   error in {e}", flush=True)
        return False

    def _refresh(self):
        rc, out, err = self._az(["postgres", "flexible-server", "show"] + self._srv(), timeout=120)
        if rc != 0:
            self._log(f"Requesting status failed: {(err or out)[:200]}")
            return self.status
        try:
            st = json.loads(out).get("state") or "?"
        except ValueError:
            st = "?"
        if st != self.status:
            self.status = st
            nice = {READY: "is ON", STOPPED: "is OFF", "Starting": "is starting up",
                    "Stopping": "is shutting down"}.get(st, f"status {st}")
            self._log(f"Database {self.server} {nice}")
        self.state["azure"]["status"] = st
        return st

    def _public_ip(self):
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

    def _ensure_firewall(self):
        """Put this PC's current public IP in the Azure firewall (one fixed rule that gets updated)."""
        if not self.firewall or time.time() - self.fw_checked < 120:
            return
        self.fw_checked = time.time()
        ip = self._public_ip()
        if not ip:
            if not self.fw_ip:
                self._log("Could not look up this PC's IP address; firewall not changed")
            return
        if ip == self.fw_ip:
            return
        args = self._srv() + ["--rule-name", self.fw_rule, "--start-ip-address", ip, "--end-ip-address", ip]
        print(f"[AZURE] Firewall: allowing this PC's IP {ip} (can take a minute)...", flush=True)
        rc, out, err = self._az(["postgres", "flexible-server", "firewall-rule", "create"] + args, timeout=300)
        if rc != 0:
            rc, out, err = self._az(["postgres", "flexible-server", "firewall-rule", "update"] + args, timeout=300)
        if rc == 0:
            self.fw_ip = ip
            print(f"[AZURE] Firewall: IP {ip} allowed (rule '{self.fw_rule}')", flush=True)
        else:
            self._log(f"Setting the firewall rule failed: {(err or out)[:200]}")

    def _power(self, action):
        rc, out, err = self._az(["postgres", "flexible-server", action] + self._srv() + ["--no-wait"])
        if rc != 0:
            self._log(f"'{action}' failed: {(err or out)[:200]}")
            return False
        self.status = "Starting" if action == "start" else "Stopping"
        self.state["azure"]["status"] = self.status
        return True

    def _run(self):
        ok = self._setup()
        self.phase = "active" if ok else "off"
        self.state["azure"]["managed"] = ok
        if not ok:
            return
        print(f"[AZURE] Managing {self.server} (resource group {self.rg}); "
              f"turns off after {self.stop_after / 60:g} min without racing", flush=True)

        while True:
            st = self._refresh()
            if st == READY:
                self._ensure_firewall()
            idle = time.time() - self.last_activity
            want_on = idle < self.stop_after
            pending = self.state.get("db", {}).get("pending", 0)

            if want_on and st == STOPPED:
                self._log(f"Starting the database ({self.start_reason}; usually takes 2 to 5 minutes). "
                          "Data that already comes in is kept and written afterwards.")
                self._power("start")
            elif not want_on and st == READY and pending == 0:
                self._log(f"No racing for {idle / 60:.0f} minutes: turning the database off. "
                          "It only turns back on when someone races again.")
                self._power("stop")
                self.start_reason = "racing again"

            busy = st not in (READY, STOPPED) or (want_on and st != READY)
            self.wake.wait(15 if busy else 60)
            self.wake.clear()

    def _spawn_stop(self):
        """Run 'az ... stop' detached from this window, so it keeps going when the window closes."""
        if self.exit_done or self.phase != "active" or not self.stop_on_exit:
            return
        self.exit_done = True
        if self.status == STOPPED:
            return
        cmd = self._cmd(["postgres", "flexible-server", "stop"] + self._srv() + ["--no-wait"])
        kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if sys.platform == "win32":
            kw["creationflags"] = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
        try:
            subprocess.Popen(cmd, **kw)
            print(f"[AZURE] Relay is stopping: turning database {self.server} off.", flush=True)
        except Exception as e:
            print(f"[AZURE] Turning off on exit failed: {e}", flush=True)

    def on_exit(self, wait_flush=8):
        """Called on Ctrl+C: first write any pending data, then turn off."""
        if self.phase != "active" or not self.stop_on_exit:
            return
        end = time.time() + wait_flush
        while self.state.get("db", {}).get("pending", 0) and time.time() < end:
            time.sleep(0.5)
        self._spawn_stop()

    def _install_close_handler(self):
        """Closing the window: Windows then gives ~5 seconds; enough to launch 'stop' detached."""
        try:
            import ctypes
            HANDLER = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

            def handler(event):
                if event in (2, 5, 6):    # window closed, log off, shut down
                    if self.before_exit:
                        try:
                            self.before_exit()       # save data that is not in Azure yet (relay.py)
                        except Exception:
                            pass
                    self._spawn_stop()
                    return 1
                return 0                  # Ctrl+C goes through KeyboardInterrupt as usual

            self._handler = HANDLER(handler)   # keep a reference, otherwise Python garbage-collects it
            ctypes.windll.kernel32.SetConsoleCtrlHandler(self._handler, True)
        except Exception:
            pass

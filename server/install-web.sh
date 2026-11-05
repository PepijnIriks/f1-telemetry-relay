#!/usr/bin/env bash
# =====================================================================================================
#  F1 telemetry relay - DEMO WEBSITE on a second Fontys Educloud VM (Ubuntu 22.04 or 24.04)
#
#  Runs the relay's own website on a server, so everyone on the Fontys network can open it in a browser:
#    - Live / Raw: the REAL live data of the game PC (its relay sends a copy to udp 20777 of this VM);
#      when nobody races, a fake game (src/simulate.py) drives laps, so there is always something live
#    - Races / Leaderboard / corner comparison: the REAL races from the database on the f1-db VM
#  Demo mode: the fake laps are NEVER stored, and stored races can't be changed from this website.
#
#  The code comes from GitHub with a read-only deploy key (/etc/f1/deploy_key; the repository is private).
#  It updates itself every 5 minutes (f1-auto-update.timer), so nobody has to log in to this VM.
#  Normally the VM's cloud-init clones the repository and runs this script by itself. By hand (as root):
#      bash /opt/f1/app/server/install-web.sh
#
#  The database login is NOT typed in: after this script, run ONE command on the database VM (f1-db),
#  which sends its login file to this VM over the internal network (only once, then it stops listening):
#      curl -T /root/f1-database-login.txt <internal IP of this VM>/
#
#  Settings (environment variables, optional):
#      DB_HOST=10.1.1.223    internal address of the database VM
#      BRANCH=main           branch of the repository to use
# =====================================================================================================
set -euo pipefail

DB_HOST="${DB_HOST:-10.1.1.223}"
BRANCH="${BRANCH:-$(git -C /opt/f1/app rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)}"
APP=/opt/f1/app
ETC=/etc/f1
REPO=git@github.com:PepijnIriks/f1-telemetry-relay.git
GIT_SSH="ssh -i $ETC/deploy_key -o UserKnownHostsFile=$ETC/known_hosts -o IdentitiesOnly=yes"

say() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m[X] %s\033[0m\n' "$*"; exit 1; }
[ "$(id -u)" -eq 0 ] || die "Run this as root (sudo -i first)"
[ -f "$ETC/deploy_key" ] || die "No deploy key in $ETC/deploy_key (cloud-init puts it there)"

export DEBIAN_FRONTEND=noninteractive

say "1/5  Packages"
apt-get update -q
apt-get install -y -q git python3-venv python3-pip ufw fail2ban curl

say "2/5  User f1 and the code from GitHub (branch $BRANCH)"
id f1 >/dev/null 2>&1 || useradd --system --home-dir /opt/f1 --shell /usr/sbin/nologin f1
install -d -m 755 -o f1 -g f1 /opt/f1
chmod 700 "$ETC"; chmod 600 "$ETC/deploy_key"
[ -s "$ETC/known_hosts" ] || ssh-keyscan -t ed25519 github.com > "$ETC/known_hosts" 2>/dev/null
chown -R f1:f1 "$ETC"
if [ -d "$APP/.git" ]; then
    chown -R f1:f1 "$APP"
    sudo -u f1 env GIT_SSH_COMMAND="$GIT_SSH" git -C "$APP" pull --ff-only
else
    sudo -u f1 env GIT_SSH_COMMAND="$GIT_SSH" git clone -b "$BRANCH" "$REPO" "$APP"
fi
chown -R f1:f1 "$APP"

say "3/5  Python environment"
sudo -u f1 python3 -m venv "$APP/.venv"
sudo -u f1 "$APP/.venv/bin/pip" install -q --disable-pip-version-check -r "$APP/requirements.txt"

say "4/5  Services (website, fake game, one-time login receiver, update command)"
cat > /etc/systemd/system/f1-demo-web.service <<EOF
[Unit]
Description=F1 telemetry relay - demo website (port 8000)
After=network-online.target
Wants=network-online.target

[Service]
User=f1
WorkingDirectory=$APP
EnvironmentFile=-$ETC/db.env
Environment=F1_WEB_HOST=0.0.0.0 F1_SHOW_INTRO=0 F1_DRIVER_MENU=0 PYTHONUNBUFFERED=1
Environment=F1_UDP_PORT=20780 F1_ACCEPT_DRIVER=1 F1_LIVE_FORWARD=
Environment=PYTHONPYCACHEPREFIX=$APP/data/pycache
ExecStart=$APP/.venv/bin/python src/relay.py --demo --driver Demo --no-browser --no-login
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/f1-demo-game.service <<EOF
[Unit]
Description=F1 telemetry relay - fake game for the demo website
After=f1-demo-web.service

[Service]
User=f1
WorkingDirectory=$APP
Environment=PYTHONPYCACHEPREFIX=$APP/data/pycache
ExecStart=$APP/.venv/bin/python src/simulate.py 127.0.0.1 20779
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/f1-live-mux.service <<EOF
[Unit]
Description=F1 telemetry relay - live switch: real data from the game PC (udp 20777) or the fake game
Before=f1-demo-game.service

[Service]
User=f1
WorkingDirectory=$APP
Environment=PYTHONUNBUFFERED=1 PYTHONPYCACHEPREFIX=$APP/data/pycache
ExecStart=$APP/.venv/bin/python src/live_mux.py 20777 20779 20780
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# One-time receiver for the database login: the database VM sends its login file with
#     curl -T /root/f1-database-login.txt <this VM>/
# Only the password is taken from it; the address becomes the internal address of the database VM.
cat > /opt/f1/receive_login.py <<'EOF'
import os
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

DB_HOST = os.environ.get("DB_HOST", "10.1.1.223")
OUT = "/etc/f1/db.env"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_PUT(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)).decode("utf-8", "replace")
        m = re.search(r"Password:\s*([0-9A-Za-z]+)", body)
        if not m:
            return self.answer(400, "No 'Password:' line found. Send /root/f1-database-login.txt\n")
        url = f"postgresql://f1_relay:{m.group(1)}@{DB_HOST}:5432/f1?sslmode=require"
        fd = os.open(OUT, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(f"DATABASE_URL={url}\n")
        subprocess.run(["chown", "f1:f1", OUT])
        self.answer(200, "OK: login received by f1-web. The demo website restarts with the database.\n")
        subprocess.run(["systemctl", "restart", "f1-demo-web.service"])
        threading.Thread(target=self.server.shutdown).start()

    def answer(self, code, text):
        data = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


HTTPServer(("0.0.0.0", 80), Handler).serve_forever()
EOF
cat > /etc/systemd/system/f1-receive-login.service <<EOF
[Unit]
Description=F1 demo website - receive the database login once (port 80, internal network only)
ConditionPathExists=!$ETC/db.env

[Service]
Environment=DB_HOST=$DB_HOST
ExecStart=/usr/bin/python3 /opt/f1/receive_login.py

[Install]
WantedBy=multi-user.target
EOF

cat > /usr/local/bin/f1-update <<EOF
#!/usr/bin/env bash
# Newest code from GitHub for the demo website, then restart it.  Usage (as root): f1-update
set -euo pipefail
sudo -u f1 env GIT_SSH_COMMAND="$GIT_SSH" git -C $APP pull --ff-only
sudo -u f1 $APP/.venv/bin/pip install -q --disable-pip-version-check -r $APP/requirements.txt
systemctl restart f1-demo-web.service f1-live-mux.service f1-demo-game.service
echo "Updated to: \$(git -C $APP log --oneline -1)"
EOF
chmod 755 /usr/local/bin/f1-update

# Updates itself: every 5 minutes it looks on GitHub; when there is a new version it gets it and runs this
# script again (so new services or settings are set up too). Nobody has to log in to the VM for updates.
cat > /usr/local/bin/f1-auto-update <<'EOF'
#!/usr/bin/env bash
set -uo pipefail
APP=/opt/f1/app
export GIT_SSH_COMMAND="ssh -i /etc/f1/deploy_key -o UserKnownHostsFile=/etc/f1/known_hosts -o IdentitiesOnly=yes"
sudo -u f1 --preserve-env=GIT_SSH_COMMAND git -C "$APP" fetch -q origin || exit 0
HERE="$(sudo -u f1 git -C "$APP" rev-parse HEAD)"
THERE="$(sudo -u f1 git -C "$APP" rev-parse '@{u}')"
[ "$HERE" = "$THERE" ] && exit 0
echo "New version on GitHub: updating $HERE -> $THERE"
sudo -u f1 --preserve-env=GIT_SSH_COMMAND git -C "$APP" pull -q --ff-only || exit 1
bash "$APP/server/install-web.sh" > /var/log/f1-web-install.log 2>&1
EOF
chmod 755 /usr/local/bin/f1-auto-update
cat > /etc/systemd/system/f1-auto-update.service <<'EOF'
[Unit]
Description=F1 demo website - get the newest version from GitHub

[Service]
Type=oneshot
ExecStart=/usr/local/bin/f1-auto-update
EOF
cat > /etc/systemd/system/f1-auto-update.timer <<'EOF'
[Unit]
Description=F1 demo website - look for a new version every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable f1-demo-web.service f1-demo-game.service f1-live-mux.service >/dev/null
systemctl restart f1-demo-web.service f1-live-mux.service f1-demo-game.service
[ -f "$ETC/db.env" ] || systemctl enable --now f1-receive-login.service >/dev/null
systemctl enable --now f1-auto-update.timer >/dev/null

say "5/5  Firewall: SSH, the website (8000), live data (udp 20777) and, from the internal network only, the login receiver (80)"
ufw allow OpenSSH >/dev/null
ufw allow 8000/tcp >/dev/null
ufw allow 20777/udp >/dev/null
ufw allow from 10.0.0.0/8 to any port 80 proto tcp >/dev/null
ufw --force enable >/dev/null
systemctl enable --now fail2ban >/dev/null 2>&1 || true

IP="$(hostname -I 2>/dev/null)"; IP="${IP%% *}"
echo
echo "Demo website runs on port 8000 of this VM ($IP)."
if [ -f "$ETC/db.env" ]; then
    echo "Database login: present. Races come from $DB_HOST."
else
    echo "Database login: NOT yet. On the database VM (f1-db), as root, run once:"
    echo "    curl -T /root/f1-database-login.txt $IP/"
fi
echo "Status:  systemctl status f1-demo-web     Log:  journalctl -u f1-demo-web -n 50"

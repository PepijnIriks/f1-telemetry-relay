#!/usr/bin/env bash
# =====================================================================================================
#  F1 telemetry relay - database server on a Fontys Educloud VM (Ubuntu 22.04 or 24.04)
#
#  Installs and sets up everything the relay needs, in one go:
#    - PostgreSQL 16 with TimescaleDB (compression of the raw packets, like on Azure before)
#    - database "f1" with the login "f1_relay" (a long random password, made here)
#    - encrypted connections only (TLS), password login only (scram-sha-256), no remote superuser
#    - firewall (ufw): only SSH (22) and PostgreSQL (5432) are open; fail2ban against SSH guessing
#    - a backup of the whole database every night (/var/backups/f1, the last 14 are kept)
#
#  Run it on the VM (as a user that may use sudo):
#      wget https://raw.githubusercontent.com/PepijnIriks/f1-telemetry-relay/main/server/install-educloud.sh
#      sudo bash install-educloud.sh
#
#  Running it again is safe: nothing is removed and the password stays the same.
#      sudo bash install-educloud.sh --new-password      makes a new password for f1_relay
#      sudo bash install-educloud.sh --show              shows the login line again
# =====================================================================================================
set -euo pipefail

PG=16
DB=f1
ROLE=f1_relay
BACKUP_DIR=/var/backups/f1
LOGIN_FILE=/root/f1-database-login.txt
KEEP_DAYS=14

say()  { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[X] %s\033[0m\n' "$*"; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run this with sudo:  sudo bash $0"
[ -r /etc/os-release ] && . /etc/os-release
[ "${ID:-}" = "ubuntu" ] || warn "This script is made for Ubuntu (found: ${PRETTY_NAME:-unknown}); trying anyway."

show_login() {
    if [ -f "$LOGIN_FILE" ]; then
        echo
        echo "================================================================================"
        cat "$LOGIN_FILE"
        echo "================================================================================"
    else
        warn "No login saved yet: run the script without --show first."
    fi
}

if [ "${1:-}" = "--show" ]; then show_login; exit 0; fi
NEW_PASSWORD=0
[ "${1:-}" = "--new-password" ] && NEW_PASSWORD=1

export DEBIAN_FRONTEND=noninteractive

# ----------------------------------------------------------------------------- 1. packages
say "1/7  Packages (PostgreSQL $PG, TimescaleDB, firewall)"
apt-get update -q
apt-get install -y -q ca-certificates curl gnupg lsb-release openssl ufw fail2ban postgresql-common

# The official PostgreSQL repository (same PostgreSQL version on every Ubuntu release)
if [ ! -f /etc/apt/sources.list.d/pgdg.list ] && [ ! -f /etc/apt/sources.list.d/pgdg.sources ]; then
    /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y
fi

# TimescaleDB repository (for compression of the raw packets). Optional: without it everything works,
# the data just takes more space.
CODENAME="$(lsb_release -cs)"
if [ ! -f /etc/apt/sources.list.d/timescaledb.list ]; then
    if curl -fsSL https://packagecloud.io/timescale/timescaledb/gpgkey -o /tmp/timescaledb.key; then
        gpg --dearmor --yes -o /etc/apt/trusted.gpg.d/timescaledb.gpg /tmp/timescaledb.key
        echo "deb https://packagecloud.io/timescale/timescaledb/ubuntu/ $CODENAME main" \
            > /etc/apt/sources.list.d/timescaledb.list
    else
        warn "TimescaleDB repository not reachable; continuing without compression."
    fi
fi
apt-get update -q
apt-get install -y -q "postgresql-$PG"
TIMESCALE=0
if apt-get install -y -q "timescaledb-2-postgresql-$PG"; then
    TIMESCALE=1
else
    warn "TimescaleDB could not be installed; the relay works without it (no compression)."
fi

CONF_DIR="/etc/postgresql/$PG/main"
[ -d "$CONF_DIR" ] || die "PostgreSQL configuration not found in $CONF_DIR"

# ----------------------------------------------------------------------------- 2. TLS certificate
say "2/7  TLS certificate (all connections are encrypted)"
CERT="$CONF_DIR/f1-server.crt"
KEY="$CONF_DIR/f1-server.key"
if [ ! -f "$CERT" ]; then
    openssl req -x509 -nodes -newkey rsa:3072 -days 3650 -subj "/CN=f1-telemetry-db" \
        -keyout "$KEY" -out "$CERT" 2>/dev/null
    chown postgres:postgres "$CERT" "$KEY"
    chmod 600 "$KEY"
fi

# ----------------------------------------------------------------------------- 3. PostgreSQL settings
say "3/7  PostgreSQL settings"
PRELOAD=""
[ "$TIMESCALE" = 1 ] && PRELOAD="shared_preload_libraries = 'timescaledb'"
cat > "$CONF_DIR/conf.d/f1.conf" <<EOF
# Made by install-educloud.sh (F1 telemetry relay). Changes here are overwritten when it runs again.
listen_addresses = '*'
ssl = on
ssl_cert_file = '$CERT'
ssl_key_file = '$KEY'
ssl_min_protocol_version = 'TLSv1.2'
password_encryption = 'scram-sha-256'
max_connections = 100
log_connections = on
$PRELOAD
EOF

if [ "$TIMESCALE" = 1 ] && command -v timescaledb-tune >/dev/null; then
    # Memory settings that fit this VM (writes to postgresql.conf; our f1.conf is read after it)
    timescaledb-tune --quiet --yes --pg-version "$PG" --conf-path "$CONF_DIR/postgresql.conf" >/dev/null || true
fi

# Who may log in from outside: only the F1 accounts, only to the f1 database, only encrypted, only with
# a password. The superuser "postgres" can only be used on the VM itself (sudo -u postgres psql).
HBA="$CONF_DIR/pg_hba.conf"
if ! grep -q "F1 telemetry relay" "$HBA"; then
    cat >> "$HBA" <<EOF

# ---- F1 telemetry relay (install-educloud.sh)
hostnossl all       all                 0.0.0.0/0   reject
hostnossl all       all                 ::/0        reject
hostssl   $DB       $ROLE,f1_reader     0.0.0.0/0   scram-sha-256
hostssl   $DB       $ROLE,f1_reader     ::/0        scram-sha-256
EOF
fi

systemctl enable --now "postgresql@$PG-main" >/dev/null 2>&1 || systemctl enable --now postgresql
systemctl restart "postgresql@$PG-main" 2>/dev/null || systemctl restart postgresql

for _ in $(seq 1 30); do
    sudo -u postgres pg_isready -q && break
    sleep 1
done
sudo -u postgres pg_isready -q || die "PostgreSQL did not start: see  sudo journalctl -u postgresql@$PG-main"

# ----------------------------------------------------------------------------- 4. database and login
say "4/7  Database '$DB' and login '$ROLE'"
psql_su() { sudo -u postgres psql -v ON_ERROR_STOP=1 -qtA "$@"; }

ROLE_EXISTS="$(psql_su -c "SELECT 1 FROM pg_roles WHERE rolname = '$ROLE'")"
PASSWORD=""
if [ -z "$ROLE_EXISTS" ] || [ "$NEW_PASSWORD" = 1 ] || [ ! -f "$LOGIN_FILE" ]; then
    PASSWORD="$(openssl rand -hex 24)"          # 48 characters, only 0-9 and a-f: safe in a URL
fi
if [ -z "$ROLE_EXISTS" ]; then
    # CREATEROLE: the relay's tools can make the read-only account for the dashboard group (tools.bat)
    psql_su -c "CREATE ROLE $ROLE WITH LOGIN CREATEROLE PASSWORD '$PASSWORD'"
elif [ -n "$PASSWORD" ]; then
    psql_su -c "ALTER ROLE $ROLE WITH LOGIN CREATEROLE PASSWORD '$PASSWORD'"
fi
if [ -z "$(psql_su -c "SELECT 1 FROM pg_database WHERE datname = '$DB'")" ]; then
    psql_su -c "CREATE DATABASE $DB OWNER $ROLE"
fi
psql_su -d "$DB" -c "ALTER SCHEMA public OWNER TO $ROLE"
# May read server settings (e.g. whether TimescaleDB is loaded), but not change them
psql_su -c "GRANT pg_read_all_settings TO $ROLE"
if [ "$TIMESCALE" = 1 ]; then
    # Needs the superuser once; after this the relay can make compressed tables itself
    psql_su -d "$DB" -c "CREATE EXTENSION IF NOT EXISTS timescaledb" \
        && psql_su -d "$DB" -c "ALTER EXTENSION timescaledb UPDATE" >/dev/null 2>&1 || true
fi

PUBLIC_IP="$(curl -fsS --max-time 6 https://api.ipify.org 2>/dev/null || true)"
LOCAL_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
HOST="${PUBLIC_IP:-$LOCAL_IP}"

if [ -n "$PASSWORD" ]; then
    umask 077
    cat > "$LOGIN_FILE" <<EOF
F1 database on this Educloud VM (made $(date '+%Y-%m-%d %H:%M'))

  Host:      $HOST          (address of this VM as seen from the internet)
  Port:      5432
  Database:  $DB
  User:      $ROLE
  Password:  $PASSWORD

Login line for the relay (tools.bat > Database login, or start.bat asks for it the first time):

  postgresql://$ROLE:$PASSWORD@$HOST:5432/$DB?sslmode=require

Keep this secret: never put it in GitHub, a chat or an e-mail. Shown again with:
  sudo bash install-educloud.sh --show
EOF
    chmod 600 "$LOGIN_FILE"
fi

# ----------------------------------------------------------------------------- 5. firewall
say "5/7  Firewall: only SSH (22) and PostgreSQL (5432) open"
ufw allow OpenSSH >/dev/null
ufw allow 5432/tcp >/dev/null
ufw --force enable >/dev/null
systemctl enable --now fail2ban >/dev/null 2>&1 || true

# ----------------------------------------------------------------------------- 6. nightly backup
say "6/7  Nightly backup to $BACKUP_DIR (last $KEEP_DAYS kept)"
install -d -m 700 -o postgres -g postgres "$BACKUP_DIR"
cat > /usr/local/bin/f1-backup <<EOF
#!/usr/bin/env bash
# Backup of the F1 database (made by install-educloud.sh). Run by hand:  sudo f1-backup
set -euo pipefail
FILE="$BACKUP_DIR/f1-\$(date +%Y-%m-%d_%H%M).dump"
sudo -u postgres pg_dump --format=custom --file="\$FILE" $DB
find "$BACKUP_DIR" -name 'f1-*.dump' -mtime +$KEEP_DAYS -delete
echo "Backup: \$FILE (\$(du -h "\$FILE" | cut -f1))"
EOF
chmod 755 /usr/local/bin/f1-backup
cat > /etc/cron.d/f1-backup <<EOF
# Every night at 04:15: backup of the F1 database (install-educloud.sh)
15 4 * * * root /usr/local/bin/f1-backup >> /var/log/f1-backup.log 2>&1
EOF
chmod 644 /etc/cron.d/f1-backup

# ----------------------------------------------------------------------------- 7. check
say "7/7  Check"
TS_TEXT="no (works without, data is not compressed)"
[ "$TIMESCALE" = 1 ] && TS_TEXT="$(psql_su -d "$DB" -c "SELECT 'yes, version ' || extversion FROM pg_extension WHERE extname = 'timescaledb'")"
echo "PostgreSQL:   $(psql_su -c 'SHOW server_version')"
echo "TimescaleDB:  ${TS_TEXT:-no}"
echo "TLS:          $(psql_su -c 'SHOW ssl')"
echo "Disk free:    $(df -h --output=avail /var/lib/postgresql | tail -n 1 | tr -d ' ')"
echo "Firewall:     $(ufw status | head -n 1)"

if [ -n "$PASSWORD" ]; then
    show_login
    echo
    echo "Next: on the game PC, double-click start.bat (or tools.bat > Database login) and paste the"
    echo "login line above when it asks for it."
else
    echo
    echo "Everything was already set up; the password did not change. Show the login with:"
    echo "  sudo bash $0 --show"
fi
if [ -n "$PUBLIC_IP" ] && [ -n "$LOCAL_IP" ] && [ "$PUBLIC_IP" != "$LOCAL_IP" ]; then
    echo
    warn "This VM's own address ($LOCAL_IP) differs from its internet address ($PUBLIC_IP)."
    warn "Check in the Educloud portal that port 5432 is forwarded / allowed to this VM, otherwise the"
    warn "game PC can't reach it. If Educloud gives the VM a DNS name, you can use that as Host instead."
fi

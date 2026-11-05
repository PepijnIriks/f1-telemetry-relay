# Changelog

What changed in each version, in plain words. The details of every change are in the commits on GitHub.

## v1.3 - November 2026

### The database moves from Azure to Fontys Educloud
- **New: `server/install-educloud.sh`** sets up an Ubuntu VM in Educloud in one go: PostgreSQL 16 with
  TimescaleDB, database `f1` with the login `f1_relay`, encrypted connections only (TLS), only the F1 accounts
  from outside, a firewall (ufw, only SSH and 5432) with fail2ban, and a backup every night. Running it again is
  safe. Step by step: [EDUCLOUD.md](EDUCLOUD.md).
- **Database login in the Windows Credential Manager** instead of Azure Key Vault: `start.bat` asks for it the
  first time (the line the VM shows), `tools.bat` > 6 changes it. It is tested before it is stored. Still no
  password in the code or on GitHub.
- **Azure removed:** no Azure CLI or `az login` needed anymore. Gone: the automatic database on/off
  (`azure_power.py`), the firewall rule per computer (`azure_firewall.py`, also the scheduled task, which
  `start.bat` removes), the Key Vault setup (`setup_keyvault.py`) and their settings in `config/config.env`.
  The VM is always on, and every PC with the login can reach it from anywhere.
- Still there: data that can't reach the database waits on the PC (`data\spool`), also when the window is
  closed (now in `src/db_server.py`).
- Read-only account for the dashboard group (`tools.bat` > 7): the login is shown once and kept in the
  Windows Credential Manager; no firewall rule needed for them.
- `tools.bat` is shorter: 8 = tests, 9 = GitHub (were 10 and 11). New setting `DB_STORAGE_GB` (size of the
  VM's disk) for the compression's safety stop; `DB_WARN_GB` is now 45.
- The relay can see whether TimescaleDB is there without superuser rights (needed on the VM).

## v1.2.1 - October 2026

### Fix: firewall rule failed with a newer Azure CLI
- Newer Azure CLI versions renamed the options of `az postgres flexible-server firewall-rule`
  (`--server-name <server> --name <rule>` instead of `--name <server> --rule-name <rule>`). On the game PC
  this gave `unrecognized arguments: --rule-name`. The relay and `tools.bat` > 8 now try the new names first
  and fall back to the old ones, so both old and new CLI versions work. Test: `tests/test_firewall.py`.

## v1.2 - October 2026

### Tidier folder, easy to put on another computer
- **Three things to double-click** instead of twelve: `start.bat` (relay), `demo.bat` (demo without the game)
  and `tools.bat`, one menu for everything else (database info, CSV export, compression, Key Vault setup,
  read-only account, firewall check, tests, GitHub sync). The separate `.bat` files are gone.
- **New computer = unzip and double-click `start.bat`:** it offers to install Python and the Azure CLI when
  they are missing, makes the Python environment again when it is missing or was copied from another
  computer, and installs new packages after an update by itself. No code has to be changed for the game PC.
- **Everything a computer makes itself goes into `data/`** (data waiting for Azure, exports, download zips,
  caches, `firewall.log`; also the old `zips` folder). `start.bat` moves these from the places older versions
  used, the data waiting for Azure included.
- Documentation in `docs/`: a short README, the [guide](GUIDE.md) with all details, [DATA.md](DATA.md),
  this changelog and the screenshots.
- `export_csv.py` moved into `src/`. All modules read `config/config.env` through one helper (`src/paths.py`).
- Removed: `start.sh` (Linux/Mac; the project runs on Windows) and `src/driver_select.py` (driver choice with
  buttons on the steering wheel; never used, the website and the driver window do this).
- GitHub sync (`tools.bat` > 11) no longer puts changes on GitHub without a description.

## v1.1 - October 2026

### Database: less space, lower costs, nothing deleted
- **Compressed storage:** `upgrade-db.bat` stores the raw packets compressed per week (TimescaleDB). All data
  is kept and readable with normal SQL. The copy is checked before anything is swapped, the old table is
  only removed when you type `YES`, and the upgrade can be stopped and continued.
- **Duplicate index removed:** the big index on `session_uid` is no longer needed (packets are found by
  `run_id`). New databases do not get it; `upgrade-db.bat` removes it from existing ones.
- **Change driver is instant:** renaming the driver of a run only changes `f1_sessions`. Before, every
  packet of the run was rewritten, which made the table grow by the size of the run.
- **Database off sooner:** after 10 minutes without racing instead of 15 (`AZURE_STOP_AFTER_MIN`).
- The storage line in the black window shows how much the compression saves.

### Code
- `relay.py` (about 1700 lines) is split into modules with one job each: `telemetry.py`, `driver_runs.py`,
  `db.py`, `website.py`, `run_tools.py`, `legacy.py`, `shared.py`. Nothing changed in what the relay does:
  checked by sending the same recorded race through the old and the new version (identical database and
  identical website answers).
- **Automated tests:** `test.bat` (about 20 tests, a few seconds, no game or Azure needed). GitHub runs them
  for every push and pull request.

### Working together
- **`github-sync.bat`:** gets the newest version from GitHub and puts your own changes on GitHub with a
  short description.
- **`DATA.md`:** guide to the database for the dashboard group: every table and column, how to read values
  out of the raw packets, and example queries.
- `CHANGELOG.md` (this file).

## v1.0 - October 2026
The relay as it was before v1.1 (kept on the branch `v1.0-start`): live dashboard, leaderboard, live delta,
corner comparison, one run per driver, Key Vault login, automatic database on/off and firewall, nothing lost
when Azure is unreachable, read-only account for the other group, export to CSV.

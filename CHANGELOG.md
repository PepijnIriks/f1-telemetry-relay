# Changelog

What changed in each version, in plain words. The details of every change are in the commits on GitHub.

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

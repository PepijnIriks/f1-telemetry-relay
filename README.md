# F1 22 Telemetry Relay

Receives the UDP telemetry from F1 22 and shows it on a website with three tabs:

| Tab | What |
|---|---|
| **Live** | Dashboard: car (speed, gear, RPM, throttle/brake/steering, ERS, fuel, tyre), 30 s chart, **damage map** (wings, floor, diffuser, sidepods, engine parts, tyres, brakes), tyres and brakes, lap, track map, standings and events |
| **Raw** | The raw packets coming in, with counters and status lights (to prove everything works) |
| **Races** | All stored runs from the database, with lap times. Click "Play in Live" for a replay. Tick runs and click **Compare corners** to compare drivers per official F1 corner. Old races from the `f22_` tables of the earlier collector are listed too (label "old"; no damage data) |

Everything is written to PostgreSQL (Azure) in the background. The database can never slow down the live view.

## Installing on a new PC (once)
1. Copy this folder to the PC (or `git clone https://github.com/PepijnIriks/f1-telemetry-relay.git`).
   Do **not** copy `.venv` (it is made again) or `exports` (big CSV files).
2. Install Python 3 from https://www.python.org/downloads/ and tick **"Add python.exe to PATH"**.
3. Install the Azure CLI. In PowerShell: `winget install -e --id Microsoft.AzureCLI`.
   Close PowerShell, open a new one and run `az login` (log in with your Fontys account).
4. Double-click `start.bat`. The first time it installs the Python packages and, because no key vault
   is set yet, it starts the **Key Vault setup** (see "Database login: Azure Key Vault").
5. In F1 22: Settings > Telemetry Settings: UDP Telemetry **On**, UDP Broadcast Mode **Off**,
   UDP IP Address **127.0.0.1**, UDP Port **20777**, UDP Send Rate **20Hz** or higher, UDP Format **2022**.
6. Bind the driver buttons in the game (see "Choosing the driver in the game").

## Starting
- Windows: double-click `start.bat`
- Fake data without the game (demo): `start.bat simulate`
- What is already in the database: double-click `inspect-db.bat`
- Set up / change the database login in Key Vault: double-click `setup-keyvault.bat`
- Linux/macOS: `./start.sh` (or `./start.sh simulate`)

On start you get a short start-lights animation (turn it off with `SHOW_INTRO=0` in `config/config.env`).
The website opens automatically at http://localhost:8000 (tabs: `#live`, `#raw`, `#races`).

## Folders
| Folder / file | What |
|---|---|
| `start.bat`, `start.sh` | Start everything in one go |
| `inspect-db.bat` | Shows all tables and row counts in the Azure database |
| `setup-keyvault.bat` | Once: puts the database login in Azure Key Vault |
| `export_csv.py` | Exports all database tables to CSV (`python export_csv.py`) |
| `config/config.env` | Settings: ports, name of the key vault, Azure on/off, driver names and buttons. **No passwords** |
| `src/relay.py` | The relay: UDP in, parsing, website, database |
| `src/azure_power.py` | Turns the Azure database on and off and keeps the firewall up to date |
| `src/intro.py` | The startup animation |
| `src/secrets_store.py` | Reads the database login from Azure Key Vault |
| `src/setup_keyvault.py` | Used by `setup-keyvault.bat` |
| `src/driver_select.py` | Choosing the driver with a button in the game |
| `src/simulate.py` | Fake game for testing and demos |
| `src/inspect_db.py` | Used by `inspect-db.bat` |
| `web/index.html` | The website (Live / Raw / Races) |
| `requirements.txt` | Python packages (installed automatically) |

## Choosing the driver in the game
You don't have to leave the game to change drivers. F1 22 has 12 "UDP Action" buttons that do nothing in the
game itself but are sent along with the telemetry. The relay uses two of them:

| Button (default) | What |
|---|---|
| **UDP Action 1** | next name in the list |
| **UDP Action 2** | previous name in the list |

1. Put the names in `config/config.env`: `DRIVERS=Pepijn,Mathijs,Name3,Name4`.
2. In F1 22: **Settings > Controls, Vibration & Force Feedback** > choose your wheel/controller > **Edit Mappings**,
   scroll down to **UDP Action 1** and **UDP Action 2** and give them a free button (e.g. two buttons on the wheel).
3. While driving or in the garage: press **UDP Action 1** until you hear your name (Windows reads it out loud), then
   just wait 3 seconds. The relay switches the driver (new run) and says "<name> driving".
   Scrolling past other names does not create runs for them; only the name you stop on counts.

Settings in `config/config.env`: `DRIVER_NEXT_BUTTON` / `DRIVER_PREV_BUTTON` (UDP1..UDP12, or a combination of
normal buttons such as `L1+R1`), `DRIVER_CONFIRM_BUTTON` (optional: apply right away instead of waiting),
`DRIVER_CONFIRM_SECONDS` (default 3) and `ANNOUNCE_DRIVER` (0 = no voice).
The website shows the name being chosen in orange next to the driver ("→ Mathijs in 2 s").
When `start.bat` starts you can also pick the first driver by number.

## Changing drivers on the website
Type the new name (or pick one from the list) at the top right of the website and click **Set** (or press Enter). The relay then:
- closes the run of the previous driver (it stays in the database with that driver's name),
- starts a new run for the new driver with the next game data, even if the game session just continues
  (you don't have to restart the session).

The lap that is in progress at the moment of the switch is driven by two people, so it is marked invalid and
never counts in comparisons. Tip: switch in the garage or at the start of an out lap.
If the current run has no name yet, setting a name just gives that run the name (no new run).
In the black window you see lines starting with `[DRIVER]` for every switch.

## Comparing drivers per corner
In the **Races** tab, tick two or more runs on the same track and click **Compare corners**.
Choose **Fastest lap** (each driver's fastest valid lap) or **Best per corner** (each driver's best time in
every corner over all their valid laps). You get:
- **Corner analysis** per corner (pick it with the T1, T2... buttons, the arrows, or by clicking a corner in the
  chart or table): the speed line of every driver through the corner on top of each other, the top speed and the
  lowest speed, a bar per driver showing where they were braking / rolling / on full throttle, and on the right who
  was fastest and by how much. Hover over the chart to see every driver's speed at that point and how much time
  they have lost there so far.
- a chart with the time each driver loses per corner to the fastest driver in that corner,
- a table per corner with corner time, minimum (apex) speed, entry speed, exit speed, braking point and
  full-throttle point (pick with "Table shows"), plus "corners won" and the total of all corners.

How it works:
- `config/corners.json` holds the **official F1 corner numbers** and their distance from the start/finish line
  for every F1 22 track (from the official F1 live-timing data via FastF1; 2022 layouts, Hockenheim and Shanghai
  from 2019, Portimao and Sochi from 2021). Hanoi and the short layouts have no list.
- The distances are scaled to the lap length the game reports, so they line up with the game's lap distance.
- A corner zone runs from about 150 m before to 100 m after the corner (never overlapping the next corner).
- The relay works the corners out live after every lap and stores them in the tables `f1_corners` and `f1_lap_traces`.
  For runs that were stored before this existed, it works them out from the raw data the first time you compare them.
- If a corner looks shifted on a track, add `"offset": <metres>` to that track in `config/corners.json`.

## In the game
UDP Telemetry on, format 2022, IP 127.0.0.1 (the relay runs on the game PC itself), port 20777.
Allow Python in the Windows firewall if asked.

## Database login: Azure Key Vault
No username or password is stored in the code or in `config/config.env`. The full connection string
(server, username, password) is a secret in **Azure Key Vault**; the relay reads it at startup with your
`az login`. `config/config.env` only contains the name of the vault (`KEYVAULT_NAME`) and of the secret
(`KEYVAULT_SECRET`, default `f1-database-url`).

Set it up once with `setup-keyvault.bat` (start.bat starts it by itself if `KEYVAULT_NAME` is empty). It:
1. creates the key vault in the same resource group and region as the database (if it does not exist yet),
2. gives your own account access to the secrets,
3. looks up the server name and admin username in Azure (you don't type them),
4. asks for the database password (hidden) **or** makes a new strong password and sets it on the server,
5. stores the connection string in the vault and writes `KEYVAULT_NAME` in `config/config.env`.

At the top of the website it says "database OK" when it works; otherwise it shows what is wrong (also in the
black window, lines starting with `[SECRETS]` and `[DB]`). Other people who run the relay or `export_csv.py`
need the role **Key Vault Secrets User** on the vault (portal > key vault > Access control (IAM)).
For a quick local test you can pass a connection string in the environment variable `DATABASE_URL` instead.

## Database on/off and firewall (automatic)
The relay manages the Azure database itself, so it doesn't use credit all day:
- **On** when the relay starts (takes 2 to 5 minutes; data that already comes in is kept and written afterwards).
- **Off** after `AZURE_STOP_AFTER_MIN` minutes (default 15) without racing and without anyone using the Races tab.
  It only turns back on when someone really races again (car driving on track), or when you click
  **Start database** in the Races tab. Game menus, a paused game or opening the website do not turn it on.
- **Off** when you close the relay (close the window or Ctrl+C).
- **Firewall:** the current public IP of this PC is put in the Azure firewall automatically (one rule called
  `relay-<computer name>`, updated when your IP changes). Turn it off with `AZURE_AUTO_FIREWALL=0`.

Needed once on the PC (PowerShell): `winget install -e --id Microsoft.AzureCLI`, then a new window and `az login`.
Messages about this appear in the black window (lines starting with `[AZURE]`). Turn the on/off feature off with `AZURE_AUTO_POWER=0`.

Note: while the database is off, the other group (dashboard) can't reach it either.

## What is stored
The tables are created right after connecting (even without data) and start with `f1_`,
so they never clash with other tables in the same database:
- `f1_telemetry_raw`: every packet with timestamp, driver, session, run, type, frame, session time and the raw bytes
- `f1_sessions`: one row per **run** (`run_id`; one driver each) with the game session, driver, track, track length, session type, start, end
- `f1_laps`: lap time and sector times per lap per run, with a flag for invalid laps
- `f1_corners`: per run, lap and official corner: corner time, entry/minimum/exit speed, braking point and full-throttle point
- `f1_lap_traces`: per run and lap: speed, throttle, brake and lap time every 2 metres of the lap (arrays), used for the speed lines

Runs stored before the driver-switch feature keep `run_id` = their `session_uid`; the relay adds the new
columns itself on start (nothing to do by hand).

## Old races
The relay reads (read only) the tables `f22_car_telemetry`, `f22_lap_data`, `f22_motion`, `f22_session` and `f22_session_history` of the earlier collector, if they are in the same database. If that fails, a message appears at the top of the Races tab; run `inspect-db.bat` to see what is in the database.

## Notes
- The track map draws itself during the first lap and is remembered per track in the browser.
- Standings and events are only available live, not in a replay.

## GitHub
The code is on https://github.com/PepijnIriks/f1-telemetry-relay. `.venv`, `exports` and caches are never
committed (`.gitignore`), and there are no passwords in any file, so the repo is safe to share with the group.

# F1 telemetry relay - guide

Everything about using the relay, in detail. The short version is in the [README](../README.md);
the database is explained for the dashboard group in [DATA.md](DATA.md).

## Contents
- [In the game](#in-the-game)
- [Choosing the driver](#choosing-the-driver)
- [What happens when you change drivers](#what-happens-when-you-change-drivers)
- [Comparing drivers per corner](#comparing-drivers-per-corner)
- [In the Races tab](#in-the-races-tab)
- [Leaderboard, delta and records](#leaderboard-delta-and-records)
- [Analysis per run](#analysis-per-run)
- [Mini-sectors and ghost](#mini-sectors-and-ghost)
- [Status page](#status-page)
- [Old races](#old-races)
- [Notes](#notes)
- [What is stored](#what-is-stored)
- [Nothing gets lost](#nothing-gets-lost)
- [The database server on Educloud](#the-database-server-on-educloud)
- [Database login: Windows Credential Manager](#database-login-windows-credential-manager)
- [Compressed storage](#compressed-storage)
- [Disk space and backups](#disk-space-and-backups)
- [Read-only account for the other group](#read-only-account-for-the-other-group)
- [Website only on this PC](#website-only-on-this-pc)
- [Tests](#tests)

## In the game
UDP Telemetry on, format 2022, IP 127.0.0.1 (the relay runs on the game PC itself), port 20777.
Allow Python in the Windows firewall if asked.

## Choosing the driver
Three ways, all doing the same thing (the current run is closed and a new run starts for the new driver):

| Where | How |
|---|---|
| **Website** | Click a name at the top right. Type a new name in "New name" and press Enter to add someone. Hover a name and click × to remove it from the list |
| **"F1 driver" window** | Opens by itself next to the relay. Click a name (or press 1-9). Type a new name + Enter to add, right-click a name to remove it. "Always on top" keeps it above the game (game in windowed / borderless mode) |
| **Black relay window** | Type the number of a name (or a new name) and press Enter. Enter on an empty line shows the list |

The names come from `DRIVERS=` in `config/config.env`; names added later are remembered in `config/drivers.json`.
When `start.bat` starts you can also pick the first driver by number. Turn the window off with `DRIVER_MENU=0`.

## What happens when you change drivers
When you pick another name (website, window or black window), the relay:
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

## In the Races tab
- **Download**: everything of one run as a zip: laps, corners, speed lines and every raw packet (CSV), for a
  backup outside the database or to analyse in Excel/Python. A README in the zip explains the files.
- **Change driver**: forgot to switch? Give the run the right name afterwards. Only the name in `f1_sessions`
  changes (the stored packets are not rewritten, so it is instant and the database does not grow).

## Leaderboard, delta and records
- **Leaderboard tab**: pick a track; per driver the fastest valid lap, best S1/S2/S3 (purple = fastest of all),
  theoretical best (best sectors added up) and consistency. Tick drivers and click **Compare selected** to open
  the corner comparison of their fastest laps.
- **Live delta** (Lap card): during a lap the time difference with the group's fastest lap on this track at the
  same point of the lap. Green = faster. It needs one stored valid lap on that track.
- **Records**: when a valid lap beats the driver's own best on that track, or the best of everyone, a pop-up
  appears and an event is added (`[RECORD]` in the black window).

## Analysis per run
In the Races tab, click a run:
- **Consistency**: 0-100, how close the valid laps are to each other (laps slower than 107% of the best,
  like spins or out laps, are left out; 1% spread = 90).
- **Tyres and strategy**: wear of each tyre per lap, tyre changes and temperatures, and a simple pit advice
  (pit window when the worst tyre passes 60% wear or lap times drop more than 1.5% for two laps).
- **Report (PDF)**: a clean printable page with winner, results, lap times and the corner-by-corner table;
  click "Save as PDF". In the corner comparison the same button makes a report of all selected runs.

## Mini-sectors and ghost
In the corner comparison, the track map splits the lap into 25 equal mini-sectors and colours each one by the
driver who was fastest there. **Play ghost** lets every driver's lap drive over the map at the same time
(1x to 8x, or drag the slider). The track outline is worked out once per track from the raw data and kept in
`config/trackmaps.json`.

## Status page
Open http://127.0.0.1:8000/status (or "Status" at the top): relay running, game data coming in, who drives,
database connected, packets stored today and anything still waiting on this PC. Every minute the relay also
writes this to the table `f1_status` (one row, `info` as JSON), so the other group can check from their side
whether data is coming in: if `updated_at` is more than a few minutes old, the relay is not running.

## Old races
The relay reads (read only) the tables `f22_car_telemetry`, `f22_lap_data`, `f22_motion`, `f22_session` and `f22_session_history` of the earlier collector, if they are in the same database. If that fails, a message appears at the top of the Races tab; `tools.bat` > 1 shows what is in the database.

## Notes
- The track map draws itself during the first lap and is remembered per track in the browser.
- Standings and events are only available live, not in a replay.

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

Every table and column is explained in [DATA.md](DATA.md) (written for the group that builds the dashboard).

## Nothing gets lost
Everything that is not in the database yet (VM off or being updated, no internet, relay closed) is written
to the folder `data\spool` on this PC. As soon as the database can be reached again, the relay sends those files to
the database (oldest first, new data waits until they are in) and only removes a file after the database confirmed it.
This also works after a restart, a closed window or a PC shutdown. In the black window you see
`[DB] Sent ... saved packets from this PC to the database`. Don't delete `data\spool` while it has files.

## The database server on Educloud
The database is PostgreSQL 16 (with TimescaleDB) on an Ubuntu VM in **Fontys Educloud**. It is always on, so
there is nothing to turn on or off and no firewall rule per computer: every PC with the login can store data,
from school, from home or anywhere else. The VM is set up with one script, `server/install-educloud.sh`;
step by step in [EDUCLOUD.md](EDUCLOUD.md). What it sets up:
- database `f1` with the login `f1_relay` (a long random password made on the VM),
- **encrypted connections only** (TLS); unencrypted connections are refused,
- from outside only the accounts `f1_relay` and `f1_reader`, only to the database `f1`; the superuser
  `postgres` only works on the VM itself,
- firewall (ufw): only SSH and PostgreSQL (port 5432) are open; fail2ban blocks SSH password guessing,
- a backup of the whole database every night in `/var/backups/f1` on the VM (the last 14 are kept).

## Database login: Windows Credential Manager
No username or password is stored in the code, in `config/config.env` or on GitHub. The full login line
(`postgresql://f1_relay:<password>@<VM address>:5432/f1?sslmode=require`) is kept in the
**Windows Credential Manager** of each PC (Control Panel > Credential Manager > Windows Credentials >
`f1-telemetry-relay`).

`start.bat` asks for it the first time on a computer (later again: `tools.bat` > 6 Database login). On the VM,
show the line with `sudo bash install-educloud.sh --show` and paste it (you don't see it while pasting). Or press
Enter and type the address and password separately. The login is tested before it is stored.

At the top of the website it says "database OK" when it works; otherwise it shows what is wrong (also in the
black window, lines starting with `[SECRETS]` and `[DB]`). Note: everyone who uses the same Windows account on
the game PC can use the stored login. For a quick local test you can pass a connection string in the
environment variable `DATABASE_URL` instead.

## Compressed storage
The raw packets are by far the biggest part of the database. Run **`tools.bat` > 3 Compress the database** once
(relay closed) to store them compressed. **All data is kept** and stays readable with normal SQL, for the
website, the CSV export and the other group alike.

How it works:
1. The packets are copied in steps into a TimescaleDB table that is split per week. Weeks that are over and
   7 days old are compressed; from then on that happens automatically.
2. The copy is checked before anything is swapped: same number of packets, same number of bytes, same ids.
3. Only then the tables are swapped (in one go). The old uncompressed table stays as `f1_telemetry_raw_old`
   until you type `YES`, or later with `tools.bat` > 4 (which checks again first).

Safe to stop: close the window at any moment and start it again, it continues where it was. It pauses on its
own before the database gets within 3 GB of its storage limit (`DB_STORAGE_GB` in `config/config.env`). `tools.bat` > 5 removes only a
half-made copy, never your data.

TimescaleDB must be installed on the server; `install-educloud.sh` does that. If it is not, the script says
so (run `install-educloud.sh` on the VM again) and meanwhile offers a smaller clean-up: the big
index on `session_uid` is replaced by a small one for only the old rows that have no `run_id`.
It can also compress the `f22_` tables of the earlier collector.

How much space it saves depends on the data: in a test with simulated races the raw packets became
about 19x smaller; real game data varies more, so expect less. After the upgrade the black window shows the
real number every 10 minutes (`[DB] Storage: ... compressed 6.2x (saves ... GB)`).

## Disk space and backups
- **Nothing is ever thrown away:** every packet is stored, so all data can be found later.
- **Compressed:** after the compression (`tools.bat` > 3) the packets take far less space on the VM's disk.
- **Warning before it is full:** a full disk stops PostgreSQL. When the database passes `DB_WARN_GB`
  (default 45), the black window and the website show a warning, so the VM's disk can be made bigger in
  Educloud in time. Set `DB_STORAGE_GB` to the size of the VM's disk.
- The size is printed every 10 minutes in the black window (`[DB] Storage: ...`) and shown on the Raw tab.
- **Backups:** every night at 04:15 the VM writes a full backup to `/var/backups/f1` (the last 14 days are kept).
  Make one by hand with `sudo f1-backup`. These backups are on the same VM: for a copy elsewhere, use
  `tools.bat` > 2 (CSV export) or download a `.dump` file with `scp` (see [EDUCLOUD.md](EDUCLOUD.md)).

## Read-only account for the other group
Run `tools.bat` > 7. It makes the account `f1_reader` that can read every table, also tables made later, but
can never change or delete anything. The window shows its login once (it is also kept in the Windows
Credential Manager of this PC); send it to the other group privately. Running it again gives a new password
(the old one stops working). They can connect from anywhere (encrypted only); no firewall rule is needed.

## Website only on this PC
By default the website and the telemetry port only accept connections from this PC (`WEB_HOST=127.0.0.1`,
`UDP_HOST=127.0.0.1`), so nobody else on the (school) wifi can see the data or change the driver.
Want to watch on a phone or tablet? Set `WEB_HOST=0.0.0.0` in `config/config.env` and open
`http://<IP of this PC>:8000` there (anyone on that network can then use it).

## Tests
`tools.bat` > 8: about 25 automated tests run in a few seconds, without the game and without
the real database. They send a recorded (simulated) race through the relay and check that every packet is passed on
unchanged, that laps, sectors, corners and lap traces come out right, that a driver switch starts a new run,
and the calculations (consistency, pit advice, corner comparison) and the safety net on disk.
Database tests (including the whole compression upgrade) run when `TEST_DATABASE_URL` points to a local,
empty test database; on GitHub they always run, against a throw-away database.

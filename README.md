# F1 22 Telemetry Relay

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16%20%2B%20TimescaleDB-4169E1?logo=postgresql&logoColor=white)
![Fontys Educloud](https://img.shields.io/badge/Hosted%20on-Fontys%20Educloud-663399?logo=ubuntu&logoColor=white)
![Windows](https://img.shields.io/badge/Platform-Windows-0078D6?logo=windows&logoColor=white)
![Tests](https://github.com/PepijnIriks/f1-telemetry-relay/actions/workflows/tests.yml/badge.svg)

Live telemetry from the **F1 22** game on a web dashboard, with every packet stored in an
**PostgreSQL** database on a **Fontys Educloud** VM, so drivers can be compared lap by lap and corner by corner afterwards.

Built for a Fontys school project: four drivers take turns on one simulator PC, and another group builds
their own dashboard on the stored data.

![Live dashboard](docs/images/live.png)

## Features
- **Live dashboard** - speed, gear, RPM, pedals, ERS, fuel, tyres, brakes, damage, track map, G-force,
  standings and race events, in real time; plus a **live delta** to the group's fastest lap
- **Races, leaderboard and corner comparison** - every stored run with lap and sector times, replay, tyre
  wear and pit advice, a printable report; drivers compared on every official F1 corner, with a ghost lap
- **One run per driver** - switch drivers on the website or in a small always-on-top window
- **Nothing gets lost** - data that can't reach the database waits on the PC and is sent later; the VM makes a
  backup every night
- **Own server** - one script sets up the Educloud VM: PostgreSQL with compression, encrypted connections only,
  a firewall and nightly backups ([server guide](docs/EDUCLOUD.md))
- **No passwords in the code** - the database login lives in the Windows Credential Manager of each PC

| Leaderboard | Mini-sectors and ghost |
|---|---|
| ![Leaderboard](docs/images/leaderboard.png) | ![Mini-sectors and ghost](docs/images/ghost.png) |

| Corner analysis | Driver window |
|---|---|
| ![Corner analysis](docs/images/corner-analysis.png) | ![Driver window](docs/images/driver-window.png) |

## How it works
```mermaid
flowchart LR
    G[F1 22 game] -- UDP 20777 --> R[relay]
    R -- WebSocket, live --> W[Website<br/>Live / Raw / Races]
    R -- batches, encrypted, in the background --> DB[(PostgreSQL<br/>Educloud VM)]
    R -. VM unreachable? .-> S[data/spool<br/>on this PC] -. later .-> DB
    KV[Windows Credential Manager] -- database login --> R
    DB -- every night --> B[backup on the VM]
    D[F1 driver window] -- who drives --> R
    DB --> O[Other group's dashboard<br/>read-only account]
```
The live view never waits for the database: packets go to the browser right away and to the database in batches.

## The database server (once)
Make an Ubuntu VM in Fontys Educloud and run `server/install-educloud.sh` on it: step by step in
[docs/EDUCLOUD.md](docs/EDUCLOUD.md). It shows the login line that the PCs need.

## Put it on a computer (the game PC, or any other)
1. On GitHub: **Code > Download ZIP**, and unzip it where you like (for example on the desktop).
   Or, with Git: `git clone https://github.com/PepijnIriks/f1-telemetry-relay.git`
2. Double-click **`start.bat`**. The first time it:
   - offers to install **Python** when it is missing (then close the window and start it again),
   - makes the Python environment and installs the packages,
   - asks once for the database login (the line the Educloud VM shows) and keeps it in the Windows Credential Manager.
3. In F1 22: *Settings > Telemetry Settings*: UDP Telemetry **On**, Broadcast Mode **Off**, IP **127.0.0.1**,
   port **20777**, send rate **20 Hz** or higher, format **2022**. Allow Python in the Windows firewall if asked.

Nothing in the code has to be changed for another computer: the settings in `config/config.env` are the same
everywhere; each computer only needs the database login once. Only if the group changes: the names in
`DRIVERS=` in `config/config.env`.

## Every day
| | |
|---|---|
| **`start.bat`** | Start the relay: the website opens at http://127.0.0.1:8000, plus the small "F1 driver" window |
| **`demo.bat`** | Demo without the game: a fake game drives laps, nothing is stored |
| **`tools.bat`** | Everything else, in one menu: what is in the database, export to CSV, compress the database, database login, read-only account for the dashboard group, tests, GitHub sync |

## What is in this folder
| | |
|---|---|
| `start.bat`, `demo.bat`, `tools.bat` | The three things you double-click |
| `server/` | `install-educloud.sh`: sets up the database server on the Educloud VM |
| `config/` | Settings (`config.env`, no passwords) and the official F1 corner list |
| `src/` | The relay (Python): start file `relay.py`, one module per job |
| `web/` | The website |
| `docs/` | [Guide](docs/GUIDE.md) with all details, [database guide for the dashboard group](docs/DATA.md), [Educloud server](docs/EDUCLOUD.md), [changelog](docs/CHANGELOG.md), screenshots |
| `tests/` | Automated tests (`tools.bat` > 8; GitHub runs them for every change) |
| `data/` | Made by this computer itself: data waiting for the database, exports, caches, logs. Not on GitHub |
| `.venv/` | The Python environment, made by `start.bat`. Not on GitHub; never copy it to another computer |

## GitHub and versions
Every change gets its own commit with an explanation of what changed and why: see **Commits** on GitHub
(click one to see the changed lines). Bigger changes come as a **pull request** with a summary;
[docs/CHANGELOG.md](docs/CHANGELOG.md) says per version what changed. `tools.bat` > 9 gets the newest
version onto this computer, and puts changes made on this computer on GitHub (with a short description).
The branch `v1.0-start` keeps the version from before v1.1.

"""
Settings and shared state of the relay. Every other relay module imports from here, so there is one copy of:
the config (config/config.env), the live state for the website, the run bookkeeping, the queue to the
database writer and the Azure on/off manager.
"""
import queue
import sys
import time
from pathlib import Path

from azure_power import AzurePower
from drivers import DriverList


ROOT = Path(__file__).resolve().parent.parent


def load_config():
    cfg = {"UDP_PORT": "20777", "HTTP_PORT": "8000", "DATABASE_URL": "",
           "DB_FLUSH_SECONDS": "2", "DB_SOURCE": "", "DB_SECRET_ERROR": ""}
    path = ROOT / "config" / "config.env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"')
    return cfg


CFG = load_config()
# Demo mode (demo.bat): fake data from the simulator is shown everywhere, but NOTHING is written to Azure,
# so demos never end up in the real data. Reading (Races, Leaderboard) still works.
DEMO = "--demo" in sys.argv

state = {
    "driver": "", "player": 0, "started": time.time(), "total": 0, "bytes": 0, "by_type": {},
    "db": {"enabled": False, "connected": False, "written": 0, "pending": 0, "error": "",
           "last_ok": 0, "hint": ""},
}
clients = set()
live_buffer = []            # fast path: raw packets to the browser
live_dirty = [False]
live = {"session": {}, "lap": {}}
db_queue = queue.Queue()    # slow path: to Azure
sess = {}                   # per-run metadata (for the races overview), key = run_id
lapstate = {}               # per run: track when a lap is completed
runs = {}                   # game session_uid -> run_id that is currently being recorded
carry_from = {}             # game session_uid -> run_id of the previous driver (until the new run starts)
cornerbuf = {}              # per run: samples of the lap being driven, for the corner analysis
names, numbers, teams = {}, {}, {}      # per car index (from Participants)
grid_state, grid_dirty, last_grid = [], [False], [0.0]
events, events_dirty = [], [False]
# Personal bests / track records (from the database) and the reference lap for the live delta
records = {"loaded": False, "pb": {}, "track": {}}
refs = {}                      # track_id -> fastest lap of the group with its lap trace (or None)
ref_wanted = {"track": None, "reload": 0.0}


def car_name(i):
    return names.get(i) or f"Car {i + 1}"


def fmt_ms(ms):
    if not ms or ms <= 0:
        return "-:--.---"
    return f"{int(ms // 60000)}:{(ms % 60000) / 1000:06.3f}"


# Names to choose from (website, driver menu window, black window); see src/drivers.py
DRIVERS = DriverList(CFG)
loop_ref = []                  # the asyncio loop (so other threads can hand work to it)


POWER = AzurePower(CFG, "", state)      # the server name is filled in once the Key Vault secret is read


# New rows carry run_id; rows from before runs existed only have session_uid (and their run_id = session_uid).
RUN_FILTER = "(run_id = %s OR (run_id IS NULL AND session_uid = %s))"

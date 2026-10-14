"""
F1 22 telemetry relay
- Listens on UDP (default port 20777) for packets from the game
- Forwards them IMMEDIATELY to the website (http://localhost:8000) over WebSocket
- Parses the player's car + the whole field for the Live dashboard
- Writes to PostgreSQL (Azure) in batches in the background: raw packets,
  an overview of sessions/races and lap times. The database can never slow down the live view.
- Every driver gets their own "run": changing the driver name closes the current run (it is kept
  in the database) and starts a new one, even if the game session simply continues.
- Per lap it works out the official F1 corners (corner time, entry/minimum/exit speed, braking point,
  throttle point) so the Races tab can compare drivers corner by corner.
"""
import asyncio
import bisect
import json
import queue
import struct
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import web

import intro
import secrets_store
from azure_power import AzurePower
from drivers import DriverList

ROOT = Path(__file__).resolve().parent.parent

# ----------------------------------------------------------------- F1 22 packet formats (little endian)
HEADER = struct.Struct("<HBBBBQfIBB")                      # 24 bytes
HEADER_FIELDS = ("format", "major", "minor", "version", "packet_id",
                 "session_uid", "session_time", "frame", "player", "player2")
TEL_STRUCT = struct.Struct("<HfffBbHBBH4H4B4BH4f4B")       # 60 bytes per car (CarTelemetry)
LAP_STRUCT = struct.Struct("<IIHHfff14BHHB")               # 43 bytes per car (LapData)
STATUS_STRUCT = struct.Struct("<BBBBBfffHHBBHBBBbfBfffB")  # 47 bytes per car (CarStatus)
DAMAGE_STRUCT = struct.Struct("<4f26B")                    # 42 bytes per car (CarDamage)
MOTION_SIZE = 60                                           # bytes per car (Motion)
assert (HEADER.size, TEL_STRUCT.size, LAP_STRUCT.size, STATUS_STRUCT.size, DAMAGE_STRUCT.size) == (24, 60, 43, 47, 42)

PACKET_NAMES = {
    0: "Motion", 1: "Session", 2: "LapData", 3: "Event", 4: "Participants",
    5: "CarSetups", 6: "CarTelemetry", 7: "CarStatus", 8: "FinalClassification",
    9: "LobbyInfo", 10: "CarDamage", 11: "SessionHistory",
}
TRACKS = {
    0: "Melbourne", 1: "Paul Ricard", 2: "Shanghai", 3: "Sakhir (Bahrain)", 4: "Barcelona",
    5: "Monaco", 6: "Montreal", 7: "Silverstone", 8: "Hockenheim", 9: "Hungaroring",
    10: "Spa", 11: "Monza", 12: "Singapore", 13: "Suzuka", 14: "Abu Dhabi", 15: "Texas",
    16: "Brazil", 17: "Austria", 18: "Sochi", 19: "Mexico", 20: "Baku",
    21: "Sakhir Short", 22: "Silverstone Short", 23: "Texas Short", 24: "Suzuka Short",
    25: "Hanoi", 26: "Zandvoort", 27: "Imola", 28: "Portimao", 29: "Jeddah", 30: "Miami",
}
SESSION_TYPES = {
    0: "Unknown", 1: "Practice 1", 2: "Practice 2", 3: "Practice 3",
    4: "Short practice", 5: "Qualifying 1", 6: "Qualifying 2", 7: "Qualifying 3",
    8: "Short qualifying", 9: "One-shot qualifying", 10: "Race", 11: "Race 2",
    12: "Race 3", 13: "Time trial",
}
WEATHER = {0: "Clear", 1: "Light cloud", 2: "Overcast", 3: "Light rain",
           4: "Heavy rain", 5: "Storm"}
COMPOUNDS = {16: "Soft", 17: "Medium", 18: "Hard", 7: "Inter", 8: "Wet"}


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


def car_name(i):
    return names.get(i) or f"Car {i + 1}"


def fmt_ms(ms):
    if not ms or ms <= 0:
        return "-:--.---"
    return f"{int(ms // 60000)}:{(ms % 60000) / 1000:06.3f}"


# ----------------------------------------------------------------- Runs (one per driver)
def new_run(uid, now, carry=None):
    """Starts a new run for game session `uid` with the current driver."""
    run_id = f"{uid}-{int(now * 1000)}"
    runs[uid] = run_id
    old = sess.get(carry) if carry else None
    sess[run_id] = {"uid": uid, "first": now, "last": now, "packets": 0,
                    "track": old["track"] if old else -1, "stype": old["stype"] if old else -1,
                    "tlen": old.get("tlen", 0) if old else 0, "driver": state["driver"], "dirty": True}
    if carry and carry in lapstate:
        prev = lapstate[carry]
        # The lap that is in progress during the switch was driven by two people: it never counts.
        mid_lap = live.get("lap", {}).get("cur", 0) > 0
        lapstate[run_id] = {"last_ms": prev["last_ms"], "s1": 0, "s2": 0, "invalid": mid_lap, "laps": []}
    cornerbuf.pop(carry, None)
    return run_id


def switch_driver(name, now):
    old_name = state["driver"]
    if name == old_name:
        return
    state["driver"] = name
    uid = state.get("uid")
    run_id = runs.get(uid)
    meta = sess.get(run_id)
    if meta is None:
        print(f"[DRIVER] {name or '(empty)'} is now driving (a new run starts with the first game data)")
    elif not meta["driver"]:
        meta["driver"] = name          # the run had no name yet: just give it this name
        meta["dirty"] = True
        print(f"[DRIVER] Current run now belongs to {name}")
    else:
        meta["dirty"] = True           # last write of the old run
        laps = len(lapstate.get(run_id, {}).get("laps", []))
        runs.pop(uid, None)            # the new run starts with the next packet of the game
        carry_from[uid] = run_id
        cornerbuf.pop(run_id, None)
        print(f"[DRIVER] {old_name} -> {name or '(empty)'}: run of {old_name} saved "
              f"({laps} lap(s)), a new run for {name or '(empty)'} starts with the next game data")
    events.insert(0, {"t": now, "text": f"Driver: {name or '(empty)'}" + (f" (was {old_name})" if old_name else "")})
    del events[40:]
    events_dirty[0] = True
    live_dirty[0] = True


# Names to choose from (website, driver menu window, black window); see src/drivers.py
DRIVERS = DriverList(CFG)
loop_ref = []


def console_menu():
    """Change the driver from the black window: type a number (or a new name) and press Enter."""
    while True:
        try:
            line = input()
        except (EOFError, OSError):
            return
        line = line.strip()
        if not line:
            print(f"[DRIVER] Now: {state['driver'] or '(empty)'}.  Choose: {DRIVERS.numbered() or '(no names yet)'}"
                  "  -> type a number or a new name + Enter")
            continue
        name = DRIVERS.resolve(line)
        if loop_ref:
            loop_ref[0].call_soon_threadsafe(switch_driver, name, time.time())


# ----------------------------------------------------------------- Official F1 corners
CORNERS_FILE = ROOT / "config" / "corners.json"
try:
    CORNERS = {int(k): v for k, v in json.loads(CORNERS_FILE.read_text(encoding="utf-8")).items()
               if not k.startswith("_")}
except Exception as e:
    print(f"[CORNERS] config/corners.json could not be read ({e}); no corner analysis")
    CORNERS = {}
BEFORE_M, AFTER_M, BRAKE_SEARCH_M = 150.0, 100.0, 400.0
VIEW_BEFORE_M, VIEW_AFTER_M = 280.0, 180.0     # part of the track shown in the corner analysis
TRACE_STEP = 2.0                               # the lap trace is stored every 2 metres


def corner_layout(track_id, game_len):
    """Corner zones for a track, scaled from the real lap length to the game's lap length.
    Returns [(label, apex, zone_start, zone_end, brake_search_start, view_start, view_end)] in metres of
    lap distance. zone = what the corner time is measured over; view = the wider part shown in the analysis."""
    d = CORNERS.get(track_id)
    if not d:
        return []
    real_len = float(d.get("length") or 0) or float(game_len or 1)
    L = float(game_len or real_len)
    k = L / real_len
    pts = sorted(((lab, (dist + float(d.get("offset", 0))) * k) for lab, dist in d["corners"]), key=lambda c: c[1])
    out = []
    for i, (lab, a) in enumerate(pts):
        prev_mid = (pts[i - 1][1] + a) / 2 if i else 0.0
        next_mid = (a + pts[i + 1][1]) / 2 if i + 1 < len(pts) else L
        start = max(a - BEFORE_M * k, prev_mid, 0.0)
        end = min(a + AFTER_M * k, next_mid, L)
        prev_apex = pts[i - 1][1] if i else 0.0
        next_apex = pts[i + 1][1] if i + 1 < len(pts) else L
        out.append((lab, a, start, end, max(a - BRAKE_SEARCH_M * k, prev_mid, 0.0),
                    max(a - VIEW_BEFORE_M * k, prev_apex, 0.0), min(a + VIEW_AFTER_M * k, next_apex, L)))
    return out


def clean_samples(samples):
    """Keep one increasing run of lap distance (a flashback rewinds the distance: drop what was undone)."""
    out = []
    for s in samples:
        if s[0] < 0:
            continue
        while out and out[-1][0] >= s[0]:
            out.pop()
        out.append(s)
    return out


def corner_stats(samples, track_id, game_len):
    """samples: [(lap_distance_m, lap_time_ms, speed_kmh, throttle, brake)] of ONE lap.
    Returns one dict per official corner, or [] if the lap is not complete enough."""
    layout = corner_layout(track_id, game_len)
    s = clean_samples(samples)
    if not layout or len(s) < 50:
        return []
    L = float(game_len or 0) or s[-1][0]
    if s[0][0] > 0.05 * L or s[-1][0] < 0.95 * L:
        return []                       # relay started mid-lap or the lap was cut off
    dist = [x[0] for x in s]

    def at(d, col):
        i = bisect.bisect_left(dist, d)
        if i <= 0:
            return s[0][col]
        if i >= len(s):
            return s[-1][col]
        a, b = s[i - 1], s[i]
        f = (d - a[0]) / ((b[0] - a[0]) or 1)
        return a[col] + (b[col] - a[col]) * f

    out = []
    for idx, (lab, apex, z0, z1, b0, _v0, _v1) in enumerate(layout):
        i0, i1 = bisect.bisect_left(dist, z0), bisect.bisect_right(dist, z1)
        zone = s[i0:i1]
        if len(zone) < 2:
            continue
        low = min(zone, key=lambda x: x[2])
        bi = bisect.bisect_left(dist, b0)
        brake_at = next((x[0] for x in s[bi:bisect.bisect_right(dist, low[0])] if x[4] > 0.1), None)
        gas_at = next((x[0] for x in s[bisect.bisect_left(dist, low[0]):i1] if x[3] >= 0.95), None)
        out.append({
            "corner": lab, "idx": idx, "apex": round(apex, 1),
            "time": int(round(at(z1, 1) - at(z0, 1))),
            "entry": round(at(z0, 2), 1), "min": round(low[2], 1), "exit": round(at(z1, 2), 1),
            "min_at": round(low[0] - apex, 1),
            "brake": round(apex - brake_at, 1) if brake_at is not None else None,
            "gas": round(gas_at - apex, 1) if gas_at is not None else None,
            "max_brake": round(max(x[4] for x in s[bi:i1]), 2) if i1 > bi else 0.0,
        })
    return out


def lap_trace(samples, game_len):
    """Resamples one lap to a point every TRACE_STEP metres: speed, throttle, brake and lap time.
    Used for the corner analysis (speed lines per driver). None if the lap is not complete."""
    s = clean_samples(samples)
    if len(s) < 50:
        return None
    L = float(game_len or 0) or s[-1][0]
    if s[0][0] > 0.05 * L or s[-1][0] < 0.95 * L:
        return None
    n = int(min(L, s[-1][0]) // TRACE_STEP) + 1
    sp, th, br, tm = [], [], [], []
    j = 0
    for i in range(n):
        d = i * TRACE_STEP
        while j < len(s) - 2 and s[j + 1][0] < d:
            j += 1
        a, b = s[j], s[j + 1]
        f = max(0.0, min(1.0, (d - a[0]) / ((b[0] - a[0]) or 1)))
        tm.append(int(round(a[1] + (b[1] - a[1]) * f)))
        sp.append(round(a[2] + (b[2] - a[2]) * f, 1))
        th.append(round(a[3] + (b[3] - a[3]) * f, 2))
        br.append(round(a[4] + (b[4] - a[4]) * f, 2))
    return {"step": TRACE_STEP, "speed": sp, "throttle": th, "brake": br, "time": tm}


def trace_row(run_id, lap, track_id, tlen, t):
    return (run_id, lap, track_id, int(tlen or 0), t["step"], t["speed"], t["throttle"], t["brake"], t["time"])


def corner_rows(run_id, lap, stats):
    return [(run_id, lap, c["corner"], c["idx"], c["apex"], c["time"], c["entry"], c["min"], c["exit"],
             c["min_at"], c["brake"], c["gas"], c["max_brake"]) for c in stats]


# ----------------------------------------------------------------- Parsing
def parse_telemetry(data, p):
    v = TEL_STRUCT.unpack_from(data, 24 + p * TEL_STRUCT.size)
    live.update({
        "speed": v[0], "throttle": round(v[1], 3), "steer": round(v[2], 3),
        "brake": round(v[3], 3), "gear": v[5], "rpm": v[6], "drs": v[7], "rev": v[8],
        "brakeTemps": list(v[10:14]), "tyreSurf": list(v[14:18]),
        "tyreInner": list(v[18:22]), "engineTemp": v[22],
        "tyrePress": [round(x, 1) for x in v[23:27]],
    })
    live_dirty[0] = True


def parse_status(data, p):
    v = STATUS_STRUCT.unpack_from(data, 24 + p * STATUS_STRUCT.size)
    live.update({
        "brakeBias": v[3], "pitLimiter": bool(v[4]), "fuel": round(v[5], 1),
        "fuelCap": round(v[6]), "fuelLaps": round(v[7], 1), "maxRpm": v[8] or 13000, "drsAllowed": bool(v[11]),
        "compound": COMPOUNDS.get(v[14], "?"), "tyreAge": v[15],
        "ers": round(v[17] / 4_000_000 * 100), "ersMode": v[18],
    })
    live_dirty[0] = True


def parse_damage(data, p):
    v = DAMAGE_STRUCT.unpack_from(data, 24 + p * DAMAGE_STRUCT.size)
    live["tyreWear"] = [round(x) for x in v[0:4]]
    live["dmg"] = {
        "tyre": list(v[4:8]), "brake": list(v[8:12]), "fwl": v[12], "fwr": v[13], "rw": v[14],
        "floor": v[15], "diff": v[16], "side": v[17], "drsFault": bool(v[18]), "ersFault": bool(v[19]),
        "gearbox": v[20], "engine": v[21], "mguh": v[22], "es": v[23], "ce": v[24], "ice": v[25],
        "mguk": v[26], "tc": v[27], "blown": bool(v[28]), "seized": bool(v[29]),
    }
    live_dirty[0] = True


def parse_motion(data, p):
    x, _y, z = struct.unpack_from("<3f", data, 24 + p * MOTION_SIZE)
    live["pos"] = [round(x, 1), round(z, 1)]
    live_dirty[0] = True


def parse_participants(data):
    for i in range(22):
        off = 25 + i * 56
        if off + 56 > len(data):
            break
        raw = data[off + 7:off + 55].split(b"\0")[0]
        names[i] = raw.decode("utf-8", "replace").strip() or f"Car {i + 1}"
        numbers[i] = data[off + 5]
        teams[i] = data[off + 3]
    grid_dirty[0] = True


def build_grid(data, p):
    rows = []
    for i in range(22):
        off = 24 + i * LAP_STRUCT.size
        if off + LAP_STRUCT.size > len(data):
            break
        v = LAP_STRUCT.unpack_from(data, off)
        if v[7] == 0:
            continue
        # position, name, number, team, lap, last lap (ms), pit status, number of stops, is this me
        rows.append([v[7], car_name(i), numbers.get(i, 0), teams.get(i, -1), v[8], v[0], v[9], v[10],
                     1 if i == p else 0])
    rows.sort(key=lambda r: r[0])
    grid_state[:] = rows
    grid_dirty[0] = True


def parse_lap(data, p, uid, now, meta):
    v = LAP_STRUCT.unpack_from(data, 24 + p * LAP_STRUCT.size)
    last_ms, cur_ms, s1, s2 = v[0], v[1], v[2], v[3]
    pos, lapnum, pit, sector, invalid, penalties = v[7], v[8], v[9], v[11], v[12], v[13]
    result_status = v[19]

    # samples for the corner analysis (lap distance, lap time, speed, throttle, brake)
    cb = cornerbuf.setdefault(uid, {"lap": lapnum, "s": [], "prev": (None, [])})
    if lapnum != cb["lap"]:
        cb["prev"] = (cb["lap"], cb["s"])
        cb["lap"], cb["s"] = lapnum, []
    if pit == 0:
        cb["s"].append((v[4], cur_ms, live.get("speed", 0), live.get("throttle", 0), live.get("brake", 0)))

    st = lapstate.setdefault(uid, {"last_ms": 0, "s1": 0, "s2": 0, "invalid": False, "laps": []})
    if s1 > 0:
        st["s1"] = s1
    if s2 > 0:
        st["s2"] = s2
    if invalid:
        st["invalid"] = True
    if last_ms > 0 and last_ms != st["last_ms"]:
        done = lapnum if result_status >= 3 else lapnum - 1
        if done >= 1:
            s3 = last_ms - st["s1"] - st["s2"] if st["s1"] and st["s2"] else 0
            db_queue.put(("lap", (meta["uid"], uid, done, last_ms, st["s1"], st["s2"], max(s3, 0), st["invalid"])))
            samples = cb["s"] if cb["lap"] == done else cb["prev"][1] if cb["prev"][0] == done else []
            stats = corner_stats(samples, meta["track"], meta.get("tlen"))
            if stats:
                db_queue.put(("corners", corner_rows(uid, done, stats)))
            trace = lap_trace(samples, meta.get("tlen"))
            if trace:
                db_queue.put(("trace", trace_row(uid, done, meta["track"], meta.get("tlen"), trace)))
            st["laps"].insert(0, [done, last_ms, st["invalid"]])
            del st["laps"][8:]
            valid = [l[1] for l in st["laps"] if not l[2]]
            best_candidates = valid or [l[1] for l in st["laps"]]
            st["best"] = min(best_candidates)
            events.insert(0, {"t": now, "text": f"Lap {done} completed: {fmt_ms(last_ms)}"})
            del events[40:]
            events_dirty[0] = True
        st["s1"] = st["s2"] = 0
        st["invalid"] = False
    st["last_ms"] = last_ms

    live["lap"] = {"pos": pos, "num": lapnum, "cur": cur_ms, "last": last_ms, "s1": s1, "s2": s2, "dist": round(v[4]),
                   "sector": sector, "invalid": bool(invalid), "pit": pit, "pen": penalties,
                   "best": st.get("best", 0), "laps": st["laps"]}
    live_dirty[0] = True

    if now - last_grid[0] >= 0.5:
        last_grid[0] = now
        build_grid(data, p)


def parse_session(data, meta):
    weather, track_temp, air_temp, total_laps, track_len = struct.unpack_from("<BbbBH", data, 24)
    session_type, track_id = struct.unpack_from("<Bb", data, 30)
    meta["track"] = track_id
    meta["stype"] = session_type
    meta["tlen"] = track_len
    live["session"] = {
        "track": TRACKS.get(track_id, f"Track {track_id}"), "type": SESSION_TYPES.get(session_type, "?"),
        "weather": WEATHER.get(weather, "?"), "trackTemp": track_temp, "airTemp": air_temp,
        "totalLaps": total_laps, "trackLength": track_len,
    }
    live_dirty[0] = True


def parse_event(data, now):
    code = bytes(data[24:28]).decode("ascii", "replace")
    text = None
    if code == "SSTA":
        text = "Session started"
    elif code == "SEND":
        text = "Session ended"
    elif code == "FTLP":
        idx, t = struct.unpack_from("<Bf", data, 28)
        text = f"Fastest lap: {car_name(idx)} {fmt_ms(t * 1000)}"
    elif code == "RTMT":
        text = f"{car_name(data[28])} retired"
    elif code == "CHQF":
        text = "Chequered flag"
    elif code == "RCWN":
        text = f"{car_name(data[28])} wins the race"
    elif code == "PENA":
        secs = data[32]
        text = f"Penalty for {car_name(data[30])}" + (f" (+{secs} s)" if secs else "")
    elif code == "SPTP":
        idx, speed = struct.unpack_from("<Bf", data, 28)
        text = f"Speedtrap {car_name(idx)}: {round(speed)} km/h"
    elif code == "STLG":
        text = f"Start lights: {data[28]}"
    elif code == "LGOT":
        text = "Lights out!"
    elif code == "DRSE":
        text = "DRS enabled"
    elif code == "DRSD":
        text = "DRS disabled"
    elif code == "FLBK":
        text = "Flashback used"
    if text:
        events.insert(0, {"t": now, "text": text})
        del events[40:]
        events_dirty[0] = True


# ----------------------------------------------------------------- Saving storage in Azure
# The game sends the fast packets (Motion, LapData, CarTelemetry, CarStatus) up to 60 times per second.
# For the database that is far more than needed: replays and the corner comparison of old runs work fine
# with 10 per second. The live view and the live corner analysis still use EVERY packet.
FAST_PACKETS = {0, 2, 6, 7}
RAW_HZ = float(CFG.get("RAW_STORE_HZ", "10") or 0)
raw_last = {}


def keep_raw(run_id, pid, now):
    if RAW_HZ <= 0 or pid not in FAST_PACKETS:
        return True
    key = (run_id, pid)
    if now - raw_last.get(key, 0.0) < 1.0 / RAW_HZ - 0.002:
        return False
    raw_last[key] = now
    return True


def storage_check(conn):
    """Shows how full the database is, and removes the OLDEST raw packets when they pass RAW_MAX_GB.
    Lap times, sectors, corner analysis and speed lines (f1_laps, f1_corners, f1_lap_traces, f1_sessions)
    are never removed; old runs then still show up in Races and in the corner comparison."""
    gb = 1024 ** 3
    max_gb = float(CFG.get("RAW_MAX_GB", "40") or 0)
    with conn, conn.cursor() as cur:
        cur.execute("""SELECT pg_database_size(current_database()), pg_total_relation_size('f1_telemetry_raw'),
                              coalesce(n_live_tup, 0), coalesce(n_dead_tup, 0)
                       FROM pg_stat_user_tables WHERE relname = 'f1_telemetry_raw'""")
        row = cur.fetchone()
    if not row:
        return
    db_size, raw_size, live, dead = row
    raw_live = raw_size * live / max(live + dead, 1)          # space taken by rows that still exist
    state["db"]["size_gb"] = round(db_size / gb, 2)
    state["db"]["raw_gb"] = round(raw_live / gb, 2)
    print(f"[DB] Storage: database {db_size / gb:.1f} GB in total, raw packets {raw_live / gb:.1f} GB"
          + (f" (limit {max_gb:g} GB)" if max_gb > 0 else ""), flush=True)
    if max_gb <= 0 or raw_live <= max_gb * gb or live == 0:
        return
    # Remove the oldest raw packets until they are back at 90 % of the limit
    remove = int(live * (1 - (max_gb * 0.9 * gb) / raw_live))
    print(f"[DB] Raw packets are over the limit: removing the oldest {remove:,} raw packets "
          "(lap times and corner analysis stay)...", flush=True)
    with conn, conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = 600000")
        cur.execute("""DELETE FROM f1_telemetry_raw WHERE id IN
                       (SELECT id FROM f1_telemetry_raw ORDER BY id LIMIT %s)""", (remove,))
        print(f"[DB] Removed {cur.rowcount:,} old raw packets; the space is reused for new data.", flush=True)


# ----------------------------------------------------------------- UDP
class UdpProtocol(asyncio.DatagramProtocol):
    def datagram_received(self, data, addr):
        now = time.time()
        if len(data) < HEADER.size:
            return
        h = dict(zip(HEADER_FIELDS, HEADER.unpack_from(data)))
        pid = h["packet_id"]
        uid = str(h["session_uid"])
        state["total"] += 1
        state["bytes"] += len(data)
        state["by_type"][pid] = state["by_type"].get(pid, 0) + 1

        live_buffer.append({
            "t": now, "id": pid, "name": PACKET_NAMES.get(pid, f"Unknown({pid})"),
            "size": len(data), "frame": h["frame"], "st": round(h["session_time"], 3),
            "uid": uid, "fmt": h["format"], "src": addr[0], "hex": data[:48].hex(" "),
            "driver": state["driver"],
        })

        state["uid"] = uid
        run_id = runs.get(uid) or new_run(uid, now, carry=carry_from.pop(uid, None))
        meta = sess[run_id]
        meta["last"] = now
        meta["packets"] += 1
        meta["dirty"] = True

        p = h["player"]
        state["player"] = p
        try:
            if pid == 6:
                parse_telemetry(data, p)
            elif pid == 2:
                parse_lap(data, p, run_id, now, meta)
            elif pid == 7:
                parse_status(data, p)
            elif pid == 10:
                parse_damage(data, p)
            elif pid == 0:
                parse_motion(data, p)
            elif pid == 1:
                parse_session(data, meta)
            elif pid == 4:
                parse_participants(data)
            elif pid == 3:
                parse_event(data, now)
        except struct.error:
            pass

        # Only real driving counts as "racing" for the Azure on/off logic: car telemetry with the
        # car moving and the session clock running. Menus, garage or a paused game don't count.
        if pid == 6 and live.get("speed", 0) > 0 and h["session_time"] != meta.get("race_st"):
            meta["race_st"] = h["session_time"]
            POWER.race()

        if state["db"]["enabled"] and keep_raw(run_id, pid, now):
            db_queue.put(("pkt", (now, meta["driver"], uid, pid, h["frame"],
                                  h["session_time"], p, data, run_id)))


# ----------------------------------------------------------------- Database
DDL = """
CREATE TABLE IF NOT EXISTS f1_telemetry_raw (
    id BIGSERIAL PRIMARY KEY,
    received_at TIMESTAMPTZ NOT NULL,
    driver TEXT,
    session_uid TEXT,
    packet_id SMALLINT,
    frame BIGINT,
    session_time REAL,
    player_idx SMALLINT,
    payload BYTEA
);
CREATE INDEX IF NOT EXISTS f1_telemetry_raw_session_idx
    ON f1_telemetry_raw (session_uid, packet_id, frame);
CREATE TABLE IF NOT EXISTS f1_sessions (
    session_uid TEXT,
    driver TEXT,
    track_id SMALLINT,
    session_type SMALLINT,
    started_at TIMESTAMPTZ,
    last_seen_at TIMESTAMPTZ,
    packets BIGINT
);
CREATE TABLE IF NOT EXISTS f1_laps (
    session_uid TEXT,
    lap_num SMALLINT,
    lap_time_ms INTEGER,
    s1_ms INTEGER,
    s2_ms INTEGER,
    s3_ms INTEGER,
    invalid BOOLEAN
);
-- One "run" per driver: a game session can contain several runs (one per driver change).
-- Rows from before runs existed get run_id = session_uid.
ALTER TABLE f1_telemetry_raw ADD COLUMN IF NOT EXISTS run_id TEXT;
CREATE INDEX IF NOT EXISTS f1_telemetry_raw_run_idx ON f1_telemetry_raw (run_id, packet_id, frame);
ALTER TABLE f1_sessions ADD COLUMN IF NOT EXISTS run_id TEXT;
ALTER TABLE f1_sessions ADD COLUMN IF NOT EXISTS track_length INTEGER;
UPDATE f1_sessions SET run_id = session_uid WHERE run_id IS NULL;
ALTER TABLE f1_sessions DROP CONSTRAINT IF EXISTS f1_sessions_pkey;
CREATE UNIQUE INDEX IF NOT EXISTS f1_sessions_run_idx ON f1_sessions (run_id);
ALTER TABLE f1_laps ADD COLUMN IF NOT EXISTS run_id TEXT;
UPDATE f1_laps SET run_id = session_uid WHERE run_id IS NULL;
ALTER TABLE f1_laps DROP CONSTRAINT IF EXISTS f1_laps_pkey;
CREATE UNIQUE INDEX IF NOT EXISTS f1_laps_run_idx ON f1_laps (run_id, lap_num);
-- Per lap and per official F1 corner (distances in metres, speeds in km/h, times in ms)
CREATE TABLE IF NOT EXISTS f1_corners (
    run_id TEXT,
    lap_num SMALLINT,
    corner TEXT,
    corner_idx SMALLINT,
    apex_m REAL,
    time_ms INTEGER,
    entry_kmh REAL,
    min_kmh REAL,
    exit_kmh REAL,
    min_at_m REAL,
    brake_before_m REAL,
    throttle_after_m REAL,
    max_brake REAL,
    PRIMARY KEY (run_id, lap_num, corner)
);
-- Per lap: speed / throttle / brake / lap time every step_m metres of lap distance (for speed-trace charts)
CREATE TABLE IF NOT EXISTS f1_lap_traces (
    run_id TEXT,
    lap_num SMALLINT,
    track_id SMALLINT,
    track_length INTEGER,
    step_m REAL,
    speed REAL[],
    throttle REAL[],
    brake REAL[],
    time_ms INTEGER[],
    PRIMARY KEY (run_id, lap_num)
);
"""
TRACE_INSERT = """
    INSERT INTO f1_lap_traces (run_id, lap_num, track_id, track_length, step_m, speed, throttle, brake, time_ms)
    VALUES %s ON CONFLICT (run_id, lap_num) DO UPDATE SET
      track_id = EXCLUDED.track_id, track_length = EXCLUDED.track_length, step_m = EXCLUDED.step_m,
      speed = EXCLUDED.speed, throttle = EXCLUDED.throttle, brake = EXCLUDED.brake, time_ms = EXCLUDED.time_ms"""
CORNER_INSERT = """
    INSERT INTO f1_corners (run_id, lap_num, corner, corner_idx, apex_m, time_ms, entry_kmh, min_kmh,
                            exit_kmh, min_at_m, brake_before_m, throttle_after_m, max_brake) VALUES %s
    ON CONFLICT (run_id, lap_num, corner) DO UPDATE SET
      corner_idx = EXCLUDED.corner_idx, apex_m = EXCLUDED.apex_m, time_ms = EXCLUDED.time_ms,
      entry_kmh = EXCLUDED.entry_kmh, min_kmh = EXCLUDED.min_kmh, exit_kmh = EXCLUDED.exit_kmh,
      min_at_m = EXCLUDED.min_at_m, brake_before_m = EXCLUDED.brake_before_m,
      throttle_after_m = EXCLUDED.throttle_after_m, max_brake = EXCLUDED.max_brake"""

schema_lock = threading.Lock()
schema_ok = [False]


def db_host():
    try:
        return urlparse(CFG["DATABASE_URL"]).hostname or "?"
    except Exception:
        return "?"


POWER = AzurePower(CFG, "", state)      # the server name is filled in once the Key Vault secret is read


def load_database_url():
    """Reads the connection string from Azure Key Vault (never from a file in the code)."""
    url, where, err = secrets_store.database_url(CFG, interactive="--no-login" not in sys.argv)
    CFG["DATABASE_URL"], CFG["DB_SOURCE"], CFG["DB_SECRET_ERROR"] = url, where, err
    if url:
        print(f"[SECRETS] Database login read from {where}", flush=True)
    elif err:
        print(f"[SECRETS] {err}", flush=True)
    POWER.set_host(db_host())


def db_connect():
    import psycopg2
    return psycopg2.connect(CFG["DATABASE_URL"], connect_timeout=10)


def ensure_schema(conn):
    """Creates the tables (once). Happens right after connecting, so also without any data."""
    with schema_lock:
        if schema_ok[0]:
            return
        with conn, conn.cursor() as cur:
            cur.execute(DDL)
        schema_ok[0] = True


def friendly_db_error(e):
    msg = str(e).strip()
    low = msg.lower()
    if "password authentication failed" in low:
        return "Wrong password or username in the Key Vault secret (run setup-keyvault.bat again)"
    if "timeout" in low or "timed out" in low:
        return "Server unreachable (time-out): check the firewall rule in Azure and your internet connection"
    if "no pg_hba.conf" in low:
        return "Access denied by Azure: your IP address is not in the firewall or SSL is missing"
    if "could not translate host" in low or "name or service not known" in low:
        return "Server name not found: check the server name in the Key Vault secret"
    return msg.replace("\n", " ")[:220]


def db_worker():
    url = CFG["DATABASE_URL"]
    if not url:
        state["db"]["hint"] = CFG["DB_SECRET_ERROR"] or "No database configured (run setup-keyvault.bat)"
        return
    try:
        import psycopg2 as pg
        from psycopg2.extras import execute_values
    except ImportError:
        state["db"]["error"] = "psycopg2 not installed (run start.bat again)"
        return

    state["db"]["enabled"] = True
    conn = None
    pkts, laps, metas, corners, traces = [], [], {}, [], []
    flush = float(CFG["DB_FLUSH_SECONDS"])
    last_flush = last_try = last_storage = 0.0
    last_report = None

    def report(ok, err=""):
        nonlocal last_report
        state["db"]["connected"] = ok
        state["db"]["error"] = err
        key = "ok" if ok else err
        if key != last_report:
            last_report = key
            if ok:
                print(f"[DB] Connected to {db_host()} - tables ready, data is being stored")
            else:
                print(f"[DB] ERROR at {db_host()}: {err}")
                print("[DB] The relay retries every few seconds; no data is lost.")

    while True:
        try:
            msg = db_queue.get(timeout=0.5)
            if msg[0] == "pkt":
                pkts.append(msg[1])
            elif msg[0] == "lap":
                laps.append(msg[1])
            elif msg[0] == "meta":
                metas[msg[1]] = msg[2]
            elif msg[0] == "corners":
                corners.extend(msg[1])
            elif msg[0] == "trace":
                traces.append(msg[1])
        except queue.Empty:
            pass
        now = time.time()

        if not POWER.db_ready():
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None
            state["db"]["connected"] = False
            state["db"]["error"] = POWER.not_ready_text()
            last_report = None
        elif conn is None or conn.closed:
            conn = None
            if now - last_try >= 3:
                last_try = now
                try:
                    conn = db_connect()
                    ensure_schema(conn)
                    report(True)
                    last_storage = 0.0         # check the storage right after (re)connecting
                except Exception as e:
                    try:
                        conn and conn.close()
                    except Exception:
                        pass
                    conn = None
                    report(False, friendly_db_error(e))

        if conn and now - last_storage >= 600:          # every 10 minutes
            last_storage = now
            try:
                storage_check(conn)
            except Exception as e:
                print(f"[DB] Storage check failed: {friendly_db_error(e)}", flush=True)
                try:
                    conn.rollback()
                except Exception:
                    pass

        if conn and (pkts or laps or metas or corners or traces) and (now - last_flush >= flush or len(pkts) >= 2000):
            try:
                with conn, conn.cursor() as cur:
                    if pkts:
                        execute_values(cur, """
                            INSERT INTO f1_telemetry_raw
                            (received_at, driver, session_uid, packet_id, frame, session_time,
                             player_idx, payload, run_id) VALUES %s""",
                            [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], pg.Binary(r[7]), r[8]) for r in pkts],
                            template="(to_timestamp(%s), %s, %s, %s, %s, %s, %s, %s, %s)")
                    if metas:
                        execute_values(cur, """
                            INSERT INTO f1_sessions
                            (run_id, session_uid, driver, track_id, session_type, track_length,
                             started_at, last_seen_at, packets)
                            VALUES %s
                            ON CONFLICT (run_id) DO UPDATE SET
                              driver = EXCLUDED.driver,
                              track_id = CASE WHEN EXCLUDED.track_id >= 0 THEN EXCLUDED.track_id
                                              ELSE f1_sessions.track_id END,
                              session_type = CASE WHEN EXCLUDED.session_type >= 0 THEN EXCLUDED.session_type
                                                  ELSE f1_sessions.session_type END,
                              track_length = COALESCE(NULLIF(EXCLUDED.track_length, 0), f1_sessions.track_length),
                              last_seen_at = EXCLUDED.last_seen_at,
                              packets = GREATEST(f1_sessions.packets, EXCLUDED.packets)""",
                            [(rid, m["uid"], m["driver"], m["track"], m["stype"], m.get("tlen", 0),
                              m["first"], m["last"], m["packets"]) for rid, m in metas.items()],
                            template="(%s, %s, %s, %s, %s, %s, to_timestamp(%s), to_timestamp(%s), %s)")
                    if laps:
                        execute_values(cur, """
                            INSERT INTO f1_laps
                            (session_uid, run_id, lap_num, lap_time_ms, s1_ms, s2_ms, s3_ms, invalid)
                            VALUES %s
                            ON CONFLICT (run_id, lap_num) DO UPDATE SET
                              lap_time_ms = EXCLUDED.lap_time_ms, s1_ms = EXCLUDED.s1_ms,
                              s2_ms = EXCLUDED.s2_ms, s3_ms = EXCLUDED.s3_ms,
                              invalid = EXCLUDED.invalid""", laps)
                    if corners:
                        execute_values(cur, CORNER_INSERT, corners)
                    if traces:
                        execute_values(cur, TRACE_INSERT, traces)
                state["db"]["written"] += len(pkts)
                state["db"]["last_ok"] = time.time()
                report(True)
                pkts, laps, metas, corners, traces = [], [], {}, [], []
            except Exception as e:  # connection lost: keep the data and reconnect
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None
                report(False, friendly_db_error(e))
            last_flush = time.time()

        if len(pkts) > 200_000:
            del pkts[:len(pkts) - 200_000]
        state["db"]["pending"] = len(pkts) + db_queue.qsize()


def run_query(sql, params=()):
    conn = db_connect()
    try:
        ensure_schema(conn)
        with conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


async def query(sql, params=()):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, run_query, sql, params)


def db_unavailable():
    if not CFG["DATABASE_URL"]:
        return web.json_response({"enabled": False,
                                  "error": CFG["DB_SECRET_ERROR"] or "No database configured (run setup-keyvault.bat)"})
    POWER.touch()      # someone is viewing Races: keeps a running database on (does not start a stopped one)
    if not POWER.db_ready():
        return web.json_response({"enabled": True, "error": POWER.not_ready_text(), "db_off": POWER.is_off()})
    return None


# ----------------------------------------------------------------- Web
async def index(request):
    return web.FileResponse(ROOT / "web" / "index.html")


async def old_raw(request):
    raise web.HTTPFound("/#raw")


async def ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    clients.add(ws)
    live_dirty[0] = grid_dirty[0] = events_dirty[0] = True
    try:
        async for _ in ws:
            pass
    finally:
        clients.discard(ws)
    return ws


async def set_driver(request):
    body = await request.json()
    if body.get("remove"):
        DRIVERS.remove(str(body["remove"]))
        return web.json_response({"driver": state["driver"], "names": DRIVERS.names})
    name = DRIVERS.resolve(str(body.get("driver", "")))
    switch_driver(name, time.time())
    return web.json_response({"driver": state["driver"], "names": DRIVERS.names})


async def get_driver(request):
    return web.json_response({"driver": state["driver"], "names": DRIVERS.names})


LEGACY = "f22_"   # tables of the earlier collector (read only)


def run_batch(queries):
    """Several queries over one connection (saves a lot of round-trip time to Azure)."""
    conn = db_connect()
    try:
        ensure_schema(conn)
        out = []
        with conn, conn.cursor() as cur:
            for sql, params in queries:
                cur.execute(sql, params)
                out.append(cur.fetchall())
        return out
    finally:
        conn.close()


async def query_batch(queries):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, run_batch, queries)


def find_key(d, *needles):
    """Find a number in a dict whose key contains all the words (schema-independent)."""
    for k, v in d.items():
        kl = str(k).lower()
        if all(n in kl for n in needles) and isinstance(v, (int, float)) and not isinstance(v, bool):
            return int(v)
    return None


def legacy_lap_list(laps):
    out = []
    for i, l in enumerate(laps or [], 1):
        t = int(l.get("lap_time_ms") or 0)
        if t <= 0:
            continue
        s1, s2, s3 = find_key(l, "sector1"), find_key(l, "sector2"), find_key(l, "sector3")
        if s3 is None and s1 and s2:
            s3 = max(t - s1 - s2, 0)
        out.append({"lap": i, "time": t, "s1": s1, "s2": s2, "s3": s3,
                    "invalid": not bool((l.get("valid_flags") or 0) & 1)})
    return out


async def legacy_sessions():
    """Earlier races from the f22_ tables of the old collector."""
    exists = await query("SELECT to_regclass('f22_car_telemetry') IS NOT NULL AND to_regclass('f22_session_history') IS NOT NULL")
    if not exists or not exists[0][0]:
        return []
    rows = await query("""
        SELECT session_uid::text, extract(epoch FROM min(time)), extract(epoch FROM max(time)),
               string_agg(DISTINCT driver, ', '), max(player_car), count(*)
        FROM f22_car_telemetry WHERE car_idx = player_car
        GROUP BY session_uid ORDER BY min(time) DESC LIMIT 40""")
    if not rows:
        return []
    uids = [r[0] for r in rows]
    sess_r, hist_r = await query_batch([
        ("""SELECT DISTINCT ON (session_uid) session_uid::text, to_jsonb(s)
            FROM f22_session s WHERE session_uid::text = ANY(%s) ORDER BY session_uid, time DESC""", (uids,)),
        ("""SELECT DISTINCT ON (session_uid, car_idx) session_uid::text, car_idx, laps
            FROM f22_session_history WHERE session_uid::text = ANY(%s)
            ORDER BY session_uid, car_idx, time DESC""", (uids,)),
    ])
    meta = {r[0]: r[1] for r in sess_r}
    hist = {(r[0], r[1]): r[2] for r in hist_r}
    out = []
    for uid, t0, t1, drivers, player, n in rows:
        m = meta.get(uid) or {}
        laps = legacy_lap_list(hist.get((uid, player)))
        valid = [l["time"] for l in laps if not l["invalid"]]
        out.append({"uid": uid, "src": "f22", "driver": drivers or "", "track_id": m.get("track_id"),
                    "track": TRACKS.get(m.get("track_id"), "Unknown"),
                    "type": SESSION_TYPES.get(m.get("session_type"), "Unknown"),
                    "start": float(t0), "end": float(t1), "packets": n, "laps": len(laps),
                    "best": min(valid) if valid else None})
    return out


LEGACY_FILE = ROOT / "config" / "old_races_cache.json"
LEGACY_MAX_AGE = 30 * 60          # refresh the list of old races at most every 30 minutes
legacy = {"rows": None, "at": 0.0, "loading": False, "error": ""}
try:
    _c = json.loads(LEGACY_FILE.read_text(encoding="utf-8"))
    legacy["rows"], legacy["at"] = _c["rows"], float(_c["at"])
except Exception:
    pass


async def refresh_legacy():
    """Loads the list of old races in the background. Scans a big table, so it can take minutes."""
    if legacy["loading"]:
        return
    legacy["loading"] = True
    t0 = time.time()
    print("[DB] Loading the list of old races (f22_ tables) in the background...", flush=True)
    try:
        rows = await legacy_sessions()
        legacy.update(rows=rows, at=time.time(), error="")
        try:
            LEGACY_FILE.write_text(json.dumps({"at": legacy["at"], "rows": rows}), encoding="utf-8")
        except Exception:
            pass
        print(f"[DB] {len(rows)} old races loaded ({time.time() - t0:.0f} s)", flush=True)
    except Exception as e:
        legacy["error"] = friendly_db_error(e)
        legacy["at"] = time.time()          # don't retry immediately
        print(f"[DB] Loading old races failed: {legacy['error']}", flush=True)
    finally:
        legacy["loading"] = False


def legacy_due():
    return (CFG["DATABASE_URL"] and state["db"]["connected"] and not legacy["loading"]
            and time.time() - legacy["at"] > LEGACY_MAX_AGE)


async def api_db_start(request):
    """'Start database' button in the Races tab."""
    POWER.race("Start database clicked in Races tab")
    return web.json_response({"ok": True})


async def api_sessions(request):
    bad = db_unavailable()
    if bad:
        return bad
    out, notes = [], []
    try:
        rows = await query("""
            SELECT s.run_id, s.driver, s.track_id, s.session_type,
                   extract(epoch FROM s.started_at), extract(epoch FROM s.last_seen_at), s.packets,
                   (SELECT count(*) FROM f1_laps l WHERE l.run_id = s.run_id),
                   (SELECT min(lap_time_ms) FROM f1_laps l
                     WHERE l.run_id = s.run_id AND NOT l.invalid AND lap_time_ms > 0),
                   s.session_uid
            FROM f1_sessions s ORDER BY s.started_at DESC LIMIT 300""")
    except Exception as e:
        return web.json_response({"enabled": True, "error": friendly_db_error(e)})
    out += [{"uid": r[0], "src": "f1", "driver": r[1] or "", "track": TRACKS.get(r[2], "Unknown"),
             "type": SESSION_TYPES.get(r[3], "Unknown"), "start": float(r[4]), "end": float(r[5]),
             "packets": r[6], "laps": r[7], "best": r[8], "game_uid": r[9], "track_id": r[2],
             "corners": r[2] in CORNERS} for r in rows]
    info = []
    if legacy_due():
        asyncio.create_task(refresh_legacy())
    if legacy["rows"] is not None:
        rev = {v: k for k, v in TRACKS.items()}
        for r in legacy["rows"]:
            r.setdefault("track_id", rev.get(r["track"]))
            r["corners"] = r["track_id"] in CORNERS
        out += legacy["rows"]
    if legacy["loading"]:
        info.append("Old races (f22_ tables) are being loaded in the background. The first time this can take "
                    "a few minutes because the table is large. Click Refresh in a moment.")
    elif legacy["error"] and legacy["rows"] is None:
        notes.append("The old races (f22_ tables) could not be read: " + legacy["error"])
    out.sort(key=lambda s: s["start"], reverse=True)
    return web.json_response({"enabled": True, "sessions": out, "notes": notes, "info": info})


async def api_laps(request):
    bad = db_unavailable()
    if bad:
        return bad
    uid = request.match_info["uid"]
    try:
        if request.query.get("src") == "f22":
            rows = await query("""SELECT laps FROM f22_session_history
                                  WHERE session_uid::text = %s AND car_idx = player_car
                                  ORDER BY time DESC LIMIT 1""", (uid,))
            return web.json_response({"enabled": True, "laps": legacy_lap_list(rows[0][0] if rows else [])})
        rows = await query("""SELECT lap_num, lap_time_ms, s1_ms, s2_ms, s3_ms, invalid
                              FROM f1_laps WHERE run_id = %s ORDER BY lap_num""", (uid,))
    except Exception as e:
        return web.json_response({"enabled": True, "error": friendly_db_error(e)})
    return web.json_response({"enabled": True, "laps": [
        {"lap": r[0], "time": r[1], "s1": r[2], "s2": r[3], "s3": r[4], "invalid": r[5]} for r in rows]})


def sample_sql(length, per_car, every):
    """Cuts only the player car's bytes out of each packet inside the database, and thins out the rows."""
    return f"""
        SELECT session_time, substring(payload from 24 + coalesce(player_idx, 0) * {per_car} + 1 for {length})
        FROM (SELECT session_time, payload, player_idx, row_number() OVER (ORDER BY frame) AS rn
              FROM f1_telemetry_raw WHERE {RUN_FILTER} AND packet_id = %s) t
        WHERE rn %% {every} = 0 ORDER BY session_time"""


# New rows carry run_id; rows from before runs existed only have session_uid (and their run_id = session_uid).
RUN_FILTER = "(run_id = %s OR (run_id IS NULL AND session_uid = %s))"


def legacy_sample_sql(cols, table, every):
    return f"""
        SELECT extract(epoch FROM time)::float8, {cols}
        FROM (SELECT time, {cols}, row_number() OVER (ORDER BY time) AS rn
              FROM {LEGACY}{table} WHERE session_uid::text = %s AND car_idx = player_car) t
        WHERE rn %% {every} = 0 ORDER BY time"""


async def legacy_replay(uid):
    tel_r, lap_r, pos_r = await asyncio.gather(
        query(legacy_sample_sql("speed, throttle, brake, steer, gear, engine_rpm, drs", "car_telemetry", 3), (uid,)),
        query(legacy_sample_sql("car_position, current_lap_num, current_lap_time_ms, last_lap_time_ms, current_lap_invalid",
                                "lap_data", 6), (uid,)),
        query(legacy_sample_sql("pos_x, pos_z", "motion", 6), (uid,)))
    tel = [[round(r[0], 2), r[1], r[5], r[6], round(float(r[2] or 0), 2), round(float(r[3] or 0), 2),
            round(float(r[4] or 0), 2), int(r[7] or 0), None, None, None, None, None, None] for r in tel_r]
    lap = [[round(r[0], 2), r[1], r[2], r[3], r[4], None, None, None, bool(r[5]), None] for r in lap_r]
    pos = [[round(r[0], 2), round(float(r[1]), 1), round(float(r[2]), 1)] for r in pos_r]
    return {"enabled": True, "tel": tel, "lap": lap, "pos": pos, "stat": [], "dmg": []}


async def api_replay(request):
    """Compact replay of the player car (telemetry ~20 Hz, laps and track position ~10 Hz)."""
    bad = db_unavailable()
    if bad:
        return bad
    uid = request.match_info["uid"]
    try:
        if request.query.get("src") == "f22":
            return web.json_response(await legacy_replay(uid))
        tel_r, lap_r, pos_r, stat_r, dmg_r = await asyncio.gather(
            query(sample_sql(60, 60, 3), (uid, uid, 6)), query(sample_sql(43, 43, 6), (uid, uid, 2)),
            query(sample_sql(12, 60, 6), (uid, uid, 0)), query(sample_sql(47, 47, 30), (uid, uid, 7)),
            query(sample_sql(42, 42, 10), (uid, uid, 10)))
    except Exception as e:
        return web.json_response({"enabled": True, "error": friendly_db_error(e)})
    tel, lap, pos, stat, dmg = [], [], [], [], []
    for t, b in tel_r:
        v = TEL_STRUCT.unpack(bytes(b))
        tel.append([round(t, 2), v[0], v[5], v[6], round(v[1], 2), round(v[3], 2), round(v[2], 2), v[7],
                    v[8], list(v[10:14]), list(v[14:18]), list(v[18:22]),
                    [round(x, 1) for x in v[23:27]], v[22]])
    for t, b in lap_r:
        v = LAP_STRUCT.unpack(bytes(b))
        lap.append([round(t, 2), v[7], v[8], v[1], v[0], v[2], v[3], v[11], v[12], v[13]])
    for t, b in pos_r:
        x, _y, z = struct.unpack("<3f", bytes(b))
        pos.append([round(t, 2), round(x, 1), round(z, 1)])
    for t, b in stat_r:
        v = STATUS_STRUCT.unpack(bytes(b))
        stat.append([round(t, 2), round(v[5], 1), round(v[7], 1), round(v[17] / 4_000_000 * 100),
                     COMPOUNDS.get(v[14], "?"), v[15], v[3], bool(v[4]), v[8] or 13000])
    for t, b in dmg_r:
        v = DAMAGE_STRUCT.unpack(bytes(b))
        dmg.append([round(t, 2)] + [round(x) for x in v[0:4]] + list(v[4:30]))
    return web.json_response({"enabled": True, "tel": tel, "lap": lap, "pos": pos, "stat": stat, "dmg": dmg})


# ----------------------------------------------------------------- Corner comparison between drivers
corner_tried = set()      # runs for which a backfill from raw data was already attempted (this relay session)


def merge_samples(lap_rows, tel_rows):
    """lap_rows: [(order, lapnum, lap_dist, cur_ms, in_pit)], tel_rows: [(order, speed, throttle, brake)],
    both sorted by `order`. Returns {lapnum: [(dist, cur_ms, speed, throttle, brake)]}."""
    out, j, last = {}, 0, (0, 0.0, 0.0)
    for order, lapnum, dist, cur_ms, in_pit in lap_rows:
        while j < len(tel_rows) and tel_rows[j][0] <= order:
            last = tel_rows[j][1:]
            j += 1
        if not in_pit:
            out.setdefault(lapnum, []).append((dist, cur_ms, last[0], last[1], last[2]))
    return out


def raw_f1(cur, run_id):
    """Reads a run's raw packets back: (track_id, track_length, {lap: samples}, completed laps)."""
    cur.execute("SELECT track_id, track_length FROM f1_sessions WHERE run_id = %s", (run_id,))
    row = cur.fetchone()
    if not row:
        return "Run not found"
    track_id, tlen = row
    if not tlen:
        cur.execute(f"""SELECT substring(payload from 29 for 2) FROM f1_telemetry_raw
                        WHERE {RUN_FILTER} AND packet_id = 1 LIMIT 1""", (run_id, run_id))
        r = cur.fetchone()
        tlen = struct.unpack("<H", bytes(r[0]))[0] if r else 0
    cur.execute(f"""SELECT id, substring(payload from 24 + coalesce(player_idx, 0) * 43 + 1 for 43)
                    FROM f1_telemetry_raw WHERE {RUN_FILTER} AND packet_id = 2 ORDER BY id""", (run_id, run_id))
    lap_rows = []
    for i, b in cur.fetchall():
        v = LAP_STRUCT.unpack(bytes(b))
        lap_rows.append((i, v[8], v[4], v[1], v[9] != 0))
    cur.execute(f"""SELECT id, substring(payload from 24 + coalesce(player_idx, 0) * 60 + 1 for 16)
                    FROM f1_telemetry_raw WHERE {RUN_FILTER} AND packet_id = 6 ORDER BY id""", (run_id, run_id))
    tel_rows = []
    for i, b in cur.fetchall():
        speed, throttle, _steer, brake = struct.unpack("<Hfff", bytes(b)[:14])
        tel_rows.append((i, speed, throttle, brake))
    cur.execute("SELECT lap_num FROM f1_laps WHERE run_id = %s AND lap_time_ms > 0", (run_id,))
    done = {r[0] for r in cur.fetchall()}
    return track_id, tlen, merge_samples(lap_rows, tel_rows), done


def legacy_cols(cur, table):
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s", (LEGACY + table,))
    return {r[0] for r in cur.fetchall()}


def raw_f22(cur, uid):
    """Same for an old race from the f22_ tables (stored under run_id 'f22:<uid>')."""
    run_id = "f22:" + uid
    lap_cols = legacy_cols(cur, "lap_data")
    dist_col = next((c for c in ("lap_distance", "lap_distance_m") if c in lap_cols), None)
    if not dist_col:
        return "The old tables have no lap distance, so the corners can't be worked out for this race"
    cur.execute(f"""SELECT DISTINCT ON (session_uid) to_jsonb(s) FROM {LEGACY}session s
                    WHERE session_uid::text = %s ORDER BY session_uid, time DESC""", (uid,))
    r = cur.fetchone()
    m = r[0] if r else {}
    track_id, tlen = m.get("track_id"), m.get("track_length") or 0
    pit = "pit_status" if "pit_status" in lap_cols else "0"
    cur.execute(f"""SELECT extract(epoch FROM time)::float8, current_lap_num, {dist_col}, current_lap_time_ms, {pit}
                    FROM {LEGACY}lap_data WHERE session_uid::text = %s AND car_idx = player_car ORDER BY time""", (uid,))
    lap_rows = [(t, n, float(d or 0), float(c or 0), bool(p)) for t, n, d, c, p in cur.fetchall()]
    cur.execute(f"""SELECT extract(epoch FROM time)::float8, speed, throttle, brake
                    FROM {LEGACY}car_telemetry WHERE session_uid::text = %s AND car_idx = player_car ORDER BY time""", (uid,))
    tel_rows = [(t, float(s or 0), float(th or 0), float(b or 0)) for t, s, th, b in cur.fetchall()]
    cur.execute(f"""SELECT laps FROM {LEGACY}session_history WHERE session_uid::text = %s AND car_idx = player_car
                    ORDER BY time DESC LIMIT 1""", (uid,))
    r = cur.fetchone()
    laps = legacy_lap_list(r[0] if r else [])
    if laps:
        from psycopg2.extras import execute_values
        execute_values(cur, """INSERT INTO f1_laps (session_uid, run_id, lap_num, lap_time_ms, s1_ms, s2_ms, s3_ms, invalid)
                               VALUES %s ON CONFLICT (run_id, lap_num) DO NOTHING""",
                       [(uid, run_id, l["lap"], l["time"], l["s1"], l["s2"], l["s3"], l["invalid"]) for l in laps])
    return track_id, tlen, merge_samples(lap_rows, tel_rows), {l["lap"] for l in laps}


def rebuild_run(cur, src, uid):
    """Works out corner stats and lap traces from the raw data of a run (for runs stored before these existed)."""
    run_id = "f22:" + uid if src == "f22" else uid
    data = raw_f22(cur, uid) if src == "f22" else raw_f1(cur, run_id)
    if isinstance(data, str):
        return data
    track_id, tlen, by_lap, done = data
    crows, trows = [], []
    for lap, samples in by_lap.items():
        if lap not in done:
            continue
        crows += corner_rows(run_id, lap, corner_stats(samples, track_id, tlen))
        t = lap_trace(samples, tlen)
        if t:
            trows.append(trace_row(run_id, lap, track_id, tlen, t))
    from psycopg2.extras import execute_values
    if crows:
        execute_values(cur, CORNER_INSERT, crows)
    if trows:
        execute_values(cur, TRACE_INSERT, trows)
    if track_id not in CORNERS:
        return "No corner list for this track"
    return len(crows), len(trows)


def load_compare(ids, mode):
    conn = db_connect()
    try:
        ensure_schema(conn)
        out, notes = [], []
        with conn, conn.cursor() as cur:
            for src, uid in ids:
                run_id = "f22:" + uid if src == "f22" else uid
                cur.execute("""SELECT (SELECT count(*) FROM f1_corners WHERE run_id = %s),
                                      (SELECT count(*) FROM f1_lap_traces WHERE run_id = %s)""", (run_id, run_id))
                nc, nt = cur.fetchone()
                if (nc == 0 or nt == 0) and run_id not in corner_tried:
                    corner_tried.add(run_id)
                    t0 = time.time()
                    res = rebuild_run(cur, src, uid)
                    if isinstance(res, str):
                        notes.append(res)
                    else:
                        print(f"[CORNERS] {res[0]} corner rows and {res[1]} lap traces worked out for {run_id} "
                              f"({time.time() - t0:.1f} s)")
                    conn.commit()
                cur.execute("""SELECT c.lap_num, c.corner, c.corner_idx, c.apex_m, c.time_ms, c.entry_kmh, c.min_kmh,
                                      c.exit_kmh, c.brake_before_m, c.throttle_after_m, l.lap_time_ms,
                                      coalesce(l.invalid, false)
                               FROM f1_corners c LEFT JOIN f1_laps l ON l.run_id = c.run_id AND l.lap_num = c.lap_num
                               WHERE c.run_id = %s ORDER BY c.lap_num, c.corner_idx""", (run_id,))
                out.append((src, uid, cur.fetchall()))
        return out, notes
    finally:
        conn.close()


def summarise_run(rows, mode):
    """Picks the best lap (mode 'best') or the best time per corner over all laps (mode 'ideal')."""
    laps = {}
    for lap, corner, idx, apex, t, entry, vmin, vexit, brake, gas, lap_ms, invalid in rows:
        L = laps.setdefault(lap, {"time": lap_ms, "invalid": invalid, "c": {}})
        L["c"][corner] = {"idx": idx, "apex": apex, "time": t, "entry": entry, "min": vmin, "exit": vexit,
                          "brake": brake, "gas": gas, "lap": lap}
    if not laps:
        return None
    full = max(len(L["c"]) for L in laps.values())
    usable = {n: L for n, L in laps.items() if not L["invalid"]}
    used_invalid = not usable
    pool = usable or laps
    if mode == "ideal":
        best = {}
        for n, L in pool.items():
            for k, c in L["c"].items():
                if k not in best or c["time"] < best[k]["time"]:
                    best[k] = c
        total = sum(c["time"] for c in best.values())
        return {"lap": None, "lap_time": None, "corners": best, "invalid_used": used_invalid,
                "laps_used": len(pool), "corner_total": total}
    complete = {n: L for n, L in pool.items() if len(L["c"]) == full and L["time"]} or pool
    n, L = min(complete.items(), key=lambda kv: (kv[1]["time"] or 10 ** 9))
    return {"lap": n, "lap_time": L["time"], "corners": L["c"], "invalid_used": used_invalid or L["invalid"],
            "laps_used": len(pool), "corner_total": sum(c["time"] for c in L["c"].values())}


def load_trace(run_id, lap):
    conn = db_connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""SELECT track_id, track_length, step_m, speed, throttle, brake, time_ms
                           FROM f1_lap_traces WHERE run_id = %s AND lap_num = %s""", (run_id, lap))
            return cur.fetchone()
    finally:
        conn.close()


async def api_trace(request):
    """Speed / throttle / brake / time every 2 m of one lap, plus the corner layout of that track."""
    bad = db_unavailable()
    if bad:
        return bad
    src, _, uid = request.query.get("id", "").partition(":")
    run_id = "f22:" + uid if src == "f22" else uid
    try:
        lap = int(request.query.get("lap", "0"))
        row = await asyncio.get_running_loop().run_in_executor(None, load_trace, run_id, lap)
    except Exception as e:
        return web.json_response({"enabled": True, "error": friendly_db_error(e)})
    if not row:
        return web.json_response({"enabled": True, "error": "No trace stored for this lap"})
    track_id, tlen, step, speed, throttle, brake, tms = row
    L = tlen or step * (len(speed) - 1)
    layout = [{"corner": c[0], "apex": round(c[1], 1), "z0": round(c[2], 1), "z1": round(c[3], 1),
               "v0": round(c[5], 1), "v1": round(c[6], 1)} for c in corner_layout(track_id, L)]
    return web.json_response({"enabled": True, "step": step, "speed": speed, "throttle": throttle, "brake": brake,
                              "time": tms, "layout": layout})


async def api_compare(request):
    bad = db_unavailable()
    if bad:
        return bad
    mode = "ideal" if request.query.get("mode") == "ideal" else "best"
    ids = []
    for part in request.query.get("ids", "").split(","):
        if ":" in part:
            src, uid = part.split(":", 1)
            ids.append(("f22" if src == "f22" else "f1", uid))
    if not ids:
        return web.json_response({"enabled": True, "error": "Select at least one run"})
    loop = asyncio.get_running_loop()
    try:
        data, notes = await loop.run_in_executor(None, load_compare, ids[:8], mode)
    except Exception as e:
        return web.json_response({"enabled": True, "error": friendly_db_error(e)})
    runs, labels = [], {}
    for src, uid, rows in data:
        r = summarise_run(rows, mode)
        runs.append({"id": f"{src}:{uid}", **(r or {"corners": {}, "empty": True})})
        for row in rows:
            labels.setdefault(row[1], (row[2], row[3]))
    corners = [{"corner": k, "apex": v[1]} for k, v in sorted(labels.items(), key=lambda kv: kv[1][1])]
    return web.json_response({"enabled": True, "mode": mode, "runs": runs, "corners": corners, "notes": notes})


async def broadcaster():
    """Live data every 25 ms, raw packets every 50 ms, standings/events every 0.5 s, stats every second."""
    last_raw = last_stats = last_meta = last_slow = 0
    while True:
        await asyncio.sleep(0.025)
        now = time.time()

        if now - last_meta >= 2:
            last_meta = now
            if legacy_due():
                asyncio.create_task(refresh_legacy())
            if state["db"]["enabled"]:
                for uid, m in sess.items():
                    if m["dirty"]:
                        m["dirty"] = False
                        db_queue.put(("meta", uid, dict(m)))

        if not clients:
            live_buffer.clear()
            continue

        msgs = []
        if live_dirty[0]:
            live_dirty[0] = False
            live["ts"] = now
            msgs.append({"type": "live", "d": live})
        if now - last_slow >= 0.5:
            last_slow = now
            if grid_dirty[0]:
                grid_dirty[0] = False
                msgs.append({"type": "grid", "d": grid_state})
            if events_dirty[0]:
                events_dirty[0] = False
                msgs.append({"type": "events", "d": events})
        if live_buffer and now - last_raw >= 0.05:
            last_raw = now
            batch = live_buffer[:]
            live_buffer.clear()
            msgs.append({"type": "packets", "data": batch[-300:], "count": len(batch)})
        if now - last_stats >= 1:
            last_stats = now
            msgs.append({"type": "stats", "total": state["total"], "bytes": state["bytes"],
                         "by_type": {PACKET_NAMES.get(k, str(k)): v
                                     for k, v in sorted(state["by_type"].items())},
                         "uptime": now - state["started"], "driver": state["driver"],
                         "drivers": DRIVERS.names,
                         "db": state["db"], "now": now})
        for m in msgs:
            text = json.dumps(m)
            for ws in list(clients):
                try:
                    await ws.send_str(text)
                except Exception:
                    clients.discard(ws)


async def main():
    udp_port = int(CFG["UDP_PORT"])
    http_port = int(CFG["HTTP_PORT"])

    loop = asyncio.get_running_loop()
    loop_ref.append(loop)
    if sys.stdin and sys.stdin.isatty():
        threading.Thread(target=console_menu, daemon=True).start()
    await loop.create_datagram_endpoint(UdpProtocol, local_addr=("0.0.0.0", udp_port))

    app = web.Application()
    app.add_routes([
        web.get("/", index), web.get("/raw", old_raw), web.get("/ws", ws_handler),
        web.post("/api/driver", set_driver),
        web.get("/api/driver", get_driver),
        web.post("/api/db/start", api_db_start),
        web.get("/api/sessions", api_sessions),
        web.get("/api/sessions/{uid}/laps", api_laps),
        web.get("/api/sessions/{uid}/replay", api_replay),
        web.get("/api/compare", api_compare),
        web.get("/api/trace", api_trace),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", http_port).start()
    asyncio.create_task(broadcaster())

    print("=" * 62)
    print(f" UDP:         listening on port {udp_port}  (game on this PC: IP 127.0.0.1)")
    print(f" Website:     http://localhost:{http_port}   (tabs: Live / Raw / Races)")
    print(f" Config:      {ROOT / 'config' / 'config.env'}")
    print(f" Database:    {('on, server ' + db_host() + ' (login from ' + CFG['DB_SOURCE'] + ')') if CFG['DATABASE_URL'] else 'OFF (' + (CFG['DB_SECRET_ERROR'] or 'not configured') + ')'}")
    print(f" Drivers:     {DRIVERS.numbered() or '(none yet: add names on the website)'}")
    print("              change driver: website, the 'F1 driver' window, or type a number here + Enter")
    print(f" Driver:      {state['driver'] or '(empty)'}")
    print("=" * 62)
    if "--no-browser" not in sys.argv:
        webbrowser.open(f"http://localhost:{http_port}/")
    await asyncio.Event().wait()


if __name__ == "__main__":
    if intro.wanted(CFG, sys.argv):
        intro.play()
    load_database_url()
    if "--driver" in sys.argv:
        state["driver"] = DRIVERS.resolve(sys.argv[sys.argv.index("--driver") + 1])
    else:
        try:
            if DRIVERS.names:
                print(f"Drivers: {DRIVERS.numbered()}")
                ask = "Who drives first? Number or name (Enter = choose later): "
            else:
                ask = "Driver name (Enter = fill it in later on the web page): "
            state["driver"] = DRIVERS.resolve(input(ask))
        except EOFError:
            pass
    threading.Thread(target=db_worker, daemon=True).start()
    if CFG["DATABASE_URL"]:
        POWER.start_thread()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        POWER.on_exit()
        print("\nStopped.")

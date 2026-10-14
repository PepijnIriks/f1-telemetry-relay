"""
F1 22 telemetry relay
- Listens on UDP (default port 20777) for packets from the game
- Forwards them IMMEDIATELY to the website (http://127.0.0.1:8000) over WebSocket
- Parses the player's car + the whole field for the Live dashboard
- Writes to PostgreSQL (Azure) in batches in the background: raw packets,
  an overview of sessions/races and lap times. The database can never slow down the live view.
- Every driver gets their own "run": changing the driver name closes the current run (it is kept
  in the database) and starts a new one, even if the game session simply continues.
- Per lap it works out the official F1 corners (corner time, entry/minimum/exit speed, braking point,
  throttle point) so the Races tab can compare drivers corner by corner.
"""
import asyncio
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
import spool
from azure_power import AzurePower
from drivers import DriverList

ROOT = Path(__file__).resolve().parent.parent

from f1_format import (HEADER, HEADER_FIELDS, TEL_STRUCT, LAP_STRUCT, STATUS_STRUCT, DAMAGE_STRUCT,  # noqa: F401
                       MOTION_SIZE, PACKET_NAMES, TRACKS, SESSION_TYPES, WEATHER, COMPOUNDS)

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


from corners import CORNERS, corner_layout, corner_stats, lap_trace, trace_row, corner_rows

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


# ----------------------------------------------------------------- Storage in Azure
# EVERY packet is stored and nothing is ever removed: all data must stay findable later.
# The relay only keeps an eye on how full the database is, because a full database becomes read-only
# (then nothing new can be stored). Azure storage: 64 GB, autogrow off.
def storage_check(conn):
    gb = 1024 ** 3
    warn_gb = float(CFG.get("DB_WARN_GB", "50") or 0)
    with conn, conn.cursor() as cur:
        cur.execute("SELECT pg_database_size(current_database()), pg_total_relation_size('f1_telemetry_raw')")
        db_size, raw_size = cur.fetchone()
    state["db"]["size_gb"] = round(db_size / gb, 2)
    print(f"[DB] Storage: database {db_size / gb:.1f} GB in total (raw packets {raw_size / gb:.1f} GB)", flush=True)
    if warn_gb > 0 and db_size > warn_gb * gb:
        msg = (f"Database is {db_size / gb:.1f} GB, above the warning level of {warn_gb:g} GB. Make the storage "
               "bigger in the Azure portal (f1db > Compute + storage) before it is full, or it becomes read-only.")
        state["db"]["warning"] = msg
        print(f"[DB] WARNING: {msg}", flush=True)
    else:
        state["db"]["warning"] = ""


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

        if state["db"]["enabled"]:
            db_queue.put(("pkt", (now, meta["driver"], uid, pid, h["frame"],
                                  h["session_time"], p, data, run_id)))


# ----------------------------------------------------------------- Database
from schema import DDL, TRACE_INSERT, CORNER_INSERT  # noqa: E402

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


# Data waiting for Azure. The writer thread swaps it out in one go, writes it, and puts it back if that failed.
pending = spool.empty_batch()
inflight = {"batch": None}           # batch being written right now (not confirmed by Azure yet)
pending_lock = threading.Lock()
SPOOL_AT = 20_000                    # packets in memory without database -> to disk
SPOOL_AFTER_S = 30                   # or older than this without database -> to disk


def write_batch(cur, b):
    """Writes one batch inside the open transaction of cursor `cur`."""
    from psycopg2 import Binary
    from psycopg2.extras import execute_values
    if b["pkts"]:
        execute_values(cur, """
            INSERT INTO f1_telemetry_raw
            (received_at, driver, session_uid, packet_id, frame, session_time,
             player_idx, payload, run_id) VALUES %s""",
            [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], Binary(r[7]), r[8]) for r in b["pkts"]],
            template="(to_timestamp(%s), %s, %s, %s, %s, %s, %s, %s, %s)", page_size=1000)
    if b["metas"]:
        execute_values(cur, """
            INSERT INTO f1_sessions
            (run_id, session_uid, driver, track_id, session_type, track_length,
             started_at, last_seen_at, packets)
            VALUES %s
            ON CONFLICT (run_id) DO UPDATE SET
              driver = COALESCE(NULLIF(f1_sessions.driver, ''), EXCLUDED.driver),
              track_id = CASE WHEN EXCLUDED.track_id >= 0 THEN EXCLUDED.track_id
                              ELSE f1_sessions.track_id END,
              session_type = CASE WHEN EXCLUDED.session_type >= 0 THEN EXCLUDED.session_type
                                  ELSE f1_sessions.session_type END,
              track_length = COALESCE(NULLIF(EXCLUDED.track_length, 0), f1_sessions.track_length),
              last_seen_at = GREATEST(f1_sessions.last_seen_at, EXCLUDED.last_seen_at),
              packets = GREATEST(f1_sessions.packets, EXCLUDED.packets)""",
            [(rid, m["uid"], m["driver"], m["track"], m["stype"], m.get("tlen", 0),
              m["first"], m["last"], m["packets"]) for rid, m in b["metas"].items()],
            template="(%s, %s, %s, %s, %s, %s, to_timestamp(%s), to_timestamp(%s), %s)")
    if b["laps"]:
        execute_values(cur, """
            INSERT INTO f1_laps
            (session_uid, run_id, lap_num, lap_time_ms, s1_ms, s2_ms, s3_ms, invalid)
            VALUES %s
            ON CONFLICT (run_id, lap_num) DO UPDATE SET
              lap_time_ms = EXCLUDED.lap_time_ms, s1_ms = EXCLUDED.s1_ms,
              s2_ms = EXCLUDED.s2_ms, s3_ms = EXCLUDED.s3_ms,
              invalid = EXCLUDED.invalid""", b["laps"])
    if b["corners"]:
        execute_values(cur, CORNER_INSERT, b["corners"])
    if b["traces"]:
        execute_values(cur, TRACE_INSERT, b["traces"])


def drain_queue():
    """Moves everything from the UDP thread's queue into `pending`."""
    with pending_lock:
        while True:
            try:
                msg = db_queue.get_nowait()
            except queue.Empty:
                return
            kind = msg[0]
            if kind == "pkt":
                pending["pkts"].append(msg[1])
            elif kind == "lap":
                pending["laps"].append(msg[1])
            elif kind == "meta":
                pending["metas"][msg[1]] = msg[2]
            elif kind == "corners":
                pending["corners"].extend(msg[1])
            elif kind == "trace":
                pending["traces"].append(msg[1])


def spool_everything(reason="relay is closing"):
    """Writes everything that is not confirmed by Azure to disk (called when the relay stops)."""
    if not state["db"]["enabled"]:
        return
    drain_queue()
    with pending_lock:
        b = {k: (dict(v) if isinstance(v, dict) else list(v)) for k, v in pending.items()}
        if inflight["batch"] is not None:          # not confirmed yet: save it too (Azure may never get it)
            spool.merge(b, inflight["batch"])
            inflight["batch"] = None
        for k, v in spool.empty_batch().items():
            pending[k] = v
    if not spool.is_empty(b):
        spool.save(b)
        print(f"[DB] {reason}: {len(b['pkts']):,} packets not in Azure yet are saved on this PC (folder 'spool') "
              "and sent to Azure the next time the relay connects.", flush=True)


def db_worker():
    url = CFG["DATABASE_URL"]
    if not url:
        state["db"]["hint"] = CFG["DB_SECRET_ERROR"] or "No database configured (run setup-keyvault.bat)"
        return
    import importlib.util
    if importlib.util.find_spec("psycopg2") is None:
        state["db"]["error"] = "psycopg2 not installed (run start.bat again)"
        return

    state["db"]["enabled"] = True
    conn = None
    flush = float(CFG["DB_FLUSH_SECONDS"])
    last_flush = last_try = last_storage = 0.0
    no_db_since = None
    last_report = None
    waiting = spool.count()
    if waiting:
        print(f"[DB] {waiting:,} packets from an earlier session are waiting on this PC; "
              "they are sent to Azure as soon as it is reachable.", flush=True)

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
                print("[DB] The relay keeps retrying; data waits on this PC meanwhile, nothing is lost.")

    def close():
        nonlocal conn
        try:
            conn and conn.close()
        except Exception:
            pass
        conn = None

    while True:
        time.sleep(0.25)
        drain_queue()
        now = time.time()

        if not POWER.db_ready():
            if conn is not None:
                close()
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
                    close()
                    report(False, friendly_db_error(e))

        # No database: don't keep piling up in memory, put it on disk
        if conn is None:
            no_db_since = no_db_since or now
            with pending_lock:
                n = len(pending["pkts"])
            if n >= SPOOL_AT or (n and now - no_db_since >= SPOOL_AFTER_S):
                spool_everything("database not reachable")
                no_db_since = now
        else:
            no_db_since = None

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

        # Files saved on this PC earlier: send them first (oldest first), one file per round.
        # New data waits until they are all in, so everything arrives in Azure in the right order.
        files = spool.files() if conn else []
        if conn:
            if files:
                path = files[0]
                try:
                    b = spool.load(path)
                    with conn, conn.cursor() as cur:
                        write_batch(cur, b)
                    spool.done(path)
                    state["db"]["written"] += len(b["pkts"])
                    left = spool.count()
                    print(f"[DB] Sent {len(b['pkts']):,} saved packets from this PC to Azure"
                          + (f" ({left:,} still waiting)" if left else " - all saved data is in Azure now"), flush=True)
                except (EOFError, ValueError, OSError) as e:      # damaged file: keep it aside, never delete data
                    print(f"[DB] Could not read {path.name} ({e}); renamed to .bad, please report it", flush=True)
                    try:
                        path.rename(path.with_suffix(".bad"))
                    except OSError:
                        pass
                except Exception as e:
                    close()
                    report(False, friendly_db_error(e))

        with pending_lock:
            n = len(pending["pkts"])
            has = not spool.is_empty(pending)
        if files and n >= SPOOL_AT:
            spool_everything("catching up with saved data")
            has = False
        if conn and has and not files and (now - last_flush >= flush or n >= 2000):
            with pending_lock:
                b = {k: v for k, v in pending.items()}
                for k, v in spool.empty_batch().items():
                    pending[k] = v
                inflight["batch"] = b
            try:
                with conn, conn.cursor() as cur:
                    write_batch(cur, b)
                with pending_lock:
                    inflight["batch"] = None
                state["db"]["written"] += len(b["pkts"])
                state["db"]["last_ok"] = time.time()
                report(True)
            except Exception as e:  # connection lost: put the data back and reconnect
                with pending_lock:
                    if inflight["batch"] is b:
                        inflight["batch"] = None
                        spool.merge(pending, b)
                close()
                report(False, friendly_db_error(e))
            last_flush = time.time()

        with pending_lock:
            mem = len(pending["pkts"])
        state["db"]["spooled"] = spool.count() if int(now) % 5 == 0 or not conn else state["db"].get("spooled", 0)
        state["db"]["pending"] = mem + db_queue.qsize() + state["db"].get("spooled", 0)


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


# ----------------------------------------------------------------- Run: change driver / download
async def api_run_driver(request):
    """Change the driver name of a stored run (e.g. someone forgot to switch). No data is removed."""
    bad = db_unavailable()
    if bad:
        return bad
    uid = request.match_info["uid"]
    body = await request.json()
    name = DRIVERS.resolve(str(body.get("driver", "")))
    if not name:
        return web.json_response({"error": "Give a name"})

    def work():
        conn = db_connect()
        try:
            with conn, conn.cursor() as cur:
                cur.execute("UPDATE f1_sessions SET driver = %s WHERE run_id = %s", (name, uid))
                if cur.rowcount == 0:
                    return 0
                cur.execute(f"UPDATE f1_telemetry_raw SET driver = %s WHERE {RUN_FILTER}", (name, uid, uid))
                return cur.rowcount
        finally:
            conn.close()
    try:
        n = await asyncio.get_running_loop().run_in_executor(None, work)
    except Exception as e:
        return web.json_response({"error": friendly_db_error(e)})
    if uid in sess:
        sess[uid]["driver"] = name
    print(f"[DRIVER] Run {uid} now belongs to {name} ({n:,} packets updated)", flush=True)
    return web.json_response({"ok": True, "driver": name})


DOWNLOAD_DIR = ROOT / "downloads"          # temporary zips for the download button
DOWNLOAD_README = """F1 22 telemetry relay - one stored run

run.json            driver, track, session type, start/end of the run
laps.csv            lap times and sectors in ms (invalid = lap does not count)
corners.csv         per lap and official F1 corner: corner time, entry/minimum/exit speed, braking and throttle point
lap_traces.csv      per lap: speed / throttle / brake every 2 metres (JSON arrays)
raw_packets.csv     EVERY packet the game sent, exactly as received:
                    received_at (unix time), packet_id (0 Motion, 1 Session, 2 LapData, 3 Event, 4 Participants,
                    5 CarSetups, 6 CarTelemetry, 7 CarStatus, 8 FinalClassification, 9 LobbyInfo, 10 CarDamage,
                    11 SessionHistory), frame, session_time, player_idx, payload_hex (the raw bytes as hex,
                    F1 22 UDP specification)
"""


def build_download(uid):
    """Writes a zip with everything of one run to disk and returns its path."""
    import csv
    import io
    import zipfile
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    for old in DOWNLOAD_DIR.glob("*.zip"):           # downloads from earlier are only temporary
        if time.time() - old.stat().st_mtime > 3600:
            try:
                old.unlink()
            except OSError:
                pass
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT run_id, session_uid, driver, track_id, session_type, track_length,
                                  extract(epoch FROM started_at), extract(epoch FROM last_seen_at), packets
                           FROM f1_sessions WHERE run_id = %s""", (uid,))
            row = cur.fetchone()
            if not row:
                raise ValueError("run not found")
            meta = dict(zip(("run_id", "session_uid", "driver", "track_id", "session_type", "track_length",
                             "started_at", "ended_at", "packets"), row))
            meta["track"] = TRACKS.get(meta["track_id"], "Unknown")
            meta["session"] = SESSION_TYPES.get(meta["session_type"], "Unknown")
            for k in ("started_at", "ended_at"):
                meta[k] = float(meta[k]) if meta[k] is not None else None
            safe = "".join(c if c.isalnum() else "-" for c in f"{meta['driver'] or 'unknown'}-{meta['track']}")
            when = time.strftime("%Y%m%d-%H%M", time.localtime(meta["started_at"] or time.time()))
            path = DOWNLOAD_DIR / f"f1-run-{when}-{safe}.zip"

            def table(sql, header):
                cur.execute(sql, (uid,))
                buf = io.StringIO()
                w = csv.writer(buf)
                w.writerow(header)
                w.writerows(cur.fetchall())
                return buf.getvalue()

            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
                z.writestr("README.txt", DOWNLOAD_README)
                z.writestr("run.json", json.dumps(meta, indent=2))
                z.writestr("laps.csv", table(
                    "SELECT lap_num, lap_time_ms, s1_ms, s2_ms, s3_ms, invalid FROM f1_laps WHERE run_id = %s "
                    "ORDER BY lap_num", ["lap", "lap_time_ms", "s1_ms", "s2_ms", "s3_ms", "invalid"]))
                z.writestr("corners.csv", table(
                    "SELECT lap_num, corner, apex_m, time_ms, entry_kmh, min_kmh, exit_kmh, min_at_m, "
                    "brake_before_m, throttle_after_m, max_brake FROM f1_corners WHERE run_id = %s "
                    "ORDER BY lap_num, corner_idx",
                    ["lap", "corner", "apex_m", "time_ms", "entry_kmh", "min_kmh", "exit_kmh", "min_at_m",
                     "brake_before_m", "throttle_after_m", "max_brake"]))
                cur.execute("SELECT lap_num, step_m, speed, throttle, brake, time_ms FROM f1_lap_traces "
                            "WHERE run_id = %s ORDER BY lap_num", (uid,))
                buf = io.StringIO()
                w = csv.writer(buf)
                w.writerow(["lap", "step_m", "speed_kmh", "throttle", "brake", "time_ms"])
                for r in cur.fetchall():
                    w.writerow([r[0], r[1]] + [json.dumps(list(x) if x is not None else None) for x in r[2:]])
                z.writestr("lap_traces.csv", buf.getvalue())
        # raw packets: streamed with a server-side cursor, so even very long runs never fill the memory
        with conn, conn.cursor(name="dl") as cur:
            cur.itersize = 5000
            cur.execute(f"""SELECT extract(epoch FROM received_at), packet_id, frame, session_time, player_idx,
                                   encode(payload, 'hex')
                            FROM f1_telemetry_raw WHERE {RUN_FILTER} ORDER BY id""", (uid, uid))
            with zipfile.ZipFile(path, "a", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
                with z.open("raw_packets.csv", "w", force_zip64=True) as f:
                    t = io.TextIOWrapper(f, encoding="utf-8", newline="")
                    w = csv.writer(t)
                    w.writerow(["received_at", "packet_id", "frame", "session_time", "player_idx", "payload_hex"])
                    for r in cur:
                        w.writerow([f"{float(r[0]):.6f}", r[1], r[2], r[3], r[4], r[5]])
                    t.flush()
                    t.detach()
        return path
    finally:
        conn.close()


async def api_run_download(request):
    bad = db_unavailable()
    if bad:
        return bad
    uid = request.match_info["uid"]
    try:
        path = await asyncio.get_running_loop().run_in_executor(None, build_download, uid)
    except Exception as e:
        return web.json_response({"error": friendly_db_error(e)}, status=500)
    return web.FileResponse(path, headers={"Content-Disposition": f'attachment; filename="{path.name}"'})


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
    udp_host = CFG.get("UDP_HOST", "127.0.0.1").strip() or "127.0.0.1"
    web_host = CFG.get("WEB_HOST", "127.0.0.1").strip() or "127.0.0.1"
    await loop.create_datagram_endpoint(UdpProtocol, local_addr=(udp_host, udp_port))

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
        web.post("/api/sessions/{uid}/driver", api_run_driver),
        web.get("/api/sessions/{uid}/download", api_run_download),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, web_host, http_port).start()
    asyncio.create_task(broadcaster())

    print("=" * 62)
    print(f" UDP:         listening on {udp_host}:{udp_port}  (in the game: IP 127.0.0.1, port {udp_port})")
    print(f" Website:     http://127.0.0.1:{http_port}   (tabs: Live / Raw / Races)"
          + ("" if web_host == "127.0.0.1" else f"  - also open for other devices on the network ({web_host})"))
    print(f" Config:      {ROOT / 'config' / 'config.env'}")
    print(f" Database:    {('on, server ' + db_host() + ' (login from ' + CFG['DB_SOURCE'] + ')') if CFG['DATABASE_URL'] else 'OFF (' + (CFG['DB_SECRET_ERROR'] or 'not configured') + ')'}")
    print(f" Drivers:     {DRIVERS.numbered() or '(none yet: add names on the website)'}")
    print("              change driver: website, the 'F1 driver' window, or type a number here + Enter")
    print(f" Driver:      {state['driver'] or '(empty)'}")
    print("=" * 62)
    if "--no-browser" not in sys.argv:
        webbrowser.open(f"http://127.0.0.1:{http_port}/")
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
    POWER.before_exit = spool_everything
    if CFG["DATABASE_URL"]:
        POWER.start_thread()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Give Azure a few seconds to take what is still waiting, then save the rest on this PC
        end = time.time() + 8
        while state["db"]["connected"] and (len(pending["pkts"]) or db_queue.qsize()) and time.time() < end:
            time.sleep(0.3)
        spool_everything()
        POWER.on_exit(wait_flush=0)
        print(f"\nStopped ({state['total']:,} packets received this session).")

"""
Races of the earlier collector (the f22_ tables, read only): the list of old races, their laps,
a replay and the raw samples for the corner comparison.
"""
import asyncio
import json
import time

from db import friendly_db_error, query, query_batch
from f1_format import SESSION_TYPES, TRACKS
from shared import CFG, ROOT, state


LEGACY = "f22_"   # tables of the earlier collector (read only)


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

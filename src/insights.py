"""
Extra analysis on stored data: leaderboard, consistency, tyres/strategy, track map from raw data
and the live status row for the other group. Everything here only READS the f1_ tables
(except write_status, which keeps one row in f1_status up to date).
"""
import json
import statistics
import struct
import time

from f1_format import TRACKS, COMPOUNDS, SESSION_TYPES

from paths import CACHE

TRACKMAP_FILE = CACHE / "trackmaps.json"
RUN_FILTER = "(run_id = %s OR (run_id IS NULL AND session_uid = %s))"


# ----------------------------------------------------------------- Consistency
def consistency(lap_times):
    """Score 0-100 for how constant someone drives, from valid lap times (ms).
    Laps slower than 107% of the best (spins, traffic, out laps) are left out.
    1% spread (standard deviation / best) = 90 points, 5% = 50, 10% or more = 0.
    Returns (score, spread_ms, laps_used) or None with fewer than 3 usable laps."""
    laps = [t for t in lap_times if t and t > 0]
    if not laps:
        return None
    best = min(laps)
    use = [t for t in laps if t <= best * 1.07]
    if len(use) < 3:
        return None
    sd = statistics.pstdev(use)
    score = max(0.0, min(100.0, 100 - 10 * (sd / best * 100)))
    return round(score), round(sd), len(use)


# ----------------------------------------------------------------- Leaderboard
def leaderboard(cur):
    """Per track: per driver best valid lap, best sectors, theoretical best, number of laps, consistency."""
    cur.execute("""
        SELECT s.track_id, s.driver, l.run_id, l.lap_num, l.lap_time_ms, l.s1_ms, l.s2_ms, l.s3_ms,
               extract(epoch FROM s.started_at), s.session_type
        FROM f1_laps l JOIN f1_sessions s ON s.run_id = l.run_id
        WHERE NOT coalesce(l.invalid, false) AND l.lap_time_ms > 0 AND s.track_id >= 0
          AND coalesce(s.driver, '') <> ''""")
    data = {}
    for track, driver, run, lap, ms, s1, s2, s3, started, stype in cur.fetchall():
        key = driver.strip()
        d = data.setdefault(track, {}).setdefault(key.lower(), {
            "driver": key, "best": None, "laps": [], "s1": None, "s2": None, "s3": None})
        d["laps"].append(ms)
        if d["best"] is None or ms < d["best"]["ms"]:
            d["best"] = {"ms": ms, "run": run, "lap": lap, "at": float(started or 0),
                         "session": SESSION_TYPES.get(stype, "?"), "s": [s1, s2, s3]}
        for k, v in (("s1", s1), ("s2", s2), ("s3", s3)):
            if v and v > 0 and (d[k] is None or v < d[k]):
                d[k] = v
    out = []
    for track, drivers in data.items():
        rows = []
        for d in drivers.values():
            c = consistency(d["laps"])
            theo = d["s1"] + d["s2"] + d["s3"] if d["s1"] and d["s2"] and d["s3"] else None
            rows.append({"driver": d["driver"], "best": d["best"]["ms"], "run": d["best"]["run"],
                         "lap": d["best"]["lap"], "at": d["best"]["at"], "session": d["best"]["session"],
                         "sectors": d["best"]["s"], "best_sectors": [d["s1"], d["s2"], d["s3"]],
                         "theoretical": theo, "laps": len(d["laps"]),
                         "consistency": c[0] if c else None, "spread": c[1] if c else None})
        rows.sort(key=lambda r: r["best"])
        last = max((r["at"] for r in rows), default=0)
        out.append({"track_id": track, "track": TRACKS.get(track, f"Track {track}"), "drivers": rows,
                    "last": last})
    out.sort(key=lambda t: -t["last"])
    return out


def driver_bests(cur):
    """{(track, driver_lower): best_ms} and {track: (best_ms, driver)} for personal records and track records."""
    cur.execute("""
        SELECT s.track_id, s.driver, min(l.lap_time_ms)
        FROM f1_laps l JOIN f1_sessions s ON s.run_id = l.run_id
        WHERE NOT coalesce(l.invalid, false) AND l.lap_time_ms > 0 AND coalesce(s.driver, '') <> ''
        GROUP BY 1, 2""")
    pb, track = {}, {}
    for t, drv, ms in cur.fetchall():
        pb[(t, drv.strip().lower())] = ms
        if t not in track or ms < track[t][0]:
            track[t] = (ms, drv.strip())
    return pb, track


def reference_lap(cur, track_id):
    """The group's fastest valid lap on this track that has a lap trace (time every 2 m), for the live delta."""
    cur.execute("""
        SELECT s.driver, l.lap_time_ms, t.step_m, t.time_ms, t.track_length
        FROM f1_laps l
        JOIN f1_sessions s ON s.run_id = l.run_id
        JOIN f1_lap_traces t ON t.run_id = l.run_id AND t.lap_num = l.lap_num
        WHERE s.track_id = %s AND NOT coalesce(l.invalid, false) AND l.lap_time_ms > 0
        ORDER BY l.lap_time_ms LIMIT 1""", (track_id,))
    r = cur.fetchone()
    if not r:
        return None
    return {"driver": r[0] or "?", "ms": r[1], "step": float(r[2]), "time": list(r[3]), "len": r[4]}


# ----------------------------------------------------------------- Tyres and strategy
def tyre_history(cur, run_id):
    """Per lap: tyre wear (%), tyre surface temperature, compound and tyre age at the end of the lap,
    plus a simple pit advice. Worked out from the raw packets (player car only)."""
    def player_rows(pid, size, off, length):
        cur.execute(f"""SELECT frame, substring(payload from 24 + coalesce(player_idx, 0) * {size} + {off} + 1
                                                for {length})
                        FROM f1_telemetry_raw WHERE {RUN_FILTER} AND packet_id = %s ORDER BY frame""",
                    (run_id, run_id, pid))
        return [(f, bytes(b)) for f, b in cur.fetchall() if b is not None and len(bytes(b)) == length]

    laps = player_rows(2, 43, 25, 2)                  # current lap number, pit status
    wear = player_rows(10, 42, 0, 16)                 # 4 floats tyre wear
    temp = player_rows(6, 60, 30, 4)                  # 4 bytes tyre surface temperature
    stat = player_rows(7, 47, 26, 2)                  # visual compound, tyre age
    if not laps:
        return {"laps": [], "advice": "No raw data for this run"}

    frames = [f for f, _ in laps]
    import bisect

    def lap_at(frame):
        i = bisect.bisect_right(frames, frame) - 1
        return laps[i][1][0] if i >= 0 else None

    per = {}
    for rows, key, conv in ((wear, "wear", lambda b: [round(x, 1) for x in struct.unpack("<4f", b)]),
                            (temp, "temp", lambda b: list(b)),
                            (stat, "tyre", lambda b: [COMPOUNDS.get(b[0], "?"), b[1]])):
        for f, b in rows:
            lap = lap_at(f)
            if lap:
                per.setdefault(lap, {})[key] = conv(b)        # keeps the last value of each lap
    cur.execute("SELECT lap_num, lap_time_ms, invalid FROM f1_laps WHERE run_id = %s", (run_id,))
    times = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    out = []
    for lap in sorted(per):
        p = per[lap]
        t = times.get(lap, (None, None))
        out.append({"lap": lap, "time": t[0], "invalid": bool(t[1]), "wear": p.get("wear"),
                    "temp": p.get("temp"), "compound": (p.get("tyre") or [None, None])[0],
                    "age": (p.get("tyre") or [None, None])[1]})
    return {"laps": out, "advice": pit_advice(out)}


def pit_advice(laps):
    """Simple advice: the pit window starts at the first lap where the worst tyre passes 60% wear,
    or where the lap time is more than 1.5% slower than the best for two laps in a row (tyres giving up)."""
    valid = [l for l in laps if l["time"] and not l["invalid"]]
    best = min((l["time"] for l in valid), default=None)
    slow = 0
    for l in laps:
        w = max(l["wear"]) if l.get("wear") else 0
        if w >= 60:
            return f"Pit window from lap {l['lap']}: worst tyre at {w:.0f}% wear"
        if best and l["time"] and not l["invalid"] and l["time"] > best * 1.015:
            slow += 1
            if slow >= 2:
                return f"Pit window from lap {l['lap'] - 1}: lap times more than 1.5% slower than the best"
        else:
            slow = 0
    worst = max((max(l["wear"]) for l in laps if l.get("wear")), default=0)
    return f"No pit stop needed yet (worst tyre {worst:.0f}% worn at the end)"


# ----------------------------------------------------------------- Track map from raw data
def _load_maps():
    try:
        return json.loads(TRACKMAP_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def track_map(cur, track_id):
    """Outline of a track as [lap distance m, x, z] every 8 m, from the raw packets of the fastest stored lap.
    Cached in config/trackmaps.json, so it is only worked out once per track."""
    maps = _load_maps()
    if str(track_id) in maps:
        return maps[str(track_id)]
    cur.execute("""
        SELECT l.run_id, l.lap_num FROM f1_laps l JOIN f1_sessions s ON s.run_id = l.run_id
        WHERE s.track_id = %s AND l.lap_time_ms > 0
        ORDER BY coalesce(l.invalid, false), l.lap_time_ms LIMIT 3""", (track_id,))
    for run_id, lap_num in cur.fetchall():
        cur.execute(f"""SELECT frame, substring(payload from 24 + coalesce(player_idx, 0) * 43 + 1 for 30)
                        FROM f1_telemetry_raw WHERE {RUN_FILTER} AND packet_id = 2 ORDER BY frame""",
                    (run_id, run_id))
        dist = {}
        for f, b in cur.fetchall():
            b = bytes(b)
            if len(b) < 30:
                continue
            d = struct.unpack_from("<f", b, 12)[0]
            if b[25] == lap_num and b[26] == 0 and d >= 0:
                dist[f] = d
        if len(dist) < 50:
            continue
        cur.execute(f"""SELECT frame, substring(payload from 24 + coalesce(player_idx, 0) * 60 + 1 for 12)
                        FROM f1_telemetry_raw WHERE {RUN_FILTER} AND packet_id = 0 ORDER BY frame""",
                    (run_id, run_id))
        bins = {}
        for f, b in cur.fetchall():
            if f in dist and b is not None and len(bytes(b)) == 12:
                x, _y, z = struct.unpack("<3f", bytes(b))
                k = int(dist[f] // 8)
                s = bins.setdefault(k, [0.0, 0.0, 0])
                s[0] += x
                s[1] += z
                s[2] += 1
        if len(bins) < 40:
            continue
        pts = [[k * 8 + 4, round(s[0] / s[2], 1), round(s[1] / s[2], 1)] for k, s in sorted(bins.items())]
        maps[str(track_id)] = pts
        try:
            TRACKMAP_FILE.parent.mkdir(parents=True, exist_ok=True)
            TRACKMAP_FILE.write_text(json.dumps(maps), encoding="utf-8")
        except OSError:
            pass
        return pts
    return None


# ----------------------------------------------------------------- Status row for the other group
STATUS_DDL = """
CREATE TABLE IF NOT EXISTS f1_status (
    name TEXT PRIMARY KEY,
    updated_at TIMESTAMPTZ NOT NULL,
    info JSONB NOT NULL
)"""


def packets_today(cur):
    cur.execute("""SELECT coalesce(sum(packets), 0), count(*) FROM f1_sessions
                   WHERE started_at >= date_trunc('day', now())""")
    p, runs = cur.fetchone()
    return int(p), int(runs)


def write_status(cur, info):
    cur.execute(STATUS_DDL)
    cur.execute("""INSERT INTO f1_status (name, updated_at, info) VALUES ('relay', now(), %s)
                   ON CONFLICT (name) DO UPDATE SET updated_at = now(), info = EXCLUDED.info""",
                (json.dumps(info),))


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")

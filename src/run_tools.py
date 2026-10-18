"""
Tools for stored runs: the download zip, the compact replay samples, and the corner comparison between
drivers (including working out corners and lap traces from raw data for runs stored before that existed).
"""
import json
import struct
import time

from corners import CORNERS, corner_rows, corner_stats, lap_trace, trace_row
from db import db_connect, ensure_schema
from f1_format import LAP_STRUCT, SESSION_TYPES, TRACKS
from legacy import merge_samples, raw_f22
from schema import CORNER_INSERT, TRACE_INSERT
from shared import ROOT, RUN_FILTER


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


def sample_sql(length, per_car, every):
    """Cuts only the player car's bytes out of each packet inside the database, and thins out the rows."""
    return f"""
        SELECT session_time, substring(payload from 24 + coalesce(player_idx, 0) * {per_car} + 1 for {length})
        FROM (SELECT session_time, payload, player_idx, row_number() OVER (ORDER BY frame) AS rn
              FROM f1_telemetry_raw WHERE {RUN_FILTER} AND packet_id = %s) t
        WHERE rn %% {every} = 0 ORDER BY session_time"""


# ----------------------------------------------------------------- Corner comparison between drivers
corner_tried = set()      # runs for which a backfill from raw data was already attempted (this relay session)


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

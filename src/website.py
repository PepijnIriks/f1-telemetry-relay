"""
The website: the pages (Live / Raw / Races / Leaderboard / status / report), the WebSocket that pushes live
data to the browser, and the /api/... endpoints the pages call.
"""
import asyncio
import json
import struct
import time

from aiohttp import web

import insights
from corners import CORNERS, corner_layout
from db import db_call, db_connect, friendly_db_error, query, status_info
from driver_runs import switch_driver
from f1_format import COMPOUNDS, DAMAGE_STRUCT, LAP_STRUCT, PACKET_NAMES, SESSION_TYPES, STATUS_STRUCT, TEL_STRUCT, TRACKS
from legacy import legacy, legacy_due, legacy_lap_list, legacy_replay, refresh_legacy
from run_tools import build_download, load_compare, load_trace, sample_sql, summarise_run
from shared import (CFG, DEMO, DRIVERS, POWER, ROOT, clients, db_queue, events, events_dirty, grid_dirty,
                    grid_state, live, live_buffer, live_dirty, sess, state)


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


async def api_status(request):
    return web.json_response(status_info())


async def page(request):
    name = {"/status": "status.html", "/report": "report.html"}[request.path]
    return web.FileResponse(ROOT / "web" / name)


async def db_json(fn, *args):
    bad = db_unavailable()
    if bad:
        return bad
    try:
        res = await asyncio.get_running_loop().run_in_executor(None, db_call, fn, *args)
    except Exception as e:
        return web.json_response({"enabled": True, "error": friendly_db_error(e)})
    return web.json_response({"enabled": True, "data": res})


async def api_leaderboard(request):
    return await db_json(insights.leaderboard)


async def api_trackmap(request):
    try:
        track = int(request.query.get("track", "-1"))
    except ValueError:
        return web.json_response({"error": "bad track"})
    return await db_json(insights.track_map, track)


async def api_tyres(request):
    return await db_json(insights.tyre_history, request.match_info["uid"])


# ----------------------------------------------------------------- Run: change driver / download
async def api_run_driver(request):
    """Change the driver name of a stored run (e.g. someone forgot to switch). No data is removed.
    Only f1_sessions changes: that is where the driver of a run is looked up. The raw packets are NOT rewritten
    (that would make PostgreSQL copy every packet of the run, so the table would grow by the size of the run,
    and compressed packets can't be changed cheaply). Their `driver` column keeps the name at recording time."""
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
                return cur.rowcount
        finally:
            conn.close()
    try:
        n = await asyncio.get_running_loop().run_in_executor(None, work)
    except Exception as e:
        return web.json_response({"error": friendly_db_error(e)})
    if not n:
        return web.json_response({"error": "Run not found"})
    if uid in sess:
        sess[uid]["driver"] = name
    print(f"[DRIVER] Run {uid} now belongs to {name}", flush=True)
    return web.json_response({"ok": True, "driver": name})


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
                         "drivers": DRIVERS.names, "demo": DEMO,
                         "db": state["db"], "now": now})
        for m in msgs:
            text = json.dumps(m)
            for ws in list(clients):
                try:
                    await ws.send_str(text)
                except Exception:
                    clients.discard(ws)


def make_app():
    """All pages and API endpoints of the website."""
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
        web.get("/api/sessions/{uid}/tyres", api_tyres),
        web.get("/api/leaderboard", api_leaderboard),
        web.get("/api/trackmap", api_trackmap),
        web.get("/api/status", api_status),
        web.get("/status", page), web.get("/report", page),
        web.get("/api/sessions/{uid}/download", api_run_download),
    ])
    return app

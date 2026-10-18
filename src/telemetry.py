"""
Game data in: receives the UDP packets of F1 22, parses the player's car and the whole field for the
Live dashboard, completes laps (lap and sector times, corner analysis, lap trace) and hands every packet
to the database writer.
"""
import asyncio
import struct
import time

from corners import corner_rows, corner_stats, lap_trace, trace_row
from driver_runs import new_run
from f1_format import (COMPOUNDS, DAMAGE_STRUCT, HEADER, HEADER_FIELDS, LAP_STRUCT, MOTION_SIZE, PACKET_NAMES,
                       SESSION_TYPES, STATUS_STRUCT, TEL_STRUCT, TRACKS, WEATHER)
from shared import (POWER, carry_from, car_name, cornerbuf, db_queue, events, events_dirty, fmt_ms, grid_dirty,
                    grid_state, lapstate, last_grid, live, live_buffer, live_dirty, names, numbers, records, ref_wanted,
                    refs, runs, sess, state, teams)


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
    base = 24 + p * MOTION_SIZE
    x, _y, z = struct.unpack_from("<3f", data, base)
    g_lat, g_long = struct.unpack_from("<2f", data, base + 36)
    live["pos"] = [round(x, 1), round(z, 1)]
    live["g"] = [round(g_lat, 2), round(g_long, 2)]
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
            if not st["invalid"]:
                check_records(meta, last_ms, now)
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

    # live delta to the group's fastest lap on this track (same distance into the lap)
    ref_wanted["track"] = meta["track"] if meta["track"] >= 0 else None
    ref = refs.get(meta["track"])
    if ref and pit == 0 and v[4] > 0 and not invalid:
        i = int(v[4] / ref["step"])
        live["delta"] = {"ms": cur_ms - ref["time"][i], "ref": ref["driver"], "refMs": ref["ms"]} \
            if i < len(ref["time"]) else None
    else:
        live["delta"] = None
    live["lap"] = {"pos": pos, "num": lapnum, "cur": cur_ms, "last": last_ms, "s1": s1, "s2": s2, "dist": round(v[4]),
                   "sector": sector, "invalid": bool(invalid), "pit": pit, "pen": penalties,
                   "best": st.get("best", 0), "laps": st["laps"]}
    live_dirty[0] = True

    if now - last_grid[0] >= 0.5:
        last_grid[0] = now
        build_grid(data, p)


def check_records(meta, ms, now):
    """A valid lap was completed: new personal best? new track record (fastest of the whole group)?"""
    driver, track = (meta.get("driver") or "").strip(), meta.get("track", -1)
    if not driver or track < 0 or not records["loaded"]:
        return
    name = TRACKS.get(track, "this track")
    old_pb = records["pb"].get((track, driver.lower()))
    old_tr = records["track"].get(track)
    text = None
    if old_tr is None or ms < old_tr[0]:
        records["track"][track] = (ms, driver)
        text = (f"TRACK RECORD {name}: {driver} {fmt_ms(ms)}"
                + (f" (-{(old_tr[0] - ms) / 1000:.3f} s, was {old_tr[1]})" if old_tr else ""))
        kind = "record"
    if old_pb is None or ms < old_pb:
        records["pb"][(track, driver.lower())] = ms
        if not text:
            text = f"New personal best for {driver} on {name}: {fmt_ms(ms)}" + \
                   (f" (-{(old_pb - ms) / 1000:.3f} s)" if old_pb else "")
            kind = "pb"
    if text:
        events.insert(0, {"t": now, "text": text})
        del events[40:]
        events_dirty[0] = True
        live["toast"] = {"t": now, "text": text, "kind": kind}
        print(f"[RECORD] {text}", flush=True)
        if kind == "record":
            ref_wanted["reload"] = now + 6      # the new fastest lap becomes the reference for the delta


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


class UdpProtocol(asyncio.DatagramProtocol):
    def datagram_received(self, data, addr):
        now = time.time()
        if len(data) < HEADER.size:
            return
        h = dict(zip(HEADER_FIELDS, HEADER.unpack_from(data)))
        pid = h["packet_id"]
        uid = str(h["session_uid"])
        state["total"] += 1
        state["last_pkt"] = now
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

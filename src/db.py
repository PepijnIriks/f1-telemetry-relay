"""
Everything with the Azure database: the login (Key Vault), the tables, the background writer that sends
packets, laps, corners and lap traces in batches (the live view never waits for it), the safety net that keeps
unsent data on this PC (spool), the storage check, the status row for the other group and small query helpers.
"""
import asyncio
import queue
import sys
import threading
import time
from urllib.parse import urlparse

import insights
import secrets_store
import spool
import storage
from f1_format import TRACKS
from schema import CORNER_INSERT, DDL, TRACE_INSERT
from shared import CFG, DEMO, POWER, db_queue, fmt_ms, live, records, ref_wanted, refs, state


schema_lock = threading.Lock()
schema_ok = [False]


def db_host():
    try:
        return urlparse(CFG["DATABASE_URL"]).hostname or "?"
    except Exception:
        return "?"


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
        return "Wrong password or username in the Key Vault secret (tools.bat > Key Vault setup)"
    if "timeout" in low or "timed out" in low:
        return "Server unreachable (time-out): check the firewall rule in Azure and your internet connection"
    if "no pg_hba.conf" in low:
        return "Access denied by Azure: your IP address is not in the firewall or SSL is missing"
    if "could not translate host" in low or "name or service not known" in low:
        return "Server name not found: check the server name in the Key Vault secret"
    return msg.replace("\n", " ")[:220]


# ----------------------------------------------------------------- Storage in Azure
# EVERY packet is stored and nothing is ever removed: all data must stay findable later.
# The relay only keeps an eye on how full the database is, because a full database becomes read-only
# (then nothing new can be stored). Azure storage: 64 GB, autogrow off.
def storage_check(conn):
    gb = 1024 ** 3
    warn_gb = float(CFG.get("DB_WARN_GB", "50") or 0)
    with conn, conn.cursor() as cur:
        cur.execute("SELECT pg_database_size(current_database())")
        db_size = cur.fetchone()[0]
        raw = storage.raw_info(cur)
        can_compress = not raw["hypertable"] and storage.timescale_available(cur)
    state["db"]["size_gb"] = round(db_size / gb, 2)
    state["db"]["raw_compressed"] = raw["hypertable"] and raw["compressed"]
    print(f"[DB] Storage: database {db_size / gb:.1f} GB in total ({storage.describe(raw)})", flush=True)
    if can_compress and raw["size"] > gb:
        print("[DB] Tip: the raw packets are not compressed yet. Close the relay and run tools.bat > "
              "Compress the database once: the same data then takes much less space.", flush=True)
    if warn_gb > 0 and db_size > warn_gb * gb:
        msg = (f"Database is {db_size / gb:.1f} GB, above the warning level of {warn_gb:g} GB. Make the storage "
               "bigger in the Azure portal (f1db > Compute + storage) before it is full, or it becomes read-only.")
        state["db"]["warning"] = msg
        print(f"[DB] WARNING: {msg}", flush=True)
    else:
        state["db"]["warning"] = ""


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
    """Moves everything from the UDP thread's queue into `pending` (in demo mode: throws it away)."""
    with pending_lock:
        while True:
            try:
                msg = db_queue.get_nowait()
            except queue.Empty:
                return
            if DEMO:
                continue
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
        print(f"[DB] {reason}: {len(b['pkts']):,} packets not in Azure yet are saved on this PC (folder data\\spool) "
              "and sent to Azure the next time the relay connects.", flush=True)


def db_worker():
    url = CFG["DATABASE_URL"]
    if not url:
        state["db"]["hint"] = CFG["DB_SECRET_ERROR"] or "No database configured (start.bat sets it up, or tools.bat > Key Vault setup)"
        return
    import importlib.util
    if importlib.util.find_spec("psycopg2") is None:
        state["db"]["error"] = "psycopg2 not installed (run start.bat again)"
        return

    state["db"]["enabled"] = True
    conn = None
    flush = float(CFG["DB_FLUSH_SECONDS"])
    last_flush = last_try = last_storage = last_status = 0.0
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
                print(f"[DB] Connected to {db_host()} - " + ("DEMO MODE: only reading, nothing is stored" if DEMO
                                                             else "tables ready, data is being stored"))
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
                    try:
                        with conn, conn.cursor() as cur:
                            records["pb"], records["track"] = insights.driver_bests(cur)
                        records["loaded"] = True
                    except Exception as e:
                        print(f"[DB] Loading personal bests failed: {friendly_db_error(e)}", flush=True)
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

        # Reference lap for the live delta (fastest lap of the group on the track being driven)
        tw = ref_wanted["track"]
        if conn and tw is not None and (tw not in refs or (ref_wanted["reload"] and now >= ref_wanted["reload"])):
            ref_wanted["reload"] = 0.0
            try:
                with conn, conn.cursor() as cur:
                    refs[tw] = insights.reference_lap(cur, tw)
                if refs[tw]:
                    print(f"[DELTA] Reference for {TRACKS.get(tw, tw)}: {refs[tw]['driver']} "
                          f"{fmt_ms(refs[tw]['ms'])}", flush=True)
            except Exception as e:
                refs[tw] = None
                print(f"[DB] Loading the reference lap failed: {friendly_db_error(e)}", flush=True)

        # Status row for the other group (f1_status), every minute
        if conn and not DEMO and now - last_status >= 60:
            last_status = now
            try:
                with conn, conn.cursor() as cur:
                    pk, nruns = insights.packets_today(cur)
                    status_cache.update({"packets_today": pk, "runs_today": nruns})
                    insights.write_status(cur, status_info())
            except Exception as e:
                print(f"[DB] Writing the status failed: {friendly_db_error(e)}", flush=True)
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


# ----------------------------------------------------------------- Status row (f1_status) for the other group


status_cache = {"packets_today": None, "runs_today": None}


def status_info():
    now = time.time()
    s = live.get("session") or {}
    return {
        "relay_running": True, "updated": insights.now_iso(),
        "game_data": now - state.get("last_pkt", 0) < 5, "last_packet_age_s": round(now - state["last_pkt"])
        if state.get("last_pkt") else None,
        "driver": state["driver"], "track": s.get("track"), "session": s.get("type"),
        "packets_this_relay_session": state["total"], "written_to_db": state["db"].get("written", 0),
        "waiting_on_pc": state["db"].get("spooled", 0), "packets_today": status_cache["packets_today"],
        "runs_today": status_cache["runs_today"], "db_size_gb": state["db"].get("size_gb"),
        "db_connected": state["db"].get("connected", False), "db_error": state["db"].get("error", ""),
        "azure": state.get("azure", {}).get("msg", ""), "uptime_s": round(now - state["started"]),
    }


# ----------------------------------------------------------------- Query helpers (used by the website)


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


def db_call(fn, *args):
    conn = db_connect()
    try:
        ensure_schema(conn)
        with conn, conn.cursor() as cur:
            return fn(cur, *args)
    finally:
        conn.close()

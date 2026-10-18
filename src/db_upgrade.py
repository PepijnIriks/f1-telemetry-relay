"""
One-time database upgrade: keeps ALL data, but makes it take much less space.

Usage: close the relay, then tools.bat > Compress the database. It can be stopped (close the window) and started
again at any moment: it continues where it was.

What it does:
1. Compression (needs TimescaleDB on the server; the earlier collector already used it):
   the table with raw packets (f1_telemetry_raw) is copied, in small steps, into a TimescaleDB "hypertable"
   that is split per week. Weeks older than 7 days are compressed. All data stays in the database and can
   be read with normal SQL, exactly like before (the website, the CSV export and the other group see no difference).
   Every step is checked: the copy must have exactly the same number of packets, the same total number of bytes
   and the same packet ids before the tables are swapped.
   The old, uncompressed table is kept as f1_telemetry_raw_old until you type YES to remove it.
2. Without TimescaleDB: replaces the large index on session_uid by a small one that only covers the
   old rows without run_id (the rest is found through the run_id index).
3. Optional: also compresses the tables of the earlier collector (f22_...) if they are not compressed yet.

Options: --drop-old   check the copy again and remove f1_telemetry_raw_old
         --cancel     stop an unfinished upgrade: removes only the half-made copy (never your data)
         --batch N    packets per step (default 200000)
"""
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import storage  # noqa: E402

RAW, NEW, OLD = "f1_telemetry_raw", "f1_telemetry_raw_new", "f1_telemetry_raw_old"
COLS = "id, received_at, driver, session_uid, packet_id, frame, session_time, player_idx, payload, run_id"
SELECT_COLS = ("id, received_at, driver, session_uid, packet_id, frame, session_time, player_idx, payload, "
               "coalesce(run_id, session_uid)")
CHUNK = "7 days"           # one chunk per week
COMPRESS_AFTER = "7 days"  # chunks are compressed when their week is over and 7 days old
GB = 1024 ** 3


def say(msg=""):
    print(msg, flush=True)


# ----------------------------------------------------------------- small helpers
def one(cur, sql, params=()):
    cur.execute(sql, params)
    row = cur.fetchone()
    return row[0] if row else None


def exists(cur, table):
    return one(cur, "SELECT to_regclass(%s) IS NOT NULL", (table,))


def ts_version(cur):
    v = one(cur, "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'") or "0"
    return tuple(int(x) for x in v.split("-")[0].split(".")[:2])


def fingerprint(cur, table, max_id=None):
    """(packets, total payload bytes, sum of ids) - two tables with the same fingerprint hold the same packets."""
    where = "" if max_id is None else f" WHERE id <= {int(max_id)}"
    cur.execute(f"SELECT count(*), coalesce(sum(length(payload)), 0), coalesce(sum(id), 0) FROM {table}{where}")
    return tuple(int(x) for x in cur.fetchone())


def progress(cur):
    cur.execute("CREATE TABLE IF NOT EXISTS f1_upgrade_progress (step TEXT PRIMARY KEY, last_id BIGINT, "
                "info JSONB, updated_at TIMESTAMPTZ DEFAULT now())")
    cur.execute("SELECT last_id, info FROM f1_upgrade_progress WHERE step = 'compress_raw'")
    return cur.fetchone()


def save_progress(cur, last_id, info):
    cur.execute("""INSERT INTO f1_upgrade_progress (step, last_id, info) VALUES ('compress_raw', %s, %s)
                   ON CONFLICT (step) DO UPDATE SET last_id = EXCLUDED.last_id, info = EXCLUDED.info,
                                                    updated_at = now()""", (last_id, json.dumps(info)))


# ----------------------------------------------------------------- what is there to do?
def plan(conn):
    with conn, conn.cursor() as cur:
        p = {"raw_exists": exists(cur, RAW), "old_exists": exists(cur, OLD), "new_exists": exists(cur, NEW),
             "ts_available": storage.timescale_available(cur), "ts_installed": storage.timescale_installed(cur)}
        p["raw"] = storage.raw_info(cur) if p["raw_exists"] else None
        p["db_size"] = one(cur, "SELECT pg_database_size(current_database())")
        p["session_idx"] = exists(cur, "f1_telemetry_raw_session_idx")
        p["legacy_idx"] = exists(cur, "f1_telemetry_raw_legacy_idx")
        p["relay_seen_s"] = None
        if exists(cur, "f1_status"):
            p["relay_seen_s"] = one(cur, "SELECT extract(epoch FROM now() - updated_at) FROM f1_status "
                                         "WHERE name = 'relay'")
        p["f22"] = []
        if p["ts_installed"]:
            cur.execute("""SELECT hypertable_name, compression_enabled FROM timescaledb_information.hypertables
                           WHERE hypertable_name LIKE 'f22\\_%' ORDER BY 1""")
            p["f22"] = cur.fetchall()
    return p


# ----------------------------------------------------------------- 1. compression of f1_telemetry_raw
def prepare_new_table(conn):
    """Creates f1_telemetry_raw_new as a compressed hypertable (once)."""
    with conn, conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        if exists(cur, NEW):
            return
        seq = one(cur, "SELECT pg_get_serial_sequence(%s, 'id')", (RAW,))
        cur.execute(f"""
            CREATE TABLE {NEW} (
                id BIGINT NOT NULL DEFAULT nextval('{seq}'::regclass),
                received_at TIMESTAMPTZ NOT NULL,
                driver TEXT,
                session_uid TEXT,
                packet_id SMALLINT,
                frame BIGINT,
                session_time REAL,
                player_idx SMALLINT,
                payload BYTEA,
                run_id TEXT)""")
        cur.execute(f"SELECT create_hypertable('{NEW}', 'received_at', chunk_time_interval => INTERVAL '{CHUNK}')")
        cur.execute(f"CREATE INDEX {NEW}_run_idx ON {NEW} (run_id, packet_id, frame)")
        # Packets of one run and one type are stored together and in arrival order: they compress well,
        # and reading one run (replay, corners, download) only unpacks that run.
        cur.execute(f"""ALTER TABLE {NEW} SET (timescaledb.compress,
                        timescaledb.compress_segmentby = 'run_id, packet_id',
                        timescaledb.compress_orderby = 'id')""")


def compress_ready_chunks(conn, table, before):
    """Compresses every chunk of `table` that ends before `before` (a timestamp) and is not compressed yet."""
    n = 0
    with conn, conn.cursor() as cur:
        cur.execute("""SELECT format('%%I.%%I', chunk_schema, chunk_name) FROM timescaledb_information.chunks
                       WHERE hypertable_name = %s AND NOT is_compressed AND range_end <= %s
                       ORDER BY range_start""", (table, before))
        chunks = [r[0] for r in cur.fetchall()]
    for c in chunks:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT compress_chunk(%s::regclass, if_not_compressed => true)", (c,))
        n += 1
    return n


class OutOfSpace(Exception):
    pass


def copy_raw(conn, batch=200_000, log=say, limit_bytes=None):
    """Copies f1_telemetry_raw into f1_telemetry_raw_new in steps of `batch` packets (resumable).
    Stops (OutOfSpace) before the database gets within 3 GB of `limit_bytes`: a full database becomes read-only."""
    with conn, conn.cursor() as cur:
        row = progress(cur)
        last = row[0] if row else 0
        info = row[1] if row else {}
        top = one(cur, f"SELECT coalesce(max(id), 0) FROM {RAW}")
        total = one(cur, f"SELECT count(*) FROM {RAW} WHERE id > %s", (last,))
        per_step_ok = ts_version(cur) >= (2, 11)     # older versions can't add rows to a compressed week
    if last:
        log(f"Continuing where it stopped last time ({info.get('copied', 0):,} packets were already copied).")
    log(f"To copy: {total:,} packets.")
    copied, t0 = 0, time.time()
    while last < top:
        if limit_bytes:
            with conn, conn.cursor() as cur:
                size = one(cur, "SELECT pg_database_size(current_database())")
            if size > limit_bytes - 3 * GB:
                raise OutOfSpace(size)
        with conn, conn.cursor() as cur:
            cur.execute(f"""INSERT INTO {NEW} ({COLS})
                            SELECT {SELECT_COLS} FROM {RAW} WHERE id > %s AND id <= %s ORDER BY id""",
                        (last, last + batch))
            n = cur.rowcount
            hi = last + batch
            cur.execute(f"SELECT max(received_at) FROM {RAW} WHERE id > %s AND id <= %s", (last, hi))
            newest = cur.fetchone()[0]
            info["copied"] = info.get("copied", 0) + n
            save_progress(cur, hi, info)
        last, copied = hi, copied + n
        if per_step_ok and newest is not None:
            # weeks the copy has fully passed (with one day of margin) are compressed right away,
            # so the extra space needed during the upgrade stays small
            with conn, conn.cursor() as cur:
                cur.execute("SELECT %s::timestamptz - INTERVAL '1 day'", (newest,))
                edge = cur.fetchone()[0]
            compress_ready_chunks(conn, NEW, edge)
        if n:
            rate = copied / max(time.time() - t0, 1)
            left = max(total - copied, 0)
            eta = left / rate / 60 if left and rate > 0 else 0
            log(f"  copied {copied:,} / {total:,} packets"
                + ((f" - about {eta:.0f} min to go" if eta >= 1 else " - less than a minute to go") if left else ""))
    return copied


def swap(conn, log=say):
    """Copies the last packets, checks the copy, and swaps the tables in one transaction.
    Returns (ok, old fingerprint, new fingerprint). On a mismatch nothing is swapped."""
    with conn, conn.cursor() as cur:
        cur.execute(f"LOCK TABLE {RAW} IN SHARE ROW EXCLUSIVE MODE")     # no new packets during the swap
        last = (progress(cur) or (0,))[0] or 0
        cur.execute(f"INSERT INTO {NEW} ({COLS}) SELECT {SELECT_COLS} FROM {RAW} WHERE id > %s ORDER BY id", (last,))
        log("Checking the copy (counting every packet in both tables; can take a few minutes)...")
        a, b = fingerprint(cur, RAW), fingerprint(cur, NEW)
        if a != b:
            conn.rollback()
            return False, a, b
        seq = one(cur, "SELECT pg_get_serial_sequence(%s, 'id')", (RAW,))
        cur.execute(f"ALTER TABLE {RAW} RENAME TO {OLD}")
        cur.execute(f"ALTER TABLE {NEW} RENAME TO {RAW}")
        cur.execute(f"ALTER SEQUENCE {seq} OWNED BY {RAW}.id")          # the id counter now belongs to the new table
        for old_name, new_name in (("f1_telemetry_raw_pkey", "f1_telemetry_raw_old_pkey"),
                                   ("f1_telemetry_raw_run_idx", "f1_telemetry_raw_old_run_idx"),
                                   ("f1_telemetry_raw_session_idx", "f1_telemetry_raw_old_session_idx"),
                                   ("f1_telemetry_raw_legacy_idx", "f1_telemetry_raw_old_legacy_idx"),
                                   (f"{NEW}_run_idx", "f1_telemetry_raw_run_idx")):
            if exists(cur, old_name):
                cur.execute(f"ALTER INDEX {old_name} RENAME TO {new_name}")
        if one(cur, "SELECT 1 FROM pg_roles WHERE rolname = 'f1_reader'"):
            cur.execute(f"GRANT SELECT ON {RAW} TO f1_reader")             # the other group keeps read access
        cur.execute("UPDATE f1_upgrade_progress SET info = info || '{\"swapped\": true}' WHERE step = 'compress_raw'")
    return True, a, b


def finish_compression(conn, log=say):
    """Policy: from now on every week is compressed automatically when it is 7 days old. Old weeks: now."""
    with conn, conn.cursor() as cur:
        cur.execute(f"SELECT add_compression_policy('{RAW}', INTERVAL '{COMPRESS_AFTER}', if_not_exists => true)")
        cur.execute(f"SELECT now() - INTERVAL '{COMPRESS_AFTER}'")
        edge = cur.fetchone()[0]
    n = compress_ready_chunks(conn, RAW, edge)
    if n:
        log(f"Compressed {n} more week(s).")
    vacuum(conn, RAW)


def vacuum(conn, table):
    """Gives the space of rows that moved into compressed form back to the database (and updates statistics)."""
    old_autocommit = conn.autocommit
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f"VACUUM ANALYZE {table}")
    finally:
        conn.autocommit = old_autocommit


def cancel(conn, log=say):
    """Stops the upgrade: removes only the half-made COPY (f1_telemetry_raw_new). The real table is untouched."""
    with conn, conn.cursor() as cur:
        if exists(cur, NEW):
            cur.execute(f"DROP TABLE {NEW}")
            log("The unfinished copy was removed. f1_telemetry_raw (all your data) was never changed.")
        else:
            log("There is no unfinished copy.")
        cur.execute("DELETE FROM f1_upgrade_progress WHERE step = 'compress_raw'")


def drop_old(conn, log=say):
    """Removes f1_telemetry_raw_old, but only after checking again that every packet is in the new table."""
    with conn, conn.cursor() as cur:
        if not exists(cur, OLD):
            log("There is no old copy (f1_telemetry_raw_old) to remove.")
            return False
        top = one(cur, f"SELECT max(id) FROM {OLD}")
        a, b = fingerprint(cur, OLD), fingerprint(cur, RAW, max_id=top)
        if a != b:
            log(f"NOT removed: the copies differ (old {a}, new {b}). Nothing was changed.")
            return False
        cur.execute(f"DROP TABLE {OLD}")
        cur.execute("DELETE FROM f1_upgrade_progress WHERE step = 'compress_raw'")
    log(f"Old copy removed: all {a[0]:,} packets are in the compressed table.")
    return True


# ----------------------------------------------------------------- 2. index cleanup without TimescaleDB
def index_cleanup(conn, log=say):
    """Small index for old rows without run_id instead of the big one on session_uid.
    CONCURRENTLY: the relay can keep writing while this runs."""
    old_autocommit = conn.autocommit
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            log("Building a small index for the old rows without run_id (can take a while on a big table)...")
            cur.execute(f"""CREATE INDEX CONCURRENTLY IF NOT EXISTS f1_telemetry_raw_legacy_idx
                            ON {RAW} (session_uid, packet_id, frame) WHERE run_id IS NULL""")
            before = one(cur, "SELECT pg_relation_size('f1_telemetry_raw_session_idx')") \
                if exists(cur, "f1_telemetry_raw_session_idx") else 0
            cur.execute("DROP INDEX CONCURRENTLY IF EXISTS f1_telemetry_raw_session_idx")
            after = one(cur, "SELECT pg_relation_size('f1_telemetry_raw_legacy_idx')")
    finally:
        conn.autocommit = old_autocommit
    log(f"Index cleanup done: {(before - after) / GB:.2f} GB freed.")
    return before - after


# ----------------------------------------------------------------- 3. tables of the earlier collector
def compress_f22(conn, tables, log=say):
    for name in tables:
        try:
            _compress_one(conn, name, log)
        except Exception as e:
            conn.rollback()
            log(f"  {name}: could not be compressed ({str(e).strip()[:160]}); left as it was")


def _compress_one(conn, name, log):
    with conn, conn.cursor() as cur:
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s", (name,))
        cols = {r[0] for r in cur.fetchall()}
        seg = ", ".join(c for c in ("session_uid", "car_idx") if c in cols)
        opts = "timescaledb.compress" + (f", timescaledb.compress_segmentby = '{seg}'" if seg else "")
        cur.execute(f"ALTER TABLE {name} SET ({opts})")
        cur.execute(f"SELECT add_compression_policy('{name}', INTERVAL '{COMPRESS_AFTER}', if_not_exists => true)")
        cur.execute(f"SELECT now() - INTERVAL '{COMPRESS_AFTER}'")
        edge = cur.fetchone()[0]
    n = compress_ready_chunks(conn, name, edge)
    vacuum(conn, name)
    log(f"  {name}: compression on, {n} chunk(s) compressed")


# ----------------------------------------------------------------- the program
def ask(question, default="n"):
    try:
        a = input(f"{question} [{'Y/n' if default == 'y' else 'y/N'}]: ").strip().lower()
    except EOFError:
        a = ""
    return (a or default).startswith(("y", "j"))


def connect():
    """Database login from Key Vault, firewall rule, start the server if it is off. Returns (conn, started)."""
    import os
    import psycopg2
    if os.environ.get("DATABASE_URL"):             # local tests
        return psycopg2.connect(os.environ["DATABASE_URL"]), None
    import azure_firewall
    import secrets_store
    from export_csv import database_on
    cfg = azure_firewall.read_cfg()
    url, where, err = secrets_store.database_url(cfg)
    if not url:
        sys.exit(f"No database login: {err}")
    say(f"Login read from {where}")
    started = database_on(cfg)
    azure_firewall.ensure(cfg)
    for _ in range(6):
        try:
            return psycopg2.connect(url, connect_timeout=20), started
        except Exception as e:
            say(f"  not reachable yet ({str(e).strip()[:120]}), retrying in 20 s...")
            time.sleep(20)
    sys.exit("Could not connect to the database.")


def storage_limit():
    """Storage size of the Azure server in bytes (from Azure), or DB_STORAGE_GB in config.env, or 64 GB."""
    import os
    if os.environ.get("DATABASE_URL"):
        return int(float(os.environ.get("DB_STORAGE_GB", "64")) * GB)
    import azure_firewall
    import secrets_store
    cfg = azure_firewall.read_cfg()
    try:
        server, rg, sub, _state = azure_firewall.find_server(cfg)
        rc, out, _err = secrets_store.az(["postgres", "flexible-server", "show", "--resource-group", rg, "--name",
                                          server, "--subscription", sub, "--query", "storage.storageSizeGb",
                                          "-o", "tsv"])
        if rc == 0 and out.strip().isdigit():
            return int(out.strip()) * GB
    except Exception:
        pass
    return int(float(cfg.get("DB_STORAGE_GB", "64") or 64) * GB)


def show(p):
    raw = p["raw"] or {"size": 0}
    say(f"Database: {p['db_size'] / GB:.1f} GB in total, {storage.describe(raw)}")
    if p["old_exists"]:
        say("An old uncompressed copy (f1_telemetry_raw_old) is still there.")


def main():
    batch = int(sys.argv[sys.argv.index("--batch") + 1]) if "--batch" in sys.argv else 200_000
    say("=" * 66)
    say(" F1 relay - database upgrade (all data is kept)")
    say("=" * 66)
    conn, started = connect()
    try:
        p = plan(conn)
        show(p)
        if "--drop-old" in sys.argv:
            drop_old(conn)
            return
        if "--cancel" in sys.argv:
            cancel(conn)
            return
        if p["relay_seen_s"] is not None and p["relay_seen_s"] < 120:
            say("\nThe relay seems to be running (it updated its status less than 2 minutes ago).")
            if not ask("Close the relay first. Continue anyway?"):
                return
        raw = p["raw"]
        did = False

        if raw and raw["hypertable"]:
            say("\nThe raw packets are already in the compressed format.")
            finish_compression(conn)
            did = True
        elif raw and p["ts_available"]:
            say("\nTimescaleDB is available: the raw packets can be stored compressed.")
            say("All packets are copied (in steps, it can be stopped and continued), checked, and only then")
            say("swapped. The old table is kept until you say it can go.")
            if ask("Start the compression now?", "y"):
                limit = storage_limit()
                prepare_new_table(conn)
                try:
                    copy_raw(conn, batch, limit_bytes=limit)
                except OutOfSpace as e:
                    say(f"\nPAUSED: the database is {e.args[0] / GB:.1f} GB, close to its storage limit of "
                        f"{limit / GB:.0f} GB.")
                    say("Nothing is lost: the copy stopped safely and f1_telemetry_raw is unchanged.")
                    say("Either make the storage bigger in the Azure portal and run it again (tools.bat),")
                    say("or choose 'Cancel an unfinished compression' in tools.bat to remove the half-made copy.")
                    return
                ok, a, b = swap(conn)
                if not ok:
                    say(f"\nSTOPPED: the copy does not match (old {a}, new {b}). Nothing was swapped,")
                    say("the relay keeps using the old table. Please report this.")
                    return
                say(f"Copy checked: {a[0]:,} packets, {a[1] / GB:.2f} GB of packet data, identical. Tables swapped.")
                finish_compression(conn)
                with conn, conn.cursor() as cur:
                    info = storage.raw_info(cur)
                say(f"Now: {storage.describe(info)}")
                say("")
                say("The old uncompressed table (f1_telemetry_raw_old) still takes space.")
                try:
                    typed = input("Type YES to remove it now (Enter = keep it for now): ").strip()
                except EOFError:
                    typed = ""
                if typed == "YES":
                    drop_old(conn)
                else:
                    say("Kept. Remove it later with tools.bat > 'Remove the old uncompressed copy'.")
                did = True
        elif raw:
            say("\nTimescaleDB is not switched on for this server, so the packets can't be compressed yet.")
            say("Switch it on once in the Azure portal: the database server > Server parameters:")
            say("  azure.extensions          -> tick TIMESCALEDB")
            say("  shared_preload_libraries  -> tick TIMESCALEDB, Save (the server restarts)")
            say("Then run the compression again (tools.bat).")
            if p["session_idx"] and ask("Meanwhile, clean up the big session_uid index (saves space)?", "y"):
                index_cleanup(conn)
                did = True

        todo = [name for name, compressed in p["f22"] if not compressed]
        if todo:
            say(f"\nTables of the earlier collector without compression: {', '.join(todo)}")
            if ask("Compress them too (weeks older than 7 days)?", "y"):
                compress_f22(conn, todo)
                did = True

        p = plan(conn)
        say("")
        show(p)
        say("Done." if did else "Nothing changed.")
    finally:
        conn.close()
        if started:                       # it was off before: turn it off again (saves costs)
            import secrets_store
            server, rg, sub = started
            say(f"Turning database {server} off again (it was off before).")
            secrets_store.az(["postgres", "flexible-server", "stop", "--resource-group", rg, "--name", server,
                              "--subscription", sub, "--no-wait"], timeout=120)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("\nStopped. Run the compression again (tools.bat) to continue where it was.")

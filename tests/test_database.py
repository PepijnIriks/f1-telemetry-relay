"""Database tests. They only run when TEST_DATABASE_URL points to an EMPTY test database (never Azure):
    set TEST_DATABASE_URL=postgresql://postgres@localhost:5432/f1test
They write packets, read them back, and do the whole compression upgrade (when TimescaleDB is there)."""
import os

import pytest

URL = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL or "azure" in URL.lower(),
                                reason="set TEST_DATABASE_URL to a local, empty test database")


@pytest.fixture()
def conn():
    import psycopg2
    c = psycopg2.connect(URL)
    with c, c.cursor() as cur:
        for t in ("f1_telemetry_raw", "f1_telemetry_raw_new", "f1_telemetry_raw_old", "f1_sessions", "f1_laps",
                  "f1_corners", "f1_lap_traces", "f1_upgrade_progress"):
            cur.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
        from schema import DDL
        cur.execute(DDL)
    yield c
    c.close()


def store(conn, packets, run_id="run-1", driver="Pepijn", start=1_700_000_000.0):
    """Writes packets the same way the relay does (db.write_batch), one second apart per 100 packets."""
    import db
    import spool
    b = spool.empty_batch()
    for i, p in enumerate(packets):
        b["pkts"].append((start + i / 100, driver, "uid-1", p[5], i, i / 100, 0, p, run_id))   # p[5] = packet id
    with conn, conn.cursor() as cur:
        db.write_batch(cur, b)


def test_packets_are_stored_exactly(conn, packets):
    store(conn, packets[:2000])
    with conn, conn.cursor() as cur:
        cur.execute("SELECT payload FROM f1_telemetry_raw ORDER BY id")
        assert [bytes(r[0]) for r in cur.fetchall()] == packets[:2000]


def test_compression_upgrade_keeps_every_packet(conn, packets):
    import db_upgrade
    import storage
    with conn, conn.cursor() as cur:
        if not storage.timescale_available(cur):
            pytest.skip("TimescaleDB is not available on this test server")
    # two "weeks" of data far in the past, so both weeks get compressed
    store(conn, packets[:3000], run_id="run-a", start=1_600_000_000.0)
    store(conn, packets[3000:6000], run_id="run-b", start=1_600_000_000.0 + 14 * 86400)
    with conn, conn.cursor() as cur:
        cur.execute("INSERT INTO f1_telemetry_raw (received_at, session_uid, packet_id, payload) "
                    "VALUES (to_timestamp(1600000000), 'old-session', 6, '\\x01')")      # an old row without run_id
        before = db_upgrade.fingerprint(cur, "f1_telemetry_raw")

    db_upgrade.prepare_new_table(conn)
    db_upgrade.copy_raw(conn, batch=1000, log=lambda *_: None)
    ok, a, b = db_upgrade.swap(conn, log=lambda *_: None)
    assert ok and a == b == before
    db_upgrade.finish_compression(conn, log=lambda *_: None)

    with conn, conn.cursor() as cur:
        info = storage.raw_info(cur)
        assert info["hypertable"] and info["chunks_compressed"] >= 2
        cur.execute("SELECT payload FROM f1_telemetry_raw WHERE run_id = 'run-a' ORDER BY id")
        assert [bytes(r[0]) for r in cur.fetchall()] == packets[:3000]               # read back exactly
        cur.execute("SELECT run_id FROM f1_telemetry_raw WHERE session_uid = 'old-session'")
        assert cur.fetchone()[0] == "old-session"                                     # old row got its run_id
    store(conn, packets[6000:6100], run_id="run-c", start=1_600_000_000.0 + 28 * 86400)   # relay keeps writing
    assert db_upgrade.drop_old(conn, log=lambda *_: None)
    with conn, conn.cursor() as cur:
        assert db_upgrade.fingerprint(cur, "f1_telemetry_raw")[0] == before[0] + 100
        assert not db_upgrade.exists(cur, "f1_telemetry_raw_old")


def test_cancel_never_touches_the_real_table(conn, packets):
    import db_upgrade
    import storage
    with conn, conn.cursor() as cur:
        if not storage.timescale_available(cur):
            pytest.skip("TimescaleDB is not available on this test server")
    store(conn, packets[:1500])
    with conn, conn.cursor() as cur:
        before = db_upgrade.fingerprint(cur, "f1_telemetry_raw")
    db_upgrade.prepare_new_table(conn)
    db_upgrade.copy_raw(conn, batch=500, log=lambda *_: None)
    db_upgrade.cancel(conn, log=lambda *_: None)
    with conn, conn.cursor() as cur:
        assert db_upgrade.fingerprint(cur, "f1_telemetry_raw") == before
        assert not db_upgrade.exists(cur, "f1_telemetry_raw_new")

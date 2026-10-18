"""
How much space the raw packets take, and whether they are stored compressed (TimescaleDB).
Used by the relay (storage line every 10 minutes) and by the database compression (db_upgrade.py).
"""
RAW = "f1_telemetry_raw"


def timescale_installed(cur):
    """True when the TimescaleDB extension is active in this database."""
    cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
    return cur.fetchone() is not None


def timescale_available(cur):
    """True when TimescaleDB is loaded on the server (on Azure: allowed in the server parameters
    azure.extensions and shared_preload_libraries), so the extension can be used in this database."""
    cur.execute("SELECT current_setting('shared_preload_libraries', true)")
    loaded = "timescaledb" in (cur.fetchone()[0] or "")
    cur.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb'")
    return loaded and cur.fetchone() is not None


def is_hypertable(cur, table=RAW):
    if not timescale_installed(cur):
        return False
    cur.execute("SELECT 1 FROM timescaledb_information.hypertables WHERE hypertable_name = %s", (table,))
    return cur.fetchone() is not None


def raw_info(cur, table=RAW):
    """{'size': bytes on disk now, 'hypertable': bool, 'compressed': bool (compression switched on),
        'before': bytes of the compressed chunks before compression, 'after': same chunks after,
        'chunks': number of chunks, 'chunks_compressed': how many of them are compressed}"""
    info = {"size": 0, "hypertable": False, "compressed": False, "before": 0, "after": 0,
            "chunks": 0, "chunks_compressed": 0}
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (table,))
    if not cur.fetchone()[0]:
        return info
    if not is_hypertable(cur, table):
        cur.execute("SELECT pg_total_relation_size(%s)", (table,))
        info["size"] = cur.fetchone()[0]
        return info
    info["hypertable"] = True
    cur.execute("SELECT hypertable_size(%s)", (table,))
    info["size"] = cur.fetchone()[0] or 0
    cur.execute("SELECT compression_enabled FROM timescaledb_information.hypertables WHERE hypertable_name = %s",
                (table,))
    info["compressed"] = bool(cur.fetchone()[0])
    cur.execute("SELECT count(*), count(*) FILTER (WHERE is_compressed) FROM timescaledb_information.chunks "
                "WHERE hypertable_name = %s", (table,))
    info["chunks"], info["chunks_compressed"] = cur.fetchone()
    if info["compressed"]:
        cur.execute("SELECT coalesce(sum(before_compression_total_bytes), 0), "
                    "coalesce(sum(after_compression_total_bytes), 0) FROM chunk_compression_stats(%s)", (table,))
        info["before"], info["after"] = (int(x) for x in cur.fetchone())
    return info


def describe(info):
    """One line for the black window, e.g. 'raw packets 3.1 GB, compressed 4.2x (saves 9.8 GB)'."""
    gb = 1024 ** 3
    text = f"raw packets {info['size'] / gb:.1f} GB"
    if info["before"] and info["after"]:
        text += (f", compressed {info['before'] / info['after']:.1f}x "
                 f"(saves {(info['before'] - info['after']) / gb:.1f} GB)")
    elif info["hypertable"] and info["compressed"]:
        text += ", compression on (data is compressed when it is 7 days old)"
    return text

"""Database tables (all start with f1_, so they never clash with other tables)."""

DDL = """
CREATE TABLE IF NOT EXISTS f1_telemetry_raw (
    id BIGSERIAL PRIMARY KEY,
    received_at TIMESTAMPTZ NOT NULL,
    driver TEXT,
    session_uid TEXT,
    packet_id SMALLINT,
    frame BIGINT,
    session_time REAL,
    player_idx SMALLINT,
    payload BYTEA
);
-- (Older versions also made an index on session_uid here. It is no longer needed: packets are found by run_id.
--  The compression upgrade (tools.bat) replaces it by a small index for only the old rows without run_id, or removes it entirely
--  when the table is moved to the compressed format.)
CREATE TABLE IF NOT EXISTS f1_sessions (
    session_uid TEXT,
    driver TEXT,
    track_id SMALLINT,
    session_type SMALLINT,
    started_at TIMESTAMPTZ,
    last_seen_at TIMESTAMPTZ,
    packets BIGINT
);
CREATE TABLE IF NOT EXISTS f1_laps (
    session_uid TEXT,
    lap_num SMALLINT,
    lap_time_ms INTEGER,
    s1_ms INTEGER,
    s2_ms INTEGER,
    s3_ms INTEGER,
    invalid BOOLEAN
);
-- One "run" per driver: a game session can contain several runs (one per driver change).
-- Rows from before runs existed get run_id = session_uid.
ALTER TABLE f1_telemetry_raw ADD COLUMN IF NOT EXISTS run_id TEXT;
CREATE INDEX IF NOT EXISTS f1_telemetry_raw_run_idx ON f1_telemetry_raw (run_id, packet_id, frame);
ALTER TABLE f1_sessions ADD COLUMN IF NOT EXISTS run_id TEXT;
ALTER TABLE f1_sessions ADD COLUMN IF NOT EXISTS track_length INTEGER;
UPDATE f1_sessions SET run_id = session_uid WHERE run_id IS NULL;
ALTER TABLE f1_sessions DROP CONSTRAINT IF EXISTS f1_sessions_pkey;
CREATE UNIQUE INDEX IF NOT EXISTS f1_sessions_run_idx ON f1_sessions (run_id);
ALTER TABLE f1_laps ADD COLUMN IF NOT EXISTS run_id TEXT;
UPDATE f1_laps SET run_id = session_uid WHERE run_id IS NULL;
ALTER TABLE f1_laps DROP CONSTRAINT IF EXISTS f1_laps_pkey;
CREATE UNIQUE INDEX IF NOT EXISTS f1_laps_run_idx ON f1_laps (run_id, lap_num);
-- Per lap and per official F1 corner (distances in metres, speeds in km/h, times in ms)
CREATE TABLE IF NOT EXISTS f1_corners (
    run_id TEXT,
    lap_num SMALLINT,
    corner TEXT,
    corner_idx SMALLINT,
    apex_m REAL,
    time_ms INTEGER,
    entry_kmh REAL,
    min_kmh REAL,
    exit_kmh REAL,
    min_at_m REAL,
    brake_before_m REAL,
    throttle_after_m REAL,
    max_brake REAL,
    PRIMARY KEY (run_id, lap_num, corner)
);
-- Per lap: speed / throttle / brake / lap time every step_m metres of lap distance (for speed-trace charts)
CREATE TABLE IF NOT EXISTS f1_lap_traces (
    run_id TEXT,
    lap_num SMALLINT,
    track_id SMALLINT,
    track_length INTEGER,
    step_m REAL,
    speed REAL[],
    throttle REAL[],
    brake REAL[],
    time_ms INTEGER[],
    PRIMARY KEY (run_id, lap_num)
);
"""
TRACE_INSERT = """
    INSERT INTO f1_lap_traces (run_id, lap_num, track_id, track_length, step_m, speed, throttle, brake, time_ms)
    VALUES %s ON CONFLICT (run_id, lap_num) DO UPDATE SET
      track_id = EXCLUDED.track_id, track_length = EXCLUDED.track_length, step_m = EXCLUDED.step_m,
      speed = EXCLUDED.speed, throttle = EXCLUDED.throttle, brake = EXCLUDED.brake, time_ms = EXCLUDED.time_ms"""
CORNER_INSERT = """
    INSERT INTO f1_corners (run_id, lap_num, corner, corner_idx, apex_m, time_ms, entry_kmh, min_kmh,
                            exit_kmh, min_at_m, brake_before_m, throttle_after_m, max_brake) VALUES %s
    ON CONFLICT (run_id, lap_num, corner) DO UPDATE SET
      corner_idx = EXCLUDED.corner_idx, apex_m = EXCLUDED.apex_m, time_ms = EXCLUDED.time_ms,
      entry_kmh = EXCLUDED.entry_kmh, min_kmh = EXCLUDED.min_kmh, exit_kmh = EXCLUDED.exit_kmh,
      min_at_m = EXCLUDED.min_at_m, brake_before_m = EXCLUDED.brake_before_m,
      throttle_after_m = EXCLUDED.throttle_after_m, max_brake = EXCLUDED.max_brake"""

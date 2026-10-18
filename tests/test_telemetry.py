"""The whole 'game data in' path without a database: recorded packets go through the relay's UDP handler
exactly like real game packets, and we check what it hands to the database writer and the website."""
import queue

import pytest

import driver_runs
import shared
import telemetry


@pytest.fixture()
def relay(monkeypatch):
    """A clean relay state for every test."""
    for d in (shared.sess, shared.lapstate, shared.runs, shared.carry_from, shared.cornerbuf):
        d.clear()
    shared.live.clear()
    shared.live.update({"session": {}, "lap": {}})
    monkeypatch.setitem(shared.state["db"], "enabled", True)
    monkeypatch.setitem(shared.state, "driver", "Pepijn")
    monkeypatch.setattr(shared.POWER, "race", lambda *a, **k: None)
    while not shared.db_queue.empty():
        shared.db_queue.get_nowait()
    yield shared
    while not shared.db_queue.empty():
        shared.db_queue.get_nowait()


def feed(pkts):
    proto = telemetry.UdpProtocol()
    for p in pkts:
        proto.datagram_received(p, ("127.0.0.1", 50000))


def drain():
    out = []
    while True:
        try:
            out.append(shared.db_queue.get_nowait())
        except queue.Empty:
            return out


def test_every_packet_goes_to_the_database(relay, packets):
    feed(packets)
    msgs = drain()
    pkts = [m[1] for m in msgs if m[0] == "pkt"]
    assert len(pkts) == len(packets)                       # nothing is dropped
    assert [p[7] for p in pkts] == packets                 # stored exactly as received, in order
    assert {p[1] for p in pkts} == {"Pepijn"}              # with the driver's name


def test_live_view_is_filled(relay, packets):
    feed(packets)
    live = shared.live
    assert live["session"]["track"] == "Barcelona"
    assert 0 <= live["speed"] <= 360
    assert 0 <= live["throttle"] <= 1 and 0 <= live["brake"] <= 1
    assert live["lap"]["num"] >= 1
    assert len(shared.grid_state) == 20                    # the simulator drives a field of 20 cars


def test_laps_are_completed_with_sectors(relay, packets):
    feed(packets)
    laps = [m[1] for m in drain() if m[0] == "lap"]
    assert laps, "at least one lap must be completed in the recording"
    # (session_uid, run_id, lap, lap_ms, s1, s2, s3, invalid)
    first = laps[0]
    assert first[2] == 1
    assert first[3] == 67740                               # lap time of lap 1 in this recording
    assert first[4] + first[5] + first[6] == first[3]      # sectors add up to the lap time
    assert first[7] is False


def test_corners_and_trace_per_completed_lap(relay, packets):
    feed(packets)
    msgs = drain()
    corners = [m[1] for m in msgs if m[0] == "corners"]
    traces = [m[1] for m in msgs if m[0] == "trace"]
    assert corners and traces
    labels = [row[2] for row in corners[0]]
    assert labels == [f"T{i}" for i in range(1, 17)]       # all 16 official corners of Barcelona (2022)
    assert traces[0][4] == 2.0                             # a point every 2 metres


def test_driver_switch_starts_a_new_run(relay, packets):
    half = len(packets) // 2
    feed(packets[:half])
    first_run = set(shared.runs.values())
    driver_runs.switch_driver("Mathijs", 0)
    feed(packets[half:])
    msgs = drain()
    runs = {}
    for m in msgs:
        if m[0] == "pkt":
            runs.setdefault(m[1][8], set()).add(m[1][1])
    assert len(runs) == 2                                  # two runs ...
    assert sorted(d for ds in runs.values() for d in ds) == ["Mathijs", "Pepijn"]   # ... one per driver
    assert first_run <= set(runs)

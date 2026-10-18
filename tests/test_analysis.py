"""Pure calculations: corners, lap traces, consistency, pit advice, replay merging and the corner comparison."""
import corners
import insights
from legacy import merge_samples
from run_tools import summarise_run

BARCELONA = 4


def fake_lap(length=4675, step=5.0):
    """One clean lap: brakes 100 m before every corner, slowest at the apex, full throttle after it."""
    apexes = [a for (_lab, a, *_rest) in corners.corner_layout(BARCELONA, length)]
    out, t = [], 0.0
    d = 0.0
    while d <= length:
        near = min((abs(d - a), a) for a in apexes)
        dist, apex = near
        braking = 0 < apex - d < 100
        speed = 300 - 200 * max(0.0, 1 - dist / 150)
        out.append((d, t, speed, 0.0 if braking else 1.0, 0.8 if braking else 0.0))
        t += step / (speed / 3.6) * 1000
        d += step
    return out


def test_corner_layout_scales_to_game_length():
    a = corners.corner_layout(BARCELONA, 4675)
    b = corners.corner_layout(BARCELONA, 4675 * 2)
    assert len(a) == len(b) == 16
    assert abs(b[0][1] - 2 * a[0][1]) < 1e-6               # apex distance scales with the lap length
    for _lab, apex, z0, z1, *_ in a:
        assert z0 <= apex <= z1                            # the apex lies inside its corner zone


def test_corner_stats_on_a_clean_lap():
    stats = corners.corner_stats(fake_lap(), BARCELONA, 4675)
    assert [c["corner"] for c in stats] == [f"T{i}" for i in range(1, 17)]
    for c in stats:
        assert c["time"] > 0
        assert c["min"] <= c["entry"] and c["min"] <= c["exit"]
        assert c["brake"] is not None and 0 < c["brake"] <= 120    # braked shortly before the apex


def test_incomplete_lap_gives_nothing():
    half = [s for s in fake_lap() if s[0] < 2000]
    assert corners.corner_stats(half, BARCELONA, 4675) == []
    assert corners.lap_trace(half, 4675) is None


def test_flashback_is_cleaned():
    lap = fake_lap()
    rewind = lap[:300] + lap[250:]                          # distance jumps back: a flashback
    assert corners.clean_samples(rewind) == corners.clean_samples(lap)


def test_lap_trace_every_two_metres():
    t = corners.lap_trace(fake_lap(), 4675)
    assert t["step"] == 2.0
    assert len(t["speed"]) == 4675 // 2 + 1
    assert t["time"] == sorted(t["time"])                   # time only goes up during a lap


def test_consistency_score():
    assert insights.consistency([90000, 90000]) is None    # fewer than 3 laps
    score, spread, used = insights.consistency([90000, 90000, 90000])
    assert (score, spread, used) == (100, 0, 3)
    score, _s, used = insights.consistency([90000, 90900, 89100, 120000])   # spin lap is left out
    assert used == 3 and 80 <= score < 100


def test_pit_advice():
    laps = [{"lap": i, "time": 90000, "invalid": False, "wear": [10 * i] * 4} for i in range(1, 8)]
    assert insights.pit_advice(laps).startswith("Pit window from lap 6")
    fresh = [{"lap": i, "time": 90000, "invalid": False, "wear": [5] * 4} for i in range(1, 4)]
    assert insights.pit_advice(fresh).startswith("No pit stop needed")


def test_merge_samples_takes_latest_telemetry_before_each_lap_sample():
    lap_rows = [(1, 1, 10.0, 100, False), (3, 1, 20.0, 200, False), (5, 1, 30.0, 300, True)]
    tel_rows = [(0, 100, 1.0, 0.0), (2, 150, 0.5, 0.2), (4, 90, 0.0, 1.0)]
    out = merge_samples(lap_rows, tel_rows)
    assert out == {1: [(10.0, 100, 100, 1.0, 0.0), (20.0, 200, 150, 0.5, 0.2)]}   # pit sample left out


def test_summarise_run_best_lap_and_best_per_corner():
    # (lap, corner, idx, apex, time, entry, min, exit, brake, gas, lap_ms, invalid)
    rows = [(1, "T1", 0, 100, 3000, 250, 90, 200, 80, 40, 80000, False),
            (1, "T2", 1, 500, 4000, 250, 90, 200, 80, 40, 80000, False),
            (2, "T1", 0, 100, 2900, 250, 90, 200, 80, 40, 81000, False),
            (2, "T2", 1, 500, 4200, 250, 90, 200, 80, 40, 81000, False),
            (3, "T1", 0, 100, 2000, 250, 90, 200, 80, 40, 70000, True)]   # invalid lap never counts
    best = summarise_run(rows, "best")
    assert best["lap"] == 1 and best["lap_time"] == 80000 and best["corner_total"] == 7000
    ideal = summarise_run(rows, "ideal")
    assert ideal["corners"]["T1"]["time"] == 2900 and ideal["corners"]["T2"]["time"] == 4000
    assert ideal["corner_total"] == 6900

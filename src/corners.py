"""
Official F1 corners: per lap the corner time, entry/minimum/exit speed, braking point and throttle point,
plus the lap trace (speed/throttle/brake every 2 m) used for the speed lines in the corner analysis.
"""
import bisect
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ----------------------------------------------------------------- Official F1 corners
CORNERS_FILE = ROOT / "config" / "corners.json"
try:
    CORNERS = {int(k): v for k, v in json.loads(CORNERS_FILE.read_text(encoding="utf-8")).items()
               if not k.startswith("_")}
except Exception as e:
    print(f"[CORNERS] config/corners.json could not be read ({e}); no corner analysis")
    CORNERS = {}
BEFORE_M, AFTER_M, BRAKE_SEARCH_M = 150.0, 100.0, 400.0
VIEW_BEFORE_M, VIEW_AFTER_M = 280.0, 180.0     # part of the track shown in the corner analysis
TRACE_STEP = 2.0                               # the lap trace is stored every 2 metres


def corner_layout(track_id, game_len):
    """Corner zones for a track, scaled from the real lap length to the game's lap length.
    Returns [(label, apex, zone_start, zone_end, brake_search_start, view_start, view_end)] in metres of
    lap distance. zone = what the corner time is measured over; view = the wider part shown in the analysis."""
    d = CORNERS.get(track_id)
    if not d:
        return []
    real_len = float(d.get("length") or 0) or float(game_len or 1)
    L = float(game_len or real_len)
    k = L / real_len
    pts = sorted(((lab, (dist + float(d.get("offset", 0))) * k) for lab, dist in d["corners"]), key=lambda c: c[1])
    out = []
    for i, (lab, a) in enumerate(pts):
        prev_mid = (pts[i - 1][1] + a) / 2 if i else 0.0
        next_mid = (a + pts[i + 1][1]) / 2 if i + 1 < len(pts) else L
        start = max(a - BEFORE_M * k, prev_mid, 0.0)
        end = min(a + AFTER_M * k, next_mid, L)
        prev_apex = pts[i - 1][1] if i else 0.0
        next_apex = pts[i + 1][1] if i + 1 < len(pts) else L
        out.append((lab, a, start, end, max(a - BRAKE_SEARCH_M * k, prev_mid, 0.0),
                    max(a - VIEW_BEFORE_M * k, prev_apex, 0.0), min(a + VIEW_AFTER_M * k, next_apex, L)))
    return out


def clean_samples(samples):
    """Keep one increasing run of lap distance (a flashback rewinds the distance: drop what was undone)."""
    out = []
    for s in samples:
        if s[0] < 0:
            continue
        while out and out[-1][0] >= s[0]:
            out.pop()
        out.append(s)
    return out


def corner_stats(samples, track_id, game_len):
    """samples: [(lap_distance_m, lap_time_ms, speed_kmh, throttle, brake)] of ONE lap.
    Returns one dict per official corner, or [] if the lap is not complete enough."""
    layout = corner_layout(track_id, game_len)
    s = clean_samples(samples)
    if not layout or len(s) < 50:
        return []
    L = float(game_len or 0) or s[-1][0]
    if s[0][0] > 0.05 * L or s[-1][0] < 0.95 * L:
        return []                       # relay started mid-lap or the lap was cut off
    dist = [x[0] for x in s]

    def at(d, col):
        i = bisect.bisect_left(dist, d)
        if i <= 0:
            return s[0][col]
        if i >= len(s):
            return s[-1][col]
        a, b = s[i - 1], s[i]
        f = (d - a[0]) / ((b[0] - a[0]) or 1)
        return a[col] + (b[col] - a[col]) * f

    out = []
    for idx, (lab, apex, z0, z1, b0, _v0, _v1) in enumerate(layout):
        i0, i1 = bisect.bisect_left(dist, z0), bisect.bisect_right(dist, z1)
        zone = s[i0:i1]
        if len(zone) < 2:
            continue
        low = min(zone, key=lambda x: x[2])
        bi = bisect.bisect_left(dist, b0)
        brake_at = next((x[0] for x in s[bi:bisect.bisect_right(dist, low[0])] if x[4] > 0.1), None)
        gas_at = next((x[0] for x in s[bisect.bisect_left(dist, low[0]):i1] if x[3] >= 0.95), None)
        out.append({
            "corner": lab, "idx": idx, "apex": round(apex, 1),
            "time": int(round(at(z1, 1) - at(z0, 1))),
            "entry": round(at(z0, 2), 1), "min": round(low[2], 1), "exit": round(at(z1, 2), 1),
            "min_at": round(low[0] - apex, 1),
            "brake": round(apex - brake_at, 1) if brake_at is not None else None,
            "gas": round(gas_at - apex, 1) if gas_at is not None else None,
            "max_brake": round(max(x[4] for x in s[bi:i1]), 2) if i1 > bi else 0.0,
        })
    return out


def lap_trace(samples, game_len):
    """Resamples one lap to a point every TRACE_STEP metres: speed, throttle, brake and lap time.
    Used for the corner analysis (speed lines per driver). None if the lap is not complete."""
    s = clean_samples(samples)
    if len(s) < 50:
        return None
    L = float(game_len or 0) or s[-1][0]
    if s[0][0] > 0.05 * L or s[-1][0] < 0.95 * L:
        return None
    n = int(min(L, s[-1][0]) // TRACE_STEP) + 1
    sp, th, br, tm = [], [], [], []
    j = 0
    for i in range(n):
        d = i * TRACE_STEP
        while j < len(s) - 2 and s[j + 1][0] < d:
            j += 1
        a, b = s[j], s[j + 1]
        f = max(0.0, min(1.0, (d - a[0]) / ((b[0] - a[0]) or 1)))
        tm.append(int(round(a[1] + (b[1] - a[1]) * f)))
        sp.append(round(a[2] + (b[2] - a[2]) * f, 1))
        th.append(round(a[3] + (b[3] - a[3]) * f, 2))
        br.append(round(a[4] + (b[4] - a[4]) * f, 2))
    return {"step": TRACE_STEP, "speed": sp, "throttle": th, "brake": br, "time": tm}


def trace_row(run_id, lap, track_id, tlen, t):
    return (run_id, lap, track_id, int(tlen or 0), t["step"], t["speed"], t["throttle"], t["brake"], t["time"])


def corner_rows(run_id, lap, stats):
    return [(run_id, lap, c["corner"], c["idx"], c["apex"], c["time"], c["entry"], c["min"], c["exit"],
             c["min_at"], c["brake"], c["gas"], c["max_brake"]) for c in stats]

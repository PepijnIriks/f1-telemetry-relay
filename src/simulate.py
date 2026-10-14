"""
Fake game for testing/demos without F1 22.
Sends real F1 22 packets (Session, LapData for 20 cars, CarTelemetry, CarStatus, CarDamage,
Motion, Participants and Events) to the relay, with a moving car, lap times and sectors.
Every start is a new "race". The car drives Barcelona and brakes for the real (official) corners,
so the corner comparison in the Races tab has something to show.

Usage:    python src/simulate.py                       (to 127.0.0.1:20777)
          python src/simulate.py 192.168.1.20 20777
Options:  --skill 0.95   slower in the corners (1.0 = default, 1.05 = faster)
          --fast 4       run the clock 4x faster (laps of ~20 s instead of ~80 s)
"""
import json
import math
import random
import socket
import struct
import sys
import time
from pathlib import Path

HEADER = struct.Struct("<HBBBBQfIBB")
TEL = struct.Struct("<HfffBbHBBH4H4B4BH4f4B")
LAP = struct.Struct("<IIHHfff14BHHB")
STATUS = struct.Struct("<BBBBBfffHHBBHBBBbfBfffB")
DAMAGE = struct.Struct("<4f26B")

def opt(name, default):
    if name in sys.argv:
        i = sys.argv.index(name)
        val = sys.argv[i + 1]
        del sys.argv[i:i + 2]
        return float(val)
    return default


SKILL = opt("--skill", 1.0)
FAST = opt("--fast", 1.0)
host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
port = int(sys.argv[2]) if len(sys.argv) > 2 else 20777
TRACK_ID, TRACK_LEN = 4, 4675          # Barcelona
try:
    _c = json.loads((Path(__file__).resolve().parent.parent / "config" / "corners.json").read_text(encoding="utf-8"))
    APEXES = [d for _, d in _c[str(TRACK_ID)]["corners"]]
except Exception:
    APEXES = [800, 1150, 1700, 2350, 2900, 3450, 4000]
# minimum speed per corner (km/h), deterministic per corner
VMIN = [90 + (i * 53) % 150 for i in range(len(APEXES))]


def speed_at(d, skill):
    """Speed profile: accelerate out of / brake into each corner (simple physics-like curve)."""
    v = 318.0
    for a, vm in zip(APEXES, VMIN):
        dd = d - a
        if dd < -TRACK_LEN / 2:
            dd += TRACK_LEN
        elif dd > TRACK_LEN / 2:
            dd -= TRACK_LEN
        vmin = vm * skill
        rate = 2.4 if dd < 0 else 1.25 * skill        # braking is quicker than accelerating
        v = min(v, vmin + rate * abs(dd))
    return max(60.0, v)
TOTAL_LAPS = 5
NAMES = ["Pepijn", "Verstappen", "Perez", "Leclerc", "Sainz", "Hamilton", "Russell", "Norris", "Ricciardo", "Alonso",
         "Ocon", "Gasly", "Tsunoda", "Vettel", "Stroll", "Bottas", "Zhou", "Albon", "Latifi", "Magnussen"]
TEAMS = [2, 2, 2, 1, 1, 0, 0, 8, 8, 5, 5, 6, 6, 4, 4, 9, 9, 3, 3, 7]


def header(pid, uid, st, frame):
    return HEADER.pack(2022, 1, 17, 1, pid, uid, st, frame, 0, 255)


def pad(body, size):
    return body + bytes(size - len(body))


def pkt_telemetry(uid, st, frame, phase, speed, accel):
    speed = int(speed)
    throttle = 1.0 if accel > 0.5 or speed >= 315 else 0.0 if accel < -0.5 else 0.35
    brake = min(1.0, -accel / 40) if accel < -0.5 else 0.0
    gear = 1 + min(7, speed // 42)
    rpm = int(7000 + (speed % 42) / 42 * 5500)
    rev = min(100, int(rpm / 13000 * 100))
    steer = 0.6 * math.sin(phase * 5)
    drs = 1 if speed > 290 else 0
    heat = int(min(1000, 300 + speed * 2))
    car = TEL.pack(
        speed, throttle, steer, brake, 0, gear, rpm, drs, rev, 0,
        heat, heat + 20, heat - 30, heat + 5,
        88 + speed // 40, 90 + speed // 45, 84 + speed // 40, 87 + speed // 50,
        96, 98, 94, 97,
        105 + speed // 30,
        23.7, 23.6, 21.5, 21.3,
        0, 0, 0, 0)
    return header(6, uid, st, frame) + car + bytes(TEL.size) * 21 + bytes(3)


def pkt_status(uid, st, frame, progress):
    fuel = max(0.5, 12.0 - progress * 2.3)
    car = STATUS.pack(0, 0, 1, 54, 0, fuel, 110.0, fuel / 1.6, 13000, 4000, 8, 1, 0, 16, 16,
                      min(40, int(progress)), 0, 4_000_000 * (0.2 + 0.6 * abs(math.sin(progress))), 2, 0.0, 0.0, 0.0, 0)
    return header(7, uid, st, frame) + car + bytes(STATUS.size) * 21


def pkt_damage(uid, st, frame, progress):
    p = progress
    hit = 35 if p > 1.6 else 0                      # after ~1.6 laps a hit on the front left wing
    wear = lambda k: min(100, int(k * p))
    car = DAMAGE.pack(
        14 + p * 3, 13 + p * 3, 15 + p * 3.2, 12 + p * 3.2,     # tyre wear
        0, 0, 0, 0,                                              # tyre damage
        wear(2), wear(2), wear(3), wear(3),                      # brake damage
        hit, min(100, hit // 4), 0, 6 if p > 2.5 else 0, 0, 3 if p > 2 else 0,   # wings, floor, diffuser, sidepod
        1 if p > 4 else 0, 0,                                    # DRS/ERS fault
        wear(1), wear(2), wear(3), wear(2), wear(1), wear(4), wear(2), wear(1),  # gearbox, engine, MGU-H, ES, CE, ICE, MGU-K, TC
        0, 0)                                                    # engine blown / seized
    return pad(header(10, uid, st, frame) + car + bytes(DAMAGE.size) * 21, 948)


def pkt_motion(uid, st, frame, phase):
    x = 650 * math.cos(phase) + 140 * math.cos(3 * phase)
    z = 380 * math.sin(phase) + 110 * math.sin(2 * phase)
    car = struct.pack("<3f", x, 0.0, z) + bytes(48)
    return pad(header(0, uid, st, frame) + car, 1464)


def pkt_lap(uid, st, frame, lap_num, cur_ms, last_ms, s1, s2, sector, total_m, lap_dist):
    out = header(2, uid, st, frame)
    for i in range(20):
        if i == 0:
            out += LAP.pack(last_ms, cur_ms, s1, s2, lap_dist, total_m, 0.0,
                            1, lap_num, 0, 0, sector, 0, 0, 0, 0, 0, 1, 4, 2, 0, 0, 0, 0)
        else:
            ai_last = last_ms + 400 + i * 130 if last_ms else 0
            out += LAP.pack(ai_last, cur_ms, 0, 0, 0.0, total_m - i * 12, 0.0,
                            i + 1, lap_num, 1 if i == 7 and sector == 1 else 0, 0, sector, 0, 0, 0, 0, 0, i + 1, 4, 2, 0, 0, 0, 0)
    return out + bytes(LAP.size) * 2 + bytes(2)


def pkt_session(uid, st, frame):
    body = struct.pack("<BbbBHBb", 0, 33, 26, TOTAL_LAPS, TRACK_LEN, 10, TRACK_ID)
    return pad(header(1, uid, st, frame) + body, 632)


def pkt_participants(uid, st, frame):
    out = header(4, uid, st, frame) + bytes([20])
    for i in range(22):
        if i < 20:
            name = NAMES[i].encode()[:47].ljust(48, b"\0")
            out += bytes([0 if i == 0 else 1, i, 255, TEAMS[i], 0, i + 1, 0]) + name + bytes([1 if i == 0 else 0])
        else:
            out += bytes(56)
    return out


def pkt_event(uid, st, frame, code, payload=b""):
    return pad(header(3, uid, st, frame) + code.encode() + payload, 40)


FILLERS = [(11, 1155, 20), (10, None, 10), (4, None, 1), (1, None, 2)]

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
send = lambda b: sock.sendto(b, (host, port))
uid = int(time.time() * 1000) % (2 ** 62)
start = last_tick = time.time()
sim_t = 0.0                      # simulated session clock (runs FAST times faster than real time)
frame = 0
lap_num, lap_start, last_ms, best_ms = 1, 0.0, 0, 10 ** 9
dist, s1_ms, s2_ms = 0.0, 0, 0
lap_skill = SKILL * random.uniform(0.985, 1.015)
prev_speed = speed_at(0.0, lap_skill)
last_sent = {pid: 0.0 for pid, _, _ in FILLERS}
send(pkt_event(uid, 0.0, 0, "SSTA"))
print(f"Simulation (race {uid}) sending to {host}:{port}  skill={SKILL} fast={FAST}x - Ctrl+C to stop")
try:
    while True:
        now = time.time()
        dt = (now - last_tick) * FAST
        last_tick = now
        sim_t += dt
        st = sim_t
        speed = speed_at(dist, lap_skill)
        accel = (speed - prev_speed) / max(dt, 1e-6) / 3.6       # m/s^2
        prev_speed = speed
        dist += speed / 3.6 * dt
        cur = int((sim_t - lap_start) * 1000)
        if not s1_ms and dist >= TRACK_LEN / 3:
            s1_ms = cur
        if not s2_ms and dist >= TRACK_LEN * 2 / 3:
            s2_ms = cur - s1_ms
        if dist >= TRACK_LEN:
            dist -= TRACK_LEN
            last_ms = cur
            if last_ms < best_ms:
                best_ms = last_ms
                send(pkt_event(uid, st, frame, "FTLP", struct.pack("<Bf", 0, last_ms / 1000)))
            lap_num += 1
            lap_start = sim_t
            cur, s1_ms, s2_ms = 0, 0, 0
            lap_skill = SKILL * random.uniform(0.985, 1.015)
            if lap_num == TOTAL_LAPS + 1:
                send(pkt_event(uid, st, frame, "CHQF"))
                send(pkt_event(uid, st, frame, "SEND"))
        sector = 0 if not s1_ms else 1 if not s2_ms else 2
        frac = dist / TRACK_LEN
        phase = frac * 2 * math.pi
        progress = (lap_num - 1) + frac

        send(pkt_telemetry(uid, st, frame, phase, speed, accel))
        send(pkt_lap(uid, st, frame, lap_num, cur, last_ms, s1_ms, s2_ms, sector,
                     (lap_num - 1) * TRACK_LEN + dist, dist))
        send(pkt_status(uid, st, frame, progress))
        send(pkt_motion(uid, st, frame, phase))
        for pid, size, hz in FILLERS:
            if now - last_sent[pid] >= 1.0 / hz:
                last_sent[pid] = now
                if pid == 1:
                    send(pkt_session(uid, st, frame))
                elif pid == 4:
                    send(pkt_participants(uid, st, frame))
                elif pid == 10:
                    send(pkt_damage(uid, st, frame, progress))
                else:
                    send(pad(header(pid, uid, st, frame), size))
        frame += 1
        time.sleep(1 / 60)
except KeyboardInterrupt:
    print("Stopped.")

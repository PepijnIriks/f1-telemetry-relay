"""
One run per driver: changing the driver name closes the current run (it stays in the database)
and starts a new one, even if the game session simply continues. Also the driver menu in the black window.
"""
import time

from shared import DRIVERS, cornerbuf, carry_from, events, events_dirty, lapstate, live, live_dirty, loop_ref, runs, sess, state


def new_run(uid, now, carry=None):
    """Starts a new run for game session `uid` with the current driver."""
    run_id = f"{uid}-{int(now * 1000)}"
    runs[uid] = run_id
    old = sess.get(carry) if carry else None
    sess[run_id] = {"uid": uid, "first": now, "last": now, "packets": 0,
                    "track": old["track"] if old else -1, "stype": old["stype"] if old else -1,
                    "tlen": old.get("tlen", 0) if old else 0, "driver": state["driver"], "dirty": True}
    if carry and carry in lapstate:
        prev = lapstate[carry]
        # The lap that is in progress during the switch was driven by two people: it never counts.
        mid_lap = live.get("lap", {}).get("cur", 0) > 0
        lapstate[run_id] = {"last_ms": prev["last_ms"], "s1": 0, "s2": 0, "invalid": mid_lap, "laps": []}
    cornerbuf.pop(carry, None)
    return run_id


def switch_driver(name, now):
    old_name = state["driver"]
    if name == old_name:
        return
    state["driver"] = name
    uid = state.get("uid")
    run_id = runs.get(uid)
    meta = sess.get(run_id)
    if meta is None:
        print(f"[DRIVER] {name or '(empty)'} is now driving (a new run starts with the first game data)")
    elif not meta["driver"]:
        meta["driver"] = name          # the run had no name yet: just give it this name
        meta["dirty"] = True
        print(f"[DRIVER] Current run now belongs to {name}")
    else:
        meta["dirty"] = True           # last write of the old run
        laps = len(lapstate.get(run_id, {}).get("laps", []))
        runs.pop(uid, None)            # the new run starts with the next packet of the game
        carry_from[uid] = run_id
        cornerbuf.pop(run_id, None)
        print(f"[DRIVER] {old_name} -> {name or '(empty)'}: run of {old_name} saved "
              f"({laps} lap(s)), a new run for {name or '(empty)'} starts with the next game data")
    events.insert(0, {"t": now, "text": f"Driver: {name or '(empty)'}" + (f" (was {old_name})" if old_name else "")})
    del events[40:]
    events_dirty[0] = True
    live_dirty[0] = True


def console_menu():
    """Change the driver from the black window: type a number (or a new name) and press Enter."""
    while True:
        try:
            line = input()
        except (EOFError, OSError):
            return
        line = line.strip()
        if not line:
            print(f"[DRIVER] Now: {state['driver'] or '(empty)'}.  Choose: {DRIVERS.numbered() or '(no names yet)'}"
                  "  -> type a number or a new name + Enter")
            continue
        name = DRIVERS.resolve(line)
        if loop_ref:
            loop_ref[0].call_soon_threadsafe(switch_driver, name, time.time())

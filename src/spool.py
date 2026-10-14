"""
Safety net so NO data is ever lost.

Everything that is not in Azure yet (database off or starting, no internet, relay closed) is written to
the folder "spool" on this PC. As soon as the database can be reached again, the relay sends those files
to Azure (oldest first) and only removes a file after Azure has confirmed it is stored.
This also works after a restart: files left behind are sent the next time the relay connects.
"""
import os
import pickle
import threading
import time
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent / "spool"
_lock = threading.Lock()


def empty_batch():
    return {"pkts": [], "laps": [], "metas": {}, "corners": [], "traces": []}


def is_empty(b):
    return not (b["pkts"] or b["laps"] or b["metas"] or b["corners"] or b["traces"])


def merge(into, b):
    """Adds batch b to batch `into` (b comes first in time for the lists)."""
    for k in ("pkts", "laps", "corners", "traces"):
        into[k][:0] = b[k]
    for rid, m in b["metas"].items():
        into["metas"].setdefault(rid, m)


def save(b):
    """Writes a batch to disk (atomic: a half-written file never counts). Returns the path."""
    if is_empty(b):
        return None
    with _lock:
        DIR.mkdir(exist_ok=True)
        name = f"{time.time_ns()}-{len(b['pkts'])}.pkl"
        tmp = DIR / (name + ".tmp")
        with open(tmp, "wb") as f:
            pickle.dump(b, f, protocol=pickle.HIGHEST_PROTOCOL)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, DIR / name)
        return DIR / name


def files():
    if not DIR.exists():
        return []
    return sorted(DIR.glob("*.pkl"), key=lambda p: int(p.name.split("-", 1)[0]))


def count():
    """Number of packets waiting in spool files."""
    n = 0
    for p in files():
        try:
            n += int(p.stem.split("-", 1)[1])
        except (IndexError, ValueError):
            pass
    return n


def load(path):
    with open(path, "rb") as f:
        return pickle.load(f)


def done(path):
    try:
        os.remove(path)
    except OSError:
        pass

"""Shared test setup: makes the relay's modules in src/ importable and gives the tests a recorded race."""
import gzip
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
DATA = Path(__file__).resolve().parent / "data"


def recorded_packets():
    """The first 7000 packets of a simulated race in Barcelona (simulate.py, recorded once).
    File format: per packet 2 bytes length + the packet exactly as the game sends it."""
    out = []
    with gzip.open(DATA / "race_barcelona.bin.gz", "rb") as f:
        while True:
            n = f.read(2)
            if not n:
                return out
            out.append(f.read(struct.unpack("<H", n)[0]))


@pytest.fixture(scope="session")
def packets():
    return recorded_packets()

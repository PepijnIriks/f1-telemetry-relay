"""
Live switch for the demo website on Educloud (runs on the f1-web VM, see server/install-web.sh).

Two kinds of game data can come in:
  - REAL live data from the game PC (its relay sends a copy with LIVE_FORWARD=...:20777)  -> port 20777
  - the FAKE game (src/simulate.py) that drives laps when nobody is racing               -> port 20779
Both go to the relay of the demo website (port 20780), but never mixed: as soon as real data comes in,
the fake game is ignored; 10 seconds after the real data stops, the fake game takes over again.

Usage: python src/live_mux.py [real_port] [fake_port] [relay_port]     (defaults 20777 20779 20780)
"""
import selectors
import socket
import sys
import time

REAL_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 20777
FAKE_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 20779
RELAY = ("127.0.0.1", int(sys.argv[3]) if len(sys.argv) > 3 else 20780)
QUIET_AFTER = 10.0          # seconds without real data before the fake game is shown again


def main():
    real = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    real.bind(("0.0.0.0", REAL_PORT))
    fake = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    fake.bind(("127.0.0.1", FAKE_PORT))
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sel = selectors.DefaultSelector()
    sel.register(real, selectors.EVENT_READ, "real")
    sel.register(fake, selectors.EVENT_READ, "fake")
    last_real, showing = 0.0, "fake"
    print(f"[LIVE] real game data on udp {REAL_PORT}, fake game on {FAKE_PORT}, relay on {RELAY[1]}", flush=True)
    while True:
        for key, _ in sel.select(timeout=1):
            data, _addr = key.fileobj.recvfrom(65535)
            now = time.time()
            if key.data == "real":
                last_real = now
                if showing != "real":
                    showing = "real"
                    print("[LIVE] Real game data from the game PC: showing it live", flush=True)
                out.sendto(data, RELAY)
            elif now - last_real > QUIET_AFTER:
                if showing != "fake":
                    showing = "fake"
                    print("[LIVE] No real game data for a while: the fake game drives again", flush=True)
                    out.sendto(b"F1RELAY-DRIVER\x00Demo", RELAY)
                out.sendto(data, RELAY)


if __name__ == "__main__":
    main()

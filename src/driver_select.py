"""
Choose the driver from inside the game, without touching the keyboard or the website.

F1 22 sends a "BUTN" event (in the Event packet) every time a button on the wheel/controller
changes. In the game you can bind up to 12 "UDP Action" buttons (Settings > Controls > edit your
device > UDP Action 1 ... 12). Those buttons do nothing in the game itself, they only end up in
the telemetry, so they are perfect for this.

Default buttons (change them in config/config.env):
    DRIVER_NEXT_BUTTON = UDP1   -> next name in the list
    DRIVER_PREV_BUTTON = UDP2   -> previous name in the list
    DRIVER_CONFIRM_BUTTON =     -> (optional) apply right away

How it works: press "next" until you hear (or see) your name, then wait DRIVER_CONFIRM_SECONDS
(default 3 s) or press the confirm button. Only then the driver switches (= new run), so scrolling
past other names never creates runs for them.

The names come from DRIVERS in config/config.env, plus every name typed on the website while
the relay runs. With ANNOUNCE_DRIVER=1 Windows reads the chosen name out loud (built-in voice).
"""
import subprocess
import sys
import threading

BUTTONS = {
    "CROSS": 0x1, "A": 0x1, "TRIANGLE": 0x2, "Y": 0x2, "CIRCLE": 0x4, "B": 0x4,
    "SQUARE": 0x8, "X": 0x8, "DPAD_LEFT": 0x10, "DPAD_RIGHT": 0x20, "DPAD_UP": 0x40,
    "DPAD_DOWN": 0x80, "OPTIONS": 0x100, "MENU": 0x100, "L1": 0x200, "LB": 0x200,
    "R1": 0x400, "RB": 0x400, "L2": 0x800, "LT": 0x800, "R2": 0x1000, "RT": 0x1000,
    "LS": 0x2000, "RS": 0x4000, "RS_LEFT": 0x8000, "RS_RIGHT": 0x10000,
    "RS_UP": 0x20000, "RS_DOWN": 0x40000, "SPECIAL": 0x80000,
}
for _i in range(12):
    BUTTONS[f"UDP{_i + 1}"] = 0x100000 << _i


def parse_buttons(text):
    """'UDP1' or a combination like 'L1+R1' -> bit mask (0 = not used)."""
    mask = 0
    for part in str(text or "").upper().replace(" ", "").split("+"):
        if not part:
            continue
        if part not in BUTTONS:
            print(f"[DRIVER] Unknown button '{part}' in config.env (use e.g. UDP1, UDP2, L1+R1)")
            return 0
        mask |= BUTTONS[part]
    return mask


def _yes(v):
    return str(v).strip().lower() not in ("0", "false", "no", "off", "")


class Speaker:
    """Reads short texts out loud with the Windows voice (no extra install). Does nothing elsewhere."""

    SCRIPT = ("Add-Type -AssemblyName System.Speech;"
              "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
              "while (($l = [Console]::In.ReadLine()) -ne $null) { $s.SpeakAsyncCancelAll(); [void]$s.SpeakAsync($l) }")

    def __init__(self, enabled):
        self.proc = None
        self.lock = threading.Lock()
        self.enabled = enabled and sys.platform == "win32"

    def say(self, text):
        if not self.enabled:
            return
        # The text goes in through stdin, never through the command line, so a name can't run code.
        line = " ".join(str(text).split())[:80] + "\n"
        with self.lock:
            try:
                if self.proc is None or self.proc.poll() is not None:
                    self.proc = subprocess.Popen(
                        ["powershell", "-NoProfile", "-NonInteractive", "-Command", self.SCRIPT],
                        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.proc.stdin.write(line)
                self.proc.stdin.flush()
            except Exception as e:
                print(f"[DRIVER] Reading the name out loud does not work here ({e}); turned off")
                self.enabled = False


class DriverPicker:
    def __init__(self, cfg, switch, current, note):
        """switch(name): really change the driver.  current(): name now.  note(text): event on the website."""
        self.names = []
        for n in str(cfg.get("DRIVERS", "")).split(","):
            self.add(n)
        self.next_mask = parse_buttons(cfg.get("DRIVER_NEXT_BUTTON", "UDP1"))
        self.prev_mask = parse_buttons(cfg.get("DRIVER_PREV_BUTTON", "UDP2"))
        self.ok_mask = parse_buttons(cfg.get("DRIVER_CONFIRM_BUTTON", ""))
        self.delay = float(cfg.get("DRIVER_CONFIRM_SECONDS", "3") or 3)
        self.speaker = Speaker(_yes(cfg.get("ANNOUNCE_DRIVER", "1")))
        self.switch, self.current, self.note = switch, current, note
        self.buttons = 0
        self.pending = None          # name chosen in the game, not applied yet
        self.apply_at = 0.0

    # ------------------------------------------------------------ list of names
    def add(self, name):
        name = str(name).strip()[:60]
        if name and name.lower() not in (n.lower() for n in self.names):
            self.names.append(name)

    def numbered(self):
        return "  ".join(f"{i + 1}={n}" for i, n in enumerate(self.names))

    def from_input(self, text):
        """Startup prompt: a number from the list or a new name."""
        text = text.strip()
        if text.isdigit() and 1 <= int(text) <= len(self.names):
            return self.names[int(text) - 1]
        self.add(text)
        return text

    # ------------------------------------------------------------ buttons from the game
    def on_buttons(self, status, now):
        prev, self.buttons = self.buttons, status

        def pressed(mask):           # all buttons of the combination held now, and not before
            return mask and (status & mask) == mask and (prev & mask) != mask

        if pressed(self.ok_mask) and self.pending is not None:
            self.apply(now)
        elif pressed(self.next_mask):
            self.step(+1, now)
        elif pressed(self.prev_mask):
            self.step(-1, now)

    def step(self, d, now):
        if not self.names:
            self.note("Driver button pressed, but DRIVERS in config.env is empty")
            self.speaker.say("No drivers in the list")
            print("[DRIVER] Button pressed, but there are no names: fill in DRIVERS= in config/config.env")
            return
        base = self.pending if self.pending is not None else self.current()
        low = [n.lower() for n in self.names]
        i = low.index(base.lower()) if base and base.lower() in low else (-1 if d > 0 else 0)
        self.pending = self.names[(i + d) % len(self.names)]
        self.apply_at = now + self.delay
        self.speaker.say(self.pending)
        self.note(f"Choosing driver in game: {self.pending} (applies in {self.delay:g} s)")
        print(f"[DRIVER] In game: {self.pending}? (applies in {self.delay:g} s, press again for the next name)")

    def tick(self, now):
        """Called a few times per second by the relay."""
        if self.pending is not None and now >= self.apply_at:
            self.apply(now)

    def apply(self, now):
        name, self.pending = self.pending, None
        if name.lower() == (self.current() or "").lower():
            self.speaker.say(f"{name} stays")
            self.note(f"Driver stays {name}")
            return
        self.switch(name)
        self.speaker.say(f"{name} driving")

    def status(self, now):
        return {"names": self.names, "pending": self.pending,
                "left": max(0.0, round(self.apply_at - now, 1)) if self.pending else 0}

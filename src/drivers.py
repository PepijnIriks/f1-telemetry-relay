"""
The list of driver names you can pick from (website, driver menu window on the PC, or the black window).

Names come from DRIVERS in config/config.env plus every name added later on the website or in the
driver menu. Added names are remembered in config/drivers.json, so they are there after a restart.
"""
import json
import threading
from pathlib import Path

FILE = Path(__file__).resolve().parent.parent / "config" / "drivers.json"


class DriverList:
    def __init__(self, cfg):
        self.lock = threading.Lock()
        self.names = []
        for n in str(cfg.get("DRIVERS", "")).split(","):
            self._add(n)
        try:
            for n in json.loads(FILE.read_text(encoding="utf-8")):
                self._add(n)
        except (OSError, ValueError):
            pass

    def _add(self, name):
        name = " ".join(str(name).split())[:60]
        if name and name.lower() not in (n.lower() for n in self.names):
            self.names.append(name)
            return True
        return False

    def _save(self):
        try:
            FILE.write_text(json.dumps(self.names, indent=1, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            print(f"[DRIVER] Could not save the list of names ({e})")

    def add(self, name):
        with self.lock:
            if self._add(name):
                self._save()

    def remove(self, name):
        with self.lock:
            low = str(name).lower()
            before = len(self.names)
            self.names = [n for n in self.names if n.lower() != low]
            if len(self.names) != before:
                self._save()

    def numbered(self):
        return "  ".join(f"{i + 1}={n}" for i, n in enumerate(self.names))

    def resolve(self, text):
        """A number from the list or a (new) name -> name. Empty stays empty."""
        text = " ".join(str(text).split())[:60]
        if text.isdigit() and 1 <= int(text) <= len(self.names):
            return self.names[int(text) - 1]
        if text:
            self.add(text)
        return text

"""
Small "F1 driver" window on the PC to change the driver with one click.

start.bat opens it next to the relay. It talks to the relay (http://localhost:8000), so it shows
the same names as the website. Click a name = that person drives now (new run). Type a new name
and press Enter (or "Add") to add someone. Right-click a name to remove it from the list.
"Always on top" keeps the window above the game (works with the game in windowed / borderless mode).
"""
import json
import sys
import tkinter as tk
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def http_port():
    port = "8000"
    cfg = ROOT / "config" / "config.env"
    if cfg.exists():
        for line in cfg.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("HTTP_PORT="):
                port = line.split("=", 1)[1].strip() or port
    return port


API = f"http://localhost:{http_port()}/api/driver"
BG, PANEL, LINE, TEXT, DIM, ACCENT = "#121214", "#1f1f24", "#3b3b44", "#e7e7ea", "#9a9aa4", "#a35fb3"


def call(body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API, data=data, headers={"Content-Type": "application/json"},
                                 method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=2) as r:
        return json.loads(r.read().decode())


class Menu:
    def __init__(self, root):
        self.root = root
        root.title("F1 driver")
        root.configure(bg=BG)
        root.minsize(240, 120)
        self.top = tk.BooleanVar(value=True)
        root.attributes("-topmost", True)

        self.now = tk.Label(root, text="relay not running", bg=BG, fg=DIM, font=("Segoe UI", 10))
        self.now.pack(fill="x", padx=10, pady=(10, 4))
        self.box = tk.Frame(root, bg=BG)
        self.box.pack(fill="both", expand=True, padx=10)

        row = tk.Frame(root, bg=BG)
        row.pack(fill="x", padx=10, pady=8)
        self.entry = tk.Entry(row, bg=PANEL, fg=TEXT, insertbackground=TEXT, relief="flat",
                              highlightthickness=1, highlightbackground=LINE, font=("Segoe UI", 10))
        self.entry.pack(side="left", fill="x", expand=True, ipady=3)
        self.entry.bind("<Return>", lambda e: self.add())
        tk.Button(row, text="Add", command=self.add, bg=PANEL, fg=TEXT, relief="flat",
                  activebackground=LINE, activeforeground=TEXT).pack(side="left", padx=(6, 0))
        tk.Checkbutton(root, text="Always on top", variable=self.top, command=self.set_top, bg=BG, fg=DIM,
                       selectcolor=PANEL, activebackground=BG, activeforeground=TEXT,
                       font=("Segoe UI", 9)).pack(anchor="w", padx=8, pady=(0, 8))
        self.shown = None
        self.refresh()

    def set_top(self):
        self.root.attributes("-topmost", self.top.get())

    def draw(self, driver, names):
        if self.shown == (driver, tuple(names)):
            return
        self.shown = (driver, tuple(names))
        self.now.config(text=f"Driving now: {driver or '(nobody)'}", fg=TEXT if driver else DIM)
        for w in self.box.winfo_children():
            w.destroy()
        if not names:
            tk.Label(self.box, text="No names yet: add one below", bg=BG, fg=DIM).pack(fill="x")
        for i, n in enumerate(names):
            on = n.lower() == (driver or "").lower()
            b = tk.Button(self.box, text=f"{i + 1}.  {n}", anchor="w", relief="flat", font=("Segoe UI", 12),
                          bg=ACCENT if on else PANEL, fg=TEXT, activebackground=LINE, activeforeground=TEXT,
                          command=lambda n=n: self.choose(n))
            b.bind("<Button-3>", lambda e, n=n: self.remove(n))
            b.pack(fill="x", pady=2, ipady=4)
            if i < 9:
                self.root.bind(str(i + 1), lambda e, n=n: self.choose(n) if e.widget is not self.entry else None)

    def send(self, body):
        try:
            r = call(body)
            self.draw(r.get("driver", ""), r.get("names", []))
        except Exception:
            self.now.config(text="relay not running (start start.bat)", fg=DIM)

    def choose(self, name):
        self.send({"driver": name})

    def add(self):
        name = self.entry.get().strip()
        if name:
            self.entry.delete(0, "end")
            self.send({"driver": name})

    def remove(self, name):
        self.send({"remove": name})

    def refresh(self):
        try:
            r = call()
            self.draw(r.get("driver", ""), r.get("names", []))
        except Exception:
            self.shown = None
            self.now.config(text="relay not running (start start.bat)", fg=DIM)
        self.root.after(1000, self.refresh)


if __name__ == "__main__":
    if "--check" in sys.argv:
        print(API)
        sys.exit(0)
    root = tk.Tk()
    Menu(root)
    root.mainloop()

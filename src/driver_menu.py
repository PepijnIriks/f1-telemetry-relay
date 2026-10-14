"""
Small "F1 driver" window on the PC to change the driver with one click.

start.bat opens it next to the relay. It talks to the relay (http://127.0.0.1:8000), so it shows
the same names as the website. Click a name = that person drives now (new run). Type a new name
and press Enter (or "Add") to add someone. Right-click a name to remove it from the list.
"Always on top" keeps the window above the game (works with the game in windowed / borderless mode).
"""
import json
import queue
import sys
import threading
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


# 127.0.0.1 and not "localhost": on Windows "localhost" first tries IPv6, which the relay does not
# listen on, and that costs about 2 seconds per request (that made the window laggy).
API = f"http://127.0.0.1:{http_port()}/api/driver"
BG, PANEL, LINE, TEXT, DIM, ACCENT = "#121214", "#1f1f24", "#3b3b44", "#e7e7ea", "#9a9aa4", "#a35fb3"


# Never use a proxy for the relay on this same PC. Windows proxy settings (school, VPN, tools) are
# otherwise also applied to 127.0.0.1, and then the window can't reach the relay at all.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API, data=data, headers={"Content-Type": "application/json"},
                                 method="POST" if body is not None else "GET")
    with OPENER.open(req, timeout=3) as r:
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
        self.todo, self.done, self.tick, self.last_error = queue.Queue(), queue.Queue(), 0, ""
        threading.Thread(target=self.worker, daemon=True).start()
        self.todo.put(None)
        self.poll()

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

    # ---- network: never in the window's own thread, so clicking and dragging never stutters
    def worker(self):
        while True:
            body = self.todo.get()
            try:
                r = call(body)
            except Exception as e:
                r = {"error": f"{type(e).__name__}: {e}"[:120]}
            self.done.put(r)

    def poll(self):
        """Every 50 ms: show answers from the relay; every second: ask for the current state."""
        try:
            while True:
                r = self.done.get_nowait()
                if "error" in r:
                    self.shown = None
                    self.now.config(text="relay not reachable (start start.bat)", fg=DIM)
                    if r["error"] != self.last_error:
                        self.last_error = r["error"]
                        print(f"[F1 driver] {API}: {r['error']}", flush=True)
                        self.root.title(f"F1 driver - {r['error'][:60]}")
                elif self.last_error:
                    self.last_error = ""
                    self.root.title("F1 driver")
                    self.draw(r.get("driver", ""), r.get("names", []))
                elif self.todo.empty():                  # an older answer must not undo a newer click
                    self.draw(r.get("driver", ""), r.get("names", []))
        except queue.Empty:
            pass
        self.tick += 1
        if self.tick % 20 == 0 and self.todo.empty():
            self.todo.put(None)
        self.root.after(50, self.poll)

    def choose(self, name):
        names = list(self.shown[1]) if self.shown else []
        if name.lower() not in (n.lower() for n in names):
            names.append(name)
        self.draw(name, names)                           # show it right away, the relay follows
        self.todo.put({"driver": name})

    def add(self):
        name = self.entry.get().strip()
        if name:
            self.entry.delete(0, "end")
            self.choose(name)

    def remove(self, name):
        if self.shown:
            self.draw(self.shown[0], [n for n in self.shown[1] if n.lower() != name.lower()])
        self.todo.put({"remove": name})

if __name__ == "__main__":
    if "--check" in sys.argv:
        print(API)
        sys.exit(0)
    root = tk.Tk()
    Menu(root)
    root.mainloop()

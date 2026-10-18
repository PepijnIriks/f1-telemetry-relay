"""
Where everything is. One place for the folders of the relay and for reading config/config.env.

    config/     settings (config.env) and the official corner list - the same on every computer
    data/       everything THIS computer makes itself: data waiting for Azure (spool), exports,
                download zips, caches, logs. Never on GitHub; safe to leave behind when you move PCs
                (except data/spool while it still has files: that is race data not in Azure yet).

`python src/paths.py` moves files from the places older versions used (spool/, exports/, zips/,
downloads/, firewall.log, config/*cache*.json) into data/ and removes old Python caches (__pycache__).
start.bat does that automatically.
"""
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
CONFIG_FILE = CONFIG_DIR / "config.env"
DATA = ROOT / "data"
SPOOL = DATA / "spool"                      # race data not in Azure yet (sent automatically later)
EXPORTS = DATA / "exports"                  # export_csv.py
DOWNLOADS = DATA / "downloads"              # temporary zips of the download button
CACHE = DATA / "cache"                      # track outlines, list of old races
FIREWALL_LOG = DATA / "firewall.log"


def read_config(defaults=None):
    """config/config.env as a dict (KEY=value lines; # starts a comment)."""
    cfg = dict(defaults or {})
    if CONFIG_FILE.exists():
        for line in CONFIG_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"')
    return cfg


def _move(src, dst):
    """Moves a file or folder; a folder is merged into an existing one. Never overwrites a file."""
    if not src.exists():
        return 0
    if src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        n = sum(_move(child, dst / child.name) for child in list(src.iterdir()))
        try:
            src.rmdir()                     # only when it is empty now
        except OSError:
            pass
        return n
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst = dst.with_name(f"{dst.stem}-old{dst.suffix}")
        if dst.exists():
            return 0                        # leave it where it is rather than overwrite anything
    shutil.move(str(src), str(dst))
    return 1


# old place -> new place (older versions kept these next to the code)
OLD_PLACES = [
    (ROOT / "spool", SPOOL),
    (ROOT / "exports", EXPORTS),
    (ROOT / "downloads", DOWNLOADS),
    (ROOT / "zips", DATA / "zips"),
    (ROOT / "firewall.log", FIREWALL_LOG),
    (CONFIG_DIR / "trackmaps.json", CACHE / "trackmaps.json"),
    (CONFIG_DIR / "old_races_cache.json", CACHE / "old_races_cache.json"),
]


def tidy(say=print):
    """Moves files from the old places into data/ and removes old Python caches (they are made in
    data/pycache now). Returns the number of files moved."""
    for cache in (ROOT / "src" / "__pycache__", ROOT / "tests" / "__pycache__", ROOT / ".pytest_cache"):
        if cache.is_dir():
            shutil.rmtree(cache, ignore_errors=True)
    total = 0
    for src, dst in OLD_PLACES:
        try:
            n = _move(src, dst)
        except OSError as e:
            say(f"[TIDY] Could not move {src.relative_to(ROOT)} ({e}); it stays where it is")
            continue
        if n:
            say(f"[TIDY] {src.relative_to(ROOT)} -> {dst.relative_to(ROOT)} ({n} file(s))")
            total += n
    return total


if __name__ == "__main__":
    tidy()
    sys.exit(0)

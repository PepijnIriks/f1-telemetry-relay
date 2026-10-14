"""
Startup animation for the relay: title, F1 start lights, lights out, car drives off.
Turn it off with SHOW_INTRO=0 in config/config.env or by starting with --no-intro.
"""
import random
import sys
import time

TITLE = r"""
███████╗ ██╗    ██████╗ ███████╗██╗      █████╗ ██╗   ██╗
██╔════╝███║    ██╔══██╗██╔════╝██║     ██╔══██╗╚██╗ ██╔╝
█████╗  ╚██║    ██████╔╝█████╗  ██║     ███████║ ╚████╔╝
██╔══╝   ██║    ██╔══██╗██╔══╝  ██║     ██╔══██║  ╚██╔╝
██║      ██║    ██║  ██║███████╗███████╗██║  ██║   ██║
╚═╝      ╚═╝    ╚═╝  ╚═╝╚══════╝╚══════╝╚═╝  ╚═╝   ╚═╝
""".strip("\n").splitlines()

CAR = [
    "         _____           ",
    r"  _||___/  O  \_________ ",
    r" |_||__(@)_________(@)__\>",
]

ESC = "\x1b["
RESET = ESC + "0m"
GRADIENT = [93, 129, 135, 141, 177, 183]          # purple -> pink (256-colour palette)
OFF, RED, GREEN, DIM = 237, 196, 46, 240
WIDTH = 64


def fg(n):
    return f"{ESC}38;5;{n}m"


def out(s):
    sys.stdout.write(s)
    sys.stdout.flush()


def enable_ansi():
    """Windows consoles need 'virtual terminal processing' switched on for colours."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        return bool(k.SetConsoleMode(h, mode.value | 0x0004))
    except Exception:
        return False


def lights_block(n_on, all_off=False):
    """5 light pods; the bottom two lights of each pod turn red one pod at a time."""
    top = "  " + "  ".join("╭──────╮" for _ in range(5))
    bot = "  " + "  ".join("╰──────╯" for _ in range(5))
    rows = [top]
    for light in range(2):
        for half in ("▄██▄", "▀██▀"):
            cells = []
            for pod in range(5):
                lit = (not all_off) and pod < n_on
                colour = RED if lit else OFF
                cells.append(f"│ {fg(colour)}{half}{RESET} │")
            rows.append("  " + "  ".join(cells))
    rows.append(bot)
    return rows


def redraw(lines, first=False):
    if not first:
        out(f"{ESC}{len(lines)}F")
    for line in lines:
        out(f"{ESC}2K{line}\n")


def play():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if not sys.stdout.isatty() or not enable_ansi():
        return
    out(f"{ESC}?25l")                                # hide cursor
    try:
        out(f"{ESC}2J{ESC}H\n")
        for i, line in enumerate(TITLE):
            out("  " + fg(GRADIENT[i % len(GRADIENT)]) + line + RESET + "\n")
            time.sleep(0.05)
        out(f"\n  {fg(DIM)}F1 22 telemetry  ·  live dashboard  ·  Azure PostgreSQL{RESET}\n\n")
        time.sleep(0.25)

        redraw(lights_block(0), first=True)
        for n in range(1, 6):
            time.sleep(0.6)
            redraw(lights_block(n))
        time.sleep(random.uniform(0.4, 1.4))         # random wait, just like the real thing
        redraw(lights_block(0, all_off=True))

        out(f"\n  {ESC}1m{fg(GREEN)}LIGHTS OUT AND AWAY WE GO!{RESET}\n\n")
        lanes = len(CAR)
        out("\n" * lanes)
        span = WIDTH - len(CAR[0])
        for x in range(0, span + 1, 2):
            trail = fg(DIM) + ("- " * (x // 2))[:x] + RESET
            frame = [trail[:] + fg(GRADIENT[3]) + row + RESET if r == 1 else " " * x + fg(GRADIENT[3]) + row + RESET
                     for r, row in enumerate(CAR)]
            redraw(frame)
            time.sleep(0.018)
        out("\n")
    except KeyboardInterrupt:
        pass
    finally:
        out(f"{RESET}{ESC}?25h")                     # show cursor again


def wanted(cfg, argv):
    if "--no-intro" in argv:
        return False
    return str(cfg.get("SHOW_INTRO", "1")).strip().lower() not in ("0", "false", "no", "off")


if __name__ == "__main__":
    play()

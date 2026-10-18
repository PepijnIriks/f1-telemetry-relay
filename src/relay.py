"""
F1 22 telemetry relay - start here (start.bat runs this file).
- Listens on UDP (default port 20777) for packets from the game                      -> telemetry.py
- Forwards them IMMEDIATELY to the website (http://127.0.0.1:8000) over WebSocket     -> website.py
- Writes to PostgreSQL (Azure) in batches in the background: raw packets,
  an overview of runs and lap times. The database can never slow down the live view.  -> db.py
- Every driver gets their own "run": changing the driver name closes the current run (it is kept
  in the database) and starts a new one, even if the game session simply continues.   -> driver_runs.py
- Per lap it works out the official F1 corners so the Races tab can compare drivers
  corner by corner                                                                      -> corners.py, run_tools.py
Settings and the state shared by all these modules live in shared.py.
"""
import asyncio
import sys
import threading
import time
import webbrowser

from aiohttp import web

import intro
import paths
from db import db_host, db_worker, load_database_url, pending, spool_everything
from driver_runs import console_menu
from shared import CFG, DEMO, DRIVERS, POWER, ROOT, db_queue, loop_ref, state
from telemetry import UdpProtocol
from website import broadcaster, make_app


async def main():
    udp_port = int(CFG["UDP_PORT"])
    http_port = int(CFG["HTTP_PORT"])

    loop = asyncio.get_running_loop()
    loop_ref.append(loop)
    if sys.stdin and sys.stdin.isatty():
        threading.Thread(target=console_menu, daemon=True).start()
    udp_host = CFG.get("UDP_HOST", "127.0.0.1").strip() or "127.0.0.1"
    web_host = CFG.get("WEB_HOST", "127.0.0.1").strip() or "127.0.0.1"
    await loop.create_datagram_endpoint(UdpProtocol, local_addr=(udp_host, udp_port))

    runner = web.AppRunner(make_app())
    await runner.setup()
    await web.TCPSite(runner, web_host, http_port).start()
    asyncio.create_task(broadcaster())

    print("=" * 62)
    print(f" UDP:         listening on {udp_host}:{udp_port}  (in the game: IP 127.0.0.1, port {udp_port})")
    print(f" Website:     http://127.0.0.1:{http_port}   (tabs: Live / Raw / Races)"
          + ("" if web_host == "127.0.0.1" else f"  - also open for other devices on the network ({web_host})"))
    print(f" Config:      {ROOT / 'config' / 'config.env'}")
    print(f" Database:    {('on, server ' + db_host() + ' (login from ' + CFG['DB_SOURCE'] + ')') if CFG['DATABASE_URL'] else 'OFF (' + (CFG['DB_SECRET_ERROR'] or 'not configured') + ')'}")
    print(f" Drivers:     {DRIVERS.numbered() or '(none yet: add names on the website)'}")
    print("              change driver: website, the 'F1 driver' window, or type a number here + Enter")
    print(f" Driver:      {state['driver'] or '(empty)'}")
    if DEMO:
        print(" DEMO MODE:   fake data from the simulator, NOTHING is written to the database")
    print("=" * 62)
    if "--no-browser" not in sys.argv:
        webbrowser.open(f"http://127.0.0.1:{http_port}/")
    await asyncio.Event().wait()


if __name__ == "__main__":
    paths.tidy()                      # files of older versions -> data/ (also data waiting for Azure)
    if intro.wanted(CFG, sys.argv):
        intro.play()
    load_database_url()
    if "--driver" in sys.argv:
        state["driver"] = DRIVERS.resolve(sys.argv[sys.argv.index("--driver") + 1])
    else:
        try:
            if DRIVERS.names:
                print(f"Drivers: {DRIVERS.numbered()}")
                ask = "Who drives first? Number or name (Enter = choose later): "
            else:
                ask = "Driver name (Enter = fill it in later on the web page): "
            state["driver"] = DRIVERS.resolve(input(ask))
        except EOFError:
            pass
    threading.Thread(target=db_worker, daemon=True).start()
    POWER.before_exit = spool_everything
    if CFG["DATABASE_URL"]:
        POWER.start_thread()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Give Azure a few seconds to take what is still waiting, then save the rest on this PC
        end = time.time() + 8
        while state["db"]["connected"] and (len(pending["pkts"]) or db_queue.qsize()) and time.time() < end:
            time.sleep(0.3)
        spool_everything()
        POWER.on_exit(wait_flush=0)
        print(f"\nStopped ({state['total']:,} packets received this session).")

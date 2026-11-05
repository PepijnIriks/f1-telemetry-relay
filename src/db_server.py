"""
The database server on the Educloud VM is always on, so (unlike the Azure server before) nothing has to be
turned on or off and no firewall rule has to be added for this PC.

What is left from the old Azure on/off manager is one job: when the relay's window is closed (instead of
Ctrl+C), Windows gives the program a few seconds. In that time the data that did not reach the database yet
is saved on this PC (data/spool), so it is sent the next time the relay runs and nothing gets lost.

The other methods are kept so the website and the relay can call them the same way as before.
"""
import sys


class DatabaseServer:
    def __init__(self, cfg=None, host="", state=None):
        self.before_exit = None       # set by relay.py: saves unsent data to disk

    def set_host(self, host):
        pass

    def race(self, reason=""):
        pass

    def touch(self):
        pass

    def is_off(self):
        return False

    def db_ready(self):
        return True

    def not_ready_text(self):
        return ""

    def start_thread(self):
        if sys.platform == "win32":
            self._install_close_handler()

    def on_exit(self, wait_flush=0):
        pass

    def _install_close_handler(self):
        """Closing the window / logging off / shutting down: save what is not in the database yet."""
        try:
            import ctypes
            HANDLER = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

            def handler(event):
                if event in (2, 5, 6):    # window closed, log off, shut down
                    if self.before_exit:
                        try:
                            self.before_exit()
                        except Exception:
                            pass
                    return 1
                return 0                  # Ctrl+C goes through KeyboardInterrupt as usual

            self._handler = HANDLER(handler)   # keep a reference, otherwise Python garbage-collects it
            ctypes.windll.kernel32.SetConsoleCtrlHandler(self._handler, True)
        except Exception:
            pass

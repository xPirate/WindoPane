"""Background checks for channels set to take over a tile when they go live.

Each takeover channel is checked on its own interval (default 2 minutes),
whether or not it is on the board. When one goes live the controller lays
it over its target tile; the tile itself notices when the broadcast ends
and hands the tile back (see SequenceMixin._not_live).
"""

import logging
import threading
import time

from . import resolver

log = logging.getLogger(__name__)

TICK_S = 10
CHECK_HEIGHT = 360  # only checking liveness; the tile picks its own quality


class LiveWatcher(threading.Thread):
    def __init__(self, ctl, check=None):
        super().__init__(name="livewatch", daemon=True)
        self.ctl = ctl
        self.check = check or self._check
        self.next_check = {}  # stream id -> monotonic time of the next check
        self.wake = threading.Event()

    @staticmethod
    def _check(stream):
        """True if the channel is live now, False if not, None if unsure."""
        try:
            media = resolver.resolve(dict(stream, mode="live"), CHECK_HEIGHT)
        except resolver.NotLive:
            return False
        except Exception as exc:
            log.info("live check for %s failed: %s", stream.get("name"), exc)
            return None
        return media.live is not False

    def poll_once(self, now=None):
        now = now if now is not None else time.monotonic()
        streams = self.ctl._takeover_streams()
        ids = {s["id"] for s in streams}
        for sid in list(self.next_check):
            if sid not in ids:
                del self.next_check[sid]
        for s in streams:
            sid = s["id"]
            interval = max(1, int(s.get("check_minutes") or 2)) * 60
            state = self.ctl.live_now.get(sid)
            if state and not state["dismissed"]:
                # on the board: the tile reports the end of the broadcast
                self.next_check[sid] = now + interval
                continue
            if now < self.next_check.get(sid, 0):
                continue
            ended = self.ctl.live_cooldown.get(sid)
            if ended and time.time() - ended < interval:
                # the platform may still call a just-finished stream live for a bit
                self.next_check[sid] = now + interval
                continue
            self.next_check[sid] = now + interval
            live = self.check(s)
            if live is None:
                continue
            if state and state["dismissed"]:
                if not live:
                    self.ctl.set_live(sid, False)  # sent back earlier, now over: re-arm
                continue
            if live:
                log.info("%s is live", s.get("name"))
                self.ctl.set_live(sid, True)

    def run(self):
        while True:
            try:
                self.poll_once()
            except Exception:
                log.exception("live watcher")
            self.wake.wait(TICK_S)
            self.wake.clear()

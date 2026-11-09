"""What a video tile plays next: one stream, a playlist in order or
shuffled, or a channel that is waited on until it goes live.

A Source lives as long as its stream stays on the same tile, so reloading
the tile, or the tile being rebuilt, keeps the playlist position.
"""

import logging
import random
import threading
import time

from . import resolver

log = logging.getLogger(__name__)

PLAYLIST_REFRESH_S = 30 * 60


class Source:
    def __init__(self, stream, lister=None):
        self.stream = dict(stream)
        self.mode = resolver.video_mode(stream)
        self.shuffle = bool(stream.get("shuffle"))
        self.signature = self.signature_of(stream)
        self._lister = lister or resolver.playlist_entries
        self.lock = threading.Lock()
        self.entries = None
        self.title = ""
        self.fetched = 0.0
        self.order = []
        self.pos = 0
        self.current_id = None

    @staticmethod
    def signature_of(stream):
        return (stream.get("url"), resolver.video_mode(stream), bool(stream.get("shuffle")))

    # -- playlist bookkeeping
    def _reorder(self, keep_id=None):
        n = len(self.entries)
        self.order = list(range(n))
        if self.shuffle:
            random.shuffle(self.order)
        if keep_id is not None:
            for i, idx in enumerate(self.order):
                if self.entries[idx]["id"] == keep_id:
                    self.pos = i
                    return
        self.pos = min(self.pos, max(0, n - 1))

    def _refresh(self, force=False):
        """Fetch the playlist (worker thread). Keeps the current video's place."""
        if not force and self.entries is not None and time.time() - self.fetched < PLAYLIST_REFRESH_S:
            return
        listing = self._lister(self.stream["url"])
        with self.lock:
            keep = self.current_id
            self.entries = listing["entries"]
            self.title = listing.get("title") or ""
            self.fetched = time.time()
            self._reorder(keep)
        log.info("playlist %s: %d videos", self.stream.get("name"), len(self.entries))

    def prepare(self):
        """The concrete stream to resolve next, and a label for it.
        Blocking (may list the playlist): call from a worker thread."""
        if self.mode != "playlist":
            return dict(self.stream), None
        self._refresh()
        with self.lock:
            n = len(self.entries)
            idx = self.order[self.pos % n]
            entry = self.entries[idx]
            self.current_id = entry["id"]
            label = "%d/%d" % (self.pos % n + 1, n)
        target = dict(self.stream, url=entry["url"], mode="single", kind="video")
        return target, {"label": label, "title": entry.get("title") or ""}

    def advance(self, step=1):
        """Move to the next (or previous) video; wraps, reshuffling each pass."""
        if self.mode != "playlist" or not self.entries:
            return
        with self.lock:
            n = len(self.entries)
            nxt = self.pos + step
            if nxt >= n:
                nxt = 0
                if self.shuffle:
                    random.shuffle(self.order)
            elif nxt < 0:
                nxt = n - 1
            self.pos = nxt
            self.current_id = None

    def position(self):
        if self.mode != "playlist" or not self.entries:
            return None
        return {"pos": self.pos % len(self.entries) + 1, "total": len(self.entries)}

    def size(self):
        return len(self.entries) if self.entries else 0

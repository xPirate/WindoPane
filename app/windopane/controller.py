"""Central state: the one place the web UI, input devices and display meet.

Every mutation goes through a Controller method, is persisted, and then fires
the change listeners (the display re-syncs from a snapshot). Audio selection,
zoom and the focused screen are runtime-only: after a reboot everything
starts muted.

Tiles are addressed as (screen, slot). Internally a tile also has a global
number, ``screen * MAX_SLOTS + slot``.
"""

import copy
import logging
import re
import threading
import time
import uuid

from . import adsb, config, layouts, resolver

log = logging.getLogger(__name__)

MAX_SCREENS = config.MAX_SCREENS
TILES = MAX_SCREENS * layouts.MAX_SLOTS


def tile_id(screen, slot):
    return int(screen) * layouts.MAX_SLOTS + int(slot)


def split(tid):
    return divmod(int(tid), layouts.MAX_SLOTS)


def _check_screen(screen):
    screen = int(screen)
    if not 0 <= screen < MAX_SCREENS:
        raise ValueError("screen out of range")
    return screen


def _check_slot(slot):
    slot = int(slot)
    if not 0 <= slot < layouts.MAX_SLOTS:
        raise ValueError("slot out of range")
    return slot


class Controller:
    def __init__(self, path=config.DEFAULT_PATH):
        self.path = path
        self._lock = threading.RLock()
        self._cfg = config.load(path)
        self._listeners = []
        self.audio_tile = None           # global tile number or None (muted)
        self.zoom = [None] * MAX_SCREENS  # zoomed slot per screen
        self.focus_screen = 0
        self.reload_tokens = [0] * TILES
        self.skip_tokens = [0] * TILES
        # live takeovers: stream id -> {"since": t, "dismissed": bool}
        self.live_now = {}
        self.live_cooldown = {}  # stream id -> time a takeover last ended
        self.tile_status = [None] * TILES
        self.monitors = None             # None = unknown (no display attached yet)
        self.audio_devices = []
        self.started = time.time()
        self.input_devices = []
        self.events = []
        self.alerts = []                 # headline alerts, newest last (active and recent)
        self.alert_watcher = None        # set by alerts.AlertWatcher

    # ------------------------------------------------------------ plumbing
    def subscribe(self, fn):
        self._listeners.append(fn)

    def _commit(self, persist=True):
        if persist:
            try:
                config.save(self._cfg, self.path)
            except OSError as exc:
                log.error("could not save config: %s", exc)
        for fn in list(self._listeners):
            try:
                fn()
            except Exception:  # a listener must never break the API
                log.exception("listener failed")

    def note(self, message):
        with self._lock:
            self.events.append({"t": time.time(), "msg": message})
            del self.events[:-30]
        log.info(message)

    def snapshot(self):
        with self._lock:
            return {
                "config": copy.deepcopy(self._cfg),
                "audio_tile": self.audio_tile,
                "zoom": list(self.zoom),
                "focus_screen": self.focus_screen,
                "reload_tokens": list(self.reload_tokens),
                "skip_tokens": list(self.skip_tokens),
                "effective": [self.effective_slots(i) for i in range(MAX_SCREENS)],
                "takeovers": self.takeovers(),
            }

    @property
    def cfg(self):
        return self._cfg

    def screen_cfg(self, screen):
        return self._cfg["screens"][screen]

    # ------------------------------------------------------------ screens
    def active_screens(self):
        """Screens that are enabled and have a monitor attached."""
        out = []
        for i, sc in enumerate(self._cfg["screens"]):
            if not sc["enabled"]:
                continue
            if self.monitors is not None and i >= len(self.monitors):
                continue
            out.append(i)
        return out or [0]

    def visible_tiles(self, screen=None):
        """Global tile numbers that are on screen and have a stream."""
        screens = self.active_screens() if screen is None else [screen]
        out = []
        for s in screens:
            sc = self._cfg["screens"][s]
            n = layouts.tile_count(sc["layout"])
            slots = self.effective_slots(s)
            out += [tile_id(s, i) for i in range(n) if slots[i]]
        return out

    # ------------------------------------------------------------ live takeovers
    def _takeover_streams(self):
        """Streams set to take over a tile when their channel goes live."""
        return [s for s in self._cfg["streams"]
                if isinstance(s.get("takeover"), dict) and resolver.video_mode(s) == "live"]

    def takeovers(self):
        """(screen, slot) -> stream id of the live channel occupying it now.
        Highest priority (1) wins a tile; ties go to whichever went live first."""
        with self._lock:
            live = []
            for s in self._takeover_streams():
                state = self.live_now.get(s["id"])
                if state and not state["dismissed"]:
                    live.append((int(s.get("priority") or 5), state["since"], s))
            out = {}
            for _prio, _since, s in sorted(live, key=lambda x: (x[0], x[1])):
                t = s["takeover"]
                key = (int(t["screen"]), int(t["slot"]))
                if key not in out:
                    out[key] = s["id"]
            return out

    def effective_slots(self, screen):
        """The screen's assigned slots with any live takeovers laid on top."""
        with self._lock:
            slots = list(self._cfg["screens"][screen]["slots"])
            for (s, slot), sid in self.takeovers().items():
                if s == screen and 0 <= slot < len(slots):
                    slots[slot] = sid
            return slots

    def is_takeover(self, stream_id):
        with self._lock:
            return stream_id in self.takeovers().values()

    def set_live(self, stream_id, live):
        """From the live watcher: a takeover channel went live or off air."""
        with self._lock:
            before = self.takeovers()
            if live:
                if stream_id in self.live_now:
                    return
                self.live_now[stream_id] = {"since": time.time(), "dismissed": False}
            else:
                if self.live_now.pop(stream_id, None) is None:
                    return
            after = self.takeovers()
            self._fix_runtime()
        if before != after:
            self._note_takeover_change(before, after)
            self._commit(persist=False)

    def live_ended(self, stream_id):
        """From a tile: the broadcast it was showing has ended."""
        with self._lock:
            self.live_cooldown[stream_id] = time.time()
        self.set_live(stream_id, False)

    def dismiss_live(self, stream_id):
        """Send a live takeover back: the tile returns to its own stream
        until this broadcast is over."""
        with self._lock:
            state = self.live_now.get(stream_id)
            if not state or state["dismissed"]:
                return False
            before = self.takeovers()
            state["dismissed"] = True
            after = self.takeovers()
            self._fix_runtime()
        self._note_takeover_change(before, after)
        self._commit(persist=False)
        return True

    def _note_takeover_change(self, before, after):
        names = {s["id"]: s.get("name") or s["url"] for s in self._cfg["streams"]}
        for key in sorted(set(before) | set(after)):
            old, new = before.get(key), after.get(key)
            if old == new:
                continue
            where = "screen %d tile %d" % (key[0] + 1, key[1] + 1)
            if new:
                self.note("%s is live: taking over %s" % (names.get(new, new), where))
            else:
                own = self._cfg["screens"][key[0]]["slots"][key[1]]
                self.note("%s handed %s back to %s" % (names.get(old, old), where,
                                                        names.get(own, "an empty tile") if own else "an empty tile"))

    def _fix_runtime(self):
        vis = self.visible_tiles()
        if self.audio_tile is not None and self.audio_tile not in vis:
            self.audio_tile = None
        for s in range(MAX_SCREENS):
            z = self.zoom[s]
            if z is not None and tile_id(s, z) not in vis:
                self.zoom[s] = None
        if self.focus_screen not in self.active_screens():
            self.focus_screen = self.active_screens()[0]

    def report_monitors(self, monitors):
        """Called by the display with the connected monitors, in screen order."""
        with self._lock:
            changed = monitors != self.monitors
            self.monitors = monitors
            self._fix_runtime()
        if changed:
            self.note("Screens connected: %d" % len(monitors))

    # ------------------------------------------------------------ streams
    def _stream(self, stream_id):
        return next((s for s in self._cfg["streams"] if s["id"] == stream_id), None)

    def _clean_stream(self, data, existing=None):
        out = dict(existing or {})
        if "url" in data or existing is None:
            out["url"] = resolver.validate_url(data.get("url"))
        if "name" in data or existing is None:
            name = (data.get("name") or "").strip()
            out["name"] = name[:80] or out["url"]
        if "kind" in data or existing is None:
            kind = data.get("kind") or "auto"
            if kind not in config.STREAM_KINDS:
                raise ValueError("kind must be one of %s" % ", ".join(config.STREAM_KINDS))
            out["kind"] = kind
        if "max_height" in data:
            mh = data.get("max_height")
            if mh in (None, "", "auto", 0, "0"):
                out.pop("max_height", None)
            else:
                mh = int(mh)
                if not 144 <= mh <= 4320:
                    raise ValueError("max_height out of range")
                out["max_height"] = mh
        if "zoom" in data:
            z = data.get("zoom")
            if z in (None, ""):
                out.pop("zoom", None)
            else:
                z = float(z)
                if not 0.25 <= z <= 4:
                    raise ValueError("zoom must be between 0.25 and 4")
                out["zoom"] = z
        if "refresh_minutes" in data:
            r = data.get("refresh_minutes")
            if r in (None, "", 0, "0"):
                out.pop("refresh_minutes", None)
            else:
                r = int(r)
                if not 1 <= r <= 1440:
                    raise ValueError("refresh_minutes must be 1-1440")
                out["refresh_minutes"] = r
        if "rtsp_tcp" in data:
            out["rtsp_tcp"] = bool(data["rtsp_tcp"])
        if "mode" in data:
            mode = data.get("mode") or "auto"
            if mode not in resolver.VIDEO_MODES:
                raise ValueError("mode must be one of %s" % ", ".join(resolver.VIDEO_MODES))
            if mode == "auto":
                out.pop("mode", None)
            else:
                out["mode"] = mode
        if "shuffle" in data:
            if data.get("shuffle"):
                out["shuffle"] = True
            else:
                out.pop("shuffle", None)
        if "check_minutes" in data:
            cm = data.get("check_minutes")
            if cm in (None, "", 0, "0"):
                out.pop("check_minutes", None)
            else:
                cm = int(cm)
                if not 1 <= cm <= 60:
                    raise ValueError("check interval must be 1-60 minutes")
                out["check_minutes"] = cm
        if "takeover" in data:
            t = data.get("takeover")
            if not t:
                out.pop("takeover", None)
            else:
                if isinstance(t, str):  # "screen:slot" from the form
                    a, _, b = t.partition(":")
                    t = {"screen": a, "slot": b}
                out["takeover"] = {"screen": _check_screen(t.get("screen")), "slot": _check_slot(t.get("slot"))}
        if "priority" in data:
            p = data.get("priority")
            if p in (None, "", 0, "0"):
                out.pop("priority", None)
            else:
                p = int(p)
                if not 1 <= p <= 9:
                    raise ValueError("priority must be 1 (highest) to 9")
                out["priority"] = p
        if out.get("takeover") and resolver.video_mode(out) != "live":
            raise ValueError("only a channel set to wait for live can take over a tile")
        if "location" in data:
            loc = (data.get("location") or "").strip()
            if loc:
                adsb.parse_location(loc)  # raises ValueError with a readable message
                out["location"] = loc[:60]
            else:
                out.pop("location", None)
        if "units" in data:
            units = data.get("units") or "nm"
            if units not in adsb.UNITS:
                raise ValueError("units must be nm, km or mi")
            out["units"] = units
        if "max_range" in data:
            mr = data.get("max_range")
            if mr in (None, "", 0, "0"):
                out.pop("max_range", None)
            else:
                mr = float(mr)
                if not 0 < mr <= 1000:
                    raise ValueError("max range must be 1-1000")
                out["max_range"] = mr
        if out.get("kind") == "adsb":
            from urllib.parse import urlparse as _up
            if _up(out["url"]).scheme.lower() not in resolver.ADSB_SCHEMES:
                raise ValueError("ADS-B needs an http(s) dump1090 address or tcp://host:30003")
        if out.get("kind") == "web" and not out["url"].lower().startswith(("http://", "https://")):
            raise ValueError("web tiles need an http(s) URL")
        return out

    def add_stream(self, data):
        with self._lock:
            stream = self._clean_stream(data)
            stream["id"] = uuid.uuid4().hex[:10]
            self._cfg["streams"].append(stream)
            slot = data.get("slot")
            if slot not in (None, ""):
                self._assign(_check_screen(data.get("screen") or 0), int(slot), stream["id"])
        self._commit()
        return stream

    def update_stream(self, stream_id, data):
        with self._lock:
            existing = self._stream(stream_id)
            if existing is None:
                raise KeyError(stream_id)
            updated = self._clean_stream(data, existing)
            existing.clear()
            existing.update(updated)
            if not updated.get("takeover"):
                self.live_now.pop(stream_id, None)
            self._fix_runtime()
        self._commit()
        return existing

    def delete_stream(self, stream_id):
        with self._lock:
            if self._stream(stream_id) is None:
                raise KeyError(stream_id)
            self._cfg["streams"] = [s for s in self._cfg["streams"] if s["id"] != stream_id]
            self.live_now.pop(stream_id, None)
            for sc in self._cfg["screens"]:
                sc["slots"] = [None if s == stream_id else s for s in sc["slots"]]
            self._fix_runtime()
        self._commit()

    def reorder_streams(self, ids):
        with self._lock:
            by_id = {s["id"]: s for s in self._cfg["streams"]}
            ordered = [by_id.pop(i) for i in ids if i in by_id]
            self._cfg["streams"] = ordered + list(by_id.values())
        self._commit()

    # ------------------------------------------------------------ slots / layout
    def _assign(self, screen, slot, stream_id):
        slot = _check_slot(slot)
        if stream_id is not None and self._stream(stream_id) is None:
            raise KeyError(stream_id)
        self._cfg["screens"][screen]["slots"][slot] = stream_id

    def set_slot(self, screen, slot, stream_id):
        with self._lock:
            self._assign(_check_screen(screen), slot, stream_id or None)
            self._fix_runtime()
        self._commit()

    def set_slots(self, screen, slots):
        screen = _check_screen(screen)
        with self._lock:
            slots = (list(slots) + [None] * layouts.MAX_SLOTS)[: layouts.MAX_SLOTS]
            for i, sid in enumerate(slots):
                self._assign(screen, i, sid or None)
            self._fix_runtime()
        self._commit()

    def swap_slots(self, screen, a, b):
        screen, a, b = _check_screen(screen), _check_slot(a), _check_slot(b)
        with self._lock:
            s = self._cfg["screens"][screen]["slots"]
            s[a], s[b] = s[b], s[a]
            ta, tb = tile_id(screen, a), tile_id(screen, b)
            if self.audio_tile in (ta, tb):
                self.audio_tile = tb if self.audio_tile == ta else ta
            if self.zoom[screen] in (a, b):
                self.zoom[screen] = b if self.zoom[screen] == a else a
            self._fix_runtime()
        self._commit()

    def set_layout(self, screen, layout_id):
        screen = _check_screen(screen)
        if layout_id not in layouts.LAYOUTS:
            raise ValueError("unknown layout %r" % layout_id)
        with self._lock:
            self._cfg["screens"][screen]["layout"] = layout_id
            self.zoom[screen] = None
            self._fix_runtime()
        self._commit()
        self.note("Screen %d layout: %s" % (screen + 1, layouts.LAYOUTS[layout_id]["name"]))

    def step_layout(self, step=1, screen=None):
        screen = self.focus_screen if screen is None else screen
        current = self._cfg["screens"][screen]["layout"]
        self.set_layout(screen, layouts.next_layout(current, step))

    # ------------------------------------------------------------ audio
    def set_audio(self, tile):
        """Make one tile audible (global number), or None to mute everything."""
        with self._lock:
            if tile is not None:
                tile = int(tile)
                if tile not in self.visible_tiles():
                    tile = None  # empty or off-screen tile: mute
            changed = tile != self.audio_tile
            self.audio_tile = tile
            if tile is not None:
                self.focus_screen = split(tile)[0]
        self._commit(persist=False)
        if changed:
            if tile is None:
                self.note("Audio: muted")
            else:
                s, i = split(tile)
                self.note("Audio: screen %d tile %d" % (s + 1, i + 1))
        return tile

    def set_audio_at(self, screen, slot):
        if slot is None:
            return self.set_audio(None)
        return self.set_audio(tile_id(_check_screen(screen), _check_slot(slot)))

    def toggle_audio(self, tile):
        """Select a tile's audio, or mute if that tile is already audible."""
        return self.set_audio(None if self.audio_tile == tile else tile)

    def toggle_focus_slot(self, slot):
        """Controller face buttons: tile N on the focused screen."""
        return self.toggle_audio(tile_id(self.focus_screen, slot))

    def cycle_audio(self, step=1):
        with self._lock:
            vis = self.visible_tiles()
        if not vis:
            return self.set_audio(None)
        if self.audio_tile not in vis:
            target = vis[0] if step > 0 else vis[-1]
        else:
            target = vis[(vis.index(self.audio_tile) + step) % len(vis)]
        return self.set_audio(target)

    def switch_screen(self, step=1):
        """Move focus (and the audio, if any) to the next screen."""
        with self._lock:
            active = self.active_screens()
            if len(active) < 2:
                return self.focus_screen
            cur = active.index(self.focus_screen) if self.focus_screen in active else 0
            target = active[(cur + step) % len(active)]
        return self.focus(target)

    def focus(self, screen):
        """Make a screen the focused one; audio, if playing, moves to its first tile."""
        with self._lock:
            screen = int(screen)
            if screen not in self.active_screens():
                return self.focus_screen
            changed = screen != self.focus_screen
            self.focus_screen = screen
            self._digit_cancel()
            listening = self.audio_tile is not None and split(self.audio_tile)[0] != screen
            first = self.visible_tiles(screen)
        if changed:
            self.note("Focus: screen %d" % (screen + 1))
        if listening:
            self.set_audio(first[0] if first else None)
        else:
            self._commit(persist=False)
        return screen

    # ------------------------------------------------------------ number keys
    DIGIT_WAIT_S = 1.2

    def _digit_cancel(self):
        timer = getattr(self, "_digit_timer", None)
        if timer is not None:
            timer.cancel()
        self._digit_timer = None
        self._digit_pending = None

    def digit(self, d):
        """Number keys pick a tile on the focused screen, like a TV remote:
        1-9 act at once; on screens with 10+ tiles, "1" waits briefly for a
        second digit (1 then 2 = tile 12). 0 on its own mutes."""
        d = int(d)
        with self._lock:
            screen = self.focus_screen
            count = layouts.tile_count(self._cfg["screens"][screen]["layout"])
            pending = getattr(self, "_digit_pending", None)
            if pending is not None:
                self._digit_cancel()
                number = pending * 10 + d
                target = number if 1 <= number <= count else None
                if target is None:
                    return None
                return self.toggle_audio(tile_id(screen, number - 1))
            if d == 0:
                return self.set_audio(None)
            if d == 1 and count >= 10:
                self._digit_pending = 1
                self._digit_timer = threading.Timer(self.DIGIT_WAIT_S, self._digit_timeout, args=(screen,))
                self._digit_timer.daemon = True
                self._digit_timer.start()
                return "pending"
        if d > count:
            return None
        return self.toggle_audio(tile_id(screen, d - 1))

    def _digit_timeout(self, screen):
        with self._lock:
            if getattr(self, "_digit_pending", None) is None or self.focus_screen != screen:
                return
            self._digit_timer = None
            self._digit_pending = None
        self.toggle_audio(tile_id(screen, 0))

    def set_volume(self, volume):
        with self._lock:
            self._cfg["volume"] = max(0, min(100, int(volume)))
        self._commit()
        return self._cfg["volume"]

    def nudge_volume(self, delta):
        return self.set_volume(self._cfg["volume"] + delta)

    # ------------------------------------------------------------ zoom / reload
    def set_zoom(self, screen, slot):
        screen = _check_screen(screen)
        with self._lock:
            if slot is not None:
                slot = int(slot)
                if tile_id(screen, slot) not in self.visible_tiles():
                    slot = None
            self.zoom[screen] = slot
        self._commit(persist=False)
        return slot

    def toggle_zoom(self):
        """Zoom the audible tile on its screen, or un-zoom the focused screen."""
        if self.audio_tile is not None:
            screen, slot = split(self.audio_tile)
        else:
            screen = self.focus_screen
            vis = self.visible_tiles(screen)
            slot = split(vis[0])[1] if vis else None
        if self.zoom[screen] is not None:
            return self.set_zoom(screen, None)
        return self.set_zoom(screen, slot)

    def reload(self, screen=None, slot=None):
        with self._lock:
            if screen is None:
                targets = range(TILES)
            elif slot is None:
                s = _check_screen(screen)
                targets = [tile_id(s, i) for i in range(layouts.MAX_SLOTS)]
            else:
                targets = [tile_id(_check_screen(screen), _check_slot(slot))]
            for t in targets:
                self.reload_tokens[t] += 1
        self._commit(persist=False)

    def skip(self, screen, slot, step=1):
        """Next (or previous, step=-1) video on a playlist tile."""
        with self._lock:
            t = tile_id(_check_screen(screen), _check_slot(slot))
            self.skip_tokens[t] += 1 if step > 0 else -1
        self._commit(persist=False)

    def skip_audible(self, step=1):
        """N key: skip on the tile being listened to, else tile 1 of the focused screen."""
        if self.audio_tile is not None:
            self.skip(*split(self.audio_tile), step=step)
        else:
            vis = self.visible_tiles(self.focus_screen)
            if vis:
                self.skip(*split(vis[0]), step=step)

    def reload_audible(self):
        if self.audio_tile is not None:
            self.reload(*split(self.audio_tile))

    # ------------------------------------------------------------ headline alerts
    ALERT_STYLES = ("popup", "ticker", "both")
    ALERT_HISTORY = 60

    def _clean_feed(self, data, existing=None):
        from urllib.parse import urlparse
        out = dict(existing or {})
        if "url" in data or existing is None:
            url = str(data.get("url") or "").strip()
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ValueError("enter the feed's full http:// or https:// address")
            out["url"] = url[:2000]
        if "name" in data or existing is None:
            name = str(data.get("name") or "").strip()[:60]
            out["name"] = name or urlparse(out["url"]).netloc.replace("www.", "")
        if "enabled" in data or existing is None:
            out["enabled"] = bool(data.get("enabled", True))
        if "every_minutes" in data or existing is None:
            every = data.get("every_minutes")
            out["every_minutes"] = 5 if every in (None, "") else max(1, min(1440, int(every)))
        if "style" in data or existing is None:
            style = data.get("style") or "popup"
            if style not in self.ALERT_STYLES:
                raise ValueError("show as must be popup, ticker or both")
            out["style"] = style
        if "seconds" in data or existing is None:
            secs = data.get("seconds")
            if secs in (None, ""):
                secs = 300 if out["style"] == "ticker" else 30
            secs = int(secs)
            if secs != 0 and not 5 <= secs <= 86400:
                raise ValueError("on-screen time must be 5 seconds to 24 hours, or 0 for until dismissed")
            out["seconds"] = secs
        if "screens" in data or existing is None:
            scr = data.get("screens", "all")
            if scr in (None, "", "all"):
                out["screens"] = "all"
            else:
                if not isinstance(scr, list):
                    scr = [scr]
                scr = sorted({_check_screen(x) for x in scr})
                out["screens"] = scr or "all"
        if "match" in data or existing is None:
            out["match"] = re.sub(r"\s+", " ", str(data.get("match") or "")).strip()[:200]
        return out

    def add_feed(self, data):
        with self._lock:
            feed = self._clean_feed(data)
            feed["id"] = uuid.uuid4().hex[:10]
            self._cfg["alerts"]["feeds"].append(feed)
        self._commit()
        if self.alert_watcher:
            self.alert_watcher.check_now(feed["id"])
        return feed

    def update_feed(self, feed_id, data):
        with self._lock:
            feed = self._feed(feed_id)
            updated = self._clean_feed(data, feed)
            feed.clear()
            feed.update(updated)
        self._commit()
        if self.alert_watcher:
            self.alert_watcher.check_now(feed_id)
        return feed

    def delete_feed(self, feed_id):
        with self._lock:
            self._feed(feed_id)
            self._cfg["alerts"]["feeds"] = [f for f in self._cfg["alerts"]["feeds"] if f["id"] != feed_id]
        self._commit()

    def _feed(self, feed_id):
        for f in self._cfg["alerts"]["feeds"]:
            if f["id"] == feed_id:
                return f
        raise KeyError(feed_id)

    def raise_alert(self, feed, title, link=None, published=None, test=False):
        """Put a headline on the wall (from the watcher, or a test from the web UI)."""
        now = time.time()
        secs = int(feed.get("seconds", 30))
        alert = {
            "id": uuid.uuid4().hex[:10],
            "feed_id": feed.get("id"),
            "feed": feed.get("name") or "Alert",
            "title": str(title)[:400],
            "link": link or "",
            "published": published,
            "style": feed.get("style") or "popup",
            "screens": feed.get("screens") or "all",
            "seconds": secs,
            "created": now,
            "expires": now + secs if secs > 0 else None,
            "dismissed": False,
            "test": bool(test),
        }
        with self._lock:
            self.alerts.append(alert)
            del self.alerts[:-self.ALERT_HISTORY]
        log.info("alert from %s: %s", alert["feed"], alert["title"])
        self._commit(persist=False)
        return alert

    def active_alerts(self, now=None):
        now = time.time() if now is None else now
        with self._lock:
            if not self._cfg["alerts"].get("enabled", True):
                return []
            return [dict(a) for a in self.alerts
                    if not a["dismissed"] and (a["expires"] is None or a["expires"] > now)]

    def recent_alerts(self, limit=20):
        now = time.time()
        with self._lock:
            out = []
            for a in reversed(self.alerts[-limit:]):
                item = dict(a)
                item["on_screen"] = not a["dismissed"] and (a["expires"] is None or a["expires"] > now)
                out.append(item)
            return out

    def dismiss_alert(self, alert_id=None):
        """Take one alert (or, with no id, every alert) off the wall."""
        changed = False
        with self._lock:
            for a in self.alerts:
                if (alert_id is None or a["id"] == alert_id) and not a["dismissed"]:
                    a["dismissed"] = True
                    changed = True
        if changed:
            self._commit(persist=False)
        return changed

    # ------------------------------------------------------------ settings
    def update_settings(self, data):
        with self._lock:
            disp = self._cfg["display"]
            d = data.get("display") or {}
            if "gap" in d:
                disp["gap"] = max(0, min(64, int(d["gap"])))
            if "border" in d:
                disp["border"] = "auto" if d["border"] in ("auto", "", None) else max(0, min(32, int(d["border"])))
            if "quality_cap" in d:
                qc = d["quality_cap"]
                disp["quality_cap"] = "auto" if qc in ("auto", "", None) else int(qc)
            if "osd" in d:
                disp["osd"] = bool(d["osd"])
            if "arrangement" in d:
                if d["arrangement"] not in ("auto", "row", "grid"):
                    raise ValueError("arrangement must be auto, row or grid")
                disp["arrangement"] = d["arrangement"]
            for i, sd in enumerate((data.get("screens") or [])[:MAX_SCREENS]):
                if not isinstance(sd, dict):
                    continue
                sc = self._cfg["screens"][i]
                if "resolution" in sd:
                    res = str(sd["resolution"]).strip().lower()
                    if res != "auto":
                        w, _, h = res.partition("x")
                        if not (w.isdigit() and h.isdigit()):
                            raise ValueError("resolution must be 'auto' or WIDTHxHEIGHT")
                    sc["resolution"] = res
                if "enabled" in sd and i > 0:
                    sc["enabled"] = bool(sd["enabled"])
                if "audio_device" in sd:
                    sc["audio_device"] = str(sd["audio_device"] or "auto")[:300]
            al = data.get("alerts") or {}
            acfg = self._cfg["alerts"]
            if "enabled" in al:
                acfg["enabled"] = bool(al["enabled"])
            for key, allowed in (("ticker_speed", ("slow", "normal", "fast")),
                                 ("ticker_position", ("bottom", "top")),
                                 ("popup_position", ("top-right", "top-left", "bottom-right", "bottom-left"))):
                if key in al:
                    if al[key] not in allowed:
                        raise ValueError("%s must be one of %s" % (key.replace("_", " "), ", ".join(allowed)))
                    acfg[key] = al[key]
            if "max_popups" in al:
                acfg["max_popups"] = max(1, min(6, int(al["max_popups"])))
            web = data.get("web") or {}
            if "password" in web:
                self._cfg["web"]["password"] = str(web["password"] or "")
            player = data.get("player") or {}
            if "network_caching_ms" in player:
                self._cfg["player"]["network_caching_ms"] = max(200, min(20000, int(player["network_caching_ms"])))
            if "engine" in player:
                if player["engine"] not in ("auto", "vlc", "mpv"):
                    raise ValueError("engine must be auto, vlc or mpv")
                self._cfg["player"]["engine"] = player["engine"]
            if "stall_timeout_s" in player:
                self._cfg["player"]["stall_timeout_s"] = max(10, min(600, int(player["stall_timeout_s"])))
            self._fix_runtime()
        self._commit()

    # ------------------------------------------------------------ status from display
    def report_status(self, tile, status):
        with self._lock:
            self.tile_status[tile] = status

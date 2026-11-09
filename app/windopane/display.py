"""The HDMI outputs: one fullscreen GTK window per monitor (up to two),
each holding up to four tiles.

Video tiles render through libVLC into a native X sub-window; web tiles are
WebKitGTK views. All GTK and libVLC calls happen on the GTK main thread;
other threads talk to it through GLib.idle_add.
"""

import json
import logging
import re
import socket
import subprocess
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkX11", "3.0")
from gi.repository import Gdk, GdkX11, GLib, Gtk, Pango  # noqa: E402,F401

WebKit2 = None
for _ver in ("4.1", "4.0"):
    try:
        gi.require_version("WebKit2", _ver)
        from gi.repository import WebKit2  # noqa: E402
        break
    except (ValueError, ImportError):
        continue

import vlc  # noqa: E402

from . import config, inputs, layouts, mpvproc, overlay, resolver, sources, sysinfo  # noqa: E402

log = logging.getLogger(__name__)

ACCENT = "#ffb020"

CSS = b"""
#wp-root, #wp-fixed { background-color: #000000; }
.wp-tile { background-color: #000000; }
.wp-tile.wp-audio { background-color: %s; }
.wp-video { background-color: #000000; }
.wp-msg { background-color: #0b0d10; color: #7d8590; }
""" % ACCENT.encode()


def _lan_address():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))  # TEST-NET; nothing is sent
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None


def _esc(text):
    return GLib.markup_escape_text(str(text or ""))


# ---------------------------------------------------------------------------
class Tile:
    kind = "empty"

    def __init__(self, disp, slot, stream, key):
        self.disp = disp
        self.slot = slot
        self.stream = stream
        self.key = key
        self.dead = False
        self.audible = False
        self.rect = (0, 0, 1, 1)
        self.state = "idle"
        self.detail = ""
        self.since = time.time()
        self.retry_at = None
        self.retry_head = ""
        self.retry_detail = ""

        self.frame = Gtk.EventBox()
        self.frame.get_style_context().add_class("wp-tile")
        self.frame.connect_after("realize", lambda w: w.get_window().ensure_native())
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.msg = Gtk.Label()
        self.msg.set_justify(Gtk.Justification.CENTER)
        self.msg.set_line_wrap(True)
        self.msg.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.msg.get_style_context().add_class("wp-msg")
        self.frame.add(self.stack)

    # -- helpers
    def title(self):
        return (self.stream or {}).get("name") or "Slot %d" % (self.slot + 1)

    def set_state(self, state, detail=""):
        if state != self.state:
            self.since = time.time()
        self.state = state
        self.detail = detail

    def show_message(self, headline, sub=""):
        h = max(1, self.rect[3])
        big = max(9, min(48, int(h / 16)))
        small = max(7, int(big * 0.55))
        markup = '<span size="%d" foreground="#c9d1d9">%s</span>' % (big * 1024, _esc(headline))
        if sub:
            markup += '\n<span size="%d">%s</span>' % (small * 1024, _esc(sub))
        self.msg.set_markup(markup)
        self.stack.set_visible_child_name("msg")

    def place(self, x, y, w, h, border, visible=True):
        changed = (x, y, w, h) != self.rect
        self.rect = (x, y, w, h)
        self.disp.fixed.move(self.frame, x, y)
        self.frame.set_size_request(w, h)
        for setter in (self.stack.set_margin_top, self.stack.set_margin_bottom,
                       self.stack.set_margin_start, self.stack.set_margin_end):
            setter(border)
        if visible:
            self.frame.show()
        else:
            self.frame.hide()
        if changed:
            self.on_resize()

    def raise_(self):
        win = self.frame.get_window()
        if win is not None:
            win.raise_()

    def set_audible(self, audible):
        ctx = self.frame.get_style_context()
        if audible:
            ctx.add_class("wp-audio")
        else:
            ctx.remove_class("wp-audio")
        was = self.audible
        self.audible = audible
        self.apply_audio()
        if audible and not was:
            self.on_became_audible()

    def retry_left(self):
        return max(0, int(round(self.retry_at - time.monotonic()))) if self.retry_at else 0

    def show_retry(self):
        lines = [self.retry_head, self.retry_detail, "Retrying in %ds" % self.retry_left()]
        self.show_message(self.title(), "\n".join(x for x in lines if x))

    def status(self):
        detail = self.detail
        if self.retry_at is not None:
            detail = "%s%s - retrying in %ds" % (
                self.retry_head, (": " + self.retry_detail) if self.retry_detail else "", self.retry_left())
        return {"kind": self.kind, "state": self.state, "detail": detail,
                "since": self.since, "audible": self.audible,
                "stream_id": (self.stream or {}).get("id")}

    # -- overridables
    def start(self):
        pass

    def tick(self, now):
        pass

    def apply_audio(self):
        pass

    def on_resize(self):
        pass

    def on_became_audible(self):
        pass

    def skip(self, step=1):
        pass

    def destroy(self):
        self.dead = True
        self.frame.destroy()


# ---------------------------------------------------------------------------
class EmptyTile(Tile):
    kind = "empty"

    def __init__(self, disp, slot, stream, key):
        super().__init__(disp, slot, stream, key)
        self.stack.add_named(self.msg, "msg")
        self.frame.show_all()

    def start(self):
        self.render()

    def on_resize(self):
        self.render()

    def render(self):
        addr = self.disp.web_hint()
        where = "Empty slot"
        if len(self.disp.app.screens) > 1:
            where += " on screen %d" % (self.disp.index + 1)
        self.show_message("%d" % self.disp.key_number(self.slot),
                          where + ("\nAdd a stream at %s" % addr if addr else ""))


# ---------------------------------------------------------------------------
class SequenceMixin:
    """Shared by the VLC and mpv video tiles: what to play next (one stream,
    a playlist, or a channel waited on until it goes live), and the
    "not live yet" card."""

    MAX_SKIPS = 10  # unavailable playlist videos skipped in a row before backing off

    def _init_sequence(self):
        self.source = self.disp.app.source_for(self.disp.index, self.slot, self.stream)
        self.waiting = None      # "Not live right now" etc. while waiting for a live
        self.item = None         # {"label": "3/42", "title": ...} for playlist videos
        self.item_failures = 0
        self.skip_token = None
        self.direction = 1       # which way unavailable playlist videos are skipped

    def _starting(self):
        if self.waiting:
            # keep the "not live" card up while checking again
            self.set_state("waiting", self.detail)
        else:
            self.stack.set_visible_child_name("video")
            self.set_state("resolving", "Looking up stream")

    def _resolve_worker(self, gen, height, caching):
        try:
            target, item = self.source.prepare()
            media = resolver.resolve(target, height, caching)
        except resolver.NotLive as exc:
            GLib.idle_add(self._not_live, gen, exc.detail)
            return
        except Exception as exc:  # report anything, never kill the thread silently
            log.warning("tile %d: cannot resolve %s: %s", self.slot + 1, self.stream.get("url"), exc)
            GLib.idle_add(self._resolve_failed, gen, str(exc) or exc.__class__.__name__)
            return
        log.info("tile %d (%s): %s -> %r (max %dp)", self.slot + 1, self.source.mode,
                 target.get("name"), media, height)
        GLib.idle_add(self._resolved_ok, gen, media, item)

    def _resolved_ok(self, gen, media, item):
        if gen != self.gen or self.dead:
            return False
        if self.waiting:
            self.disp.ctl.note("%s is live" % self.title())
        self.waiting = None
        self.item = item
        return self._launch(gen, media)

    def _resolve_failed(self, gen, message):
        if gen != self.gen or self.dead:
            return False
        if self.source.mode == "playlist" and self.source.size() > 1 and self.item_failures < min(
                self.MAX_SKIPS, self.source.size() - 1):
            # a deleted, private or region-blocked video: move on
            self.item_failures += 1
            self.source.advance(self.direction)
            self.schedule_retry("Skipping unavailable video", message, quick=True)
        else:
            self.item_failures = 0
            self.schedule_retry("Can't open stream", message)
        return False

    def _not_live(self, gen, detail):
        if gen != self.gen or self.dead:
            return False
        ctl = self.disp.ctl
        if ctl.takeovers().get((self.disp.index, self.slot)) == self.stream.get("id"):
            # a live takeover whose broadcast is over: give the tile back
            self.gen += 1
            self._release_player()
            self.set_state("retrying", "Live ended")
            ctl.live_ended(self.stream["id"])
            return False
        minutes = max(1, int(self.stream.get("check_minutes") or 2))
        self.gen += 1
        self._release_player()
        self.waiting = detail
        self.retry_head, self.retry_detail = "", ""
        self.retry_at = time.monotonic() + minutes * 60
        self.backoff = 3.0
        self.set_state("waiting", "%s \u00b7 checking every %d min" % (detail, minutes))
        self.show_retry()
        return False

    def _ended(self, played):
        if self.source.mode == "playlist":
            self.direction = 1
            self.source.advance()
            self.item_failures = 0
            self.schedule_retry("Next video", "", quick=True)
        elif self.source.mode == "live":
            # check again at once: plays on if still live, otherwise waits
            self.schedule_retry("Live stream ended", "", quick=played)
        else:
            self.schedule_retry("Stream ended", "", quick=played)

    def playing_detail(self):
        if self.item:
            title = self.item.get("title") or (self.resolved.title if self.resolved else "") or ""
            return ("%s \u00b7 %s" % (self.item["label"], title)).strip(" \u00b7")
        return (self.resolved.title or "") if self.resolved else ""

    def skip(self, step=1):
        if self.source.mode != "playlist":
            return
        self.direction = 1 if step > 0 else -1
        self.source.advance(step)
        self.item_failures = 0
        self.start()

    def show_retry(self):
        if self.waiting:
            left = self.retry_left()
            self.show_message(self.title(), "%s\nChecking again in %d:%02d" % (self.waiting, left // 60, left % 60))
        else:
            super().show_retry()

    def status(self):
        d = super().status()
        if self.waiting:
            d["detail"] = self.detail  # stable text, no countdown
        d["mode"] = self.source.mode
        pos = self.source.position()
        if pos:
            d["playlist"] = pos
        return d


# ---------------------------------------------------------------------------
class VideoTile(SequenceMixin, Tile):
    kind = "video"

    def __init__(self, disp, slot, stream, key):
        super().__init__(disp, slot, stream, key)
        self.area = Gtk.DrawingArea()
        self.area.get_style_context().add_class("wp-video")
        self.area.connect("draw", self._draw_black)
        self.area.connect_after("realize", self._area_realized)
        self.stack.add_named(self.area, "video")
        self.stack.add_named(self.msg, "msg")
        self.frame.show_all()
        self.stack.set_visible_child_name("video")
        self.xid = None
        self.player = None
        self.media = None
        self.pending = None
        self.gen = 0
        self.backoff = 3.0
        self.launched_at = 0.0
        self.playing_since = None
        self.last_time = None
        self.last_progress = 0.0
        self.resolved = None
        self.want_osd = False
        self._init_sequence()

    @staticmethod
    def _draw_black(_widget, cr):
        cr.set_source_rgb(0, 0, 0)
        cr.paint()
        return False

    def _area_realized(self, area):
        win = area.get_window()
        win.ensure_native()
        self.xid = win.get_xid()
        if self.pending is not None:
            gen, media = self.pending
            self.pending = None
            self._launch(gen, media)

    # -- lifecycle
    def start(self):
        self.gen += 1
        gen = self.gen
        self.retry_at = None
        self._release_player()
        self._starting()
        height = self.disp.height_for(self)
        caching = self.disp.caching_ms
        threading.Thread(target=self._resolve_worker, args=(gen, height, caching),
                         name="resolve-%d" % self.slot, daemon=True).start()

    def _launch(self, gen, media):
        if gen != self.gen or self.dead:
            return False
        if self.xid is None:
            self.pending = (gen, media)
            return False
        inst = self.disp.vlc
        player = inst.media_player_new()
        # VLC 3 creates the audio output with the player, so this sticks
        # before any sound is decoded; apply_audio re-asserts it later.
        player.audio_set_mute(True)
        player.video_set_key_input(False)
        player.video_set_mouse_input(False)
        m = inst.media_new(media.url, *media.options)
        player.set_media(m)
        device = self.disp.audio_device()
        if device:
            # play through this screen's own HDMI port
            player.audio_output_device_set(None, device)
        player.set_xwindow(self.xid)
        em = player.event_manager()
        for etype, name in ((vlc.EventType.MediaPlayerPlaying, "playing"),
                            (vlc.EventType.MediaPlayerESAdded, "es"),
                            (vlc.EventType.MediaPlayerVout, "vout"),
                            (vlc.EventType.MediaPlayerEncounteredError, "error"),
                            (vlc.EventType.MediaPlayerEndReached, "ended")):
            em.event_attach(etype, self._vlc_event, gen, name)
        self.player = player
        self.media = m
        self.resolved = media
        self.launched_at = time.monotonic()
        self.playing_since = None
        self.last_time = None
        self.set_state("connecting", "Connecting (%s)" % media.via)
        player.play()
        self.apply_audio()
        return False

    def _vlc_event(self, _event, gen, name):
        # libVLC thread: never call back into libVLC from here.
        GLib.idle_add(self._on_vlc_event, gen, name)

    def _on_vlc_event(self, gen, name):
        if gen != self.gen or self.dead or self.player is None:
            return False
        if name in ("es", "vout", "playing"):
            self.apply_audio()
        if name == "playing":
            now = time.monotonic()
            self.playing_since = now
            self.last_progress = now
            self.set_state("playing", self.playing_detail())
            self.stack.set_visible_child_name("video")
        elif name == "vout" and self.want_osd:
            self._osd()
        elif name == "error":
            self.schedule_retry("Playback error", "VLC could not play this stream")
        elif name == "ended":
            played = self.playing_since is not None and time.monotonic() - self.playing_since > 5
            self._ended(played)
        return False

    def schedule_retry(self, headline, detail, quick=False):
        if self.dead or self.retry_at is not None:
            return
        now = time.monotonic()
        if quick:
            # a file/VOD that played and finished normally: just loop it
            delay = 1.0
            self.backoff = 3.0
        else:
            if self.playing_since and now - self.playing_since > 60:
                self.backoff = 3.0
            delay = self.backoff
            self.backoff = min(self.backoff * 2, 60.0)
        self.retry_at = now + delay
        self.retry_head, self.retry_detail = headline, detail
        if not quick:
            log.info("screen %d tile %d (%s): %s%s; retrying in %ds", self.disp.index + 1, self.slot + 1,
                     self.title(), headline, (": " + detail) if detail else "", delay)
        self.gen += 1  # ignore late events from the old player
        self._release_player()
        self.set_state("retrying", headline)
        if not quick:
            self.show_retry()

    def _release_player(self):
        player, self.player = self.player, None
        self.media = None
        if player is not None:
            threading.Thread(target=self._stop_and_release, args=(player,),
                             name="vlc-stop", daemon=True).start()

    @staticmethod
    def _stop_and_release(player, done=None):
        try:
            player.stop()
            player.release()
        except Exception:
            log.exception("stopping player")
        if done is not None:
            GLib.idle_add(done)

    def tick(self, now):
        if self.retry_at is not None:
            if now >= self.retry_at:
                self.start()
            elif self.stack.get_visible_child_name() == "msg":
                self.show_retry()
            return
        p = self.player
        if p is None:
            return
        state = p.get_state()
        stall = self.disp.stall_s
        if state == vlc.State.Playing:
            t = p.get_time()
            if t != self.last_time:
                self.last_time = t
                self.last_progress = now
            elif now - self.last_progress > stall:
                self.schedule_retry("Stream stalled", "no new video for %ds" % stall)
                return
            if self.state != "playing":
                self.set_state("playing", self.playing_detail())
        elif state == vlc.State.Error:
            self.schedule_retry("Playback error", "")
            return
        elif state == vlc.State.Ended:
            played = self.playing_since is not None and now - self.playing_since > 5
            self._ended(played)
            return
        elif state in (vlc.State.Opening, vlc.State.Buffering, vlc.State.NothingSpecial):
            if self.playing_since is None and now - self.launched_at > stall:
                self.schedule_retry("Timed out connecting", "")
                return
            if self.playing_since is not None and state == vlc.State.Buffering:
                self.set_state("buffering", "")
        self.apply_audio()

    def on_resize(self):
        if self.retry_at is not None and self.stack.get_visible_child_name() == "msg":
            self.show_retry()

    def apply_audio(self):
        p = self.player
        if p is None:
            return
        want_mute = 0 if self.audible else 1
        if p.audio_get_mute() != want_mute:
            p.audio_set_mute(bool(want_mute))
        if self.audible:
            vol = int(self.disp.volume)
            if p.audio_get_volume() != vol:
                p.audio_set_volume(vol)

    def on_became_audible(self):
        if self.disp.osd:
            self.want_osd = True
            self._osd()

    def _osd(self):
        p = self.player
        if p is None or not self.audible:
            return
        try:
            size = p.video_get_size(0)
        except Exception:
            size = None
        if not size or not size[1]:
            return  # no video yet; retried on the vout event
        self.want_osd = False
        try:
            mq = vlc.VideoMarqueeOption
            p.video_set_marquee_int(mq.Enable, 1)
            p.video_set_marquee_int(mq.Size, max(14, size[1] // 16))
            p.video_set_marquee_int(mq.Position, 5)  # top-left
            p.video_set_marquee_int(mq.X, max(8, size[1] // 40))
            p.video_set_marquee_int(mq.Y, max(8, size[1] // 40))
            p.video_set_marquee_int(mq.Timeout, 2500)
            p.video_set_marquee_int(mq.Color, 0xFFB020)
            p.video_set_marquee_string(mq.Text, "♪ " + self.title())
        except Exception:
            log.debug("marquee unavailable", exc_info=True)

    def destroy(self):
        # Stop VLC before its X window goes away, then drop the widgets.
        self.dead = True
        self.gen += 1
        self.frame.hide()
        player, self.player = self.player, None
        frame = self.frame
        if player is None:
            frame.destroy()
            return
        threading.Thread(target=self._stop_and_release, args=(player, frame.destroy),
                         name="vlc-stop", daemon=True).start()


# ---------------------------------------------------------------------------
class WebTile(Tile):
    kind = "web"

    def __init__(self, disp, slot, stream, key):
        super().__init__(disp, slot, stream, key)
        self.view = None
        self.backoff = 5.0
        self.loaded_at = None
        self.last_reload = time.monotonic()
        self.stack.add_named(self.msg, "msg")
        if WebKit2 is not None:
            view = WebKit2.WebView()
            s = view.get_settings()
            s.set_enable_webgl(True)
            s.set_media_playback_requires_user_gesture(False)
            s.set_javascript_can_open_windows_automatically(False)
            s.set_enable_developer_extras(False)
            if hasattr(view, "set_is_muted"):
                view.set_is_muted(True)
            view.connect("load-changed", self._load_changed)
            view.connect("load-failed", self._load_failed)
            view.connect("web-process-terminated", self._crashed)
            view.connect("context-menu", lambda *a: True)
            view.connect("permission-request", self._deny_permission)
            view.connect("create", lambda *a: None)  # no popups
            self.stack.add_named(view, "video")
            self.view = view
        self.frame.show_all()

    @staticmethod
    def _deny_permission(_view, request):
        request.deny()
        return True

    def page_url(self):
        return self.stream["url"]

    def start(self):
        self.retry_at = None
        if self.view is None:
            self.set_state("error", "WebKitGTK is not installed")
            self.show_message(self.title(), "Web tiles need gir1.2-webkit2-4.1")
            return
        self.apply_zoom()
        self.set_state("connecting", "Loading page")
        self.stack.set_visible_child_name("video")
        self.view.load_uri(self.page_url())
        self.last_reload = time.monotonic()

    def apply_zoom(self):
        if self.view is None:
            return
        zoom = self.stream.get("zoom")
        if not zoom:
            # Aim for a ~900px-tall virtual viewport: big tiles on a 4K
            # screen get scaled up, small tiles stay readable.
            zoom = max(0.75, min(2.0, self.rect[3] / 900.0)) if self.rect[3] > 1 else 1.0
        self.view.set_zoom_level(zoom)

    def on_resize(self):
        self.apply_zoom()
        if self.retry_at is not None:
            self.show_retry()
        elif self.state == "error":
            self.show_message(self.title(), self.detail)

    def _load_changed(self, _view, event):
        if event == WebKit2.LoadEvent.FINISHED and self.retry_at is None:
            self.loaded_at = time.monotonic()
            self.backoff = 5.0
            self.set_state("playing", "Page loaded")
            self.stack.set_visible_child_name("video")

    def _load_failed(self, _view, _event, uri, error):
        if error.matches(WebKit2.network_error_quark(), WebKit2.NetworkError.CANCELLED):
            return True
        self.schedule_retry("Page failed to load", error.message)
        return True

    def _crashed(self, _view, _reason):
        self.schedule_retry("Browser process crashed", "")

    def schedule_retry(self, headline, detail):
        if self.dead or self.retry_at is not None:
            return
        delay = self.backoff
        self.backoff = min(self.backoff * 2, 120.0)
        self.retry_at = time.monotonic() + delay
        self.retry_head, self.retry_detail = headline, detail
        self.set_state("retrying", headline)
        self.show_retry()

    def tick(self, now):
        if self.retry_at is not None:
            if now >= self.retry_at:
                self.start()
            else:
                self.show_retry()
            return
        minutes = self.stream.get("refresh_minutes")
        if minutes and self.view is not None and self.retry_at is None \
                and now - self.last_reload > minutes * 60:
            self.last_reload = now
            self.view.reload()

    def apply_audio(self):
        if self.view is not None and hasattr(self.view, "set_is_muted"):
            self.view.set_is_muted(not self.audible)


class MpvTile(SequenceMixin, Tile):
    """Video tile played by its own mpv process (PCs): VA-API / NVDEC GPU decoding."""

    kind = "video"

    def __init__(self, disp, slot, stream, key):
        super().__init__(disp, slot, stream, key)
        self.area = Gtk.DrawingArea()
        self.area.get_style_context().add_class("wp-video")
        self.area.connect("draw", VideoTile._draw_black)
        self.area.connect_after("realize", self._area_realized)
        self.stack.add_named(self.area, "video")
        self.stack.add_named(self.msg, "msg")
        self.frame.show_all()
        self.stack.set_visible_child_name("video")
        self.xid = None
        self.player = None
        self.pending = None
        self.gen = 0
        self.backoff = 3.0
        self.launched_at = 0.0
        self.playing_since = None
        self.last_pos = None
        self.last_progress = 0.0
        self.resolved = None
        self.extra_audio = None
        self.want_osd = False
        self.cur_mute = True
        self.cur_vol = None
        self._init_sequence()

    def _area_realized(self, area):
        win = area.get_window()
        win.ensure_native()
        self.xid = win.get_xid()
        if self.pending is not None:
            gen, media = self.pending
            self.pending = None
            self._launch(gen, media)

    def start(self):
        self.gen += 1
        gen = self.gen
        self.retry_at = None
        self._release_player()
        self._starting()
        height = self.disp.height_for(self)
        caching = self.disp.caching_ms
        threading.Thread(target=self._resolve_worker, args=(gen, height, caching),
                         name="resolve-%d" % self.slot, daemon=True).start()

    @staticmethod
    def mpv_options(media, caching_ms):
        """Translate our resolved media (VLC-style ':opt=value' list) to mpv."""
        opts = {}
        audio = None
        for o in media.options:
            name, _, value = o.lstrip(":").partition("=")
            if name == "http-user-agent":
                opts["user_agent"] = value
            elif name == "http-referrer":
                opts["referrer"] = value
            elif name == "input-slave":
                audio = value
            elif name == "rtsp-tcp":
                opts["rtsp_transport"] = "tcp"
        secs = max(1.0, caching_ms / 1000.0)
        opts["cache"] = "yes"
        opts["cache_secs"] = "%g" % secs
        opts["demuxer_readahead_secs"] = "%g" % secs
        opts["demuxer_max_bytes"] = "64MiB"
        return opts, audio

    def _launch(self, gen, media):
        if gen != self.gen or self.dead:
            return False
        if self.xid is None:
            self.pending = (gen, media)
            return False
        opts, audio = self.mpv_options(media, self.disp.caching_ms)
        device = self.disp.audio_device()
        if device:
            opts["audio_device"] = device if "/" in device else "pulse/" + device
        opts.update(
            vo="gpu,xv,x11", hwdec="auto-safe", ytdl="no",
            input_default_bindings="no", input_vo_keyboard="no", input_cursor="no",
            osc="no", load_scripts="no", keep_open="no", mute="yes",
            # e.g. a TV switched off takes its HDMI audio device with it:
            # keep showing the picture rather than failing the whole tile
            audio_fallback_to_null="yes",
            volume=str(int(self.disp.volume)), audio_display="no", network_timeout="20",
            osd_font_size="40", osd_color="#FFB020", osd_border_size="2",
            osd_align_x="left", osd_align_y="top")
        self.launched_at = time.monotonic()
        self.set_state("connecting", "Starting mpv")
        threading.Thread(target=self._spawn, args=(gen, media, audio, self.xid, opts),
                         name="mpv-start", daemon=True).start()
        return False

    def _spawn(self, gen, media, audio, xid, opts):
        try:
            proc = mpvproc.MpvProc(xid, opts, lambda msg, gen=gen: self._mpv_event(msg, gen))
        except Exception as exc:
            log.warning("tile %d: mpv failed to start: %s", self.slot + 1, exc)
            GLib.idle_add(self._resolve_failed, gen, "mpv failed to start: %s" % exc)
            return
        GLib.idle_add(self._attach, gen, proc, media, audio)

    def _attach(self, gen, proc, media, audio):
        if gen != self.gen or self.dead:
            threading.Thread(target=proc.terminate, daemon=True).start()
            return False
        self.player = proc
        self.resolved = media
        self.extra_audio = audio
        self.playing_since = None
        self.last_pos = None
        self.cur_mute = True
        self.cur_vol = None
        self.launched_at = time.monotonic()
        self.set_state("connecting", "Connecting (%s, mpv)" % media.via)
        proc.command("loadfile", media.url)
        return False

    def _mpv_event(self, msg, gen):
        # IPC reader thread: hand over to GTK
        name = msg.get("event")
        if name in ("file-loaded", "playback-restart", "end-file", "video-reconfig"):
            GLib.idle_add(self._on_mpv_event, gen, name, msg.get("reason"), msg.get("file_error"))

    def _on_mpv_event(self, gen, name, reason, err):
        if gen != self.gen or self.dead or self.player is None:
            return False
        if name == "file-loaded":
            if self.extra_audio:
                try:
                    self.player.command("audio-add", self.extra_audio, "select")
                except Exception:
                    log.debug("audio-add failed", exc_info=True)
            self.apply_audio()
        elif name == "playback-restart":
            now = time.monotonic()
            if self.playing_since is None:
                self.playing_since = now
            self.last_progress = now
            self.set_state("playing", self.playing_detail())
            self.stack.set_visible_child_name("video")
            self.apply_audio()
        elif name == "video-reconfig" and self.want_osd:
            self._osd()
        elif name == "end-file":
            if reason == "error":
                self.schedule_retry("Playback error", err or "mpv could not play this stream")
            elif reason == "eof":
                played = self.playing_since is not None and time.monotonic() - self.playing_since > 5
                self._ended(played)
        return False

    schedule_retry = VideoTile.schedule_retry

    def _release_player(self, done=None):
        player, self.player = self.player, None
        if player is not None:
            threading.Thread(target=self._terminate, args=(player, done), name="mpv-stop",
                             daemon=True).start()
        elif done is not None:
            done()

    @staticmethod
    def _terminate(player, done=None):
        try:
            player.terminate()
        except Exception:
            log.exception("stopping mpv")
        if done is not None:
            GLib.idle_add(done)

    def tick(self, now):
        if self.retry_at is not None:
            if now >= self.retry_at:
                self.start()
            elif self.stack.get_visible_child_name() == "msg":
                self.show_retry()
            return
        p = self.player
        if p is None:
            return
        stall = self.disp.stall_s
        pos = p.time_pos
        buffering = p.paused_for_cache
        if self.playing_since is None:
            if now - self.launched_at > stall:
                self.schedule_retry("Timed out connecting", "")
            return
        if pos is not None and pos != self.last_pos:
            self.last_pos = pos
            self.last_progress = now
            if self.state != "playing":
                self.set_state("playing", self.playing_detail())
        elif now - self.last_progress > stall:
            self.schedule_retry("Stream stalled", "no new video for %ds" % stall)
            return
        elif buffering:
            self.set_state("buffering", "")
        self.apply_audio()

    def apply_audio(self):
        p = self.player
        if p is None:
            return
        want_mute = not self.audible
        if self.cur_mute != want_mute:
            p.set("mute", want_mute)
            self.cur_mute = want_mute
        if self.audible:
            vol = int(self.disp.volume)
            if self.cur_vol != vol:
                p.set("volume", vol)
                self.cur_vol = vol

    def on_resize(self):
        if self.retry_at is not None and self.stack.get_visible_child_name() == "msg":
            self.show_retry()

    def on_became_audible(self):
        if self.disp.osd:
            self.want_osd = True
            self._osd()

    def _osd(self):
        p = self.player
        if p is None or not self.audible or self.playing_since is None:
            return
        self.want_osd = False
        p.command("show-text", "♪ " + self.title(), 2500)

    def destroy(self):
        self.dead = True
        self.gen += 1
        self.frame.hide()
        frame = self.frame
        self._release_player(done=frame.destroy)


class AdsbTile(WebTile):
    """Text aircraft table, served by our own web UI on this machine."""
    kind = "adsb"

    def page_url(self):
        port = getattr(self.disp.ctl, "web_port", None) or self.disp.ctl.cfg["web"]["port"]
        return "http://127.0.0.1:%d/tile/adsb/%s" % (port, self.stream["id"])


# ---------------------------------------------------------------------------
_HDMI_RE = re.compile(r"HDMI-(?:A-)?(\d+)", re.I)
_CONNECTOR_RE = re.compile(r"^([A-Za-z]+)(?:-[A-Za-z])*-?(\d+)$")
_CONNECTOR_RANK = {"hdmi": 0, "dp": 1, "displayport": 1, "dvi": 2, "vga": 3, "edp": 4, "lvds": 4}


def _output_number(name):
    """HDMI-1 -> 0, HDMI-A-2 -> 1; None for anything else."""
    m = _HDMI_RE.search(name or "")
    return int(m.group(1)) - 1 if m else None


def _output_order(name):
    """Stable screen order: HDMI ports first, then DisplayPort, each by number
    (HDMI-1, HDMI-A-0, DP-1, DisplayPort-0, ...). Unknown names go last."""
    m = _CONNECTOR_RE.match(name or "")
    if not m:
        return (9, 0, name or "")
    return (_CONNECTOR_RANK.get(m.group(1).lower(), 8), int(m.group(2)), name)


class FakeMonitor:
    """A monitor given on the command line (--monitors), for testing layouts
    of two screens on a single desktop or virtual display."""

    def __init__(self, index, spec):
        m = re.fullmatch(r"(\d+)x(\d+)\+(\d+)\+(\d+)", spec.strip())
        if not m:
            raise ValueError("monitor must look like 1920x1080+0+0, got %r" % spec)
        w, h, x, y = (int(v) for v in m.groups())
        self._geo = Gdk.Rectangle()
        self._geo.x, self._geo.y, self._geo.width, self._geo.height = x, y, w, h
        self._model = "HDMI-%d" % (index + 1)

    def get_model(self):
        return self._model

    def get_geometry(self):
        return self._geo


def parse_monitors(text):
    return [FakeMonitor(i, part) for i, part in enumerate(text.split(",")) if part.strip()]


class ScreenWin:
    """One fullscreen window on one monitor, holding that screen's tiles."""

    def __init__(self, app, index, monitor=None):
        self.app = app
        self.ctl = app.ctl
        self.index = index
        self.output = None
        self.tiles = {}
        self.size = (0, 0)
        self.dead = False
        self.alerts = overlay.AlertOverlay(index)

        self.win = Gtk.Window(title="WindoPane %d" % (index + 1))
        self.win.set_name("wp-root")
        self.win.set_decorated(False)
        self.win.set_skip_taskbar_hint(True)
        self.fixed = Gtk.Fixed()
        self.fixed.set_name("wp-fixed")
        self.win.add(self.fixed)
        self.win.connect("configure-event", self._on_configure)
        self.win.connect("realize", self._hide_cursor)
        if not app.input_active:
            self.win.connect("key-press-event", app.on_key)
        if app.windowed:
            self.win.set_default_size(*app.windowed)
            self.win.connect("destroy", lambda *_: Gtk.main_quit())
        else:
            self.set_monitor(monitor)
        self.win.show_all()

    # -- what tiles ask of their screen
    @property
    def vlc(self):
        return self.app.vlc

    @property
    def volume(self):
        return self.app.volume

    @property
    def osd(self):
        return self.app.osd

    @property
    def caching_ms(self):
        return self.app.caching_ms

    @property
    def stall_s(self):
        return self.app.stall_s

    def web_hint(self):
        return self.app.web_hint()

    def key_number(self, slot):
        """Tiles are numbered 1-N on each screen, matching the number keys."""
        return slot + 1

    def height_for(self, tile):
        return self.app.height_for(tile, self)

    def audio_device(self):
        return self.app.device_for(self)

    # -- window
    def set_monitor(self, monitor):
        if monitor is None:
            return
        self.output = monitor.get_model()
        geo = monitor.get_geometry()
        # No window manager on the Pi: a fullscreen request alone does
        # nothing, so place and size the window on its monitor directly.
        self.win.set_default_size(geo.width, geo.height)
        self.win.move(geo.x, geo.y)
        self.win.resize(geo.width, geo.height)

    @staticmethod
    def _hide_cursor(win):
        disp = win.get_display()
        win.get_window().set_cursor(Gdk.Cursor.new_for_display(disp, Gdk.CursorType.BLANK_CURSOR))

    def _on_configure(self, _win, event):
        size = (event.width, event.height)
        if size != self.size:
            self.size = size
            self.app.request_sync()
        return False

    # -- sync with controller
    def _border(self, disp_cfg, height):
        b = disp_cfg.get("border", "auto")
        if b in ("auto", None, ""):
            return max(2, height // 360)
        return int(b)

    def wanted(self, snap):
        """slot -> (kind, stream, key) for this screen."""
        cfg = snap["config"]
        sc = cfg["screens"][self.index]
        streams = {s["id"]: s for s in cfg["streams"]}
        device = self.audio_device()
        out = {}
        slots = snap["effective"][self.index]  # assigned slots + live takeovers
        for slot in range(layouts.tile_count(sc["layout"])):
            stream = streams.get(slots[slot])
            if stream is None:
                out[slot] = ("empty", None, None)
                continue
            kind = resolver.classify(stream)
            token = snap["reload_tokens"][self.index * layouts.MAX_SLOTS + slot]
            key = (kind, json.dumps(stream, sort_keys=True), token,
                   (device, self.app.engine) if kind == "video" else None)
            out[slot] = (kind, stream, key)
        return out

    def sync(self, snap, wanted):
        width, height = self.size
        if width < 2 or height < 2 or self.dead:
            return
        cfg = snap["config"]
        disp_cfg = cfg["display"]
        lay = cfg["screens"][self.index]["layout"]
        rects = layouts.compute_rects(lay, width, height, int(disp_cfg.get("gap", 0)))
        zoom = snap["zoom"][self.index]
        border = self._border(disp_cfg, height)
        audio = snap["audio_tile"]

        for slot in list(self.tiles):
            tile = self.tiles[slot]
            want = wanted.get(slot)
            if want is None or tile.kind != want[0] or tile.key != want[2]:
                tile.destroy()
                del self.tiles[slot]

        cls = {"empty": EmptyTile, "video": self.app.video_tile_class(), "web": WebTile, "adsb": AdsbTile}
        for slot, (kind, stream, key) in wanted.items():
            tile = self.tiles.get(slot)
            fresh = tile is None
            if fresh:
                tile = cls[kind](self, slot, stream, key)
                self.fixed.put(tile.frame, 0, 0)
                self.tiles[slot] = tile
            if zoom is None:
                tile.place(*rects[slot], border, True)
            elif slot == zoom:
                tile.place(0, 0, width, height, border, True)
            else:
                tile.place(*tile.rect, border, False)
            token = snap["skip_tokens"][self.index * layouts.MAX_SLOTS + slot]
            if fresh:
                tile.skip_token = token
                tile.start()
            elif getattr(tile, "skip_token", token) != token:
                step = 1 if token > tile.skip_token else -1
                tile.skip_token = token
                tile.skip(step)
            tile.set_audible(audio == self.index * layouts.MAX_SLOTS + slot)

        on_top = [zoom] if zoom is not None else layouts.LAYOUTS[lay].get("overlay", [])
        for slot in on_top:
            if slot in self.tiles:
                self.tiles[slot].raise_()

    def tick(self, now):
        for tile in list(self.tiles.values()):
            if not tile.dead:
                try:
                    tile.tick(now)
                except Exception:
                    log.exception("screen %d tile %d tick failed", self.index + 1, tile.slot + 1)

    def update_alerts(self, alerts, settings):
        gw = self.win.get_window()
        if self.dead or gw is None or self.size[0] < 2:
            return
        origin = gw.get_origin()
        try:
            self.alerts.update(alerts, settings, (origin[-2], origin[-1]) + self.size)
        except Exception:
            log.exception("screen %d alerts", self.index + 1)

    def report(self):
        base = self.index * layouts.MAX_SLOTS
        for slot in range(layouts.MAX_SLOTS):
            tile = self.tiles.get(slot)
            self.ctl.report_status(base + slot, tile.status() if tile else None)

    def destroy(self):
        """Monitor unplugged or screen disabled: stop its streams, drop the window."""
        self.dead = True
        self.alerts.destroy()
        for tile in self.tiles.values():
            tile.destroy()
        self.tiles.clear()
        self.report()
        self.win.hide()
        # give libVLC time to let go of the X windows before they disappear
        GLib.timeout_add_seconds(5, lambda: (self.win.destroy(), False)[1])


def parse_xrandr(text):
    """Connected outputs (screen order), stale outputs, modes and the
    largest desktop the GPU allows, from `xrandr --query`."""
    max_size = None
    m = re.search(r"maximum (\d+) x (\d+)", text)
    if m:
        max_size = (int(m.group(1)), int(m.group(2)))
    connected, stale, modes = [], [], {}
    current = None
    for line in text.splitlines():
        if not line.startswith(" "):
            parts = line.split()
            current = None
            if len(parts) >= 2 and parts[1] == "connected":
                connected.append(parts[0])
                current = parts[0]
                modes[current] = {"preferred": None, "first": None}
            elif len(parts) >= 2 and parts[1] == "disconnected" and re.search(r"\d+x\d+\+\d+\+\d+", line):
                stale.append(parts[0])  # unplugged but still holding desktop space
            continue
        if current is None:
            continue
        mm = re.match(r"\s+(\d+)x(\d+)i?\s+(.*)$", line)
        if not mm:
            continue
        size = (int(mm.group(1)), int(mm.group(2)))
        if modes[current]["first"] is None:
            modes[current]["first"] = size
        if "+" in mm.group(3) and modes[current]["preferred"] is None:
            modes[current]["preferred"] = size
    connected.sort(key=_output_order)
    return connected, stale, modes, max_size


def arrange(sizes, how="auto", max_size=None):
    """Top-left positions for screens of the given sizes.

    "row" puts them side by side; "grid" makes two columns (screens 1 2 on
    top, 3 4 below); "auto" uses a row when it fits the GPU's maximum
    desktop size and a grid when it doesn't. Four 4K screens fit in a row
    on the common 16384-pixel limit; on an 8192 limit they become 2 x 2."""
    n = len(sizes)
    if n == 0:
        return []

    def row():
        x, out = 0, []
        for w, _h in sizes:
            out.append((x, 0))
            x += w
        return out, (x, max(h for _w, h in sizes))

    def grid(cols=2):
        rows = (n + cols - 1) // cols
        col_w = [max((sizes[i][0] for i in range(c, n, cols)), default=0) for c in range(cols)]
        row_h = [max(sizes[i][1] for i in range(r * cols, min(n, (r + 1) * cols))) for r in range(rows)]
        out = []
        for i in range(n):
            r, c = divmod(i, cols)
            out.append((sum(col_w[:c]), sum(row_h[:r])))
        return out, (sum(col_w), sum(row_h))

    if how == "grid" and n > 2:
        return grid()[0]
    positions, total = row()
    if how == "auto" and max_size and n > 2 and (total[0] > max_size[0] or total[1] > max_size[1]):
        return grid()[0]
    return positions


class OutputManager(threading.Thread):
    """Keeps every connected output on (extended, never mirrored) with each
    screen's chosen mode, arranged in a row or grid, and notices monitors
    being plugged in."""

    def __init__(self, ctl):
        super().__init__(name="outputs", daemon=True)
        self.ctl = ctl
        self.applied = None
        self.kick = threading.Event()

    def run(self):
        while True:
            try:
                self.check()
            except Exception:
                log.exception("output check failed")
            self.kick.wait(5)
            self.kick.clear()

    @staticmethod
    def query():
        out = subprocess.run(["xrandr", "--query"], capture_output=True, text=True,
                             timeout=10, check=False).stdout
        return parse_xrandr(out)

    def plan(self, connected, modes, max_size):
        screens = self.ctl.cfg["screens"]
        how = self.ctl.cfg["display"].get("arrangement", "auto")
        items, sizes = [], []
        for i, name in enumerate(connected):
            res = screens[i]["resolution"] if i < len(screens) else "auto"
            size = None
            if res != "auto":
                w, _, h = res.partition("x")
                if w.isdigit() and h.isdigit():
                    size = (int(w), int(h))
            if size is None:
                size = modes.get(name, {}).get("preferred") or modes.get(name, {}).get("first") or (1920, 1080)
            items.append((name, res))
            sizes.append(size)
        return items, arrange(sizes, how, max_size)

    def check(self):
        try:
            connected, stale, modes, max_size = self.query()
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("xrandr unavailable: %s", exc)
            self.kick.wait(55)
            return
        items, positions = self.plan(connected, modes, max_size)
        want = (tuple(items), tuple(positions), tuple(stale))
        if want == self.applied:
            return
        cmd = ["xrandr"]
        for (name, res), (x, y) in zip(items, positions):
            cmd += ["--output", name] + (["--auto"] if res == "auto" else ["--mode", res])
            cmd += ["--pos", "%dx%d" % (x, y)]
        for name in stale:
            cmd += ["--output", name, "--off"]
        if len(cmd) == 1:
            self.applied = want
            return
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
        if result.returncode != 0:
            log.warning("%s failed: %s", " ".join(cmd), result.stderr.strip())
        else:
            log.info("outputs: %s", ", ".join("%s=%s@%dx%d" % (n, r, x, y)
                                              for (n, r), (x, y) in zip(items, positions)) or "none")
        self.applied = want


class App:
    """Owns libVLC, the screens (one window per monitor) and the tick loop."""

    def __init__(self, ctl, windowed=None, input_active=False, monitors=None, manage_outputs=True):
        self.ctl = ctl
        self.windowed = windowed  # (w, h): single test window instead of real monitors
        self.fake_monitors = monitors  # list of FakeMonitor from --monitors
        self.input_active = input_active
        self.profile = sysinfo.profile()
        self.engine = None
        self._pick_engine()
        self.volume = ctl.cfg["volume"]
        self.osd = True
        self.caching_ms = 2000
        self.stall_s = 45
        self.video_tiles = 0
        self.screens = {}
        self.sources = {}
        self.audio_devices = []
        self._sync_queued = False
        self._monitors_queued = False
        self._lan = None
        self._lan_checked = 0.0

        args = ["--no-video-title-show", "--no-osd", "--no-snapshot-preview",
                "--no-stats", "--no-lua", "--quiet",
                "--network-caching=%d" % ctl.cfg["player"]["network_caching_ms"]]
        args += list(ctl.cfg["player"].get("extra_vlc_args") or [])
        self.vlc = vlc.Instance(args)
        if self.vlc is None:
            raise RuntimeError("libVLC failed to start (check extra_vlc_args)")

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.outputs = None
        if not windowed and not monitors:
            gscreen = Gdk.Screen.get_default()
            gscreen.connect("monitors-changed", lambda *_: self.monitors_changed_soon())
            gscreen.connect("size-changed", lambda *_: self.monitors_changed_soon())
            if manage_outputs:
                self.outputs = OutputManager(ctl)
                self.outputs.start()

        self._poll_audio_devices()
        self._refresh_monitors()
        ctl.subscribe(self.request_sync)
        GLib.timeout_add(1000, self._tick)
        GLib.timeout_add_seconds(30, self._poll_audio_devices)

    # -- video engine
    def _pick_engine(self):
        """VLC on a Pi, mpv on PCs (unless the Settings say otherwise)."""
        setting = self.ctl.cfg["player"].get("engine", "auto")
        engine = "vlc"
        if setting == "mpv" or (setting == "auto" and not self.profile["id"].startswith("pi")):
            if mpvproc.available():
                engine = "mpv"
            elif setting == "mpv":
                log.warning("mpv requested but the mpv program is not installed; using VLC")
        if engine != self.engine:
            log.info("video engine: %s", engine)
        self.engine = engine
        self.ctl.engine = engine

    def source_for(self, screen, slot, stream):
        """The playlist/live state for a stream on a tile, kept across tile rebuilds."""
        key = (screen, slot, stream.get("id"))
        src = self.sources.get(key)
        if src is None or src.signature != sources.Source.signature_of(stream):
            src = sources.Source(stream)
            self.sources[key] = src
        return src

    def video_tile_class(self):
        return MpvTile if self.engine == "mpv" else VideoTile

    # -- monitors -> screens
    def _monitor_list(self):
        if self.fake_monitors:
            return self.fake_monitors[: config.MAX_SCREENS]
        gdisp = Gdk.Display.get_default()
        mons, seen = [], set()
        for i in range(gdisp.get_n_monitors()):
            m = gdisp.get_monitor(i)
            g = m.get_geometry()
            geo = (g.x, g.y, g.width, g.height)
            if geo in seen:  # mirrored outputs count once
                continue
            seen.add(geo)
            mons.append(m)

        def order(m):
            g = m.get_geometry()
            return (_output_order(m.get_model()), g.x, g.y)
        return sorted(mons, key=order)[: config.MAX_SCREENS]

    def monitors_changed_soon(self):
        if not self._monitors_queued:
            self._monitors_queued = True
            GLib.timeout_add(300, self._refresh_monitors)

    def _refresh_monitors(self):
        self._monitors_queued = False
        if self.windowed:
            mons = [None]
            info = [{"output": "window", "width": self.windowed[0], "height": self.windowed[1]}]
        else:
            mons = self._monitor_list()
            info = []
            for m in mons:
                g = m.get_geometry()
                info.append({"output": m.get_model() or "", "width": g.width, "height": g.height})
        self.ctl.report_monitors(info)
        screens_cfg = self.ctl.cfg["screens"]
        for i in range(config.MAX_SCREENS):
            want = i < len(mons) and screens_cfg[i]["enabled"]
            sw = self.screens.get(i)
            if want and sw is None:
                log.info("screen %d on %s", i + 1, info[i]["output"])
                self.screens[i] = ScreenWin(self, i, mons[i])
            elif want:
                sw.set_monitor(mons[i])
            elif sw is not None:
                log.info("screen %d off", i + 1)
                sw.destroy()
                del self.screens[i]
        self.request_sync()
        return False

    # -- audio outputs
    def _poll_audio_devices(self):
        devices = []
        try:
            probe = self.vlc.media_player_new()
            head = probe.audio_output_device_enum()
            node = head
            while node:
                d = node.contents
                dev_id = (d.device or b"").decode("utf-8", "replace")
                desc = (d.description or b"").decode("utf-8", "replace")
                if dev_id:
                    devices.append({"id": dev_id, "name": desc or dev_id})
                node = d.next
            if head:
                vlc.libvlc_audio_output_device_list_release(head)
            probe.release()
        except Exception:
            log.debug("audio device enumeration failed", exc_info=True)
        if devices != self.audio_devices:
            self.audio_devices = devices
            self.ctl.audio_devices = devices
            log.info("audio outputs: %s", ", ".join(d["id"] for d in devices) or "default only")
            self.request_sync()
        return True

    def device_for(self, screen):
        """Audio output for a screen's video tiles (None = system default)."""
        setting = self.ctl.cfg["screens"][screen.index].get("audio_device", "auto")
        ids = [d["id"] for d in self.audio_devices]
        if setting == "default":
            return None
        if setting != "auto":
            return setting if setting in ids else None
        if not self.profile["id"].startswith("pi"):
            # Per-port audio is only known to line up on the Pi's two HDMI
            # ports; elsewhere pick a device per screen in Settings.
            return None
        hdmi = sorted(d["id"] for d in self.audio_devices
                      if "hdmi" in (d["id"] + " " + d["name"]).lower())
        if len(hdmi) < 2:
            return None  # one HDMI audio device: the default already is it
        n = _output_number(screen.output)
        n = screen.index if n is None else n
        return hdmi[n] if n < len(hdmi) else None

    # -- shared helpers for tiles
    def web_hint(self):
        now = time.monotonic()
        if now - self._lan_checked > 30:
            self._lan_checked = now
            self._lan = _lan_address()
        port = self.ctl.cfg["web"]["port"]
        host = self._lan or sysinfo.hostname() + ".local"
        return "http://%s:%d" % (host, port)

    def height_for(self, tile, screen):
        return sysinfo.pick_height(
            tile.rect[3] if tile.rect[3] > 1 else (screen.size[1] or 1080) // 2,
            max(1, self.video_tiles), self.profile,
            self.ctl.cfg["display"].get("quality_cap", "auto"),
            (tile.stream or {}).get("max_height"))

    def on_key(self, _win, event):
        name = Gdk.keyval_name(event.keyval) or ""
        if self.windowed and name == "Escape":
            Gtk.main_quit()
            return True
        return inputs.handle_gdk_key(self.ctl, name)

    # -- sync / tick
    def request_sync(self):
        if not self._sync_queued:
            self._sync_queued = True
            GLib.idle_add(self._sync)

    def _sync(self):
        self._sync_queued = False
        if self.outputs is not None:
            self.outputs.kick.set()
        snap = self.ctl.snapshot()
        cfg = snap["config"]
        # a screen enabled/disabled in settings
        for i in range(config.MAX_SCREENS):
            if (i in self.screens) != (cfg["screens"][i]["enabled"] and self._has_monitor(i)):
                self.monitors_changed_soon()
                break
        self._pick_engine()
        self.volume = cfg["volume"]
        self.osd = bool(cfg["display"].get("osd", True))
        self.caching_ms = int(cfg["player"]["network_caching_ms"])
        self.stall_s = int(cfg["player"]["stall_timeout_s"])
        wanted = {i: sw.wanted(snap) for i, sw in self.screens.items()}
        # the decoder budget is shared by every screen
        self.video_tiles = sum(1 for w in wanted.values() for k, _s, _ in w.values() if k == "video")
        for i, sw in self.screens.items():
            sw.sync(snap, wanted[i])
        self._update_alerts()
        self._report()
        return False

    def _update_alerts(self):
        active = self.ctl.active_alerts()
        settings = self.ctl.cfg["alerts"]
        for sw in self.screens.values():
            sw.update_alerts(active, settings)

    def _has_monitor(self, i):
        mons = self.ctl.monitors
        return mons is not None and i < len(mons)

    def _tick(self):
        now = time.monotonic()
        for sw in list(self.screens.values()):
            sw.tick(now)
        self._update_alerts()
        self._report()
        return True

    def shutdown(self):
        """Stop every player (mpv runs as separate processes) before exiting."""
        for sw in list(self.screens.values()):
            sw.alerts.destroy()
            for tile in list(sw.tiles.values()):
                try:
                    tile.destroy()
                except Exception:
                    log.debug("tile cleanup", exc_info=True)
            sw.tiles.clear()

    def _report(self):
        for i in range(config.MAX_SCREENS):
            sw = self.screens.get(i)
            if sw is not None:
                sw.report()
            else:
                for slot in range(layouts.MAX_SLOTS):
                    self.ctl.report_status(i * layouts.MAX_SLOTS + slot, None)

    def run(self):
        Gtk.main()

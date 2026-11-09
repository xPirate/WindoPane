"""Headline alerts drawn over a screen: pop-up cards and a scrolling ticker.

Both are small override-redirect windows stacked above the screen's
window, so they sit on top of the video tiles (which draw into native
child windows). No compositor is assumed: everything is opaque.
"""

import math
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango, PangoCairo  # noqa: E402

BG = (0.082, 0.106, 0.141)        # #151b24
BG_TICKER = (0.055, 0.071, 0.094)  # #0e1218
AMBER = (1.0, 0.69, 0.125)        # #ffb020
RED = (0.94, 0.33, 0.31)
TEXT = (0.93, 0.95, 0.97)
MUTED = (0.55, 0.61, 0.68)
TRACK = (0.17, 0.21, 0.27)

SPEEDS = {"slow": 0.075, "normal": 0.115, "fast": 0.17}  # screen heights per second
FRAME_MS = 33


def _font(px, bold=False, family="Sans"):
    fd = Pango.FontDescription.from_string(family + (" Bold" if bold else ""))
    fd.set_absolute_size(max(6, px) * Pango.SCALE)
    return fd


def _blank_cursor(win):
    gw = win.get_window()
    if gw is not None:
        gw.set_cursor(Gdk.Cursor.new_for_display(gw.get_display(), Gdk.CursorType.BLANK_CURSOR))


def _clock(epoch):
    return time.strftime("%H:%M", time.localtime(epoch)) if epoch else ""


def _popup_window():
    win = Gtk.Window(type=Gtk.WindowType.POPUP)
    win.set_app_paintable(True)
    win.set_accept_focus(False)
    win.connect("realize", _blank_cursor)
    area = Gtk.DrawingArea()
    win.add(area)
    return win, area


class AlertCard:
    """One pop-up headline."""

    def __init__(self, alert):
        self.alert = alert
        self.win, self.area = _popup_window()
        self.area.connect("draw", self._draw)
        self.size = (0, 0)
        self.scale = 1.0
        self.pos = None
        self._layouts = None

    def measure(self, scale, width):
        """Card height for this width (wraps the headline to at most 3 lines)."""
        self.scale = scale
        ctx = self.area.get_pango_context()
        pad = round(18 * scale)
        bar = round(7 * scale)
        inner = width - bar - 2 * pad
        head = Pango.Layout.new(ctx)
        head.set_font_description(_font(15 * scale, True))
        label = (self.alert["feed"] or "Alert").upper()
        if self.alert.get("test"):
            label += "  ·  TEST"
        when = _clock(self.alert.get("published") or self.alert["created"])
        head.set_text(label + ("  ·  " + when if when else ""), -1)
        head.set_width(inner * Pango.SCALE)
        head.set_ellipsize(Pango.EllipsizeMode.END)
        body = Pango.Layout.new(ctx)
        body.set_font_description(_font(25 * scale, True))
        body.set_text(self.alert["title"], -1)
        body.set_width(inner * Pango.SCALE)
        body.set_wrap(Pango.WrapMode.WORD_CHAR)
        body.set_height(-3)  # at most three lines
        body.set_ellipsize(Pango.EllipsizeMode.END)
        hh = head.get_pixel_size()[1]
        bh = body.get_pixel_size()[1]
        gap = round(8 * scale)
        prog = round(4 * scale)
        height = pad + hh + gap + bh + pad + prog
        self._layouts = (head, body, pad, bar, gap, prog)
        return height

    def place(self, x, y, w, h):
        if self.size != (w, h):
            self.size = (w, h)
            self.area.set_size_request(w, h)
            self.win.resize(w, h)
        if self.pos != (x, y):
            self.pos = (x, y)
            self.win.move(x, y)
        if not self.win.get_visible():
            self.win.show_all()
        self.area.queue_draw()

    def raise_(self):
        gw = self.win.get_window()
        if gw is not None:
            gw.raise_()

    def _draw(self, _area, cr):
        if not self._layouts:
            return False
        w, h = self.size
        head, body, pad, bar, gap, prog = self._layouts
        accent = MUTED if self.alert.get("test") else AMBER
        cr.set_source_rgb(*BG)
        cr.paint()
        cr.set_source_rgb(*accent)
        cr.rectangle(0, 0, bar, h)
        cr.fill()
        cr.set_source_rgb(*accent)
        cr.move_to(bar + pad, pad)
        PangoCairo.show_layout(cr, head)
        cr.set_source_rgb(*TEXT)
        cr.move_to(bar + pad, pad + head.get_pixel_size()[1] + gap)
        PangoCairo.show_layout(cr, body)
        # time left
        cr.set_source_rgb(*TRACK)
        cr.rectangle(bar, h - prog, w - bar, prog)
        cr.fill()
        exp, made = self.alert.get("expires"), self.alert["created"]
        frac = 1.0 if not exp else max(0.0, min(1.0, (exp - time.time()) / max(1, exp - made)))
        cr.set_source_rgb(*accent)
        cr.rectangle(bar, h - prog, (w - bar) * frac, prog)
        cr.fill()
        return False

    def destroy(self):
        self.win.destroy()


class Ticker:
    """A news-style strip that scrolls every ticker alert for a screen."""

    def __init__(self):
        self.win, self.area = _popup_window()
        self.area.connect("draw", self._draw)
        self.items = []
        self.size = (0, 0)
        self.pos = None
        self.layout = None
        self.label = None
        self.text_w = 1
        self.speed = 100.0
        self.t0 = time.monotonic()
        self.timer = None
        self.scale = 1.0

    @staticmethod
    def height_for(screen_h):
        return max(34, round(screen_h * 0.056))

    def set_items(self, items, scale, speed):
        """items: [(feed, title)]. Rebuilds the text only when it changed."""
        self.speed = speed
        if items == self.items and self.layout is not None and scale == self.scale:
            return
        old_w = self.text_w
        offset = self._offset()
        self.items = list(items)
        self.scale = scale
        ctx = self.area.get_pango_context()
        px = round(24 * scale)
        lay = Pango.Layout.new(ctx)
        lay.set_font_description(_font(px))
        parts = []
        for feed, title in self.items:
            parts.append('<span foreground="#ffb020" weight="bold">%s</span>   %s' % (
                GLib.markup_escape_text(feed.upper()), GLib.markup_escape_text(title)))
        sep = '      <span foreground="#5b6573">◆</span>      '
        lay.set_markup(sep.join(parts) + sep, -1)
        self.layout = lay
        self.text_w = max(1, lay.get_pixel_size()[0])
        label = Pango.Layout.new(ctx)
        label.set_font_description(_font(px * 0.82, True))
        label.set_text("ALERTS" if len(self.items) != 1 else "ALERT", -1)
        self.label = label
        # keep the scroll position steady across a content change
        if old_w > 1 and self.text_w != old_w:
            self.t0 = time.monotonic() - (offset % self.text_w) / max(1.0, self.speed)

    def _offset(self):
        return (time.monotonic() - self.t0) * self.speed

    def place(self, x, y, w, h):
        if self.size != (w, h):
            self.size = (w, h)
            self.area.set_size_request(w, h)
            self.win.resize(w, h)
        if self.pos != (x, y):
            self.pos = (x, y)
            self.win.move(x, y)
        if not self.win.get_visible():
            self.t0 = time.monotonic()
            self.win.show_all()
        if self.timer is None:
            self.timer = GLib.timeout_add(FRAME_MS, self._frame)

    def raise_(self):
        gw = self.win.get_window()
        if gw is not None:
            gw.raise_()

    def hide(self):
        if self.timer is not None:
            GLib.source_remove(self.timer)
            self.timer = None
        if self.win.get_visible():
            self.win.hide()
        self.items = []

    def _frame(self):
        self.area.queue_draw()
        return True

    def _draw(self, _area, cr):
        if self.layout is None:
            return False
        w, h = self.size
        cr.set_source_rgb(*BG_TICKER)
        cr.paint()
        cr.set_source_rgb(*AMBER)
        cr.rectangle(0, 0, w, max(2, round(2 * self.scale)))
        cr.fill()
        lw, lh = self.label.get_pixel_size()
        pad = round(16 * self.scale)
        box_w = lw + 2 * pad
        # scrolling text, clipped to the right of the label box
        th = self.layout.get_pixel_size()[1]
        y = (h - th) / 2
        cr.save()
        cr.rectangle(box_w, 0, w - box_w, h)
        cr.clip()
        x = box_w + pad - math.fmod(self._offset(), self.text_w)
        cr.set_source_rgb(*TEXT)
        while x < w:
            cr.move_to(x, y)
            PangoCairo.show_layout(cr, self.layout)
            x += self.text_w
        cr.restore()
        cr.set_source_rgb(*AMBER)
        cr.rectangle(0, 0, box_w, h)
        cr.fill()
        cr.set_source_rgb(*BG_TICKER)
        cr.move_to(pad, (h - lh) / 2)
        PangoCairo.show_layout(cr, self.label)
        return False

    def destroy(self):
        self.hide()
        self.win.destroy()


class AlertOverlay:
    """Everything alert-related on one screen."""

    def __init__(self, index):
        self.index = index
        self.cards = {}       # alert id -> AlertCard
        self.ticker = None

    @staticmethod
    def _for_screen(alert, index):
        scr = alert.get("screens", "all")
        return scr == "all" or index in (scr or [])

    def update(self, alerts, settings, geo):
        """alerts: active alerts (oldest first); geo: (x, y, w, h) of the screen."""
        x0, y0, sw, sh = geo
        if sw < 50 or sh < 50:
            return
        scale = sh / 1080.0
        mine = [a for a in alerts if self._for_screen(a, self.index)]

        # ticker
        tick = [(a["feed"], a["title"]) for a in mine if a["style"] in ("ticker", "both")]
        th = 0
        ticker_top = settings.get("ticker_position") == "top"
        if tick:
            if self.ticker is None:
                self.ticker = Ticker()
            th = Ticker.height_for(sh)
            speed = SPEEDS.get(settings.get("ticker_speed"), SPEEDS["normal"]) * sh
            self.ticker.set_items(list(reversed(tick)), scale, speed)  # newest first
            self.ticker.place(x0, y0 if ticker_top else y0 + sh - th, sw, th)
            self.ticker.raise_()
        elif self.ticker is not None:
            self.ticker.hide()

        # pop-up cards, newest on top of the stack
        limit = max(1, int(settings.get("max_popups") or 3))
        pops = [a for a in mine if a["style"] in ("popup", "both")][-limit:][::-1]
        keep = {a["id"] for a in pops}
        for aid in list(self.cards):
            if aid not in keep:
                self.cards.pop(aid).destroy()
        pos = settings.get("popup_position") or "top-right"
        margin = round(28 * scale)
        gap = round(12 * scale)
        width = int(min(sw - 2 * margin, max(360, sw * 0.30)))
        bottom = pos.startswith("bottom")
        y = margin + (th if ticker_top == (not bottom) and th else 0)
        for a in pops:
            card = self.cards.get(a["id"])
            if card is None:
                card = self.cards[a["id"]] = AlertCard(a)
            card.alert = a
            h = card.measure(scale, width)
            x = x0 + (sw - margin - width if pos.endswith("right") else margin)
            top = y0 + (sh - y - h if bottom else y)
            card.place(int(x), int(top), width, int(h))
            card.raise_()
            y += h + gap

    def destroy(self):
        for card in self.cards.values():
            card.destroy()
        self.cards.clear()
        if self.ticker is not None:
            self.ticker.destroy()
            self.ticker = None

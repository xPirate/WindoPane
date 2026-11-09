#!/usr/bin/python3
"""windopane-oled: show what the wall is playing on a small SSD1306 OLED.

Made for the 0.96" 128x64 OLED in Freenove's Raspberry Pi 5 cases (FNK0100,
FNK0107), and works with any SSD1306 128x64 on I2C.

The "primary" video is the tile you are listening to. When everything is
muted it is tile 1 of the focused screen. Its name and title scroll across
the middle of the display; the top line shows which tile it is and the
time, the bottom line its state, the volume and the CPU temperature.

    windopane-oled                      run (reads /var/lib/windopane/config.json)
    windopane-oled --rotate 180         force orientation (default: detect the case)
    windopane-oled --preview DIR        render PNGs instead of driving the OLED
"""

import argparse
import base64
import json
import os
import re
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 128, 64
CONFIG = "/var/lib/windopane/config.json"
FONT_DIR = "/usr/share/fonts/truetype/dejavu/"

# Freenove case controller (fans/LEDs) and how each case mounts the OLED.
FREENOVE_ADDR = 0x21
FREENOVE_REG_VERSION = 0xFE
FREENOVE_ROTATION = {
    "20250317_V1.0": 0, "20250724_V1.1": 0,        # FNK0100
    "20251015_V1.0": 180, "20251125_V2.1": 180,    # FNK0107 (Pro)
}

STATE_WORDS = {"playing": "LIVE", "buffering": "BUFFERING", "connecting": "CONNECTING",
               "resolving": "STARTING", "retrying": "RETRYING", "error": "ERROR", "idle": "IDLE",
               "waiting": "OFF AIR"}


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- SSD1306
class SSD1306:
    """Minimal SSD1306 (128x64, I2C) driver: sends only the parts that changed."""

    def __init__(self, bus, address=0x3C, contrast=96):
        self.bus = bus
        self.addr = address
        self.prev = None
        self.on = False
        self._block = getattr(bus, "i2c_rdwr", None) is not None
        self.init(contrast)

    def _send(self, control, data):
        data = list(data)
        if self._block:
            from smbus2 import i2c_msg
            self.bus.i2c_rdwr(i2c_msg.write(self.addr, [control] + data))
        else:  # plain smbus: 32-byte blocks
            for i in range(0, len(data), 31):
                self.bus.write_i2c_block_data(self.addr, control, data[i:i + 31])

    def command(self, *cmds):
        self._send(0x00, cmds)

    def init(self, contrast):
        self.command(
            0xAE,               # display off
            0xD5, 0x80,         # clock divide
            0xA8, 0x3F,         # multiplex: 64 rows
            0xD3, 0x00,         # display offset
            0x40,               # start line 0
            0x8D, 0x14,         # charge pump on
            0x20, 0x00,         # horizontal addressing
            0xA1,               # segment remap
            0xC8,               # COM scan direction
            0xDA, 0x12,         # COM pins
            0x81, max(0, min(255, contrast)),
            0xD9, 0xF1,         # pre-charge
            0xDB, 0x40,         # VCOMH
            0xA4,               # follow RAM
            0xA6,               # normal (not inverted)
        )
        self.prev = None
        self.power(True)

    def power(self, on):
        if on != self.on:
            self.command(0xAF if on else 0xAE)
            self.on = on

    def contrast(self, value):
        self.command(0x81, max(0, min(255, value)))

    @staticmethod
    def pages(image):
        """1-bit image -> 8 pages of 128 column bytes (bit n = row page*8+n)."""
        px = image.load()
        out = []
        for page in range(HEIGHT // 8):
            y0 = page * 8
            row = bytearray(WIDTH)
            for x in range(WIDTH):
                b = 0
                for bit in range(8):
                    if px[x, y0 + bit]:
                        b |= 1 << bit
                row[x] = b
            out.append(row)
        return out

    def display(self, image):
        pages = self.pages(image)
        for p, row in enumerate(pages):
            old = self.prev[p] if self.prev else None
            if old == row:
                continue
            if old is None:
                first, last = 0, WIDTH - 1
            else:
                diff = [i for i in range(WIDTH) if row[i] != old[i]]
                first, last = diff[0], diff[-1]
            self.command(0x21, first, last, 0x22, p, p)
            self._send(0x40, row[first:last + 1])
        self.prev = pages

    def clear(self):
        self.display(Image.new("1", (WIDTH, HEIGHT)))


class PreviewDisplay:
    """Writes frames as enlarged PNGs (for checking layouts without hardware)."""

    def __init__(self, directory):
        self.dir = directory
        os.makedirs(directory, exist_ok=True)
        self.n = 0
        self.on = True

    def display(self, image):
        self.n += 1
        big = image.convert("L").resize((WIDTH * 4, HEIGHT * 4), Image.NEAREST)
        big.save(os.path.join(self.dir, "frame-%04d.png" % self.n))

    def power(self, on):
        self.on = on

    def contrast(self, value):
        pass

    def clear(self):
        pass


def detect_rotation(bus):
    """Read the Freenove case controller's firmware version to tell the cases apart."""
    try:
        raw = bus.read_i2c_block_data(FREENOVE_ADDR, FREENOVE_REG_VERSION, 14)
    except OSError:
        return 0, None
    version = bytes(b for b in raw if 32 <= b < 127).decode("ascii", "ignore")
    return FREENOVE_ROTATION.get(version, 0), version


# ---------------------------------------------------------------- wall data
def wall_endpoint(url=None, config_path=CONFIG):
    """Base URL and auth header for the wall's own web API."""
    port, password = 8090, ""
    try:
        with open(config_path) as fh:
            cfg = json.load(fh)
        port = int(cfg.get("web", {}).get("port", 8090))
        password = cfg.get("web", {}).get("password") or ""
    except (OSError, ValueError):
        pass
    base = url or "http://127.0.0.1:%d" % port
    headers = {"User-Agent": "windopane-oled"}
    if password:
        token = base64.b64encode(("oled:" + password).encode()).decode()
        headers["Authorization"] = "Basic " + token
    return base.rstrip("/"), headers


def get_json(base, headers, path, timeout=2.5):
    req = urllib.request.Request(base + path, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def lan_address():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None


def _shown_slots(sc):
    """Assigned slots with any live takeover laid on top (what's really on screen)."""
    slots = list(sc["slots"])
    for k, sid in (sc.get("takeovers") or {}).items():
        if 0 <= int(k) < len(slots):
            slots[int(k)] = sid
    return slots


def primary(state):
    """(screen, slot, audible) of the tile to feature, or None."""
    audio = state.get("audio")
    if audio:
        return audio["screen"], audio["slot"], True
    layouts = {l["id"]: len(l["tiles"]) for l in state.get("layouts", [])}
    screens = state.get("screens", [])
    order = [state.get("focus_screen", 0)] + [i for i in range(len(screens))]
    for s in order:
        if s >= len(screens) or not screens[s].get("active", s == 0):
            continue
        sc = screens[s]
        count = layouts.get(sc["layout"], len(sc["slots"]))
        shown = _shown_slots(sc)
        for slot in range(count):
            if shown[slot]:
                return s, slot, False
    return None


def describe(state, adsb=None, address=None):
    """Turn /api/state into what the OLED shows."""
    clock = time.strftime("%H:%M")
    sysinfo = state.get("system", {})
    temp = sysinfo.get("temp_c")
    temp_txt = "%d°C" % round(temp) if temp is not None else ""
    pick = primary(state)
    if pick is None:
        web = "http://%s:%d" % (address or "this-pi", state.get("settings", {}).get("web", {}).get("port", 8090))
        return {"head": ("windoPane", clock), "ticker": "No streams yet • add some at " + web,
                "foot": ("IDLE", "", temp_txt), "stream_id": None, "kind": None}
    s, slot, audible = pick
    screens = state["screens"]
    sc = screens[s]
    tile = (sc.get("tiles") or [None] * 16)[slot] or {}
    takeover = str(slot) in (sc.get("takeovers") or {})
    sid = tile.get("stream_id") or _shown_slots(sc)[slot]
    stream = next((x for x in state.get("streams", []) if x["id"] == sid), {"name": "?", "url": ""})
    multi = sum(1 for x in screens if x.get("active")) > 1
    where = ("S%d·" % (s + 1) if multi else "") + "T%d" % (slot + 1)
    if takeover:
        where = "LIVE " + where
    head = ("♪ " + where, clock) if audible else ("MUTED · " + where, clock)

    kind = tile.get("kind") or stream.get("resolved_kind")
    st = tile.get("state") or "idle"
    # drop the live countdown so the ticker text stays stable while retrying
    detail = re.sub(r"\s*-\s*retrying in \d+s$", "", (tile.get("detail") or "").strip())
    name = stream.get("name") or stream.get("url")
    parts = [name]
    if kind == "adsb":
        parts += adsb_summary(adsb)
    elif kind == "web":
        host = urlparse(stream.get("url", "")).hostname
        if host:
            parts.append(host)
        if st in ("retrying", "error") and detail:
            parts.append(detail)
    else:
        if detail and detail.lower() != (name or "").lower() and not detail.startswith(("Connecting", "Starting", "Looking up")):
            parts.append(detail)
    vol = "vol %d" % state.get("volume", 0) if audible else "muted"
    # headline alerts on the wall lead the ticker while they're up
    active = (state.get("alerts") or {}).get("active") or []
    if active:
        parts = ["ALERT %s: %s" % (a.get("feed", ""), a.get("title", "")) for a in reversed(active[-3:])] + parts
        head = ("ALERT · " + where, clock)
    return {"head": head, "ticker": "  •  ".join(p for p in parts if p),
            "foot": (STATE_WORDS.get(st, st.upper()), vol, temp_txt),
            "stream_id": sid, "kind": kind}


def adsb_summary(t):
    if not t:
        return []
    if t.get("error") and not t.get("total"):
        return ["no data: " + t["error"][:60]]
    out = ["%d aircraft" % t.get("total", 0)]
    units = t.get("units", "nm")
    for a in t.get("aircraft", []):
        if a.get("alert"):
            out.append("%s %s %s" % (a["alert"], a.get("flight") or a.get("hex"), a.get("squawk") or ""))
    nearest = next((a for a in t.get("aircraft", []) if not a.get("alert") and a.get("dist") is not None), None)
    if nearest:
        alt = "GND" if nearest.get("ground") else ("%s ft" % format(nearest["alt"], ",") if nearest.get("alt") is not None else "")
        out.append("nearest %s %s%s %s %s" % (nearest.get("flight") or nearest.get("hex"), nearest["dist"], units,
                                              nearest.get("brg_card") or "", alt))
    return [o.strip() for o in out]


class Feed(threading.Thread):
    """Polls the wall once a second; keeps the latest description."""

    def __init__(self, base, headers):
        super().__init__(name="feed", daemon=True)
        self.base, self.headers = base, headers
        self.info = None
        self.ok_at = 0.0
        self.address = None

    def run(self):
        adsb_cache = (None, 0.0, None)
        while True:
            try:
                state = get_json(self.base, self.headers, "/api/state")
                self.address = lan_address() or self.address
                d = describe(state, None, self.address)
                if d["kind"] == "adsb" and d["stream_id"]:
                    sid, when, data = adsb_cache
                    if sid != d["stream_id"] or time.time() - when > 3:
                        try:
                            data = get_json(self.base, self.headers, "/api/adsb/" + d["stream_id"])
                        except (urllib.error.URLError, OSError, ValueError):
                            data = None
                        adsb_cache = (d["stream_id"], time.time(), data)
                    d = describe(state, data, self.address)
                self.info = d
                self.ok_at = time.time()
            except urllib.error.HTTPError as exc:
                self.info = {"error": "check web password" if exc.code == 401 else "web UI error %d" % exc.code}
            except (urllib.error.URLError, OSError, ValueError):
                if time.time() - self.ok_at > 5:
                    self.info = {"error": "wall not responding"}
                    self.address = lan_address() or self.address
            time.sleep(1)


# ---------------------------------------------------------------- drawing
def font(name, size):
    try:
        return ImageFont.truetype(FONT_DIR + name, size)
    except OSError:
        return ImageFont.load_default()


class Renderer:
    TICKER_TOP = 17
    TICKER_H = 30
    SPEED = 34  # pixels per second
    GAP = 36    # pixels between repeats

    def __init__(self):
        self.small = font("DejaVuSans.ttf", 11)
        self.big = font("DejaVuSans-Bold.ttf", 22)
        self.title_font = font("DejaVuSans-Bold.ttf", 18)
        self.strip = None
        self.strip_text = None
        self.scroll_start = time.monotonic()

    def _strip(self, text):
        if text == self.strip_text:
            return self.strip
        self.strip_text = text
        self.scroll_start = time.monotonic()
        w = max(1, int(self.big.getlength(text)))
        img = Image.new("1", (w, self.TICKER_H))
        ImageDraw.Draw(img).text((0, (self.TICKER_H - 22) // 2 - 3), text, font=self.big, fill=1)
        self.strip = img
        return img

    def ticker(self, frame, text, nudge_y=0):
        strip = self._strip(text)
        top = self.TICKER_TOP + nudge_y
        if strip.width <= WIDTH:
            frame.paste(strip, ((WIDTH - strip.width) // 2, top))
            return
        period = strip.width + self.GAP
        off = int((time.monotonic() - self.scroll_start) * self.SPEED) % period
        frame.paste(strip, (-off, top))
        frame.paste(strip, (-off + period, top))

    def line(self, draw, y, left, mid, right, dx):
        if left:
            draw.text((dx, y), left, font=self.small, fill=1)
        if right:
            draw.text((WIDTH - dx - self.small.getlength(right), y), right, font=self.small, fill=1)
        if mid:
            lw = self.small.getlength(left or "")
            mw = self.small.getlength(mid)
            rw = self.small.getlength(right or "")
            x = (WIDTH - mw) / 2
            x = max(x, dx + lw + 6)
            if x + mw <= WIDTH - dx - rw - 4:
                draw.text((x, y), mid, font=self.small, fill=1)

    def frame(self, info, shift):
        dx, dy = shift
        img = Image.new("1", (WIDTH, HEIGHT))
        d = ImageDraw.Draw(img)
        if not info:
            self._center(d, "windoPane", "starting…")
            return img
        if "error" in info:
            self._center(d, "windoPane", info["error"], dy)
            return img
        left, right = info["head"]
        self.line(d, 0 + dy, left, None, right, 1 + dx)
        d.line((0, 14, WIDTH - 1, 14), fill=1)
        self.ticker(img, info["ticker"], dy)
        d.line((0, 49, WIDTH - 1, 49), fill=1)
        self.line(d, 51 - dy, *info["foot"], dx=1 + dx)
        return img

    def _center(self, d, title, sub, dy=0):
        tw = self.title_font.getlength(title)
        d.text(((WIDTH - tw) / 2, 12 + dy), title, font=self.title_font, fill=1)
        sw = self.small.getlength(sub)
        if sw > WIDTH:
            sub = sub[: max(4, int(len(sub) * WIDTH / sw) - 1)] + "…"
            sw = self.small.getlength(sub)
        d.text(((WIDTH - sw) / 2, 40 + dy), sub, font=self.small, fill=1)


# ---------------------------------------------------------------- main
def in_window(spec, now=None):
    """True if the current time is inside "HH:MM-HH:MM" (may wrap midnight)."""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})", spec or "")
    if not m:
        return False
    t = now or time.localtime()
    cur = t.tm_hour * 60 + t.tm_min
    a = int(m.group(1)) * 60 + int(m.group(2))
    b = int(m.group(3)) * 60 + int(m.group(4))
    return a <= cur < b if a <= b else (cur >= a or cur < b)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="windopane-oled")
    ap.add_argument("--bus", type=int, default=1)
    ap.add_argument("--address", default="0x3c")
    ap.add_argument("--rotate", default="auto", choices=["auto", "0", "180"])
    ap.add_argument("--contrast", type=int, default=96, help="0-255; lower slows OLED burn-in")
    ap.add_argument("--sleep", default="", help="turn the display off between HH:MM-HH:MM")
    ap.add_argument("--fps", type=float, default=15)
    ap.add_argument("--url", help="wall web UI (default: from the wall's config)")
    ap.add_argument("--config", default=CONFIG)
    ap.add_argument("--preview", metavar="DIR", help="write PNG frames to DIR instead of using the OLED")
    ap.add_argument("--frames", type=int, default=0, help="stop after N frames (testing)")
    args = ap.parse_args(argv)

    if args.sleep and not re.fullmatch(r"\d{1,2}:\d{2}-\d{1,2}:\d{2}", args.sleep):
        ap.error("--sleep must look like 23:00-07:00")

    rotate = 0 if args.rotate == "auto" else int(args.rotate)
    bus = None
    if args.preview:
        disp = PreviewDisplay(args.preview)
    else:
        try:
            from smbus2 import SMBus
        except ImportError:
            from smbus import SMBus
        try:
            bus = SMBus(args.bus)
        except OSError as exc:
            sys.exit("cannot open I2C bus %d (%s): is I2C enabled?" % (args.bus, exc))
        if args.rotate == "auto":
            rotate, version = detect_rotation(bus)
            log("Freenove case controller: %s; rotation %d" % (version or "not found", rotate))
        try:
            disp = SSD1306(bus, int(args.address, 0), args.contrast)
        except OSError as exc:
            sys.exit("no OLED answering at %s on I2C bus %d (%s)" % (args.address, args.bus, exc))

    base, headers = wall_endpoint(args.url, args.config)
    feed = Feed(base, headers)
    feed.start()
    renderer = Renderer()

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    period = 1.0 / max(1.0, args.fps)
    frames = 0
    log("windopane-oled running: %s, contrast %d%s" % (base, args.contrast,
                                                       ", off %s" % args.sleep if args.sleep else ""))
    while not stop.is_set():
        started = time.monotonic()
        if args.sleep and in_window(args.sleep):
            disp.power(False)
            stop.wait(15)
            continue
        disp.power(True)
        # move static lines a pixel every few minutes to spread OLED wear
        step = int(time.time() // 180) % 4
        shift = ((0, 0), (1, 0), (1, 1), (0, 1))[step]
        img = renderer.frame(feed.info, shift)
        if rotate == 180:
            img = img.transpose(Image.ROTATE_180)
        try:
            disp.display(img)
        except OSError as exc:
            log("OLED write failed: %s; retrying" % exc)
            time.sleep(2)
            try:
                disp.init(args.contrast)
            except (OSError, AttributeError):
                pass
        frames += 1
        if args.frames and frames >= args.frames:
            break
        stop.wait(max(0.0, period - (time.monotonic() - started)))

    try:
        disp.clear()
        disp.power(False)
    except OSError:
        pass
    if bus is not None:
        bus.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

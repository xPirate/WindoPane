"""Text ADS-B tiles: aircraft from a dump1090-family receiver, with distance
and bearing from your location.

Sources
  http(s)://...   dump1090-fa / readsb / tar1090 / dump1090-mutability web
                  data (aircraft.json). A bare receiver address is fine: the
                  usual paths are tried until one answers.
  tcp://host:30003  the BaseStation (SBS-1) text feed every dump1090 build
                  can output (--net). Kept open by a background reader.

Location: a Maidenhead locator (EM15, EM15fl, EM15fl42) or "lat, lon" in
decimal degrees (N/S/E/W suffixes are fine). Without one, the receiver's own
location from receiver.json is used when it publishes one.
"""

import json
import logging
import math
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlparse

log = logging.getLogger(__name__)

UNITS = {"nm": 1.0 / 1.852, "km": 1.0, "mi": 1.0 / 1.609344}
EMERGENCY_SQUAWKS = {"7500": "HIJACK", "7600": "RADIO FAIL", "7700": "EMERGENCY"}
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
ARROWS = "↑↗→↘↓↙←↖"  # ↑↗→↘↓↙←↖
JSON_PATHS = ("data/aircraft.json", "/data/aircraft.json", "/tar1090/data/aircraft.json",
              "/dump1090-fa/data/aircraft.json", "/skyaware/data/aircraft.json",
              "/dump1090/data/aircraft.json")
AIRCRAFT_TIMEOUT_S = 60


# ------------------------------------------------------------------ location
_MH = re.compile(r"^([A-R]{2})(\d{2})(?:([A-X]{2})(?:(\d{2}))?)?$", re.I)
_COORD = re.compile(r"^\s*([-+]?\d+(?:\.\d+)?)\s*°?\s*([NS])?\s*[,; ]\s*([-+]?\d+(?:\.\d+)?)\s*°?\s*([EW])?\s*$", re.I)


def maidenhead_to_latlon(loc):
    """Center of a 4, 6 or 8 character Maidenhead locator."""
    m = _MH.match(loc.strip())
    if not m:
        raise ValueError("not a Maidenhead locator: %r" % loc)
    field, square, sub, ext = m.groups()
    field = field.upper()
    lon = (ord(field[0]) - 65) * 20 - 180 + int(square[0]) * 2
    lat = (ord(field[1]) - 65) * 10 - 90 + int(square[1])
    dlon, dlat = 2.0, 1.0
    if sub:
        sub = sub.upper()
        lon += (ord(sub[0]) - 65) * (2.0 / 24)
        lat += (ord(sub[1]) - 65) * (1.0 / 24)
        dlon, dlat = 2.0 / 24, 1.0 / 24
        if ext:
            lon += int(ext[0]) * (dlon / 10)
            lat += int(ext[1]) * (dlat / 10)
            dlon, dlat = dlon / 10, dlat / 10
    return lat + dlat / 2, lon + dlon / 2


def parse_location(text):
    """Return (lat, lon) from a Maidenhead locator or "lat, lon"."""
    text = (text or "").strip()
    if not text:
        raise ValueError("location is empty")
    if _MH.match(text):
        return maidenhead_to_latlon(text)
    m = _COORD.match(text)
    if not m:
        raise ValueError("location must be a Maidenhead locator (EM15fl) or 'lat, lon' (35.47, -97.52)")
    lat, ns, lon, ew = float(m.group(1)), m.group(2), float(m.group(3)), m.group(4)
    if ns and ns.upper() == "S":
        lat = -abs(lat)
    if ew and ew.upper() == "W":
        lon = -abs(lon)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("latitude must be -90..90 and longitude -180..180")
    return lat, lon


def distance_bearing(lat1, lon1, lat2, lon2):
    """Great-circle distance (km) and initial bearing (degrees true)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = p2 - p1
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    dist = 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(a)))
    y = math.sin(dlon) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlon)
    brg = (math.degrees(math.atan2(y, x)) + 360) % 360
    return dist, brg


def compass(deg):
    return COMPASS[int((deg % 360) / 22.5 + 0.5) % 16]


def arrow(deg):
    return ARROWS[int((deg % 360) / 45 + 0.5) % 8]


# ------------------------------------------------------------------ normalizing
def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def normalize_json(ac):
    """One aircraft from any dump1090-family aircraft.json."""
    alt = ac.get("alt_baro", ac.get("altitude", ac.get("alt_geom")))
    on_ground = alt == "ground"
    vrate = ac.get("baro_rate", ac.get("geom_rate", ac.get("vert_rate")))
    emerg = ac.get("emergency")
    return {
        "hex": str(ac.get("hex", "")).lower().lstrip("~"),
        "flight": (ac.get("flight") or "").strip(),
        "alt": None if on_ground else _num(alt),
        "ground": on_ground,
        "gs": _num(ac.get("gs", ac.get("speed"))),
        "track": _num(ac.get("track")),
        "vrate": _num(vrate),
        "squawk": str(ac["squawk"]) if ac.get("squawk") else "",
        "lat": _num(ac.get("lat")),
        "lon": _num(ac.get("lon")),
        "rssi": _num(ac.get("rssi")),
        "seen": _num(ac.get("seen")) or 0.0,
        "seen_pos": _num(ac.get("seen_pos")),
        "category": ac.get("category") or "",
        "emergency": emerg if emerg and emerg != "none" else "",
    }


def parse_sbs_line(line):
    """Parse one BaseStation line into (hex, partial fields) or None."""
    f = line.strip().split(",")
    if len(f) < 11 or f[0] != "MSG" or not f[4]:
        return None
    out = {}

    def get(i):
        return f[i].strip() if len(f) > i and f[i].strip() != "" else None

    if get(10):
        out["flight"] = get(10)
    if get(11) is not None:
        out["alt"] = _num(get(11))
    if get(12) is not None:
        out["gs"] = _num(get(12))
    if get(13) is not None:
        out["track"] = _num(get(13))
    if get(14) is not None and get(15) is not None:
        out["lat"], out["lon"] = _num(get(14)), _num(get(15))
        out["_pos_ts"] = True
    if get(16) is not None:
        out["vrate"] = _num(get(16))
    if get(17) is not None:
        out["squawk"] = get(17).zfill(4)
    if get(19) in ("-1", "1"):
        out["emergency"] = "emergency"
    if get(21) is not None:
        out["ground"] = get(21) in ("-1", "1")
    return f[4].strip().lower(), out


# ------------------------------------------------------------------ feeds
class JsonFeed:
    def __init__(self, url):
        self.url = url
        self.data_url = None
        self.receiver = None
        self.cache = None
        self.cache_t = 0.0
        self.error = None
        self.last_messages = None
        self.msg_rate = None
        self.lock = threading.Lock()

    def _candidates(self):
        if urlparse(self.url).path.endswith(".json"):
            return [self.url]
        base = self.url if self.url.endswith("/") else self.url + "/"
        return [urljoin(base, p) for p in JSON_PATHS]

    @staticmethod
    def _get(url):
        req = urllib.request.Request(url, headers={"User-Agent": "WindoPane"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def _fetch(self):
        urls = [self.data_url] if self.data_url else self._candidates()
        last_err = None
        for url in urls:
            try:
                data = self._get(url)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                last_err = exc
                continue
            if isinstance(data, dict) and "aircraft" in data:
                if self.data_url != url:
                    log.info("ADS-B data found at %s", url)
                    self.data_url = url
                    self.receiver = None
                return data
        self.data_url = None
        raise ConnectionError("no aircraft.json at %s (%s)" % (self.url, last_err))

    def _load_receiver(self):
        if self.receiver is not None or not self.data_url:
            return
        try:
            rec = self._get(self.data_url.rsplit("/", 1)[0] + "/receiver.json")
            self.receiver = {"lat": _num(rec.get("lat")), "lon": _num(rec.get("lon"))}
        except (urllib.error.URLError, OSError, ValueError):
            self.receiver = {}

    def snapshot(self):
        with self.lock:
            now = time.time()
            if self.cache is not None and now - self.cache_t < 1.0:
                return self.cache
            try:
                data = self._fetch()
                self._load_receiver()
                msgs = data.get("messages")
                if msgs is not None and self.last_messages is not None:
                    dt = now - self.last_messages[1]
                    if dt > 0:
                        self.msg_rate = max(0.0, (msgs - self.last_messages[0]) / dt)
                if msgs is not None:
                    self.last_messages = (msgs, now)
                aircraft = [normalize_json(a) for a in data.get("aircraft", [])]
                aircraft = [a for a in aircraft if a["hex"] and a["seen"] < AIRCRAFT_TIMEOUT_S]
                self.error = None
            except ConnectionError as exc:
                aircraft = []
                self.error = str(exc)
            self.cache = {"aircraft": aircraft, "error": self.error, "receiver": self.receiver or {},
                          "msg_rate": self.msg_rate, "source": self.data_url or self.url}
            self.cache_t = now
            return self.cache

    def stop(self):
        pass


class SbsFeed(threading.Thread):
    """Reads a BaseStation (port 30003) feed and keeps aircraft state."""

    def __init__(self, host, port):
        super().__init__(name="sbs-%s:%d" % (host, port), daemon=True)
        self.host, self.port = host, port
        self.aircraft = {}
        self.lock = threading.Lock()
        self.error = "connecting"
        self.running = True
        self.last_used = time.time()
        self.msg_count = 0
        self.rate_window = (time.time(), 0)
        self.msg_rate = None
        self.start()

    def run(self):
        backoff = 2
        while self.running:
            if time.time() - self.last_used > 300:  # nobody is looking: stop
                self.running = False
                break
            try:
                with socket.create_connection((self.host, self.port), timeout=10) as sock:
                    sock.settimeout(30)
                    self.error = None
                    backoff = 2
                    buf = b""
                    while self.running and time.time() - self.last_used <= 300:
                        chunk = sock.recv(65536)
                        if not chunk:
                            raise ConnectionError("feed closed")
                        buf += chunk
                        *lines, buf = buf.split(b"\n")
                        for line in lines:
                            self._handle(line.decode("ascii", "replace"))
            except (OSError, ConnectionError) as exc:
                self.error = "%s:%d %s" % (self.host, self.port, exc)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def _handle(self, line):
        parsed = parse_sbs_line(line)
        if not parsed:
            return
        hexid, fields = parsed
        now = time.time()
        with self.lock:
            self.msg_count += 1
            ac = self.aircraft.setdefault(hexid, {"hex": hexid, "flight": "", "alt": None, "ground": False,
                                                  "gs": None, "track": None, "vrate": None, "squawk": "",
                                                  "lat": None, "lon": None, "rssi": None, "category": "",
                                                  "emergency": "", "pos_t": None})
            if fields.pop("_pos_ts", False):
                ac["pos_t"] = now
            ac.update(fields)
            ac["t"] = now

    def snapshot(self):
        self.last_used = time.time()
        now = time.time()
        with self.lock:
            for k in [k for k, a in self.aircraft.items() if now - a["t"] > AIRCRAFT_TIMEOUT_S]:
                del self.aircraft[k]
            started, count = self.rate_window
            if now - started >= 5:
                self.msg_rate = (self.msg_count - count) / (now - started)
                self.rate_window = (now, self.msg_count)
            out = []
            for a in self.aircraft.values():
                row = dict(a)
                row["seen"] = now - a["t"]
                row["seen_pos"] = now - a["pos_t"] if a["pos_t"] else None
                row.pop("t", None)
                row.pop("pos_t", None)
                out.append(row)
        return {"aircraft": out, "error": self.error, "receiver": {}, "msg_rate": self.msg_rate,
                "source": "tcp://%s:%d" % (self.host, self.port)}

    def stop(self):
        self.running = False


class FeedRegistry:
    def __init__(self):
        self.feeds = {}
        self.lock = threading.Lock()

    def get(self, url):
        with self.lock:
            feed = self.feeds.get(url)
            if isinstance(feed, SbsFeed) and not feed.running:
                feed = None
            if feed is None:
                parsed = urlparse(url)
                if parsed.scheme in ("tcp", "sbs"):
                    feed = SbsFeed(parsed.hostname, parsed.port or 30003)
                else:
                    feed = JsonFeed(url)
                self.feeds[url] = feed
            return feed


FEEDS = FeedRegistry()


def is_adsb_url(url):
    p = urlparse(url or "")
    if p.scheme in ("tcp", "sbs") and p.port == 30003:
        return True
    return p.path.endswith("aircraft.json")


# ------------------------------------------------------------------ the table
def table(stream, feed=None):
    """Everything the ADS-B tile shows, computed for one stream."""
    feed = feed or FEEDS.get(stream["url"])
    snap = feed.snapshot()
    units = stream.get("units") or "nm"
    factor = UNITS.get(units, UNITS["nm"])
    max_range = _num(stream.get("max_range"))

    station = None
    station_src = None
    if stream.get("location"):
        try:
            station = parse_location(stream["location"])
            station_src = stream["location"]
        except ValueError:
            station = None
    rec = snap.get("receiver") or {}
    if station is None and rec.get("lat") is not None and rec.get("lon") is not None:
        station = (rec["lat"], rec["lon"])
        station_src = "receiver"

    rows = []
    for a in snap["aircraft"]:
        row = {
            "hex": a["hex"].upper(),
            "flight": a["flight"],
            "squawk": a["squawk"],
            "alt": None if a["alt"] is None else int(round(a["alt"])),
            "ground": a["ground"],
            "gs": None if a["gs"] is None else int(round(a["gs"])),
            "track": None if a["track"] is None else int(round(a["track"])) % 360,
            "track_arrow": None if a["track"] is None else arrow(a["track"]),
            "vrate": None if a["vrate"] is None else int(round(a["vrate"])),
            "rssi": None if a["rssi"] is None else round(a["rssi"], 1),
            "seen": round(a["seen"], 1),
            "dist": None, "brg": None, "brg_card": None, "brg_arrow": None,
            "alert": EMERGENCY_SQUAWKS.get(a["squawk"]) or (a["emergency"].upper() if a["emergency"] else ""),
            "category": a.get("category") or "",
        }
        if station and a["lat"] is not None and a["lon"] is not None:
            d, b = distance_bearing(station[0], station[1], a["lat"], a["lon"])
            row["dist"] = round(d * factor, 1)
            row["brg"] = int(round(b)) % 360
            row["brg_card"] = compass(b)
            row["brg_arrow"] = arrow(b)
            if max_range and row["dist"] > max_range:
                continue
        rows.append(row)

    def key(r):
        if r["alert"]:
            return (0, 0, "")
        if r["dist"] is not None:
            return (1, r["dist"], "")
        return (2, -(r["alt"] or 0), r["flight"] or r["hex"])
    rows.sort(key=key)

    with_pos = [r for r in rows if r["dist"] is not None]
    return {
        "name": stream.get("name"),
        "units": units,
        "station": {"lat": round(station[0], 4), "lon": round(station[1], 4), "from": station_src} if station else None,
        "source": snap.get("source"),
        "error": snap.get("error"),
        "msg_rate": None if snap.get("msg_rate") is None else round(snap["msg_rate"], 1),
        "total": len(rows),
        "with_pos": len(with_pos),
        "max_dist": max((r["dist"] for r in with_pos), default=None),
        "aircraft": rows,
        "time": time.time(),
    }

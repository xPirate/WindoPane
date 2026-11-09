"""Persistent configuration (JSON on disk, atomic writes)."""

import copy
import json
import logging
import os
import tempfile

from . import layouts

log = logging.getLogger(__name__)

DEFAULT_PATH = os.environ.get("WINDOPANE_CONFIG", "/var/lib/windopane/config.json")

MAX_SCREENS = 4

SCREEN_DEFAULTS = {
    "layout": layouts.DEFAULT_LAYOUT,
    "slots": [None] * layouts.MAX_SLOTS,
    # "auto" keeps the monitor's preferred mode; otherwise e.g. "3840x2160"
    "resolution": "auto",
    # extra screens are used automatically whenever a monitor is connected
    "enabled": True,
    # "auto": this screen's own HDMI port; "default": system default; or a device id
    "audio_device": "auto",
}

DEFAULTS = {
    "version": 2,
    "screens": [copy.deepcopy(SCREEN_DEFAULTS) for _ in range(MAX_SCREENS)],
    "streams": [],
    "volume": 80,
    "display": {
        # black pixels between tiles
        "gap": 0,
        # width of the highlight frame on the audible tile; "auto" scales with screen
        "border": "auto",
        # highest stream quality fetched for any tile: "auto" or e.g. 1080
        "quality_cap": "auto",
        # show the stream name on screen briefly when a tile becomes audible
        "osd": True,
        # how monitors are placed on the shared desktop: "auto", "row" or "grid"
        "arrangement": "auto",
    },
    "web": {
        "host": "0.0.0.0",
        # 8080 is avoided on purpose: dump1090-fa / SkyAware already use it.
        "port": 8090,
        # empty = no login. Otherwise HTTP basic auth, any username.
        "password": "",
    },
    "alerts": {
        # headline alerts from RSS/Atom feeds (see alerts.py)
        "enabled": True,
        "feeds": [],
        "ticker_speed": "normal",       # slow | normal | fast
        "ticker_position": "bottom",    # bottom | top
        "popup_position": "top-right",  # top-right | top-left | bottom-right | bottom-left
        "max_popups": 3,
    },
    "player": {
        "network_caching_ms": 2000,
        "stall_timeout_s": 45,
        "extra_vlc_args": [],
        # "auto": VLC on a Raspberry Pi, mpv on PCs (VA-API GPU decoding)
        "engine": "auto",
    },
}

STREAM_KINDS = ("auto", "video", "web", "adsb")


def _merge(defaults, data):
    out = copy.deepcopy(defaults)
    for key, value in (data or {}).items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _migrate(data):
    """Bring a version-1 (single screen) config forward."""
    data = dict(data or {})
    if "screens" not in data and ("layout" in data or "slots" in data):
        first = {"layout": data.get("layout", layouts.DEFAULT_LAYOUT),
                 "slots": data.get("slots", [None] * layouts.MAX_SLOTS)}
        res = (data.get("display") or {}).get("resolution")
        if res:
            first["resolution"] = res
        data["screens"] = [first]
    data.pop("layout", None)
    data.pop("slots", None)
    if isinstance(data.get("display"), dict):
        data["display"] = {k: v for k, v in data["display"].items() if k != "resolution"}
    return data


def normalize(cfg):
    """Repair anything a hand edit or older version may have left behind."""
    clean = []
    for s in cfg.get("streams", []):
        if not isinstance(s, dict) or not s.get("id") or not s.get("url"):
            continue
        s.setdefault("name", s["url"])
        if s.get("kind") not in STREAM_KINDS:
            s["kind"] = "auto"
        clean.append(s)
    cfg["streams"] = clean
    ids = {s["id"] for s in clean}

    screens = [sc for sc in (cfg.get("screens") or []) if isinstance(sc, dict)]
    screens = (screens + [{}] * MAX_SCREENS)[:MAX_SCREENS]
    fixed = []
    for sc in screens:
        sc = _merge(SCREEN_DEFAULTS, sc)
        if sc["layout"] not in layouts.LAYOUTS:
            sc["layout"] = layouts.DEFAULT_LAYOUT
        slots = (list(sc.get("slots") or []) + [None] * layouts.MAX_SLOTS)[: layouts.MAX_SLOTS]
        sc["slots"] = [s if s in ids else None for s in slots]
        sc["enabled"] = bool(sc.get("enabled", True))
        sc["resolution"] = str(sc.get("resolution") or "auto").lower()
        sc["audio_device"] = str(sc.get("audio_device") or "auto")
        fixed.append(sc)
    fixed[0]["enabled"] = True  # the first screen is always on
    alerts = cfg.get("alerts") if isinstance(cfg.get("alerts"), dict) else {}
    alerts = _merge(DEFAULTS["alerts"], alerts)
    alerts["feeds"] = [f for f in (alerts.get("feeds") or [])
                       if isinstance(f, dict) and f.get("id") and f.get("url")]
    cfg["alerts"] = alerts
    cfg["screens"] = fixed
    try:
        cfg["volume"] = max(0, min(100, int(cfg.get("volume", 80))))
    except (TypeError, ValueError):
        cfg["volume"] = 80
    cfg["version"] = 2
    return cfg


def load(path=DEFAULT_PATH):
    data = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            bad = path + ".corrupt"
            log.error("config unreadable (%s); moving it to %s and starting fresh", exc, bad)
            try:
                os.replace(path, bad)
            except OSError:
                pass
            data = {}
    return normalize(_merge(DEFAULTS, _migrate(data)))


def save(cfg, path=DEFAULT_PATH):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

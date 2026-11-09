"""Hardware detection and per-model playback limits."""

import os
import platform
import socket

STANDARD_HEIGHTS = (240, 360, 480, 720, 1080, 1440, 2160)


_DMI_JUNK = ("to be filled", "system product name", "system manufacturer", "default string",
             "not applicable", "o.e.m.")


def board_model():
    for path in ("/proc/device-tree/model", "/sys/firmware/devicetree/base/model"):
        try:
            with open(path, "rb") as fh:
                return fh.read().decode("utf-8", "ignore").strip("\x00 \n")
        except OSError:
            continue
    # PCs: "Beelink SER5", "MINISFORUM UM790 Pro", ...
    parts = []
    for name in ("sys_vendor", "product_name"):
        try:
            with open("/sys/class/dmi/id/" + name) as fh:
                value = fh.read().strip()
        except OSError:
            continue
        if value and not any(j in value.lower() for j in _DMI_JUNK) and value not in parts:
            parts.append(value)
    if parts:
        return " ".join(parts) + " (%s)" % (platform.machine() or "pc")
    return platform.machine() or "unknown"


def profile(model=None):
    """Return the playback profile for this board.

    ``hw_h264``: whether H.264 is hardware-decoded (Pi 4 yes, Pi 5 no).
    ``cap_few`` / ``cap_many`` / ``cap_lots`` / ``cap_huge``: highest stream
    height to fetch per tile when 1-2, 3-4, 5-8 or 9+ video tiles are playing
    (counted across all screens: the decoder budget is shared).
    """
    model = model or board_model()
    m = model.lower()
    if "raspberry pi 5" in m or "compute module 5" in m:
        return {"id": "pi5", "model": model, "hw_h264": False, "cap_few": 1080, "cap_many": 720, "cap_lots": 480,
                "cap_huge": 360}
    if "raspberry pi 4" in m or "compute module 4" in m or "raspberry pi 400" in m:
        return {"id": "pi4", "model": model, "hw_h264": True, "cap_few": 1080, "cap_many": 720, "cap_lots": 480,
                "cap_huge": 360}
    if "raspberry pi" in m:
        return {"id": "pi-old", "model": model, "hw_h264": True, "cap_few": 720, "cap_many": 480, "cap_lots": 360,
                "cap_huge": 240}
    return {"id": "generic", "model": model, "hw_h264": None, "cap_few": 2160, "cap_many": 1080, "cap_lots": 720,
            "cap_huge": 480}


def pick_height(tile_height, video_tiles, prof, cap_setting="auto", stream_max=None):
    """Choose the max stream height for a tile."""
    if stream_max:
        try:
            return int(stream_max)
        except (TypeError, ValueError):
            pass
    want = next((h for h in STANDARD_HEIGHTS if h >= tile_height * 0.9), STANDARD_HEIGHTS[-1])
    if video_tiles <= 2:
        cap = prof["cap_few"]
    elif video_tiles <= 4:
        cap = prof["cap_many"]
    elif video_tiles <= 8:
        cap = prof["cap_lots"]
    else:
        cap = prof["cap_huge"]
    if cap_setting not in (None, "", "auto"):
        try:
            cap = int(cap_setting)
        except (TypeError, ValueError):
            pass
    return max(240, min(want, cap))


def hostname():
    return socket.gethostname()


def temperature_c():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as fh:
            return round(int(fh.read().strip()) / 1000.0, 1)
    except (OSError, ValueError):
        return None


def throttled():
    """Raw vcgencmd get_throttled bits via sysfs, if the firmware exposes them."""
    path = "/sys/devices/platform/soc/soc:firmware/get_throttled"
    try:
        with open(path) as fh:
            return int(fh.read().strip(), 16)
    except (OSError, ValueError):
        return None


def load_average():
    try:
        return [round(x, 2) for x in os.getloadavg()]
    except OSError:
        return None


def memory():
    info = {}
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0])
        return {"total_mb": info["MemTotal"] // 1024,
                "available_mb": info["MemAvailable"] // 1024}
    except (OSError, KeyError, ValueError):
        return None

"""GPU detection, hardware-decode report and the driver switcher (PCs only).

Reading is done as the normal wall user. Changing drivers goes through the
root helper /opt/windopane/bin/windopane-gpu, which the installer allows the
wall user to run (and nothing else) via /etc/sudoers.d/windopane.
"""

import fcntl
import glob
import os
import re
import shutil
import subprocess
import time

from . import sysinfo

HELPER = "/opt/windopane/bin/windopane-gpu"
STATE_FILE = "/var/log/windopane/gpu.state"
LOG_FILE = "/var/log/windopane/gpu.log"
ENV_FILE = "/etc/windopane/gpu.env"
LOCK_FILE = "/run/windopane-gpu.lock"

VENDOR_IDS = {"0x1002": "amd", "0x8086": "intel", "0x10de": "nvidia"}
VENDORS = ("amd", "intel", "nvidia", "none")
CODECS = ("H264", "HEVC", "VP9", "AV1", "MPEG2", "VC1")

_cache = {"t": 0.0, "decode": None}


# ------------------------------------------------------------------ detection
def is_pi():
    return sysinfo.profile()["id"].startswith("pi")


def _read(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def _lspci_name(slot):
    if not shutil.which("lspci"):
        return None
    try:
        out = subprocess.run(["lspci", "-mm", "-s", slot], capture_output=True, text=True,
                             timeout=5, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    fields = re.findall(r'"([^"]*)"', out)
    if len(fields) >= 3:
        vendor = re.sub(r"\[(.*?)\]", r"\1", fields[1])
        device = fields[2]
        bracket = re.search(r"\[([^\]]+)\]", device)  # "Navi 23 [Radeon RX 6600 XT]"
        return "%s %s" % (vendor.split()[0], bracket.group(1) if bracket else device)
    return None


def detect_gpus(root="/sys/bus/pci/devices"):
    gpus = []
    for dev in sorted(glob.glob(os.path.join(root, "*"))):
        cls = _read(os.path.join(dev, "class")) or ""
        if not cls.startswith("0x03"):
            continue
        vendor_id = _read(os.path.join(dev, "vendor")) or ""
        driver_link = os.path.join(dev, "driver")
        driver = os.path.basename(os.readlink(driver_link)) if os.path.islink(driver_link) else None
        slot = os.path.basename(dev)
        gpus.append({
            "slot": slot,
            "vendor": VENDOR_IDS.get(vendor_id, "other"),
            "vendor_id": vendor_id,
            "device_id": _read(os.path.join(dev, "device")),
            "name": _lspci_name(slot),
            "kernel_driver": driver,
            "primary": _read(os.path.join(dev, "boot_vga")) == "1",
        })
    return gpus


def recommended(gpus):
    """AMD first, then Intel; NVIDIA only when it's the only GPU."""
    vendors = {g["vendor"] for g in gpus}
    for v in ("amd", "intel", "nvidia"):
        if v in vendors:
            return v
    return "none"


def current_vendor(path=ENV_FILE):
    text = _read(path) or ""
    m = re.search(r"^WINDOPANE_GPU=(\w+)", text, re.M)
    return m.group(1) if m else None


# ------------------------------------------------------------------ decode report
def parse_vdpauinfo(text):
    info = re.search(r"Information string:\s*(.+)", text or "")
    supported = set()
    in_dec = False
    for line in (text or "").splitlines():
        if line.startswith("Decoder capabilities"):
            in_dec = True
            continue
        if in_dec and re.match(r"^[A-Z][a-z].*:", line):  # next section, e.g. "Output surface:"
            break
        if not in_dec:
            continue
        m = re.match(r"^([A-Z0-9_]+)\s+(.*)$", line.strip())
        if not m or "not supported" in m.group(2) or m.group(1) == "name":
            continue
        name = m.group(1)
        for codec in CODECS:
            if name.startswith(codec):
                supported.add(codec)
    return {"driver": info.group(1).strip() if info else None,
            "codecs": [c for c in CODECS if c in supported]}


def parse_vainfo(text):
    drv = re.search(r"Driver version:\s*(.+)", text or "")
    supported = set()
    for m in re.finditer(r"VAProfile(\w+)\s*:\s*VAEntrypointVLD", text or ""):
        name = m.group(1).upper()
        for codec in CODECS:
            if name.startswith(codec):
                supported.add(codec)
    return {"driver": drv.group(1).strip() if drv else None,
            "codecs": [c for c in CODECS if c in supported]}


def _run(cmd, env=None):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False, env=env)
        return out.stdout + out.stderr
    except (OSError, subprocess.TimeoutExpired):
        return ""


def decode_support(max_age=60):
    """What the GPU can decode, as seen by VDPAU (what VLC uses) and VA-API."""
    now = time.time()
    if _cache["decode"] is not None and now - _cache["t"] < max_age:
        return _cache["decode"]
    env = dict(os.environ)
    for line in (_read(ENV_FILE) or "").splitlines():
        if re.match(r"^(VDPAU_DRIVER|LIBVA_DRIVER_NAME)=\w+$", line):
            k, v = line.split("=", 1)
            env[k] = v
    result = {"vdpau": None, "vaapi": None}
    if shutil.which("vdpauinfo") and env.get("DISPLAY"):
        result["vdpau"] = parse_vdpauinfo(_run(["vdpauinfo"], env))
    if shutil.which("vainfo"):
        render = sorted(glob.glob("/dev/dri/renderD*"))
        cmd = ["vainfo", "--display", "drm"] + (["--device", render[0]] if render else [])
        result["vaapi"] = parse_vainfo(_run(cmd, env))
    _cache.update(t=now, decode=result)
    return result


# ------------------------------------------------------------------ driver job
def _lock_held():
    try:
        fd = os.open(LOCK_FILE, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def job_state():
    state = {}
    for line in (_read(STATE_FILE) or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            state[k] = v
    status = state.get("status")
    if status in ("starting", "running") and not _lock_held():
        started = int(state.get("started") or 0)
        if status == "running" or time.time() - started > 60:
            state["status"] = "interrupted"
    state["reboot"] = state.get("reboot") == "1"
    return state


def log_tail(limit=6000):
    try:
        with open(LOG_FILE, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - limit))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    # apt progress output uses carriage returns; keep only the final state of each line
    lines = [ln.split("\r")[-1] for ln in text.splitlines()]
    lines = [ln for ln in lines if not re.match(r"^\(Reading database \.\.\. \d+%", ln)]
    return "\n".join(lines[-80:])


class DriverError(Exception):
    pass


def _helper(*args):
    if not os.path.exists(HELPER):
        raise DriverError("the driver helper is not installed; re-run install.sh")
    try:
        out = subprocess.run(["sudo", "-n", HELPER, *args], capture_output=True, text=True,
                             timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DriverError(str(exc)) from exc
    if out.returncode != 0:
        msg = (out.stderr or out.stdout).strip().splitlines()
        msg = msg[-1] if msg else "the driver helper failed"
        if "password is required" in msg or "not allowed" in msg:
            msg = "driver switching isn't allowed for this user; re-run install.sh"
        raise DriverError(msg.replace("windopane-gpu: ", ""))
    return out.stdout.strip()


def apply(vendor):
    if vendor not in VENDORS:
        raise ValueError("vendor must be one of %s" % ", ".join(VENDORS))
    if is_pi():
        raise DriverError("Raspberry Pi graphics are built in; there is nothing to switch")
    if job_state().get("status") in ("starting", "running"):
        raise DriverError("a driver change is already running")
    _cache["decode"] = None
    return _helper("apply", vendor)


def reboot():
    from . import power
    try:
        power.system("reboot")
    except power.PowerError as exc:
        raise DriverError(str(exc)) from exc


def report():
    pi = is_pi()
    out = {"platform": "pi" if pi else "pc", "job": job_state(), "log": log_tail()}
    if pi:
        return out
    gpus = detect_gpus()
    out.update(gpus=gpus, recommended=recommended(gpus), current=current_vendor(),
               decode=decode_support())
    return out

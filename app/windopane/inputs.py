"""Keyboards, keypads and USB/Bluetooth controllers -> controller actions.

Devices are read directly with evdev (works without focus, survives the
browser tiles stealing keyboard focus, and picks up hot-plugged devices).
If evdev isn't usable the display window falls back to GTK key events.

Default map
-----------
Keyboard                         Controller
1-9 (10-16: 1+N)  tile on focused screen   A B X Y       tile 1-4 on the focused screen
F1-F4 / S         focus screen N / next    right stick   next screen
0, M, Mute        mute all           Start               mute all
Right/Tab, Left   next/prev tile     LB / RB, d-pad L/R  prev / next tile (both screens)
Up/Down, +/-      volume             d-pad up / down     volume
L / K             next/prev layout   Select              next layout (focused screen)
Z, Enter, F       zoom tile          Guide / left stick  zoom tile
R                 reload tile
N / B             next / previous video on a playlist tile (media keys too)
Pressing the key of the tile that is already audible mutes it again. The
focused screen is the one with the audible tile (or the last one switched to).
On screens with 10 or more tiles, type two digits for tiles 10-16 (1 then 2).
"""

import logging
import os
import selectors
import threading
import time

log = logging.getLogger(__name__)

try:
    import evdev
    from evdev import ecodes
except ImportError:  # pragma: no cover - only on systems without python3-evdev
    evdev = None
    ecodes = None

VOLUME_STEP = 5


def _action_table(ctl):
    return {
        **{"digit%d" % n: (lambda n=n: ctl.digit(n)) for n in range(10)},
        **{"screen%d" % n: (lambda n=n: ctl.focus(n)) for n in range(4)},
        **{"face%d" % n: (lambda n=n: ctl.toggle_focus_slot(n)) for n in range(4)},
        "screen_next": lambda: ctl.switch_screen(1),
        "mute": lambda: ctl.set_audio(None),
        "next": lambda: ctl.cycle_audio(1),
        "prev": lambda: ctl.cycle_audio(-1),
        "volup": lambda: ctl.nudge_volume(VOLUME_STEP),
        "voldown": lambda: ctl.nudge_volume(-VOLUME_STEP),
        "layout_next": lambda: ctl.step_layout(1),
        "layout_prev": lambda: ctl.step_layout(-1),
        "zoom": lambda: ctl.toggle_zoom(),
        "reload": lambda: ctl.reload_audible(),
        "skip": lambda: ctl.skip_audible(1),
        "skip_back": lambda: ctl.skip_audible(-1),
        "dismiss": lambda: ctl.dismiss_alert(),
    }


REPEATABLE = {"volup", "voldown"}

# evdev key/button names -> action
EVDEV_MAP = {
    **{"KEY_%d" % n: "digit%d" % n for n in range(10)},
    **{"KEY_KP%d" % n: "digit%d" % n for n in range(10)},
    "KEY_F1": "screen0", "KEY_F2": "screen1", "KEY_F3": "screen2", "KEY_F4": "screen3",
    "KEY_S": "screen_next",
    "KEY_M": "mute", "KEY_MUTE": "mute",
    "KEY_RIGHT": "next", "KEY_TAB": "next", "KEY_LEFT": "prev",
    "KEY_UP": "volup", "KEY_VOLUMEUP": "volup", "KEY_EQUAL": "volup", "KEY_KPPLUS": "volup",
    "KEY_DOWN": "voldown", "KEY_VOLUMEDOWN": "voldown", "KEY_MINUS": "voldown",
    "KEY_KPMINUS": "voldown",
    "KEY_L": "layout_next", "KEY_K": "layout_prev",
    "KEY_Z": "zoom", "KEY_ENTER": "zoom", "KEY_KPENTER": "zoom", "KEY_F": "zoom",
    "KEY_R": "reload",
    "KEY_N": "skip", "KEY_B": "skip_back",
    "KEY_D": "dismiss", "KEY_ESC": "dismiss",
    # media remotes / keyboards with media keys
    "KEY_NEXTSONG": "skip", "KEY_PREVIOUSSONG": "skip_back", "KEY_PLAYPAUSE": "mute",
    # gamepads (Linux names the face buttons by compass direction)
    "BTN_SOUTH": "face0", "BTN_EAST": "face1", "BTN_NORTH": "face2", "BTN_WEST": "face3",
    "BTN_THUMBR": "screen_next",
    "BTN_START": "mute", "BTN_SELECT": "layout_next",
    "BTN_TL": "prev", "BTN_TR": "next",
    "BTN_MODE": "zoom", "BTN_THUMBL": "zoom",
    "BTN_DPAD_LEFT": "prev", "BTN_DPAD_RIGHT": "next",
    "BTN_DPAD_UP": "volup", "BTN_DPAD_DOWN": "voldown",
    # cheap "joystick class" pads (many SNES/arcade USB pads)
    "BTN_TRIGGER": "face0", "BTN_THUMB": "face1", "BTN_THUMB2": "face2", "BTN_TOP": "face3",
    "BTN_BASE3": "layout_next", "BTN_BASE4": "mute", "BTN_TOP2": "screen_next",
}

# GTK keyval names -> action (fallback when evdev can't be used)
GDK_MAP = {
    **{str(n): "digit%d" % n for n in range(10)},
    **{"KP_%d" % n: "digit%d" % n for n in range(10)},
    "KP_Insert": "digit0", "KP_End": "digit1", "KP_Down": "digit2", "KP_Next": "digit3",
    "KP_Left": "digit4", "KP_Begin": "digit5", "KP_Right": "digit6", "KP_Home": "digit7",
    "KP_Up": "digit8", "KP_Prior": "digit9",
    "F1": "screen0", "F2": "screen1", "F3": "screen2", "F4": "screen3",
    "s": "screen_next", "S": "screen_next",
    "m": "mute", "M": "mute",
    "AudioMute": "mute",
    "Right": "next", "Tab": "next", "Left": "prev", "ISO_Left_Tab": "prev",
    "Up": "volup", "plus": "volup", "equal": "volup", "KP_Add": "volup", "AudioRaiseVolume": "volup",
    "Down": "voldown", "minus": "voldown", "KP_Subtract": "voldown", "AudioLowerVolume": "voldown",
    "l": "layout_next", "L": "layout_next", "k": "layout_prev", "K": "layout_prev",
    "z": "zoom", "Z": "zoom", "Return": "zoom", "KP_Enter": "zoom", "f": "zoom", "F": "zoom",
    "r": "reload", "R": "reload",
    "n": "skip", "N": "skip", "b": "skip_back", "B": "skip_back",
    "d": "dismiss", "D": "dismiss",
}


def run_action(ctl, action):
    fn = _action_table(ctl).get(action)
    if fn is None:
        return False
    try:
        fn()
    except Exception:
        log.exception("input action %s failed", action)
    return True


def handle_gdk_key(ctl, keyname):
    action = GDK_MAP.get(keyname)
    return run_action(ctl, action) if action else False


def _names(code):
    """All evdev names for a key code (some codes have aliases)."""
    name = ecodes.bytype[ecodes.EV_KEY].get(code)
    if name is None:
        return []
    return list(name) if isinstance(name, (list, tuple)) else [name]


class InputManager(threading.Thread):
    """Background thread reading every keyboard and game controller."""

    RESCAN_S = 3.0

    def __init__(self, ctl):
        super().__init__(name="inputs", daemon=True)
        self.ctl = ctl
        self.sel = selectors.DefaultSelector()
        self.devices = {}  # path -> InputDevice
        self.hat = {}
        self._last_repeat = 0.0

    @staticmethod
    def available():
        if evdev is None:
            return False, "python3-evdev is not installed"
        paths = evdev.list_devices()
        if not paths and os.path.isdir("/dev/input") and any(
                n.startswith("event") for n in os.listdir("/dev/input")):
            return False, "no permission to read /dev/input (add the user to the 'input' group)"
        return True, ""

    @staticmethod
    def _interesting(dev):
        caps = dev.capabilities()
        keys = set(caps.get(ecodes.EV_KEY, []))
        if not keys:
            return False
        if ecodes.KEY_1 in keys or ecodes.KEY_M in keys:  # keyboards and keypads
            return True
        pad_buttons = {ecodes.BTN_SOUTH, ecodes.BTN_TRIGGER, ecodes.BTN_START, ecodes.BTN_THUMB}
        if keys & pad_buttons:
            return True
        return bool(keys & {ecodes.KEY_MUTE, ecodes.KEY_VOLUMEUP, ecodes.KEY_PLAYPAUSE})

    def _scan(self):
        try:
            paths = set(evdev.list_devices())
        except OSError:
            return
        for path in list(self.devices):
            if path not in paths:
                self._drop(path)
        for path in paths - set(self.devices):
            try:
                dev = evdev.InputDevice(path)
            except OSError:
                continue
            if not self._interesting(dev):
                dev.close()
                continue
            self.devices[path] = dev
            self.sel.register(dev, selectors.EVENT_READ)
            log.info("input device: %s (%s)", dev.name, path)
            self.ctl.note("Input connected: %s" % dev.name)
        self.ctl.input_devices = sorted(d.name for d in self.devices.values())

    def _drop(self, path):
        dev = self.devices.pop(path, None)
        if dev is None:
            return
        try:
            self.sel.unregister(dev)
        except (KeyError, ValueError):
            pass
        try:
            dev.close()
        except OSError:
            pass
        self.hat.pop(path, None)
        log.info("input device removed: %s", path)
        self.ctl.input_devices = sorted(d.name for d in self.devices.values())

    def _handle(self, path, event):
        if event.type == ecodes.EV_KEY:
            if event.value == 0:
                return
            for name in _names(event.code):
                action = EVDEV_MAP.get(name)
                if action is None:
                    continue
                if event.value == 2 and action not in REPEATABLE:
                    return
                if event.value == 2:
                    now = time.monotonic()
                    if now - self._last_repeat < 0.08:
                        return
                    self._last_repeat = now
                run_action(self.ctl, action)
                return
        elif event.type == ecodes.EV_ABS and event.code in (ecodes.ABS_HAT0X, ecodes.ABS_HAT0Y):
            # d-pads that report as a hat switch: act on press, not release
            key = (path, event.code)
            prev = self.hat.get(key, 0)
            self.hat[key] = event.value
            if event.value == 0 or event.value == prev:
                return
            if event.code == ecodes.ABS_HAT0X:
                run_action(self.ctl, "next" if event.value > 0 else "prev")
            else:
                run_action(self.ctl, "voldown" if event.value > 0 else "volup")

    def run(self):
        next_scan = 0.0
        while True:
            now = time.monotonic()
            if now >= next_scan:
                self._scan()
                next_scan = now + self.RESCAN_S
            if not self.devices:
                time.sleep(self.RESCAN_S)
                continue
            for key, _mask in self.sel.select(timeout=1.0):
                dev = key.fileobj
                try:
                    for event in dev.read():
                        self._handle(dev.path, event)
                except (OSError, IOError):
                    self._drop(dev.path)


def start(ctl):
    """Start the evdev reader. Returns True if keyboard input is covered."""
    ok, why = InputManager.available()
    if not ok:
        log.warning("evdev input disabled: %s; using window key events instead", why)
        return False
    InputManager(ctl).start()
    return True

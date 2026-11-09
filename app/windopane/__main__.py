"""Entry point.

    python3 -m windopane                 fullscreen wall + web UI (normal mode)
    python3 -m windopane --window 1600x900   test screen 1 in a desktop window
    python3 -m windopane --monitors 960x540+0+0,960x540+960+0   two screens on one desktop
    python3 -m windopane --headless      web UI only, no display (development)
"""

import argparse
import ctypes
import ctypes.util
import logging
import os
import signal
import sys


def _xinit_threads():
    # libVLC's X11 video outputs need Xlib in thread-safe mode, and that has
    # to happen before GTK opens its own display connection.
    name = ctypes.util.find_library("X11")
    if name:
        try:
            ctypes.cdll.LoadLibrary(name).XInitThreads()
        except OSError:
            pass


def main(argv=None):
    parser = argparse.ArgumentParser(prog="windopane")
    parser.add_argument("--config", default=None, help="config file (default /var/lib/windopane/config.json)")
    parser.add_argument("--window", metavar="WxH", help="run in a normal window instead of fullscreen")
    parser.add_argument("--headless", action="store_true", help="web UI only, no display")
    parser.add_argument("--port", type=int, help="override the web UI port")
    parser.add_argument("--no-input", action="store_true", help="don't read keyboards/controllers via evdev")
    parser.add_argument("--monitors", metavar="WxH+X+Y,...",
                        help="use these screen areas instead of detecting monitors (testing two screens on one display)")
    parser.add_argument("--no-xrandr", action="store_true",
                        help="don't arrange HDMI outputs; keep whatever xrandr setup you made yourself")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    if args.config:
        os.environ["WINDOPANE_CONFIG"] = args.config

    if not args.headless:
        _xinit_threads()

    from . import __version__, config, controller, web

    ctl = controller.Controller(args.config or config.DEFAULT_PATH)
    logging.getLogger("windopane").info("WindoPane %s starting (config %s)", __version__, ctl.path)
    web.start_thread(ctl, port=args.port)
    from . import alerts
    alerts.AlertWatcher(ctl).start()

    if args.headless:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        try:
            signal.pause()
        except KeyboardInterrupt:
            pass
        return 0

    from . import inputs, livewatch
    evdev_ok = False if args.no_input else inputs.start(ctl)
    watcher = livewatch.LiveWatcher(ctl)
    ctl.subscribe(watcher.wake.set)  # check new takeover channels promptly
    watcher.start()

    from gi.repository import GLib
    from .display import App, parse_monitors

    size = None
    if args.window:
        w, _, h = args.window.lower().partition("x")
        size = (int(w), int(h))
    fake = parse_monitors(args.monitors) if args.monitors else None
    disp = App(ctl, windowed=size, input_active=evdev_ok, monitors=fake,
               manage_outputs=not args.no_xrandr)
    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, _quit)
    disp.run()
    disp.shutdown()
    return 0


def _quit():
    from gi.repository import Gtk
    Gtk.main_quit()
    return False


if __name__ == "__main__":
    sys.exit(main())

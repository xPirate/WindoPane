"""Restart the wall, reboot or shut down, from the web UI.

Reboot and shut down run the small root helper windopane-power, which the
installer allows the wall's user to run (with only those two arguments)
through /etc/sudoers.d/windopane.
"""

import logging
import os
import signal
import subprocess
import threading

log = logging.getLogger(__name__)

HELPER = "/opt/windopane/bin/windopane-power"
ACTIONS = ("reboot", "poweroff")


class PowerError(Exception):
    pass


def system(action):
    if action not in ACTIONS:
        raise ValueError("action must be reboot or poweroff")
    if not os.path.exists(HELPER):
        raise PowerError("the power helper isn't installed; re-run install.sh")
    try:
        out = subprocess.run(["sudo", "-n", HELPER, action], capture_output=True, text=True,
                             timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PowerError(str(exc)) from exc
    if out.returncode != 0:
        msg = (out.stderr or out.stdout).strip().splitlines()
        msg = msg[-1] if msg else "the power helper failed"
        if "password is required" in msg or "not allowed" in msg:
            msg = "this user isn't allowed to reboot or shut down; re-run install.sh"
        raise PowerError(msg)


def restart_wall(delay=1.0):
    """Exit cleanly; the session loop (windopane-xinit) starts the wall again."""
    def go():
        log.info("restart requested from the web UI")
        os.kill(os.getpid(), signal.SIGTERM)
    t = threading.Timer(delay, go)
    t.daemon = True
    t.start()

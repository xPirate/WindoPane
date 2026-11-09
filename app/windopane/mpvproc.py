"""One mpv process per video tile, drawing into the tile's X window and
controlled over mpv's JSON IPC socket.

Running mpv out of process keeps the wall alive whatever happens inside a
player: X errors, decoder crashes or hangs only take down that one mpv,
and the tile restarts it.
"""

import itertools
import json
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time

log = logging.getLogger(__name__)

_counter = itertools.count(1)
_sockdir = None


def available():
    return shutil.which("mpv") is not None


def _socket_dir():
    global _sockdir
    if _sockdir is None:
        base = os.environ.get("XDG_RUNTIME_DIR")
        if not base or not os.access(base, os.W_OK):
            base = None
        _sockdir = tempfile.mkdtemp(prefix="windopane-mpv-", dir=base)
    return _sockdir


class MpvProc:
    """A running mpv. ``on_event(dict)`` is called from a reader thread."""

    def __init__(self, xid, options, on_event):
        self.on_event = on_event
        self.props = {}
        self.lock = threading.Lock()
        self.sock = None
        self.alive = True
        self.path = os.path.join(_socket_dir(), "p%d-%d.sock" % (os.getpid(), next(_counter)))
        args = ["mpv", "--no-config", "--wid=%d" % xid, "--input-ipc-server=" + self.path,
                "--idle=yes", "--no-terminal", "--msg-level=all=error"]
        for key, value in options.items():
            args.append("--%s=%s" % (key.replace("_", "-"), value))
        self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     start_new_session=True)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("mpv exited with status %s" % self.proc.returncode)
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(self.path)
                self.sock = s
                break
            except OSError:
                s.close()
                time.sleep(0.05)
        if self.sock is None:
            self.terminate()
            raise RuntimeError("mpv did not open its control socket")
        threading.Thread(target=self._reader, name="mpv-ipc", daemon=True).start()
        for i, name in enumerate(("time-pos", "paused-for-cache", "video-params/h"), 1):
            self.command("observe_property", i, name)

    # -- IPC
    def command(self, *args):
        if self.sock is None:
            return
        data = (json.dumps({"command": list(args)}) + "\n").encode()
        with self.lock:
            try:
                self.sock.sendall(data)
            except OSError:
                pass

    def set(self, name, value):
        self.command("set_property", name, value)

    def _reader(self):
        buf = b""
        try:
            while True:
                chunk = self.sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for line in lines:
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    ev = msg.get("event")
                    if ev == "property-change":
                        self.props[msg.get("name")] = msg.get("data")
                    elif ev:
                        self.on_event(msg)
        except OSError:
            pass
        if self.alive:
            # mpv went away on its own: report it like a playback error
            self.alive = False
            self.on_event({"event": "end-file", "reason": "error", "file_error": "mpv stopped unexpectedly"})

    # -- state
    @property
    def time_pos(self):
        return self.props.get("time-pos")

    @property
    def paused_for_cache(self):
        return bool(self.props.get("paused-for-cache"))

    @property
    def video_height(self):
        return self.props.get("video-params/h")

    def terminate(self):
        """Stop mpv (blocking, call from a worker thread)."""
        self.alive = False
        try:
            self.command("quit")
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                log.warning("mpv %d did not exit", self.proc.pid)
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        try:
            os.unlink(self.path)
        except OSError:
            pass

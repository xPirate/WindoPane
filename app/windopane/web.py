"""Management web UI + JSON API."""

import hmac
import logging
import os
import threading
import time

from flask import Flask, Response, jsonify, request, send_from_directory

from . import __version__, adsb, alerts, controller, gpu, layouts, power, resolver, sysinfo

log = logging.getLogger(__name__)

STATIC = os.path.join(os.path.dirname(__file__), "static")


def create_app(ctl):
    app = Flask(__name__, static_folder=None)
    app.config["JSON_SORT_KEYS"] = False
    profile = sysinfo.profile()
    info_cache = {"t": 0, "ytdlp": None, "js": None}

    # ---------------------------------------------------------------- auth
    @app.before_request
    def _auth():
        password = ctl.cfg["web"].get("password") or ""
        if not password:
            return None
        # The wall's own ADS-B tiles load these from the same machine.
        if request.method == "GET" and request.remote_addr in ("127.0.0.1", "::1") and \
                request.path.startswith(("/tile/", "/api/adsb/", "/static/")):
            return None
        auth = request.authorization
        supplied = (auth.password if auth else "") or ""
        if hmac.compare_digest(supplied.encode(), password.encode()):
            return None
        return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="WindoPane"'})

    @app.after_request
    def _headers(resp):
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        return resp

    def _body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data

    @app.errorhandler(ValueError)
    def _bad(exc):
        return jsonify(error=str(exc)), 400

    @app.errorhandler(KeyError)
    def _missing(exc):
        return jsonify(error="not found: %s" % exc), 404

    # ---------------------------------------------------------------- static
    @app.get("/")
    @app.get("/settings")
    def index():
        return send_from_directory(STATIC, "index.html")

    @app.get("/tile/adsb/<sid>")
    def adsb_tile(sid):
        return send_from_directory(STATIC, "adsb.html")

    @app.get("/api/adsb/<sid>")
    def adsb_data(sid):
        stream = next((s for s in ctl.cfg["streams"] if s["id"] == sid), None)
        if stream is None:
            return jsonify(error="no such stream"), 404
        return jsonify(adsb.table(stream))

    @app.get("/static/<path:name>")
    def static_files(name):
        return send_from_directory(STATIC, name)

    # ---------------------------------------------------------------- state
    def _system():
        now = time.time()
        if now - info_cache["t"] > 300:
            info_cache.update(t=now, ytdlp=resolver.ytdlp_version(), js=resolver.has_js_runtime())
        return {
            "version": __version__,
            "hostname": sysinfo.hostname(),
            "model": profile["model"],
            "profile": profile["id"],
            "hw_h264": profile["hw_h264"],
            "temp_c": sysinfo.temperature_c(),
            "throttled": sysinfo.throttled(),
            "load": sysinfo.load_average(),
            "memory": sysinfo.memory(),
            "uptime_s": int(now - ctl.started),
            "ytdlp": info_cache["ytdlp"],
            "js_runtime": info_cache["js"],
            "monitors": ctl.monitors,
            "engine": getattr(ctl, "engine", None),
            "inputs": list(ctl.input_devices),
        }

    def _state():
        snap = ctl.snapshot()
        cfg = snap["config"]
        streams = []
        for st in cfg["streams"]:
            item = dict(st)
            item["resolved_kind"] = resolver.classify(st)
            if item["resolved_kind"] == "video":
                item["video_mode"] = resolver.video_mode(st)
            if st.get("takeover"):
                live = ctl.live_now.get(st["id"])
                item["live_now"] = bool(live)
                item["sent_back"] = bool(live and live["dismissed"])
            streams.append(item)
        takeovers = snap["takeovers"]
        monitors = ctl.monitors
        active = ctl.active_screens()
        screens = []
        for i, sc in enumerate(cfg["screens"]):
            mon = monitors[i] if monitors is not None and i < len(monitors) else None
            base = i * layouts.MAX_SLOTS
            screens.append({
                "index": i,
                "layout": sc["layout"],
                "slots": sc["slots"],
                "takeovers": {str(slot): sid for (s_i, slot), sid in takeovers.items() if s_i == i},
                "resolution": sc["resolution"],
                "enabled": sc["enabled"],
                "audio_device": sc["audio_device"],
                "connected": mon is not None if monitors is not None else i == 0,
                "active": i in active,
                "monitor": mon,
                "zoom": snap["zoom"][i],
                "tiles": ctl.tile_status[base: base + layouts.MAX_SLOTS],
            })
        audio = None
        if snap["audio_tile"] is not None:
            s_i, slot = controller.split(snap["audio_tile"])
            audio = {"screen": s_i, "slot": slot}
        settings = {k: cfg[k] for k in ("display", "player")}
        settings["alerts"] = {k: v for k, v in cfg["alerts"].items() if k != "feeds"}
        watcher = ctl.alert_watcher
        feeds = []
        for f in cfg["alerts"]["feeds"]:
            item = dict(f)
            item["status"] = watcher.feed_status(f["id"]) if watcher else {}
            feeds.append(item)
        settings["web"] = {"port": cfg["web"]["port"], "password_set": bool(cfg["web"]["password"])}
        return {
            "screens": screens,
            "layouts": layouts.describe(),
            "streams": streams,
            "volume": cfg["volume"],
            "audio": audio,
            "focus_screen": snap["focus_screen"],
            "audio_devices": list(ctl.audio_devices),
            "settings": settings,
            "now": time.time(),
            "events": ctl.events[-10:],
            "alerts": {"active": ctl.active_alerts(), "recent": ctl.recent_alerts(20), "feeds": feeds},
            "system": _system(),
        }

    @app.get("/api/state")
    def state():
        return jsonify(_state())

    # ---------------------------------------------------------------- streams
    @app.post("/api/streams")
    def add_stream():
        return jsonify(ctl.add_stream(_body())), 201

    @app.put("/api/streams/<sid>")
    def update_stream(sid):
        return jsonify(ctl.update_stream(sid, _body()))

    @app.delete("/api/streams/<sid>")
    def delete_stream(sid):
        ctl.delete_stream(sid)
        return jsonify(ok=True)

    @app.post("/api/streams/order")
    def order_streams():
        ids = _body().get("ids")
        if not isinstance(ids, list):
            raise ValueError("ids must be a list")
        ctl.reorder_streams(ids)
        return jsonify(ok=True)

    @app.post("/api/streams/test")
    def test_stream():
        """Resolve a URL without putting it on screen (checks yt-dlp etc.)."""
        data = _body()
        stream = {"url": resolver.validate_url(data.get("url")), "kind": data.get("kind", "auto")}
        stream["mode"] = data.get("mode") or "auto"
        kind = resolver.classify(stream)
        if kind == "video" and resolver.video_mode(stream) == "playlist":
            result = {}

            def listing():
                try:
                    pl = resolver.playlist_entries(stream["url"])
                    result.update(ok=True, kind="video", message="Playlist%s: %d videos" % (
                        (' "%s"' % pl["title"]) if pl["title"] else "", len(pl["entries"])))
                except Exception as exc:
                    result.update(ok=False, kind="video", message=str(exc))
            t = threading.Thread(target=listing, daemon=True)
            t.start()
            t.join(90)
            return jsonify(result or {"ok": False, "kind": "video", "message": "Timed out listing the playlist"})
        if kind == "web":
            return jsonify(ok=True, kind="web", message="Will open as a web page tile")
        if kind == "adsb":
            probe = dict(stream, location=data.get("location") or "")
            try:
                t = adsb.table(probe)
            except Exception as exc:  # report anything to the form
                return jsonify(ok=False, kind="adsb", message=str(exc))
            if t["error"] and not t["total"]:
                return jsonify(ok=False, kind="adsb", message=t["error"])
            where = "" if t["station"] else " (set a location for distances)"
            return jsonify(ok=True, kind="adsb",
                           message="Receiving %d aircraft from %s%s" % (t["total"], t["source"], where))
        result = {}

        def work():
            try:
                media = resolver.resolve(stream, int(data.get("max_height") or 720))
                result.update(ok=True, kind="video", via=media.via, title=media.title,
                              live=media.live, message="Playable via %s" % media.via)
            except resolver.NotLive as exc:
                result.update(ok=True, kind="video", message="Channel found: %s. The tile will wait and "
                              "start by itself when it goes live." % exc.detail.lower())
            except Exception as exc:
                result.update(ok=False, kind="video", message=str(exc))

        t = threading.Thread(target=work, daemon=True)
        t.start()
        t.join(60)
        if not result:
            result = {"ok": False, "kind": "video", "message": "Timed out resolving"}
        return jsonify(result)

    # ---------------------------------------------------------------- screen
    def _screen(data):
        return int(data.get("screen") or 0)

    @app.post("/api/layout")
    def set_layout():
        data = _body()
        ctl.set_layout(_screen(data), data.get("layout"))
        return jsonify(ok=True)

    @app.post("/api/slots")
    def set_slots():
        data = _body()
        if "slots" in data:
            ctl.set_slots(_screen(data), data["slots"])
        else:
            ctl.set_slot(_screen(data), int(data["slot"]), data.get("stream_id"))
        return jsonify(ok=True)

    @app.post("/api/slots/swap")
    def swap_slots():
        data = _body()
        ctl.swap_slots(_screen(data), int(data["a"]), int(data["b"]))
        return jsonify(ok=True)

    def _audio_json():
        audio = None
        if ctl.audio_tile is not None:
            s_i, slot = controller.split(ctl.audio_tile)
            audio = {"screen": s_i, "slot": slot}
        return {"audio": audio, "volume": ctl.cfg["volume"]}

    @app.post("/api/audio")
    def audio():
        data = _body()
        if "volume" in data:
            ctl.set_volume(data["volume"])
        if "slot" in data:
            ctl.set_audio_at(_screen(data), data["slot"])
        return jsonify(_audio_json())

    @app.post("/api/screen/focus")
    def focus_screen():
        ctl.switch_screen(1)
        return jsonify(focus_screen=ctl.focus_screen, **_audio_json())

    @app.post("/api/skip")
    def skip():
        data = _body()
        ctl.skip(_screen(data), int(data["slot"]), -1 if int(data.get("step", 1)) < 0 else 1)
        return jsonify(ok=True)

    @app.post("/api/live/send-back")
    def live_send_back():
        sid = str(_body().get("stream_id") or "")
        if not ctl.dismiss_live(sid):
            raise ValueError("that channel isn't taking over a tile")
        return jsonify(ok=True)

    @app.post("/api/zoom")
    def zoom():
        data = _body()
        slot = ctl.set_zoom(_screen(data), data.get("slot"))
        return jsonify(zoom=slot)

    @app.post("/api/reload")
    def reload_tiles():
        data = _body()
        screen, slot = data.get("screen"), data.get("slot")
        ctl.reload(None if screen is None else int(screen), None if slot is None else int(slot))
        return jsonify(ok=True)

    # ---------------------------------------------------------------- graphics drivers (PC)
    @app.get("/api/gpu")
    def gpu_report():
        return jsonify(gpu.report())

    @app.post("/api/gpu/driver")
    def gpu_driver():
        data = _body()
        if data.get("confirm") is not True:
            raise ValueError("tick the confirmation box first")
        try:
            gpu.apply(str(data.get("vendor")))
        except gpu.DriverError as exc:
            return jsonify(error=str(exc)), 409
        ctl.note("Video driver change started: %s" % data.get("vendor"))
        return jsonify(ok=True), 202

    @app.post("/api/gpu/reboot")
    def gpu_reboot():
        if _body().get("confirm") is not True:
            raise ValueError("confirm the reboot first")
        try:
            gpu.reboot()
        except gpu.DriverError as exc:
            return jsonify(error=str(exc)), 409
        return jsonify(ok=True), 202

    # ---------------------------------------------------------------- headline alerts
    @app.post("/api/alerts/feeds")
    def add_feed():
        return jsonify(ctl.add_feed(_body())), 201

    @app.put("/api/alerts/feeds/<fid>")
    def update_feed(fid):
        return jsonify(ctl.update_feed(fid, _body()))

    @app.delete("/api/alerts/feeds/<fid>")
    def delete_feed(fid):
        ctl.delete_feed(fid)
        return jsonify(ok=True)

    @app.post("/api/alerts/feeds/<fid>/check")
    def check_feed(fid):
        ctl._feed(fid)
        if ctl.alert_watcher:
            ctl.alert_watcher.check_now(fid)
        return jsonify(ok=True)

    @app.post("/api/alerts/feeds/<fid>/show")
    def show_feed(fid):
        feed = dict(ctl._feed(fid))
        if not ctl.alert_watcher:
            raise ValueError("alerts aren't running")
        try:
            alert = ctl.alert_watcher.show_latest(feed)
        except alerts.FeedError as exc:
            raise ValueError(str(exc)) from exc
        return jsonify(alert)

    @app.post("/api/alerts/test")
    def test_feed():
        data = _body()
        url = str(data.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("enter the feed's full http:// or https:// address")
        try:
            r = alerts.probe(url, data.get("match") or "")
        except alerts.FeedError as exc:
            return jsonify(ok=False, message=str(exc))
        name = r["title"] or "Feed"
        msg = "%s: %d headlines" % (name, r["count"])
        if data.get("match"):
            msg += ", %d matching" % r["matching"]
        return jsonify(ok=True, message=msg, title=r["title"], items=r["items"])

    @app.post("/api/alerts/dismiss")
    def dismiss_alerts():
        data = request.get_json(silent=True) or {}
        ctl.dismiss_alert(data.get("id") or None)
        return jsonify(ok=True)

    # ---------------------------------------------------------------- power
    @app.post("/api/system/power")
    def system_power():
        data = _body()
        action = data.get("action")
        if data.get("confirm") is not True:
            raise ValueError("confirm first")
        if action == "restart":
            ctl.note("Wall restart requested from the web UI")
            power.restart_wall()
            return jsonify(ok=True), 202
        if action not in power.ACTIONS:
            raise ValueError("action must be restart, reboot or poweroff")
        try:
            power.system(action)
        except power.PowerError as exc:
            return jsonify(error=str(exc)), 409
        ctl.note("%s requested from the web UI" % ("Reboot" if action == "reboot" else "Shut down"))
        return jsonify(ok=True), 202

    @app.post("/api/settings")
    def settings():
        ctl.update_settings(_body())
        return jsonify(ok=True)

    return app


def serve(ctl, host=None, port=None):
    app = create_app(ctl)
    host = host or ctl.cfg["web"].get("host", "0.0.0.0")
    port = int(port or ctl.cfg["web"].get("port", 8090))
    ctl.web_port = port
    log.info("web UI on http://%s:%d", host, port)
    try:
        from waitress import serve as waitress_serve
        waitress_serve(app, host=host, port=port, threads=6, ident="WindoPane")
    except ImportError:
        app.run(host=host, port=port, threaded=True, use_reloader=False)


def start_thread(ctl, host=None, port=None):
    t = threading.Thread(target=serve, args=(ctl, host, port), name="web", daemon=True)
    t.start()
    return t

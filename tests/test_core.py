"""Core tests: layouts, classification, controller and API.

Run:  python3 -m pytest tests   (or: python3 tests/test_core.py)
"""

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from windopane import adsb, controller, gpu, layouts, resolver, sysinfo  # noqa: E402


class LayoutTests(unittest.TestCase):
    def test_all_layouts_fit_and_have_tiles(self):
        for lid, spec in layouts.LAYOUTS.items():
            self.assertTrue(1 <= len(spec["tiles"]) <= layouts.MAX_SLOTS, lid)
            for x, y, w, h in spec["tiles"]:
                self.assertGreaterEqual(x, 0)
                self.assertGreaterEqual(y, 0)
                self.assertLessEqual(round(x + w, 6), 1, lid)
                self.assertLessEqual(round(y + h, 6), 1, lid)

    def test_non_overlay_tiles_cover_screen_exactly(self):
        for lid, spec in layouts.LAYOUTS.items():
            if spec.get("overlay"):
                continue
            rects = layouts.compute_rects(lid, 3840, 2160)
            area = sum(w * h for _, _, w, h in rects)
            if lid == "trio":
                self.assertEqual(area, 3840 * 2160 * 3 // 4)
            else:
                self.assertEqual(area, 3840 * 2160, lid)

    def test_quad_rects_4k(self):
        self.assertEqual(layouts.compute_rects("quad", 3840, 2160),
                         [(0, 0, 1920, 1080), (1920, 0, 1920, 1080),
                          (0, 1080, 1920, 1080), (1920, 1080, 1920, 1080)])

    def test_gap(self):
        r = layouts.compute_rects("split-v", 1920, 1080, gap=8)
        self.assertEqual(r[0], (4, 4, 952, 1072))
        self.assertEqual(r[1], (964, 4, 952, 1072))


class ClassifyTests(unittest.TestCase):
    def k(self, url, kind="auto"):
        return resolver.classify({"url": url, "kind": kind})

    def test_auto(self):
        self.assertEqual(self.k("https://www.youtube.com/watch?v=abc"), "video")
        self.assertEqual(self.k("https://www.twitch.tv/somechannel"), "video")
        self.assertEqual(self.k("rtsp://10.0.0.5:554/stream1"), "video")
        self.assertEqual(self.k("http://cam.local/live/index.m3u8"), "video")
        self.assertEqual(self.k("http://192.168.1.20/tar1090/"), "web")
        self.assertEqual(self.k("http://192.168.1.20:8080/"), "web")
        self.assertEqual(self.k("udp://@239.0.0.1:1234"), "video")

    def test_forced(self):
        self.assertEqual(self.k("http://192.168.1.20/tar1090/", "video"), "video")
        self.assertEqual(self.k("https://www.youtube.com/watch?v=abc", "web"), "web")
        self.assertEqual(self.k("rtsp://x/y", "web"), "video")

    def test_validate(self):
        with self.assertRaises(ValueError):
            resolver.validate_url("javascript:alert(1)")
        with self.assertRaises(ValueError):
            resolver.validate_url("")
        self.assertEqual(resolver.validate_url(" https://x.y/ "), "https://x.y/")

    def test_direct_resolve(self):
        m = resolver.resolve({"url": "rtsp://cam/1"}, 720, 1500)
        self.assertEqual(m.url, "rtsp://cam/1")
        self.assertIn(":rtsp-tcp", m.options)
        self.assertIn(":network-caching=1500", m.options)

    def test_format_selector_prefers_h264(self):
        self.assertTrue(resolver._format_selector(720).startswith("best[height<=720][vcodec^=avc1]"))

    def test_media_from_info_split_formats(self):
        info = {"title": "t", "is_live": False, "requested_formats": [
            {"url": "http://v", "vcodec": "avc1", "http_headers": {"User-Agent": "UA"}},
            {"url": "http://a", "vcodec": "none"}]}
        m = resolver._media_from_info(info)
        self.assertEqual(m.url, "http://v")
        self.assertIn(":input-slave=http://a", m.options)
        self.assertIn(":http-user-agent=UA", m.options)


class ProfileTests(unittest.TestCase):
    def test_profiles(self):
        self.assertEqual(sysinfo.profile("Raspberry Pi 5 Model B Rev 1.0")["id"], "pi5")
        self.assertEqual(sysinfo.profile("Raspberry Pi 4 Model B Rev 1.4")["id"], "pi4")
        self.assertEqual(sysinfo.profile("x86_64")["id"], "generic")

    def test_pick_height(self):
        p4 = sysinfo.profile("Raspberry Pi 4 Model B")
        self.assertEqual(sysinfo.pick_height(1080, 4, p4), 720)   # 2x2 at 4K
        self.assertEqual(sysinfo.pick_height(2160, 1, p4), 1080)  # single tile at 4K
        self.assertEqual(sysinfo.pick_height(540, 2, p4), 720)
        self.assertEqual(sysinfo.pick_height(1080, 4, p4, stream_max=1080), 1080)
        self.assertEqual(sysinfo.pick_height(1080, 1, p4, cap_setting=480), 480)
        self.assertEqual(sysinfo.pick_height(1080, 8, p4), 480)  # both screens full of video


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "config.json")
        self.c = controller.Controller(self.path)
        self.events = []
        self.c.subscribe(lambda: self.events.append(1))

    def add(self, url="rtsp://cam/1", **kw):
        return self.c.add_stream(dict(url=url, **kw))

    def two_screens(self):
        self.c.report_monitors([{"output": "HDMI-1"}, {"output": "HDMI-2"}])

    def test_defaults_start_muted(self):
        self.assertIsNone(self.c.audio_tile)
        self.assertEqual(len(self.c.cfg["screens"]), 4)
        self.assertEqual(len(self.c.cfg["screens"][3]["slots"]), layouts.MAX_SLOTS)
        self.assertEqual(self.c.cfg["screens"][0]["layout"], "quad")

    def test_add_assign_persist(self):
        s = self.add(name="Cam", slot=2)
        self.assertEqual(self.c.cfg["screens"][0]["slots"][2], s["id"])
        t = self.add(url="rtsp://cam/2", screen=1, slot=0)
        self.assertEqual(self.c.cfg["screens"][1]["slots"][0], t["id"])
        with open(self.path) as fh:
            saved = json.load(fh)
        self.assertEqual(saved["streams"][0]["name"], "Cam")
        self.assertTrue(self.events)
        c2 = controller.Controller(self.path)
        self.assertEqual(c2.cfg["screens"][0]["slots"][2], s["id"])
        self.assertIsNone(c2.audio_tile)

    def test_migrates_single_screen_config(self):
        with open(self.path, "w") as fh:
            json.dump({"layout": "pip", "slots": ["x", None, None, None],
                       "streams": [{"id": "x", "url": "rtsp://a/b", "name": "A"}],
                       "display": {"resolution": "1920x1080", "gap": 3}}, fh)
        c = controller.Controller(self.path)
        sc = c.cfg["screens"]
        self.assertEqual(sc[0]["layout"], "pip")
        self.assertEqual(sc[0]["slots"][0], "x")
        self.assertEqual(sc[0]["resolution"], "1920x1080")
        self.assertEqual(sc[1]["layout"], "quad")
        self.assertNotIn("layout", c.cfg)
        self.assertNotIn("resolution", c.cfg["display"])
        self.assertEqual(c.cfg["display"]["gap"], 3)

    def test_audio_rules_one_screen(self):
        a = self.add(slot=0)
        self.add(url="rtsp://cam/2", slot=3)
        self.c.report_monitors([{"output": "HDMI-1"}])
        self.assertEqual(self.c.set_audio(1), None)  # empty slot -> mute
        self.assertEqual(self.c.set_audio(0), 0)
        self.assertEqual(self.c.toggle_audio(0), None)
        self.assertEqual(self.c.cycle_audio(1), 0)
        self.assertEqual(self.c.cycle_audio(1), 3)
        self.assertEqual(self.c.cycle_audio(1), 0)
        self.assertEqual(self.c.cycle_audio(-1), 3)
        self.c.set_layout(0, "split-v")  # slot 3 now off-screen
        self.assertIsNone(self.c.audio_tile)
        self.c.set_audio(0)
        self.c.delete_stream(a["id"])
        self.assertIsNone(self.c.audio_tile)
        self.assertIsNone(self.c.cfg["screens"][0]["slots"][0])

    def test_audio_across_two_screens(self):
        self.add(slot=0)
        self.add(url="rtsp://cam/2", screen=1, slot=1)
        # screen 2 assigned but not plugged in: its tile can't be selected
        s2t2 = controller.tile_id(1, 1)
        self.c.report_monitors([{"output": "HDMI-1"}])
        self.assertIsNone(self.c.set_audio(s2t2))
        self.two_screens()
        self.assertEqual(self.c.set_audio(s2t2), s2t2)
        self.assertEqual(self.c.focus_screen, 1)
        self.assertEqual(self.c.cycle_audio(1), 0)
        self.assertEqual(self.c.focus_screen, 0)
        # face buttons follow the focused screen
        self.c.set_audio(None)
        self.c.switch_screen()
        self.assertEqual(self.c.focus_screen, 1)
        self.assertEqual(self.c.toggle_focus_slot(1), s2t2)
        # switching screens while listening moves the audio along
        self.assertEqual(self.c.switch_screen(), 0)
        self.assertEqual(self.c.audio_tile, 0)
        # unplugging screen 2 drops anything that pointed at it
        self.c.set_audio(s2t2)
        self.c.report_monitors([{"output": "HDMI-1"}])
        self.assertIsNone(self.c.audio_tile)
        self.assertEqual(self.c.focus_screen, 0)

    def test_screen_disable(self):
        self.add(url="rtsp://cam/2", screen=1, slot=0)
        self.two_screens()
        self.c.set_audio(controller.tile_id(1, 0))
        self.c.update_settings({"screens": [{}, {"enabled": False}]})
        self.assertIsNone(self.c.audio_tile)
        self.assertEqual(self.c.active_screens(), [0])
        self.c.update_settings({"screens": [{"enabled": False}]})  # screen 1 can't be disabled
        self.assertTrue(self.c.cfg["screens"][0]["enabled"])

    def test_zoom_per_screen(self):
        self.add(slot=1)
        self.add(url="rtsp://cam/2", screen=1, slot=0)
        self.two_screens()
        self.c.set_audio(1)
        self.c.toggle_zoom()
        self.assertEqual(self.c.zoom[:2], [1, None])
        self.c.set_zoom(1, 0)
        self.assertEqual(self.c.zoom[:2], [1, 0])
        self.c.toggle_zoom()
        self.assertEqual(self.c.zoom[:2], [None, 0])
        self.c.set_zoom(0, 2)  # empty -> no zoom
        self.assertEqual(self.c.zoom[0], None)

    def test_layout_keys_follow_focus(self):
        self.two_screens()
        self.c.switch_screen()
        self.c.step_layout(1)
        self.assertEqual(self.c.cfg["screens"][0]["layout"], "quad")
        self.assertNotEqual(self.c.cfg["screens"][1]["layout"], "quad")

    def test_volume_clamped(self):
        self.assertEqual(self.c.set_volume(500), 100)
        self.assertEqual(self.c.nudge_volume(-200), 0)

    def test_validation(self):
        with self.assertRaises(ValueError):
            self.add(url="ftp://x/y")
        with self.assertRaises(ValueError):
            self.add(url="rtsp://x/y", kind="web")
        with self.assertRaises(ValueError):
            self.add(url="http://x/", max_height=99999)
        with self.assertRaises(KeyError):
            self.c.set_slot(0, 0, "nope")
        with self.assertRaises(ValueError):
            self.c.set_slot(4, 0, None)
        with self.assertRaises(ValueError):
            self.c.set_slot(0, 16, None)

    def test_corrupt_config_recovers(self):
        with open(self.path, "w") as fh:
            fh.write("{not json")
        c = controller.Controller(self.path)
        self.assertEqual(c.cfg["streams"], [])
        self.assertTrue(os.path.exists(self.path + ".corrupt"))


class ApiTests(unittest.TestCase):
    def setUp(self):
        try:
            from windopane import web
        except ImportError as exc:
            self.skipTest("flask missing: %s" % exc)
        self.dir = tempfile.mkdtemp()
        self.c = controller.Controller(os.path.join(self.dir, "config.json"))
        self.c.report_monitors([{"output": "HDMI-1", "width": 3840, "height": 2160},
                                {"output": "HDMI-2", "width": 1920, "height": 1080}])
        self.app = web.create_app(self.c).test_client()

    def test_flow(self):
        r = self.app.get("/api/state")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json["layouts"]), len(layouts.LAYOUTS))
        self.assertEqual([s["connected"] for s in r.json["screens"]], [True, True, False, False])
        r = self.app.post("/api/streams", json={"url": "http://10.0.0.2/tar1090/", "name": "ADS-B", "slot": 0})
        self.assertEqual(r.status_code, 201, r.json)
        sid = r.json["id"]
        st = self.app.get("/api/state").json
        self.assertEqual(st["screens"][0]["slots"][0], sid)
        self.assertEqual(st["streams"][0]["resolved_kind"], "web")
        self.assertEqual(self.app.post("/api/audio", json={"slot": 0, "volume": 40}).json,
                         {"audio": {"screen": 0, "slot": 0}, "volume": 40})
        self.assertEqual(self.app.post("/api/layout", json={"screen": 1, "layout": "pip"}).status_code, 200)
        self.assertEqual(self.c.cfg["screens"][1]["layout"], "pip")
        self.assertEqual(self.app.post("/api/layout", json={"layout": "bogus"}).status_code, 400)
        self.assertEqual(self.app.post("/api/layout", json={"screen": 5, "layout": "pip"}).status_code, 400)
        self.assertEqual(self.app.put("/api/streams/" + sid, json={"name": "Map", "zoom": 1.5}).json["zoom"], 1.5)
        self.assertEqual(self.app.post("/api/slots/swap", json={"a": 0, "b": 1}).status_code, 200)
        self.assertEqual(self.c.cfg["screens"][0]["slots"][1], sid)
        self.assertEqual(self.c.audio_tile, 1)
        self.assertEqual(self.app.post("/api/slots", json={"screen": 1, "slot": 1, "stream_id": sid}).status_code, 200)
        r = self.app.post("/api/audio", json={"screen": 1, "slot": 1}).json
        self.assertEqual(r["audio"], {"screen": 1, "slot": 1})
        self.assertEqual(self.app.post("/api/zoom", json={"screen": 1, "slot": 1}).json, {"zoom": 1})
        self.assertEqual(self.app.post("/api/screen/focus", json={}).json["focus_screen"], 0)
        self.assertEqual(self.app.post("/api/reload", json={"screen": 1}).status_code, 200)
        self.assertEqual(self.app.delete("/api/streams/" + sid).status_code, 200)
        self.assertEqual(self.app.delete("/api/streams/" + sid).status_code, 404)
        self.assertEqual(self.app.post("/api/streams", data="x").status_code, 400)
        r = self.app.post("/api/streams/test", json={"url": "http://10.0.0.2/tar1090/"})
        self.assertEqual(r.json["kind"], "web")
        self.assertEqual(self.app.get("/").status_code, 200)
        self.assertEqual(self.app.get("/static/app.js").status_code, 200)

    def test_settings_and_password(self):
        r = self.app.post("/api/settings", json={
            "display": {"gap": 4},
            "screens": [{"resolution": "1920x1080"}, {"enabled": False, "audio_device": "default"}],
            "web": {"password": "s3cret"}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.app.get("/api/state").status_code, 401)
        import base64
        auth = {"Authorization": "Basic " + base64.b64encode(b"x:s3cret").decode()}
        st = self.app.get("/api/state", headers=auth).json
        self.assertEqual(st["screens"][0]["resolution"], "1920x1080")
        self.assertFalse(st["screens"][1]["enabled"])
        self.assertFalse(st["screens"][1]["active"])
        self.assertEqual(st["screens"][1]["audio_device"], "default")
        self.assertNotIn("password", st["settings"]["web"])
        self.assertEqual(self.app.post("/api/settings", json={"screens": [{"resolution": "big"}]},
                                       headers=auth).status_code, 400)


class GpuTests(unittest.TestCase):
    VDPAU = """display: :0   screen: 0
API version: 1
Information string: G3DVL VDPAU Driver Shared Library version 1.0

Decoder capabilities:

name                        level macbs width height
----------------------------------------------------
MPEG1                          --- not supported ---
MPEG2_SIMPLE                     3 65536  4096  4096
H264_BASELINE                   52 65536  4096  4096
H264_HIGH                       52 65536  4096  4096
VC1_SIMPLE                     --- not supported ---
HEVC_MAIN                      186 139264  8192  4352
VP9_PROFILE_0                  --- not supported ---

Output surface:

name              width height nat types
B8G8R8A8         16384 16384    y  NV12
"""
    VAINFO = """libva info: VA-API version 1.20.0
vainfo: Driver version: Mesa Gallium driver 25.2.8 for AMD Radeon RX 6600 XT (radeonsi, navi23)
vainfo: Supported profile and entrypoints
      VAProfileMPEG2Simple            :\tVAEntrypointVLD
      VAProfileH264ConstrainedBaseline:\tVAEntrypointVLD
      VAProfileH264Main               :\tVAEntrypointEncSlice
      VAProfileHEVCMain               :\tVAEntrypointVLD
      VAProfileVP9Profile0            :\tVAEntrypointVLD
      VAProfileAV1Profile0            :\tVAEntrypointVLD
      VAProfileNone                   :\tVAEntrypointVideoProc
"""

    def test_parse_vdpauinfo(self):
        r = gpu.parse_vdpauinfo(self.VDPAU)
        self.assertEqual(r["codecs"], ["H264", "HEVC", "MPEG2"])
        self.assertIn("G3DVL", r["driver"])
        self.assertEqual(gpu.parse_vdpauinfo("vdpauinfo: cannot connect")["codecs"], [])

    def test_parse_vainfo(self):
        r = gpu.parse_vainfo(self.VAINFO)
        self.assertEqual(r["codecs"], ["H264", "HEVC", "VP9", "AV1", "MPEG2"])
        self.assertIn("RX 6600 XT", r["driver"])

    def fake_sysfs(self, devices):
        root = tempfile.mkdtemp()
        for slot, cls, vendor in devices:
            d = os.path.join(root, slot)
            os.makedirs(d)
            for name, value in (("class", cls), ("vendor", vendor), ("device", "0x73ff"), ("boot_vga", "1")):
                with open(os.path.join(d, name), "w") as fh:
                    fh.write(value + "\n")
        return root

    def test_detect_and_recommend(self):
        root = self.fake_sysfs([("0000:00:02.0", "0x030000", "0x8086"),
                                ("0000:03:00.0", "0x030000", "0x1002"),
                                ("0000:03:00.1", "0x040300", "0x1002"),   # its HDMI audio: not a GPU
                                ("0000:04:00.0", "0x010802", "0x144d")])  # NVMe
        gpus = gpu.detect_gpus(root)
        self.assertEqual([g["vendor"] for g in gpus], ["intel", "amd"])
        self.assertEqual(gpu.recommended(gpus), "amd")
        self.assertEqual(gpu.recommended([{"vendor": "intel"}, {"vendor": "nvidia"}]), "intel")
        self.assertEqual(gpu.recommended([]), "none")

    def test_current_vendor_and_job_state(self):
        d = tempfile.mkdtemp()
        env = os.path.join(d, "gpu.env")
        with open(env, "w") as fh:
            fh.write("# x\nWINDOPANE_GPU=amd\nVDPAU_DRIVER=radeonsi\n")
        self.assertEqual(gpu.current_vendor(env), "amd")
        self.assertIsNone(gpu.current_vendor(os.path.join(d, "missing")))

    def test_api_requires_confirmation(self):
        from windopane import web
        c = controller.Controller(os.path.join(tempfile.mkdtemp(), "c.json"))
        app = web.create_app(c).test_client()
        r = app.post("/api/gpu/driver", json={"vendor": "amd"})
        self.assertEqual(r.status_code, 400)
        r = app.post("/api/gpu/driver", json={"vendor": "amd; rm -rf /", "confirm": True})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(app.post("/api/gpu/reboot", json={}).status_code, 400)
        self.assertIn(app.get("/api/gpu").json["platform"], ("pc", "pi"))


class FakeFeed:
    def __init__(self, aircraft, receiver=None):
        self.data = {"aircraft": aircraft, "error": None, "receiver": receiver or {},
                     "msg_rate": 12.5, "source": "fake"}

    def snapshot(self):
        return self.data


def ac(**kw):
    base = {"hex": "abc123", "flight": "", "alt": None, "ground": False, "gs": None, "track": None,
            "vrate": None, "squawk": "", "lat": None, "lon": None, "rssi": None, "seen": 1.0,
            "seen_pos": None, "category": "", "emergency": ""}
    base.update(kw)
    return base


class AdsbTests(unittest.TestCase):
    def test_maidenhead(self):
        lat, lon = adsb.maidenhead_to_latlon("FN31pr")   # W1AW, Newington CT
        self.assertAlmostEqual(lat, 41.729, places=2)
        self.assertAlmostEqual(lon, -72.708, places=2)
        lat, lon = adsb.maidenhead_to_latlon("JJ00")     # 0,0 corner square
        self.assertEqual((lat, lon), (0.5, 1.0))
        lat8, lon8 = adsb.maidenhead_to_latlon("FN31pr21")  # inside the FN31pr square
        self.assertTrue(41.7083 < lat8 < 41.75 and -72.75 < lon8 < -72.6667)
        self.assertEqual(adsb.parse_location("fn31PR"), adsb.parse_location("FN31pr"))

    def test_latlon_forms(self):
        self.assertEqual(adsb.parse_location("35.47, -97.52"), (35.47, -97.52))
        self.assertEqual(adsb.parse_location("35.47N 97.52W"), (35.47, -97.52))
        self.assertEqual(adsb.parse_location("-33.86;151.21"), (-33.86, 151.21))
        for bad in ("", "here", "95, 10", "10, 200", "ZZ99"):
            with self.assertRaises(ValueError):
                adsb.parse_location(bad)

    def test_distance_bearing(self):
        d, b = adsb.distance_bearing(0, 0, 0, 1)
        self.assertAlmostEqual(d, 111.19, places=1)
        self.assertAlmostEqual(b, 90.0, places=3)
        d, b = adsb.distance_bearing(51.5, 0, 52.5, 0)
        self.assertAlmostEqual(b, 0.0, places=3)
        self.assertEqual(adsb.compass(350), "N")
        self.assertEqual(adsb.compass(225), "SW")
        self.assertEqual(adsb.arrow(90), "→")

    def test_parse_sbs(self):
        line = "MSG,3,1,1,A1B2C3,1,2026/11/03,22:00:00.000,2026/11/03,22:00:00.000,,35000,,,35.1,-97.2,,,0,0,0,0"
        hexid, f = adsb.parse_sbs_line(line)
        self.assertEqual(hexid, "a1b2c3")
        self.assertEqual((f["alt"], f["lat"], f["lon"], f["ground"]), (35000.0, 35.1, -97.2, False))
        hexid, f = adsb.parse_sbs_line("MSG,6,1,1,A1B2C3,1,d,t,d,t,,,,,,,,7700,0,-1,0,0")
        self.assertEqual((f["squawk"], f["emergency"]), ("7700", "emergency"))
        self.assertIsNone(adsb.parse_sbs_line("STA,,5,179,400AE7,10103,2008/11/28"))
        self.assertIsNone(adsb.parse_sbs_line("garbage"))

    def test_normalize_json_variants(self):
        new = adsb.normalize_json({"hex": "~ABC", "flight": "SWA1 ", "alt_baro": "ground", "gs": 12, "seen": 1})
        self.assertTrue(new["ground"])
        self.assertIsNone(new["alt"])
        self.assertEqual((new["hex"], new["flight"]), ("abc", "SWA1"))
        old = adsb.normalize_json({"hex": "abc", "altitude": 9000, "speed": 250, "vert_rate": -500})
        self.assertEqual((old["alt"], old["gs"], old["vrate"]), (9000.0, 250.0, -500.0))

    def test_table_sorting_units_range_and_alerts(self):
        feed = FakeFeed([
            ac(hex="aaa001", flight="FAR", lat=36.5, lon=-97.0, alt=30000),
            ac(hex="aaa002", flight="NEAR", lat=35.6, lon=-97.0, alt=5000),
            ac(hex="aaa003", flight="NOPOS", alt=12000),
            ac(hex="aaa004", flight="MAYDAY", squawk="7700", lat=36.9, lon=-97.0),
        ])
        t = adsb.table({"url": "x", "name": "t", "location": "35.5, -97.0", "units": "km"}, feed)
        self.assertEqual([r["flight"] for r in t["aircraft"]], ["MAYDAY", "NEAR", "FAR", "NOPOS"])
        self.assertEqual(t["aircraft"][0]["alert"], "EMERGENCY")
        self.assertAlmostEqual(t["aircraft"][1]["dist"], 11.1, places=1)
        self.assertEqual(t["aircraft"][1]["brg_card"], "N")
        nm = adsb.table({"url": "x", "location": "35.5, -97.0", "units": "nm"}, feed)
        self.assertAlmostEqual(nm["aircraft"][1]["dist"], 6.0, places=1)
        ranged = adsb.table({"url": "x", "location": "35.5, -97.0", "units": "km", "max_range": 50}, feed)
        self.assertEqual([r["flight"] for r in ranged["aircraft"]], ["NEAR", "NOPOS"])
        self.assertEqual(ranged["with_pos"], 1)

    def test_table_uses_receiver_location(self):
        feed = FakeFeed([ac(lat=35.6, lon=-97.0)], receiver={"lat": 35.5, "lon": -97.0})
        t = adsb.table({"url": "x"}, feed)
        self.assertEqual(t["station"]["from"], "receiver")
        self.assertIsNotNone(t["aircraft"][0]["dist"])
        none = adsb.table({"url": "x"}, FakeFeed([ac(lat=35.6, lon=-97.0)]))
        self.assertIsNone(none["station"])
        self.assertIsNone(none["aircraft"][0]["dist"])

    def test_stream_validation(self):
        c = controller.Controller(os.path.join(tempfile.mkdtemp(), "c.json"))
        s = c.add_stream({"url": "tcp://192.168.1.9:30003", "location": "EM15fl", "units": "km"})
        self.assertEqual(resolver.classify(s), "adsb")
        self.assertEqual(s["units"], "km")
        with self.assertRaises(ValueError):
            c.add_stream({"url": "http://pi/tar1090/", "kind": "adsb", "location": "nowhere"})
        with self.assertRaises(ValueError):
            c.add_stream({"url": "rtsp://cam/1", "kind": "adsb"})
        with self.assertRaises(ValueError):
            c.add_stream({"url": "http://pi/", "kind": "adsb", "units": "furlongs"})

    def test_adsb_api_and_loopback_exemption(self):
        from windopane import web
        c = controller.Controller(os.path.join(tempfile.mkdtemp(), "c.json"))
        c.update_settings({"web": {"password": "pw"}})
        s = c.add_stream({"url": "http://127.0.0.1:9/tar1090/", "kind": "adsb", "location": "EM15"})
        app = web.create_app(c).test_client()
        r = app.get("/api/adsb/" + s["id"], environ_base={"REMOTE_ADDR": "127.0.0.1"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("error", r.json)
        self.assertEqual(app.get("/tile/adsb/" + s["id"], environ_base={"REMOTE_ADDR": "127.0.0.1"}).status_code, 200)
        self.assertEqual(app.get("/api/adsb/" + s["id"], environ_base={"REMOTE_ADDR": "10.0.0.7"}).status_code, 401)
        self.assertEqual(app.get("/api/state", environ_base={"REMOTE_ADDR": "127.0.0.1"}).status_code, 401)


class MpvOptionTests(unittest.TestCase):
    def test_translate_resolved_media(self):
        from windopane.display import MpvTile
        media = resolver.Media("http://v", [":http-user-agent=UA", ":http-referrer=https://r/",
                                            ":input-slave=http://a", ":rtsp-tcp", ":network-caching=1500"])
        opts, audio = MpvTile.mpv_options(media, 1500)
        self.assertEqual(audio, "http://a")
        self.assertEqual(opts["user_agent"], "UA")
        self.assertEqual(opts["referrer"], "https://r/")
        self.assertEqual(opts["rtsp_transport"], "tcp")
        self.assertEqual(opts["cache_secs"], "1.5")


class ScaleTests(unittest.TestCase):
    def setUp(self):
        self.c = controller.Controller(os.path.join(tempfile.mkdtemp(), "c.json"))
        self.c.report_monitors([{"output": "DP-%d" % i} for i in range(4)])

    def fill(self, screen, n):
        for i in range(n):
            self.c.add_stream({"url": "rtsp://cam/%d-%d" % (screen, i), "screen": screen, "slot": i})

    def test_four_screens_sixteen_tiles(self):
        self.c.set_layout(3, "grid-4x4")
        self.fill(3, 16)
        self.assertEqual(len(self.c.visible_tiles(3)), 16)
        t = controller.tile_id(3, 15)
        self.assertEqual(self.c.set_audio(t), t)
        self.assertEqual(self.c.focus_screen, 3)
        self.assertEqual(controller.split(t), (3, 15))

    def test_number_keys_small_screen_act_at_once(self):
        self.fill(0, 4)
        self.assertEqual(self.c.digit(3), controller.tile_id(0, 2))
        self.assertEqual(self.c.digit(3), None)          # same key again mutes
        self.assertIsNone(self.c.digit(7))                 # no tile 7 on a 2x2
        self.c.digit(2)
        self.assertIsNone(self.c.digit(0))                 # 0 mutes

    def test_number_keys_two_digits_on_big_screens(self):
        self.c.set_layout(1, "grid-4x4")
        self.fill(1, 16)
        self.c.focus(1)
        self.assertEqual(self.c.digit(1), "pending")
        self.assertEqual(self.c.digit(2), controller.tile_id(1, 11))   # tile 12
        self.assertEqual(self.c.digit(5), controller.tile_id(1, 4))    # 2-9 act at once
        self.c.DIGIT_WAIT_S = 0.05
        self.assertEqual(self.c.digit(1), "pending")
        time.sleep(0.3)                                             # timeout: tile 1
        self.assertEqual(self.c.audio_tile, controller.tile_id(1, 0))
        self.c.digit(1)
        self.assertIsNone(self.c.digit(9))                          # 19 doesn't exist
        self.assertEqual(self.c.audio_tile, controller.tile_id(1, 0))

    def test_focus_keys(self):
        self.fill(0, 2)
        self.fill(2, 2)
        self.c.set_audio(controller.tile_id(0, 1))
        self.assertEqual(self.c.focus(2), 2)
        self.assertEqual(self.c.audio_tile, controller.tile_id(2, 0))  # audio follows
        self.c.report_monitors([{"output": "DP-0"}, {"output": "DP-1"}])
        self.assertEqual(self.c.focus(3), 0)                       # not connected: ignored

    def test_new_layouts(self):
        for lid, n in (("grid-3x2", 6), ("main-5", 6), ("grid-3x3", 9), ("main-7", 8),
                       ("main-12", 13), ("grid-4x4", 16)):
            self.assertEqual(layouts.tile_count(lid), n)
            rects = layouts.compute_rects(lid, 3840, 2160)
            self.assertEqual(sum(w * h for _, _, w, h in rects), 3840 * 2160, lid)

    def test_quality_tiers(self):
        pc = sysinfo.profile("x86_64")
        self.assertEqual(sysinfo.pick_height(540, 16, pc), 480)
        self.assertEqual(sysinfo.pick_height(1080, 6, pc), 720)
        p4 = sysinfo.profile("Raspberry Pi 4 Model B")
        self.assertEqual(sysinfo.pick_height(540, 9, p4), 360)


class ArrangeTests(unittest.TestCase):
    XRANDR = """Screen 0: minimum 320 x 200, current 9600 x 2160, maximum 16384 x 16384
DisplayPort-0 connected primary 3840x2160+0+0 (normal left inverted right x axis y axis) 600mm x 340mm
   3840x2160     60.00*+  30.00
   1920x1080     60.00
HDMI-A-0 connected 1920x1080+3840+0 (normal) 520mm x 290mm
   1920x1080     60.00*+  50.00
DisplayPort-1 connected (normal left inverted right x axis y axis)
   2560x1440     59.95 +
   1920x1080     60.00
DisplayPort-2 disconnected 1920x1080+5760+0 (normal) 0mm x 0mm
"""

    def test_parse(self):
        from windopane.display import parse_xrandr
        connected, stale, modes, max_size = parse_xrandr(self.XRANDR)
        self.assertEqual(connected, ["HDMI-A-0", "DisplayPort-0", "DisplayPort-1"])
        self.assertEqual(stale, ["DisplayPort-2"])
        self.assertEqual(modes["DisplayPort-1"]["preferred"], (2560, 1440))
        self.assertEqual(max_size, (16384, 16384))

    def test_arrange(self):
        from windopane.display import arrange
        k4 = (3840, 2160)
        self.assertEqual(arrange([k4] * 4, "auto", (16384, 16384)),
                         [(0, 0), (3840, 0), (7680, 0), (11520, 0)])
        self.assertEqual(arrange([k4] * 4, "auto", (8192, 8192)),
                         [(0, 0), (3840, 0), (0, 2160), (3840, 2160)])
        self.assertEqual(arrange([k4, (1920, 1080), (2560, 1440)], "grid"),
                         [(0, 0), (3840, 0), (0, 2160)])
        self.assertEqual(arrange([k4, k4], "grid"), [(0, 0), (3840, 0)])   # 2 screens stay a row
        self.assertEqual(arrange([], "auto"), [])

    def test_inputs_map(self):
        from windopane import inputs
        self.assertEqual(inputs.EVDEV_MAP["KEY_7"], "digit7")
        self.assertEqual(inputs.EVDEV_MAP["KEY_F3"], "screen2")
        self.assertEqual(inputs.GDK_MAP["F4"], "screen3")
        self.assertEqual(inputs.EVDEV_MAP["BTN_SOUTH"], "face0")


class PlaylistLiveTests(unittest.TestCase):
    def setUp(self):
        import fake_ytdlp
        self.fake = fake_ytdlp
        self.flagdir = tempfile.mkdtemp()
        fake_ytdlp.LIVE_FLAG = os.path.join(self.flagdir, "live")
        self._orig = resolver._ytdlp_module
        resolver._ytdlp_module = lambda: fake_ytdlp

    def tearDown(self):
        resolver._ytdlp_module = self._orig

    def test_playlist_listing_skips_private(self):
        pl = resolver.playlist_entries("https://www.youtube.com/playlist?list=PL1")
        self.assertEqual(pl["title"], "Test playlist")
        self.assertEqual([e["id"] for e in pl["entries"]], ["AAA", "GONE", "BBB"])
        self.assertTrue(self.fake.CALLS[-1][1], "playlist must be listed flat (fast, no per-video lookups)")

    def test_source_sequence_wraps(self):
        from windopane import sources
        src = sources.Source({"id": "p", "url": "https://www.youtube.com/playlist?list=PL1"})
        self.assertEqual(src.mode, "playlist")
        target, item = src.prepare()
        self.assertEqual((target["url"], item["label"]), ("https://www.youtube.com/watch?v=AAA", "1/3"))
        self.assertEqual(target["mode"], "single")
        media = resolver.resolve(target, 720)
        self.assertTrue(media.url.endswith("short_a.mp4"))
        src.advance()
        target, item = src.prepare()
        with self.assertRaises(resolver.ResolveError):
            resolver.resolve(target, 720)                     # removed video
        src.advance()
        src.advance()
        self.assertEqual(src.position(), {"pos": 1, "total": 3})   # wrapped
        src.advance(-1)
        self.assertEqual(src.position()["pos"], 3)

    def test_source_keeps_place_on_refresh_and_shuffles(self):
        from windopane import sources
        calls = []

        def lister(url):
            calls.append(url)
            return {"title": "t", "entries": [{"id": str(i), "url": "u%d" % i, "title": str(i)} for i in range(20)]}
        src = sources.Source({"url": "https://www.youtube.com/playlist?list=X", "shuffle": True}, lister)
        seen = set()
        for _ in range(20):
            t, _item = src.prepare()
            seen.add(t["url"])
            src.advance()
        self.assertEqual(len(seen), 20)                  # every video once per pass
        t, _item = src.prepare()
        src._refresh(force=True)
        t2, _item = src.prepare()
        self.assertEqual(t["url"], t2["url"])             # refresh keeps the current video
        self.assertEqual(len(calls), 2)

    def test_live_channel(self):
        stream = {"url": "https://www.youtube.com/@testchan/live"}
        with self.assertRaises(resolver.NotLive) as cm:
            resolver.resolve(stream, 720)
        self.assertEqual(cm.exception.detail, "Not live right now")
        with self.assertRaises(resolver.NotLive) as cm:
            resolver.resolve({"url": "https://www.youtube.com/@upcoming/live"}, 720)
        self.assertEqual(cm.exception.detail, "Scheduled: starts in 25 minutes")
        open(self.fake.LIVE_FLAG, "w").close()
        media = resolver.resolve(stream, 720)
        self.assertTrue(media.live)
        # a finished broadcast must not be treated as live
        self.fake.VIDEOS["CCC"] = ("short_a.mp4", "old")
        with self.assertRaises(resolver.NotLive):
            resolver.resolve({"url": "https://www.youtube.com/watch?v=CCC", "mode": "live"}, 720)

    def test_controller_fields_and_skip(self):
        c = controller.Controller(os.path.join(tempfile.mkdtemp(), "c.json"))
        s = c.add_stream({"url": "https://www.youtube.com/playlist?list=PL1", "mode": "playlist",
                          "shuffle": True, "slot": 0})
        self.assertEqual((s["mode"], s["shuffle"]), ("playlist", True))
        s = c.update_stream(s["id"], {"mode": "auto", "shuffle": False})
        self.assertNotIn("mode", s)
        self.assertNotIn("shuffle", s)
        live = c.add_stream({"url": "https://www.youtube.com/@x/live", "check_minutes": 5})
        self.assertEqual(live["check_minutes"], 5)
        with self.assertRaises(ValueError):
            c.add_stream({"url": "https://www.youtube.com/@x/live", "check_minutes": 0.5})
        with self.assertRaises(ValueError):
            c.add_stream({"url": "https://x.y/", "mode": "loop"})
        c.report_monitors([{"output": "HDMI-1"}])
        c.skip(0, 0)
        c.skip(0, 0, -1)
        c.skip(0, 0)
        self.assertEqual(c.snapshot()["skip_tokens"][0], 1)
        c.set_audio(0)
        c.skip_audible()
        self.assertEqual(c.skip_tokens[0], 2)

    def test_api_test_button(self):
        from windopane import web
        c = controller.Controller(os.path.join(tempfile.mkdtemp(), "c.json"))
        app = web.create_app(c).test_client()
        r = app.post("/api/streams/test", json={"url": "https://www.youtube.com/playlist?list=PL1"}).json
        self.assertTrue(r["ok"])
        self.assertIn("3 videos", r["message"])
        r = app.post("/api/streams/test", json={"url": "https://www.youtube.com/@testchan/live"}).json
        self.assertTrue(r["ok"])
        self.assertIn("wait", r["message"])
        s = c.add_stream({"url": "https://www.youtube.com/playlist?list=PL1"})
        st = app.get("/api/state").json
        self.assertEqual(st["streams"][0]["video_mode"], "playlist")
        self.assertEqual(app.post("/api/skip", json={"screen": 0, "slot": 3}).status_code, 200)
        self.assertEqual(app.post("/api/skip", json={"screen": 0, "slot": 99}).status_code, 400)
        self.assertTrue(s)


class TakeoverTests(unittest.TestCase):
    YT = "https://www.youtube.com/@chan%s/live"

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.c = controller.Controller(os.path.join(self.dir, "config.json"))
        self.c.report_monitors([{"output": "HDMI-1"}, {"output": "HDMI-2"}])
        self.cam = self.c.add_stream({"url": "rtsp://cam/1", "name": "Cam", "screen": 0, "slot": 0})["id"]

    def chan(self, n, screen=0, slot=0, prio=None):
        data = {"url": self.YT % n, "name": "Chan%s" % n, "takeover": "%d:%d" % (screen, slot)}
        if prio:
            data["priority"] = prio
        return self.c.add_stream(data)["id"]

    def test_validation(self):
        with self.assertRaises(ValueError):
            self.c.add_stream({"url": "rtsp://cam/2", "takeover": "0:0"})       # not a live channel
        with self.assertRaises(ValueError):
            self.c.add_stream({"url": self.YT % 1, "takeover": "0:0", "priority": 12})
        with self.assertRaises(ValueError):
            self.c.add_stream({"url": self.YT % 1, "takeover": "0:99"})
        sid = self.chan(1, 1, 2, prio=3)
        st = next(s for s in self.c.cfg["streams"] if s["id"] == sid)
        self.assertEqual((st["takeover"], st["priority"]), ({"screen": 1, "slot": 2}, 3))
        self.c.update_stream(sid, {"takeover": "", "priority": None})
        st = next(s for s in self.c.cfg["streams"] if s["id"] == sid)
        self.assertNotIn("takeover", st)
        self.assertNotIn("priority", st)

    def test_takeover_and_hand_back(self):
        a = self.chan(1)
        self.assertEqual(self.c.effective_slots(0)[0], self.cam)
        self.c.set_live(a, True)
        self.assertEqual(self.c.effective_slots(0)[0], a)
        self.assertEqual(self.c.cfg["screens"][0]["slots"][0], self.cam)   # assignment untouched
        snap = self.c.snapshot()
        self.assertEqual(snap["takeovers"], {(0, 0): a})
        self.assertEqual(snap["effective"][0][0], a)
        self.c.live_ended(a)
        self.assertEqual(self.c.effective_slots(0)[0], self.cam)
        self.assertIn(a, self.c.live_cooldown)

    def test_takeover_of_empty_tile_is_visible(self):
        a = self.chan(1, 1, 3)
        self.assertNotIn(controller.tile_id(1, 3), self.c.visible_tiles())
        self.c.set_live(a, True)
        self.assertIn(controller.tile_id(1, 3), self.c.visible_tiles())

    def test_priority_and_queue(self):
        low = self.chan(1, prio=7)
        high = self.chan(2, prio=2)
        self.c.set_live(low, True)
        self.assertEqual(self.c.effective_slots(0)[0], low)
        self.c.set_live(high, True)                       # higher priority bumps it
        self.assertEqual(self.c.effective_slots(0)[0], high)
        self.c.live_ended(high)                           # queued one comes back
        self.assertEqual(self.c.effective_slots(0)[0], low)
        self.c.live_ended(low)
        self.assertEqual(self.c.effective_slots(0)[0], self.cam)

    def test_ties_go_to_first_live(self):
        a, b = self.chan(1), self.chan(2)
        self.c.set_live(b, True)
        time.sleep(0.01)
        self.c.set_live(a, True)
        self.assertEqual(self.c.effective_slots(0)[0], b)

    def test_send_back_and_rearm(self):
        a = self.chan(1)
        self.c.set_live(a, True)
        self.assertTrue(self.c.dismiss_live(a))
        self.assertFalse(self.c.dismiss_live(a))
        self.assertEqual(self.c.effective_slots(0)[0], self.cam)
        self.c.set_live(a, True)                          # still live: stays sent back
        self.assertEqual(self.c.effective_slots(0)[0], self.cam)
        self.c.set_live(a, False)                         # broadcast over: re-armed
        self.c.set_live(a, True)
        self.assertEqual(self.c.effective_slots(0)[0], a)

    def test_delete_drops_live_state(self):
        a = self.chan(1)
        self.c.set_live(a, True)
        self.c.delete_stream(a)
        self.assertEqual(self.c.takeovers(), {})
        self.assertEqual(self.c.effective_slots(0)[0], self.cam)

    def test_watcher(self):
        from windopane import livewatch
        a = self.chan(1)
        self.c.update_stream(a, {"check_minutes": 1})
        answers = {"live": False}
        calls = []

        def check(stream):
            calls.append(stream["id"])
            return answers["live"]
        w = livewatch.LiveWatcher(self.c, check=check)
        w.poll_once(now=0)
        self.assertEqual(calls, [a])
        w.poll_once(now=30)                               # not due yet
        self.assertEqual(len(calls), 1)
        answers["live"] = True
        w.poll_once(now=61)
        self.assertEqual(self.c.effective_slots(0)[0], a)
        w.poll_once(now=200)                              # on the board: the tile watches it
        self.assertEqual(len(calls), 2)
        self.c.live_ended(a)
        w.poll_once(now=400)                              # cooldown right after the end
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.c.effective_slots(0)[0], self.cam)
        self.c.live_cooldown[a] -= 120
        answers["live"] = None                            # a failed check changes nothing
        w.poll_once(now=600)
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.c.effective_slots(0)[0], self.cam)
        # sent back while live, then the broadcast ends -> re-armed
        self.c.set_live(a, True)
        self.c.dismiss_live(a)
        answers["live"] = False
        w.poll_once(now=700)
        self.assertNotIn(a, self.c.live_now)

    def test_api(self):
        try:
            from windopane import web
        except ImportError as exc:
            self.skipTest("flask missing: %s" % exc)
        app = web.create_app(self.c).test_client()
        r = app.post("/api/streams", json={"url": self.YT % 9, "name": "News", "takeover": "0:0", "priority": 1})
        self.assertEqual(r.status_code, 201, r.json)
        sid = r.json["id"]
        st = app.get("/api/state").json
        item = next(s for s in st["streams"] if s["id"] == sid)
        self.assertEqual((item["video_mode"], item["live_now"]), ("live", False))
        self.assertEqual(st["screens"][0]["takeovers"], {})
        self.assertEqual(app.post("/api/live/send-back", json={"stream_id": sid}).status_code, 400)
        self.c.set_live(sid, True)
        st = app.get("/api/state").json
        self.assertEqual(st["screens"][0]["takeovers"], {"0": sid})
        self.assertEqual(st["screens"][0]["slots"][0], self.cam)
        self.assertEqual(app.post("/api/live/send-back", json={"stream_id": sid}).status_code, 200)
        st = app.get("/api/state").json
        item = next(s for s in st["streams"] if s["id"] == sid)
        self.assertEqual((item["live_now"], item["sent_back"], st["screens"][0]["takeovers"]), (True, True, {}))


RSS = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><title>News Releases - Dept</title>
%s
</channel></rss>"""


def rss(*items):
    body = "".join(
        "<item><title>%s</title><link>https://example.mil/%s</link><guid isPermaLink=\"false\">g-%s</guid>"
        "<pubDate>%s</pubDate></item>" % (t, i, i, d) for i, t, d in items)
    return RSS % body.encode()


def now_rfc(offset=0):
    import email.utils as eu
    return eu.formatdate(time.time() + offset, usegmt=True)


class FeedParseTests(unittest.TestCase):
    def test_rss2(self):
        from windopane import alerts
        title, items = alerts.parse_feed(rss(("1", "Navy &amp; <b>Army</b>  news", "Fri, 01 May 2026 21:14:40 GMT")))
        self.assertEqual(title, "News Releases - Dept")
        self.assertEqual(items[0]["title"], "Navy & Army news")
        self.assertEqual(items[0]["id"], "g-1")
        self.assertEqual(items[0]["link"], "https://example.mil/1")
        self.assertAlmostEqual(items[0]["published"], 1777670080, delta=1)

    def test_atom_and_rdf(self):
        from windopane import alerts
        atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>Advisories</title>
          <entry><title type="html">Level 4: Do Not Travel</title><id>tag:a,1</id>
          <link rel="self" href="https://x/self"/><link rel="alternate" href="https://x/page"/>
          <updated>2026-11-09T12:00:00Z</updated></entry></feed>"""
        t, items = alerts.parse_feed(atom)
        self.assertEqual((t, items[0]["title"], items[0]["id"], items[0]["link"]),
                         ("Advisories", "Level 4: Do Not Travel", "tag:a,1", "https://x/page"))
        self.assertIsNotNone(items[0]["published"])
        rdf = b"""<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/"
          xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><title>Old</title></channel>
          <item><title>One</title><link>https://y/1</link><dc:date>2026-11-09T10:00:00+00:00</dc:date></item></rdf:RDF>"""
        t, items = alerts.parse_feed(rdf)
        self.assertEqual((t, items[0]["title"], items[0]["id"]), ("Old", "One", "https://y/1"))

    def test_not_a_feed(self):
        from windopane import alerts
        with self.assertRaisesRegex(alerts.FeedError, "web page"):
            alerts.parse_feed(b"<!doctype html><html><body>hi</body></html>")
        with self.assertRaises(alerts.FeedError):
            alerts.parse_feed(b"<foo/>")

    def test_match(self):
        from windopane import alerts
        self.assertTrue(alerts.matches("Secretary visits Japan", ""))
        self.assertTrue(alerts.matches("Secretary visits Japan", "china, JAPAN"))
        self.assertFalse(alerts.matches("Secretary visits Japan", "china, korea"))


class AlertWatcherTests(unittest.TestCase):
    def setUp(self):
        from windopane import alerts
        self.alerts = alerts
        self.dir = tempfile.mkdtemp()
        self.c = controller.Controller(os.path.join(self.dir, "config.json"))
        self.body = rss(("1", "Old one", now_rfc(-600)), ("2", "Older one", now_rfc(-900)))
        self.calls = []

        def fetcher(url, etag=None, modified=None):
            self.calls.append((url, etag))
            if self.body is None:
                return 304, b"", etag, modified
            return 200, self.body, "E1", None
        self.fetcher = fetcher
        self.w = alerts.AlertWatcher(self.c, fetcher=fetcher)
        self.feed = self.c.add_feed({"url": "https://example.mil/rss.xml", "name": "DoW"})

    def test_seed_then_alert(self):
        self.w.poll_once(now=0)
        self.assertEqual(self.c.active_alerts(), [])                 # existing headlines don't alert
        self.assertTrue(self.w.feed_status(self.feed["id"])["seeded"])
        self.body = rss(("3", "Brand new", now_rfc(-5)), ("1", "Old one", now_rfc(-600)))
        self.w.poll_once(now=30)                                     # not due yet (5 min)
        self.assertEqual(len(self.calls), 1)
        self.w.poll_once(now=301)
        active = self.c.active_alerts()
        self.assertEqual([a["title"] for a in active], ["Brand new"])
        self.assertEqual((active[0]["feed"], active[0]["style"], active[0]["seconds"]), ("DoW", "popup", 30))
        self.assertEqual(self.calls[-1][1], "E1")                    # conditional GET
        self.w.poll_once(now=700)                                    # nothing new: no repeat
        self.assertEqual(len(self.c.recent_alerts()), 1)

    def test_304_filter_age_and_cap(self):
        self.c.update_feed(self.feed["id"], {"match": "missile, launch"})
        self.w.poll_once(now=0)
        self.body = None
        self.w.poll_once(now=400)
        self.assertTrue(self.w.feed_status(self.feed["id"])["unchanged"])
        fresh = [("n%d" % i, "Launch report %d" % i, now_rfc(-i * 60)) for i in range(8)]
        self.body = rss(("x", "Budget hearing", now_rfc(-30)), ("stale", "Missile story", now_rfc(-5 * 86400)), *fresh)
        self.w.poll_once(now=800)
        titles = [a["title"] for a in self.c.active_alerts()]
        self.assertEqual(len(titles), 5)                             # burst capped
        self.assertEqual(titles[-1], "Launch report 0")              # newest ends on top
        self.assertNotIn("Budget hearing", titles)
        self.assertNotIn("Missile story", titles)

    def test_persisted_seen_and_url_change(self):
        self.w.poll_once(now=0)
        w2 = self.alerts.AlertWatcher(self.c, fetcher=self.fetcher)  # restart
        self.body = rss(("3", "While we were off", now_rfc(-60)), ("1", "Old one", now_rfc(-600)))
        w2.poll_once(now=0)
        self.assertEqual([a["title"] for a in self.c.active_alerts()], ["While we were off"])
        self.c.update_feed(self.feed["id"], {"url": "https://example.mil/other.xml"})
        self.body = rss(("9", "Other feed item", now_rfc(-10)))
        w2.poll_once(now=1)                                          # forced by the edit; re-seeds
        self.assertEqual(len(self.c.active_alerts()), 1)

    def test_disabled_and_check_now(self):
        self.c.update_feed(self.feed["id"], {"enabled": False})
        self.calls.clear()
        self.w.force.clear()
        self.w.poll_once(now=0)
        self.assertEqual(self.calls, [])
        self.w.check_now(self.feed["id"])
        self.w.poll_once(now=1)
        self.assertEqual(len(self.calls), 1)

    def test_error_status_and_show_latest(self):
        def broken(url, etag=None, modified=None):
            raise self.alerts.FeedError("the site answered HTTP 403 Forbidden")
        self.w.fetch = broken
        self.w.poll_once(now=0)
        st = self.w.feed_status(self.feed["id"])
        self.assertEqual((st["ok"], st["error"]), (False, "the site answered HTTP 403 Forbidden"))
        self.w.fetch = self.fetcher
        alert = self.w.show_latest(self.c.cfg["alerts"]["feeds"][0])
        self.assertEqual((alert["title"], alert["test"]), ("Old one", True))


class AlertControllerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.c = controller.Controller(os.path.join(self.dir, "config.json"))

    def test_feed_validation(self):
        with self.assertRaises(ValueError):
            self.c.add_feed({"url": "ftp://x/feed"})
        with self.assertRaises(ValueError):
            self.c.add_feed({"url": "https://x/feed", "style": "banner"})
        with self.assertRaises(ValueError):
            self.c.add_feed({"url": "https://x/feed", "seconds": 2})
        f = self.c.add_feed({"url": "https://www.state.gov/feed", "style": "ticker", "screens": [1], "every_minutes": "0"})
        self.assertEqual((f["name"], f["seconds"], f["screens"], f["every_minutes"]), ("state.gov", 300, [1], 1))
        f = self.c.update_feed(f["id"], {"seconds": 0, "screens": "all"})
        self.assertEqual((f["seconds"], f["screens"]), (0, "all"))
        from windopane import config
        cfg = config.load(self.c.path)                               # persisted
        self.assertEqual(cfg["alerts"]["feeds"][0]["id"], f["id"])
        self.c.delete_feed(f["id"])
        self.assertEqual(self.c.cfg["alerts"]["feeds"], [])

    def test_expiry_dismiss_and_off_switch(self):
        a = self.c.raise_alert({"name": "A", "seconds": 10}, "one")
        b = self.c.raise_alert({"name": "B", "seconds": 0, "style": "ticker"}, "two")
        self.assertEqual(len(self.c.active_alerts()), 2)
        self.assertEqual([x["id"] for x in self.c.active_alerts(now=time.time() + 60)], [b["id"]])
        self.c.dismiss_alert(b["id"])
        self.assertEqual([x["id"] for x in self.c.active_alerts()], [a["id"]])
        self.c.update_settings({"alerts": {"enabled": False}})
        self.assertEqual(self.c.active_alerts(), [])
        self.c.update_settings({"alerts": {"enabled": True}})
        self.c.dismiss_alert()
        self.assertEqual(self.c.active_alerts(), [])
        recent = self.c.recent_alerts()
        self.assertEqual([r["title"] for r in recent], ["two", "one"])
        with self.assertRaises(ValueError):
            self.c.update_settings({"alerts": {"popup_position": "middle"}})

    def test_api(self):
        try:
            from windopane import web, power
        except ImportError as exc:
            self.skipTest("flask missing: %s" % exc)
        app = web.create_app(self.c).test_client()
        self.assertIn(b"view-settings", app.get("/settings").data)
        r = app.post("/api/alerts/feeds", json={"url": "https://example.mil/rss.xml", "style": "both"})
        self.assertEqual(r.status_code, 201, r.json)
        fid = r.json["id"]
        st = app.get("/api/state").json
        self.assertEqual(st["alerts"]["feeds"][0]["id"], fid)
        self.assertEqual(st["settings"]["alerts"]["popup_position"], "top-right")
        self.assertNotIn("feeds", st["settings"]["alerts"])
        self.assertEqual(app.put("/api/alerts/feeds/" + fid, json={"seconds": 3}).status_code, 400)
        self.assertEqual(app.post("/api/alerts/test", json={"url": "file:///etc/passwd"}).status_code, 400)
        self.c.raise_alert({"name": "X"}, "hello")
        self.assertEqual(len(app.get("/api/state").json["alerts"]["active"]), 1)
        self.assertEqual(app.post("/api/alerts/dismiss", json={}).status_code, 200)
        self.assertEqual(app.get("/api/state").json["alerts"]["active"], [])
        self.assertEqual(app.delete("/api/alerts/feeds/" + fid).status_code, 200)
        # power
        self.assertEqual(app.post("/api/system/power", json={"action": "reboot"}).status_code, 400)
        old_helper, old_restart = power.HELPER, power.restart_wall
        calls = []
        try:
            power.HELPER = "/nonexistent/windopane-power"
            r = app.post("/api/system/power", json={"action": "reboot", "confirm": True})
            self.assertEqual(r.status_code, 409)
            self.assertIn("re-run install.sh", r.json["error"])
            power.restart_wall = lambda delay=1.0: calls.append(delay)
            self.assertEqual(app.post("/api/system/power", json={"action": "restart", "confirm": True}).status_code, 202)
            self.assertEqual(len(calls), 1)
            self.assertEqual(app.post("/api/system/power", json={"action": "halt", "confirm": True}).status_code, 400)
        finally:
            power.HELPER, power.restart_wall = old_helper, old_restart


if __name__ == "__main__":
    unittest.main(verbosity=2)

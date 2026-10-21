"""Wi-Fi from the Maintenance page (actions/wifi.py): parsing, connect / rollback, who may change it."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aio_system_dashboard.actions import wifi as w
from aio_system_dashboard.config import DEFAULTS, Config
from aio_system_dashboard.state import still

SECRET = "correct-horse-9"

DEVICES = "enp6s0:ethernet:connected:Wired connection 1\nwlan0:wifi:connected:office\nlo:loopback:unmanaged:\n"
PROFILES = "u-wired:802-3-ethernet:Wired connection 1\nu-office:802-11-wireless:office\n"
SCAN = ("*:office:70:WPA2\n :field\\:router:55:WPA2 WPA3\n :field\\:router:80:WPA2 WPA3\n"
        " ::40:WPA2\n :guest:20:\n")


class FakeNM:
    """Answers nmcli calls; ``connect`` decides how the connection attempt goes. Like NetworkManager,
    an attempt that reaches a network drops the active Wi-Fi first; one refused up front does not."""

    def __init__(self, connect="ok", ip_after=0):
        self.calls, self.connect, self.ip_after = [], connect, ip_after
        self.profiles = PROFILES
        self.active = "u-office"                      # the Wi-Fi profile active on wlan0
        self.ip_polls = 0

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        a = argv[1:]
        if a[:4] == ["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"]:
            return 0, DEVICES
        if a[:5] == ["-t", "-f", "UUID,TYPE,NAME", "connection", "show"]:
            return 0, self.profiles
        if a[:6] == ["-t", "-f", "UUID,TYPE,DEVICE", "connection", "show", "--active"]:
            return 0, "u-wired:802-3-ethernet:enp6s0\n" + (f"{self.active}:802-11-wireless:wlan0\n" if self.active else "")
        if "wifi" in a and "list" in a:
            return 0, SCAN
        if "connect" in a:
            if self.connect == "denied":
                return 1, "Error: Not authorized to control networking."
            if self.connect == "not_found":
                return 10, "Error: No network with SSID 'lab' found."
            self.profiles = PROFILES + "u-new:802-11-wireless:lab\n"       # nmcli saves a profile even on failure
            self.active = "u-new" if self.connect == "ok" else None
            if self.connect == "ok":
                return 0, "Device 'wlan0' successfully activated with 'u-new'."
            return 4, f"Error: Connection activation failed: Secrets were required, but not provided ({SECRET})."
        if a[:2] == ["-g", "IP4.ADDRESS"]:
            self.ip_polls += 1
            return 0, "192.0.2.50/24\n" if self.ip_polls > self.ip_after else ""
        if "delete" in a:
            self.active = None if self.active == a[-1] else self.active
            return 0, ""
        if "up" in a:
            self.active = a[-1]
            return 0, ""
        return 1, "unexpected call"


def make_cfg(fake=False):
    data = copy.deepcopy(DEFAULTS)
    data["fake"]["enabled"] = fake
    data["wifi"].update(ip_timeout_s=5)
    return Config(data, None)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Events:
    def __init__(self):
        self.items = []

    def add(self, *a, **k):
        self.items.append(" ".join(str(x) for x in a))


class ParseTest(unittest.TestCase):
    def test_escaped_fields(self):
        self.assertEqual(w.split_terse(" :field\\:router:55:WPA2"), [" ", "field:router", "55", "WPA2"])
        self.assertEqual(w.split_terse("a\\\\b:c"), ["a\\b", "c"])

    def test_scan_keeps_one_row_per_network_and_skips_hidden(self):
        m = w.WifiManager(make_cfg(), Events(), runner=FakeNM())
        nets = m.scan()
        self.assertEqual([n["ssid"] for n in nets], ["office", "field:router", "guest"])
        self.assertEqual(nets[1]["signal"], 80)                     # the strongest access point
        self.assertTrue(nets[0]["in_use"])


class ConnectTest(unittest.TestCase):
    def mgr(self, nm):
        self.events, self.clock = Events(), Clock()
        return w.WifiManager(make_cfg(), self.events, runner=nm, clock=self.clock, sleep=self.clock.sleep)

    def assert_secret_nowhere(self, result, nm):
        text = json.dumps(result) + " ".join(self.events.items)
        self.assertNotIn(SECRET, text)

    def test_success(self):
        nm = FakeNM()
        r = self.mgr(nm).connect("lab", SECRET)
        self.assertTrue(r["success"])
        self.assertEqual(r["stages"], {"saved": True, "associated": True, "ip": True})
        self.assertFalse(any("delete" in c for c in nm.calls))
        self.assert_secret_nowhere(r, nm)

    def test_wrong_password_rolls_back_to_the_previous_network(self):
        nm = FakeNM(connect="wrong")
        r = self.mgr(nm).connect("lab", SECRET)
        self.assertFalse(r["success"])
        self.assertIn(["nmcli", "connection", "delete", "uuid", "u-new"], nm.calls)
        self.assertTrue(any(c[-3:] == ["up", "uuid", "u-office"] for c in nm.calls))
        self.assertIn("back on office", r["summary"])
        self.assertEqual((r["code"], r["restored"], r["kept"]), ("wrong_password", "office", False))
        self.assertFalse(any(c[-1] in ("u-wired",) and "delete" in c for c in nm.calls))   # wired untouched
        self.assert_secret_nowhere(r, nm)                           # nmcli echoed it; scrubbed

    def test_no_address_in_time_rolls_back(self):
        nm = FakeNM(ip_after=100)
        r = self.mgr(nm).connect("lab", SECRET)
        self.assertFalse(r["success"])
        self.assertEqual(r["stages"], {"saved": True, "associated": True, "ip": False})
        self.assertIn("no IPv4 address within 5 s", r["summary"])
        self.assertEqual(r["code"], "no_ip")
        self.assertIn(["nmcli", "connection", "delete", "uuid", "u-new"], nm.calls)
        self.assertEqual(nm.active, "u-office")                     # the new network was joined: back

    def test_a_network_not_found_leaves_the_current_one_alone(self):
        # Seen on the Jetson: "connection up" of the still active network dropped Wi-Fi for ~10 s.
        nm = FakeNM(connect="not_found")
        r = self.mgr(nm).connect("lab", SECRET)
        self.assertFalse(r["success"])
        self.assertEqual((r["code"], r["restored"], r["kept"]), ("not_found", "office", True))
        self.assertIn("still on office", r["summary"])
        self.assertFalse(any("up" in c or "delete" in c for c in nm.calls))

    def test_missing_permission_names_the_one_time_setup(self):
        r = self.mgr(FakeNM(connect="denied")).connect("lab", SECRET)
        self.assertIn("--with-wifi", r["summary"])
        self.assertEqual(r["code"], "denied")

    def test_input_checks(self):
        m = self.mgr(FakeNM())
        for ssid, pw in (("", SECRET), ("x" * 33, SECRET), ("lab", "short")):
            with self.assertRaises(w.WifiError):
                m.connect(ssid, pw)


class WhoMayChangeTest(unittest.TestCase):
    def test_client_path(self):
        wired = lambda dev: dev.startswith("en")
        self.assertTrue(w.client_path("127.0.0.1", "wlan0", route=lambda a: None, wired=wired)[0])
        self.assertTrue(w.client_path("192.0.2.9", "wlan0", route=lambda a: "enp6s0", wired=wired)[0])
        ok, why = w.client_path("192.0.2.9", "wlan0", route=lambda a: "wlan0", wired=wired)
        self.assertFalse(ok)
        self.assertIn("over Wi-Fi", why)
        self.assertFalse(w.client_path("100.64.0.2", "wlan0", route=lambda a: "tailscale0", wired=wired)[0])
        self.assertFalse(w.client_path("192.0.2.9", "wlan0", route=lambda a: None, wired=wired)[0])

    def test_is_wired_from_sysfs(self):
        with tempfile.TemporaryDirectory() as d:
            for dev, typ, wireless in (("enp6s0", "1", False), ("wlan0", "1", True), ("tailscale0", "65534", False)):
                (Path(d) / dev).mkdir()
                (Path(d) / dev / "type").write_text(typ + "\n")
                if wireless:
                    (Path(d) / dev / "wireless").mkdir()
            self.assertTrue(w.is_wired("enp6s0", d))
            self.assertFalse(w.is_wired("wlan0", d))
            self.assertFalse(w.is_wired("tailscale0", d))


class WifiWebTest(unittest.TestCase):
    def setUp(self):
        try:
            from aio_system_dashboard.web.maintenance import create_maintenance_app
            from aio_system_dashboard.web.auth import write_password_file
        except ImportError as e:
            self.skipTest(str(e))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        write_password_file(str(base / "pw"), "right-password")
        data = copy.deepcopy(DEFAULTS)
        data["maintenance"]["password_file"] = str(base / "pw")
        data["maintenance"]["lock"] = "tools"
        p = mock.patch.object(Config, "state_file", lambda _s, n: base / "state" / n)
        p.start()
        self.addCleanup(p.stop)
        self.store = {"readiness": {"state": "KEEP_STILL"}, "network": {}}
        self.events = Events()
        cfg = Config(data, None)
        limits = still.StillLimits(cfg, base / "state" / "still_limits.json", self.events)
        ctx = SimpleNamespace(cfg=cfg, modules=[], store=SimpleNamespace(get=lambda k, d=None: self.store.get(k, d)),
                              actions=SimpleNamespace(describe=lambda: []), preview=SimpleNamespace(describe=lambda: {}),
                              events=self.events, still_limits=limits,
                              still_calibration=still.StillCalibration(lambda: (None, None, None), limits))
        app = create_maintenance_app(ctx)
        self.addCleanup(lambda: ctx.terminals.shutdown())
        self.nm = FakeNM()
        ctx.wifi._run = self.nm
        self.c = app.test_client()
        self.h = {"X-Requested-With": "aio-dashboard"}

    def post(self, url, body):
        return self.c.post(url, json=body, headers=self.h)

    def test_connect_needs_password_then_confirmation_while_in_use(self):
        body = {"ssid": "lab", "password": SECRET}
        self.assertEqual(self.post("/api/maint/wifi/connect", body).status_code, 401)
        self.post("/api/maint/auth/unlock", {"password": "right-password"})
        self.store["readiness"] = {"state": "READY"}
        r = self.post("/api/maint/wifi/connect", body)
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.get_json()["needs_confirm"])
        r = self.post("/api/maint/wifi/connect", dict(body, confirm=True)).get_json()
        self.assertTrue(r["success"], r)
        self.assertNotIn(SECRET, json.dumps(r) + " ".join(self.events.items))
        st = self.c.get("/api/maint/wifi").get_json()
        self.assertTrue(st["may_change"])                           # the test client is loopback
        self.assertNotIn(SECRET, json.dumps(st))

    def test_a_browser_on_wifi_is_refused(self):
        self.post("/api/maint/auth/unlock", {"password": "right-password"})
        with mock.patch.object(w, "client_path", lambda addr, dev: (False, "your computer is connected over Wi-Fi")):
            r = self.post("/api/maint/wifi/connect", {"ssid": "lab", "password": SECRET})
        self.assertEqual(r.status_code, 403)
        self.assertFalse(any("connect" in c for c in self.nm.calls))


class UserPageSettingsTest(unittest.TestCase):
    """The user app (:8080): Wi-Fi and the RTK switch without a password, but with the same rules."""

    def setUp(self):
        try:
            from aio_system_dashboard.web.product import create_product_app
        except ImportError as e:
            self.skipTest(str(e))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        p = mock.patch.object(Config, "state_file", lambda _s, n: base / "state" / n)
        p.start()
        self.addCleanup(p.stop)
        self.store = {"readiness": {"state": "KEEP_STILL", "reasons": ["System starting"]}, "network": {},
                      "product": {"health": {"title": "Not running", "detail": "Press Start to begin navigation."}}}
        self.events = Events()
        ctx = SimpleNamespace(cfg=Config(copy.deepcopy(DEFAULTS), None), events=self.events,
                              store=SimpleNamespace(get=lambda k, d=None: self.store.get(k, d)))
        self.app = create_product_app(ctx)
        self.nm = FakeNM()
        ctx.wifi._run = self.nm
        self.ctx, self.c = ctx, self.app.test_client()
        self.h = {"X-Requested-With": "aio-dashboard"}

    def post(self, url, body):
        return self.c.post(url, json=body, headers=self.h)

    def test_wifi_without_password_but_confirmed_while_in_use(self):
        self.store["readiness"] = {"state": "READY"}
        body = {"ssid": "lab", "password": SECRET}
        r = self.post("/api/wifi/connect", body)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["risks"], ["navigation is in use (READY)"])
        r = self.post("/api/wifi/connect", dict(body, confirm=True)).get_json()
        self.assertTrue(r["success"], r)
        self.assertNotIn(SECRET, json.dumps(r) + " ".join(self.events.items))
        self.assertTrue(any("user page" in e for e in self.events.items))

    def test_wifi_refused_unless_wired(self):
        with mock.patch.object(w, "client_path", lambda addr, dev: (False, "your computer is connected over Wi-Fi (wlan0); "
                                                                            "connect with a network cable to change Wi-Fi")):
            r = self.post("/api/wifi/connect", {"ssid": "lab", "password": SECRET})
            self.assertEqual(r.status_code, 403)
            self.assertFalse(self.c.get("/api/wifi").get_json()["may_change"])
        self.assertFalse(any("connect" in c for c in self.nm.calls))

    def test_rtk_off_needs_a_confirm(self):
        from aio_system_dashboard.state.health import rtk_setting
        r = self.post("/api/settings/rtk", {"required": False})
        self.assertEqual(r.status_code, 409)
        self.assertTrue(rtk_setting(self.ctx.cfg)[0])                       # unchanged
        self.assertTrue(self.post("/api/settings/rtk", {"required": False, "confirm": True}).get_json()["success"])
        self.assertFalse(rtk_setting(self.ctx.cfg)[0])
        self.assertFalse(self.c.get("/api/settings").get_json()["rtk"]["required"])
        self.assertTrue(self.post("/api/settings/rtk", {"required": True}).get_json()["success"])   # on: no confirm
        self.assertTrue(rtk_setting(self.ctx.cfg)[0])

    def test_chinese_pages_and_answers_english_machine_interface(self):
        self.c.set_cookie("aio_lang", "zh-TW")
        for page in ("/", "/navigation", "/data", "/settings"):
            html = self.c.get(page).get_data(as_text=True)
            self.assertIn("總覽", html, page)
            self.assertIn('lang="zh-TW"', html)
        state = self.c.get("/api/state").get_json()
        self.assertEqual(state["health"]["title"], "未執行")
        self.assertEqual(self.c.get("/api/readiness").get_json()["reasons"], ["System starting"])   # vehicle API: English
        r = self.post("/api/settings/rtk", {"required": False}).get_json()
        self.assertIn("不要求 RTK", r["summary"])
        self.c.set_cookie("aio_lang", "en")
        self.assertIn(">Overview<", self.c.get("/").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()

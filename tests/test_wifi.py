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
        if a[-3:-1] == ["device", "disconnect"]:
            self.active = None
            return 0, f"Device '{a[-1]}' successfully disconnected."
        return 1, "unexpected call"


def make_cfg(fake=False):
    data = copy.deepcopy(DEFAULTS)
    data["fake"]["enabled"] = fake
    data["wifi"].update(ip_timeout_s=5)
    return Config(data, None)


def hotspot_cfg():
    cfg = make_cfg()
    cfg.data["wifi"]["hotspot"]["enabled"] = True
    return cfg


class HotspotNM(FakeNM):
    """FakeNM with the hotspot profile. up: how starting it goes ("ok" / "fail"); saved: the profile
    as it is on disk ({"ssid", "autoconnect"}), None when there is none yet."""
    NAMES = {"u-office": "office", "u-new": "lab", "u-hs": w.HOTSPOT}

    def __init__(self, up="ok", saved=None, **kw):
        super().__init__(**kw)
        self.up, self.hotspot = up, saved

    def __call__(self, argv, timeout):
        a = argv[1:]
        if a[:4] == ["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"]:
            self.calls.append(argv)
            state = "connected" if self.active else "disconnected"
            return 0, f"enp6s0:ethernet:connected:Wired connection 1\nwlan0:wifi:{state}:{self.NAMES.get(self.active, '')}\n"
        if w.HOTSPOT not in a:
            return super().__call__(argv, timeout)
        self.calls.append(argv)
        if a[0] == "-g":
            if not self.hotspot:
                return 10, "Error: aio-hotspot - no such connection profile."
            return 0, f"{self.hotspot['ssid']}\n{self.hotspot['autoconnect']}\nu-hs\n"
        if "add" in a or "modify" in a:
            h = self.hotspot or {}
            for key, field in (("802-11-wireless.ssid", "ssid"), ("connection.autoconnect", "autoconnect")):
                if key in a:
                    h[field] = a[a.index(key) + 1]
            self.hotspot = h
            return 0, ""
        if "up" in a:
            self.active = "u-hs" if self.up == "ok" else None        # the radio leaves the network first
            return (0, "Connection successfully activated") if self.up == "ok" else \
                (4, f"Error: Connection activation failed: ({SECRET}) IP configuration could not be reserved")
        if "down" in a:
            self.active = "u-office"                                 # NetworkManager joins a saved network
            return 0, ""
        if "delete" in a:
            self.hotspot = None
            return 0, ""
        return 1, "unexpected call"


class HotspotTest(unittest.TestCase):
    def mgr(self, nm):
        self.events, self.clock = Events(), Clock()
        return w.WifiManager(hotspot_cfg(), self.events, runner=nm, clock=self.clock, sleep=self.clock.sleep,
                             stations=lambda dev: {"02:00:00:00:00:01"})

    def assert_secret_nowhere(self, *results):
        self.assertNotIn(SECRET, json.dumps(results) + " ".join(self.events.items))

    def test_start_makes_an_access_point_that_comes_back_at_power_on(self):
        nm = HotspotNM()
        m = self.mgr(nm)
        r = m.start_hotspot("AIO-NAV", SECRET)
        self.assertTrue(r["success"], r)
        add = next(c for c in nm.calls if "add" in c)
        for key, value in (("802-11-wireless.mode", "ap"), ("802-11-wireless.band", "bg"), ("ipv4.method", "shared"),
                           ("ipv4.addresses", "10.42.0.1/24"), ("wifi-sec.key-mgmt", "wpa-psk"),
                           ("wifi-sec.pmf", "disable"),                  # plain WPA2: iPhones list it
                           ("connection.autoconnect", "yes")):
            self.assertEqual(add[add.index(key) + 1], value, key)
        self.assertIn("http://10.42.0.1:8080", r["summary"])
        st = m.status()
        self.assertEqual((st["mode"], st["connection"]), ("hotspot", ""))
        h = st["hotspot"]
        self.assertEqual((h["active"], h["ssid"], h["autostart"], h["clients"]), (True, "AIO-NAV", True, 1))
        self.assert_secret_nowhere(r, st)

    def test_a_hotspot_that_does_not_start_gives_the_network_back(self):
        nm = HotspotNM(up="fail")
        r = self.mgr(nm).start_hotspot("AIO-NAV", SECRET)
        self.assertFalse(r["success"])
        self.assertEqual(nm.hotspot["autoconnect"], "no")           # nor at the next power-on
        self.assertEqual(nm.active, "u-office")
        self.assertIn("back on office", r["summary"])
        self.assert_secret_nowhere(r)

    def test_stop_is_for_good_and_a_saved_network_comes_back(self):
        nm = HotspotNM(saved={"ssid": "AIO-NAV", "autoconnect": "yes"})
        nm.active = "u-hs"
        r = self.mgr(nm).stop_hotspot()
        self.assertTrue(r["success"], r)
        self.assertEqual(nm.hotspot["autoconnect"], "no")
        self.assertIn("joined office", r["summary"])

    def test_joining_a_network_is_refused_while_the_hotspot_runs(self):
        nm = HotspotNM(saved={"ssid": "AIO-NAV", "autoconnect": "yes"})
        nm.active = "u-hs"
        with self.assertRaises(w.WifiError) as e:
            self.mgr(nm).connect("lab", SECRET)
        self.assertEqual(e.exception.status, 409)
        self.assertFalse(any("connect" in c for c in nm.calls))

    def test_input_checks(self):
        m = self.mgr(HotspotNM())
        for ssid, pw in (("", SECRET), ("x" * 33, SECRET), ("AIO-NAV", "short")):
            with self.assertRaises(w.WifiError):
                m.start_hotspot(ssid, pw)


class HotspotAccessTest(unittest.TestCase):
    def test_phones_on_the_hotspot_may_open_the_pages_once_it_is_installed(self):
        from aio_system_dashboard.web.access import client_allowed, hotspot_clients, parse_allowed_clients
        data = copy.deepcopy(DEFAULTS)
        cfg = Config(data, None)
        nets = parse_allowed_clients(["192.0.2.154"])
        with mock.patch("aio_system_dashboard.config.installed_options", lambda: set()):
            self.assertEqual(hotspot_clients(cfg), [])
        with mock.patch("aio_system_dashboard.config.installed_options", lambda: {"sysmon", "hotspot"}):
            extra = hotspot_clients(cfg)
            self.assertTrue(client_allowed("10.42.0.23", nets + extra))
            self.assertFalse(client_allowed("10.42.1.23", nets + extra))
            data["wifi"]["hotspot"]["allow_clients"] = False
            self.assertEqual(hotspot_clients(cfg), [])

    def test_installed_options_file(self):
        from aio_system_dashboard import config as config_mod
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "installed-options"
            p.write_text("# written by install.sh\nsysmon replay\nwifi hotspot\n")
            with mock.patch.object(config_mod, "INSTALLED_OPTIONS", str(p)):
                self.assertEqual(config_mod.installed_options(), {"sysmon", "replay", "wifi", "hotspot"})
            with mock.patch.object(config_mod, "INSTALLED_OPTIONS", str(Path(d) / "none")):
                self.assertEqual(config_mod.installed_options(), set())


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


    def test_nmcli_runs_in_a_utf8_locale_so_chinese_names_survive(self):
        # In the plain C locale nmcli prints a Chinese SSID as "?????" and it cannot be joined.
        rc, out = w._nm_runner(["sh", "-c", 'echo "$LC_ALL"; printf "%s\\n" "$1"', "sh", "粉紅豬小妹"], 5)
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines(), ["C.UTF-8", "粉紅豬小妹"])


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

    def test_disconnect_leaves_the_network_and_keeps_its_profile(self):
        nm = HotspotNM()                                             # its device state follows "active"
        r = self.mgr(nm).disconnect()
        self.assertTrue(r["success"], r)
        self.assertEqual(r["disconnected"], "office")
        self.assertIn(["nmcli", "--wait", "15", "device", "disconnect", "wlan0"], nm.calls)
        self.assertFalse(any("delete" in c or "down" in c for c in nm.calls))     # the profile stays
        self.assertIsNone(nm.active)
        self.assertTrue(any("disconnected from office" in e for e in self.events.items))
        r = self.mgr(nm).disconnect()                                # already off: nothing to do
        self.assertEqual((r["success"], r["disconnected"]), (True, ""))

    def test_disconnect_is_refused_while_the_hotspot_runs(self):
        nm = HotspotNM(saved={"ssid": "AIO-NAV", "autoconnect": "yes"})
        nm.active = "u-hs"
        with self.assertRaises(w.WifiError) as cm:
            self.mgr(nm).disconnect()
        self.assertEqual(cm.exception.status, 409)
        self.assertFalse(any("disconnect" in c for c in nm.calls))

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
        data["wifi"]["hotspot"]["enabled"] = True
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
        from aio_system_dashboard.collectors.hotspot_relay import HotspotRelay
        ctx.hotspot_relay = self.relay = HotspotRelay(cfg, base / "state" / "hotspot_relay.json",
                                                      runner=lambda argv, timeout=3.0: (1, ""), access_point=lambda: None)
        app = create_maintenance_app(ctx)
        self.addCleanup(lambda: ctx.terminals.shutdown())
        self.nm = HotspotNM()
        ctx.wifi._run = self.nm
        ctx.wifi._stations = lambda dev: {"02:00:00:00:00:01"}               # one phone on the hotspot
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

    def test_hotspot_needs_the_password_a_cable_and_a_confirm(self):
        body = {"action": "start", "ssid": "AIO-NAV", "password": SECRET}
        self.assertEqual(self.post("/api/maint/wifi/hotspot", body).status_code, 401)
        self.post("/api/maint/auth/unlock", {"password": "right-password"})
        r = self.post("/api/maint/wifi/hotspot", body)
        self.assertEqual(r.status_code, 409)                           # it would leave "office"
        self.assertIn("leaves office", r.get_json()["summary"])
        r = self.post("/api/maint/wifi/hotspot", dict(body, confirm=True)).get_json()
        self.assertTrue(r["success"], r)
        st = self.c.get("/api/maint/wifi").get_json()
        self.assertEqual((st["mode"], st["hotspot"]["ssid"]), ("hotspot", "AIO-NAV"))
        self.assertNotIn(SECRET, json.dumps(r) + json.dumps(st) + " ".join(self.events.items))
        with mock.patch.object(w, "client_path", lambda addr, dev: (False, "your computer is connected over Wi-Fi")):
            self.assertEqual(self.post("/api/maint/wifi/hotspot", {"action": "stop", "confirm": True}).status_code, 403)
        r = self.post("/api/maint/wifi/hotspot", {"action": "stop"})       # a phone is on it: confirm
        self.assertEqual(r.status_code, 409)
        self.assertTrue(self.post("/api/maint/wifi/hotspot", {"action": "stop", "confirm": True}).get_json()["success"])

    def test_disconnect_needs_the_password_then_a_confirm_while_in_use(self):
        self.assertEqual(self.post("/api/maint/wifi/disconnect", {}).status_code, 401)
        self.post("/api/maint/auth/unlock", {"password": "right-password"})
        self.store["readiness"] = {"state": "READY"}
        r = self.post("/api/maint/wifi/disconnect", {})
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.get_json()["needs_confirm"])
        self.assertFalse(any("disconnect" in c for c in self.nm.calls))
        r = self.post("/api/maint/wifi/disconnect", {"confirm": True}).get_json()
        self.assertEqual((r["success"], r["disconnected"]), (True, "office"))
        st = self.c.get("/api/maint/wifi").get_json()
        self.assertEqual((st["state"], st["connection"]), ("disconnected", ""))
        self.assertTrue(any("disconnect requested" in e for e in self.events.items))

    def test_packet_copies_switch_needs_the_password(self):
        self.assertEqual(self.post("/api/maint/wifi/hotspot/relay", {"enabled": False}).status_code, 401)
        self.post("/api/maint/auth/unlock", {"password": "right-password"})
        self.assertEqual(self.post("/api/maint/wifi/hotspot/relay", {"enabled": "no"}).status_code, 400)
        r = self.post("/api/maint/wifi/hotspot/relay", {"enabled": False}).get_json()
        self.assertTrue(r["success"], r)
        self.assertFalse(self.relay.enabled)
        self.assertFalse(self.c.get("/api/maint/wifi").get_json()["hotspot"]["relay"]["enabled"])

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
        ctx.wifi._stations = lambda dev: set()
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
            self.assertEqual(self.post("/api/wifi/disconnect", {"confirm": True}).status_code, 403)
            self.assertFalse(self.c.get("/api/wifi").get_json()["may_change"])
        self.assertFalse(any("connect" in c for c in self.nm.calls))

    def test_disconnect_from_the_user_page(self):
        r = self.post("/api/wifi/disconnect", {}).get_json()
        self.assertEqual((r["success"], r["disconnected"]), (True, "office"), r)
        self.assertIn(["nmcli", "--wait", "15", "device", "disconnect", "wlan0"], self.nm.calls)
        self.assertTrue(any("user page" in e for e in self.events.items))

    def test_the_hotspot_is_shown_but_switched_on_maintenance_only(self):
        self.ctx.cfg.data["wifi"]["hotspot"]["enabled"] = True
        nm = HotspotNM(saved={"ssid": "AIO-NAV", "autoconnect": "yes"})
        nm.active = "u-hs"
        self.ctx.wifi._run = nm
        st = self.c.get("/api/wifi").get_json()
        self.assertEqual((st["mode"], st["hotspot"]["ssid"]), ("hotspot", "AIO-NAV"))
        self.assertEqual(self.post("/api/wifi/hotspot", {"action": "stop", "confirm": True}).status_code, 404)
        r = self.post("/api/wifi/connect", {"ssid": "lab", "password": SECRET})
        self.assertEqual(r.status_code, 409)
        self.assertIn("hotspot", r.get_json()["summary"])
        self.assertEqual(nm.active, "u-hs")

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


# ---------------------------------------------------------------------- the system hotspot service
from aio_system_dashboard.actions import hotspot_service  # noqa: E402


class FakeService(hotspot_service.HotspotService):
    """The aio-hotspot service as the dashboard sees it: a status file, a config file, and
    ``sudo -n aio-hotspot <command>`` (recorded, and answered like the real service)."""

    def __init__(self, tmp, running=True, **status):
        self.dir = Path(tmp)
        (self.dir / "config.env").write_text('# comment\nHOTSPOT_SSID="AIO-NAV-TEST"\nHOTSPOT_PASSWORD="old-hotspot-pw1"\n'
                                             'HOTSPOT_ADDRESS="192.168.77.1/24"\nAP_INTERFACE="ap0"\n')
        (self.dir / "aio-hotspot").write_text("")
        self.calls, self.sudo = [], "ok"
        super().__init__(status_file=self.dir / "status.json", config_file=self.dir / "config.env",
                         program=str(self.dir / "aio-hotspot"), runner=self.fake_sudo)
        if running:
            self.write(**status)

    def write(self, active=True, channel=6, reason="", clients=0, auto_join=True):
        import time
        self.status_file.write_text(json.dumps({
            "hotspot": {"active": active, "ssid": "AIO-NAV-TEST", "channel": channel if active else None,
                        "band": ("5 GHz" if channel > 14 else "2.4 GHz") if active else None,
                        "address": "192.168.77.1", "interface": "ap0", "access": "dev", "clients": clients,
                        "reason": reason},
            "wifi": {"interface": "wlan0", "state": "connected", "connection": "office", "channel": 44,
                     "auto_join": auto_join},
            "mode": "hotspot+wifi" if active else "wifi", "updated": time.time()}))

    def fake_sudo(self, argv, stdin, timeout):
        assert argv[:3] == ["sudo", "-n", self.program], argv
        self.calls.append((argv[3:], stdin))
        if self.sudo != "ok":
            return 1, "sudo: a password is required"
        cmd = argv[3]
        if cmd == "pause":
            self.write(active=False, reason="paused on request")
        elif cmd == "resume":
            self.write(active=True, channel=44)
        elif cmd == "hold":
            self.write(auto_join=False)
        elif cmd == "on":
            self.write()
        elif cmd == "off":
            self.status_file.unlink(missing_ok=True)
        elif cmd == "set-credentials":
            ssid, password = stdin.split("\n")[:2]
            self.config_file.write_text(f'HOTSPOT_SSID="{ssid}"\nHOTSPOT_PASSWORD="{password}"\n'
                                        'HOTSPOT_ADDRESS="192.168.77.1/24"\n')
        return 0, ""


class ServiceNM(FakeNM):
    """The hotspot service's own interface is listed first, unmanaged."""

    def __call__(self, argv, timeout):
        if argv[1:5] == ["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"]:
            self.calls.append(argv)
            state = "connected" if self.active else "disconnected"
            return 0, f"ap0:wifi:unmanaged:\nwlan0:wifi:{state}:{'office' if self.active else ''}\n"
        return super().__call__(argv, timeout)


class HotspotServiceTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def mgr(self, nm, svc):
        self.events, self.clock = Events(), Clock()
        return w.WifiManager(make_cfg(), self.events, runner=nm, clock=self.clock, sleep=self.clock.sleep, service=svc)

    def test_status_shows_the_service_next_to_the_wifi_connection(self):
        svc = FakeService(self.tmp, channel=44, clients=2)
        st = self.mgr(ServiceNM(), svc).status()
        self.assertEqual((st["device"], st["mode"], st["connection"]), ("wlan0", "client", "office"))   # not ap0
        h = st["hotspot"]
        self.assertEqual((h["service"], h["running"], h["active"], h["ssid"], h["channel"], h["band"], h["clients"]),
                         (True, True, True, "AIO-NAV-TEST", 44, "5 GHz", 2))
        self.assertEqual((h["address"], h["port"], h["access"]), ("192.168.77.1", 8080, "dev"))
        self.assertNotIn("old-hotspot-pw1", json.dumps(st))                 # the password is not in the status

    def test_paused_on_a_radar_channel_and_switched_off(self):
        svc = FakeService(self.tmp, active=False, reason="Wi-Fi is on radar channel 108: no hotspot there (Wi-Fi is kept)")
        m = self.mgr(ServiceNM(), svc)
        h = m.status()["hotspot"]
        self.assertEqual((h["running"], h["active"], h["clients"]), (True, False, 0))
        self.assertIn("radar channel 108", h["reason"])
        svc.status_file.unlink()                                       # the service is not running
        h = m.status()["hotspot"]
        self.assertEqual((h["running"], h["active"], h["reason"]), (False, False, "switched off"))

    def test_a_stale_status_file_means_not_running(self):
        svc = FakeService(self.tmp)
        st = json.loads(svc.status_file.read_text())
        st["updated"] -= 120
        svc.status_file.write_text(json.dumps(st))
        self.assertIsNone(svc.status())

    def test_joining_a_network_makes_the_hotspot_step_aside_first(self):
        nm, svc = ServiceNM(), FakeService(self.tmp)
        r = self.mgr(nm, svc).connect("lab", SECRET)
        self.assertTrue(r["success"], r)
        self.assertEqual([c[0][0] for c in svc.calls], ["pause", "resume"])
        connect_at = next(i for i, c in enumerate(nm.calls) if "connect" in c)
        self.assertTrue(connect_at >= 0)
        self.assertTrue(svc.status()["hotspot"]["active"])             # back afterwards

    def test_the_hotspot_comes_back_when_joining_fails(self):
        nm, svc = ServiceNM(connect="wrong_password"), FakeService(self.tmp)
        r = self.mgr(nm, svc).connect("lab", SECRET)
        self.assertFalse(r["success"])
        self.assertEqual([c[0][0] for c in svc.calls], ["pause", "resume"])

    def test_nothing_is_asked_of_a_service_that_is_switched_off(self):
        nm, svc = ServiceNM(), FakeService(self.tmp, running=False)
        self.assertTrue(self.mgr(nm, svc).connect("lab", SECRET)["success"])
        self.assertEqual(svc.calls, [])

    def test_disconnect_tells_the_service_not_to_join_again(self):
        nm, svc = ServiceNM(), FakeService(self.tmp)
        m = self.mgr(nm, svc)
        self.assertTrue(m.disconnect()["success"])
        self.assertEqual([c[0] for c in svc.calls], [["hold"]])
        self.assertFalse(m.status()["hotspot"]["auto_join"])

    def test_a_new_name_keeps_the_password_and_goes_on_standard_input(self):
        nm, svc = ServiceNM(), FakeService(self.tmp)
        r = self.mgr(nm, svc).start_hotspot("AIO-CAR-7", "")
        self.assertTrue(r["success"], r)
        self.assertEqual(svc.calls, [(["set-credentials"], "AIO-CAR-7\nold-hotspot-pw1\n"), (["on"], None)])
        self.assertNotIn("old-hotspot-pw1", json.dumps(r) + " ".join(self.events.items))
        self.assertFalse(any("connection" in c for c in nm.calls))     # NetworkManager's own hotspot is not used

    def test_a_new_password_and_turning_on_unchanged(self):
        nm, svc = ServiceNM(), FakeService(self.tmp, running=False)
        m = self.mgr(nm, svc)
        self.assertTrue(m.start_hotspot("AIO-NAV-TEST", "")["success"])
        self.assertEqual(svc.calls, [(["on"], None)])                  # nothing changed: just on
        svc.calls.clear()
        r = m.start_hotspot("AIO-NAV-TEST", SECRET)
        self.assertEqual(svc.calls[0], (["set-credentials"], f"AIO-NAV-TEST\n{SECRET}\n"))
        self.assertNotIn(SECRET, json.dumps(r) + " ".join(self.events.items))
        with self.assertRaises(w.WifiError):
            m.start_hotspot("AIO-NAV-TEST", "short")

    def test_turn_off_and_a_missing_sudo_rule(self):
        nm, svc = ServiceNM(), FakeService(self.tmp)
        m = self.mgr(nm, svc)
        r = m.stop_hotspot()
        self.assertTrue(r["success"], r)
        self.assertEqual(svc.calls, [(["off"], None)])
        self.assertFalse(m.status()["hotspot"]["running"])
        svc.sudo = "denied"
        r = m.start_hotspot("AIO-NAV-TEST", "")
        self.assertFalse(r["success"])
        self.assertIn("install.sh <model> hotspot", r["summary"])

    def test_the_hotspot_network_is_the_services(self):
        from aio_system_dashboard.web import access
        svc = FakeService(self.tmp)
        cfg = make_cfg()
        self.assertFalse(w.hotspot_enabled(cfg))                       # nothing installed
        with mock.patch.object(hotspot_service, "SERVICE", svc):
            self.assertTrue(w.hotspot_enabled(cfg))
            self.assertEqual(w.hotspot_address(cfg), "192.168.77.1/24")
            self.assertEqual([str(n) for n in access.hotspot_clients(cfg)], ["192.168.77.0/24"])
            cfg.data["wifi"]["hotspot"]["service"] = False             # told not to use it
            self.assertFalse(w.hotspot_enabled(cfg))
            self.assertEqual(w.hotspot_address(cfg), "10.42.0.1/24")
